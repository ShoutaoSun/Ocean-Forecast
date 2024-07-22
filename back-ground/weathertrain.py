import os
import os.path as osp
import json
import copy
import torch
import pickle
import logging
import numpy as np
from model import IAM4VP
from tqdm import tqdm
from API import *
from utils import *

class Exp:
    def __init__(self, args):
        super(Exp, self).__init__()
        self.args = args
        self.config = self.args.__dict__
        self.device = self._acquire_device()

        self._preparation()#准备模型
        print_log(output_namespace(self.args))

        self._get_data()#获取数据
        self._select_optimizer()#优化器
        self._select_criterion()#损失函数

        self.ema = EMA(0.995)
        self.ema_model = copy.deepcopy(self.model)
        self.update_ema_every = 10
        self.step_start_ema = 2000
        self.step = 0

        self.t_sample = [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]

    def reset_parameters(self):
        self.ema_model.load_state_dict(self.model.state_dict())

    def step_ema(self):
        if self.step < self.step_start_ema:
            self.reset_parameters()
            return
        self.ema.update_model_average(self.ema_model, self.model)

    def _acquire_device(self):
        if self.args.use_gpu:
            os.environ["CUDA_VISIBLE_DEVICES"] = str(self.args.gpu)
            device = torch.device('cuda:{}'.format(0))
            print_log('Use GPU: {}'.format(self.args.gpu))
        else:
            device = torch.device('cpu')
            print_log('Use CPU')
        return device

    def _preparation(self):
        # seed
        set_seed(self.args.seed)
        # log and checkpoint
        self.path = osp.join(self.args.res_dir, self.args.ex_name)
        check_dir(self.path)

        self.checkpoints_path = osp.join(self.path, 'checkpoints')
        check_dir(self.checkpoints_path)

        sv_param = osp.join(self.path, 'model_param.json')
        with open(sv_param, 'w') as file_obj:
            json.dump(self.args.__dict__, file_obj)

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(level=logging.INFO, filename=osp.join(self.path, 'log.log'),
                            filemode='a', format='%(asctime)s - %(message)s')
        # prepare data
        self._get_data()
        # build the model
        self._build_model()

    def _build_model(self):
        args = self.args
        self.model = IAM4VP(tuple(args.in_shape), args.hid_S,
                           args.hid_T, args.N_S, args.N_T).to(self.device)

    def _get_data(self):
        config = self.args.__dict__
        self.train_loader, self.vali_loader, self.test_loader = load_data(**config)
        self.vali_loader = self.test_loader if self.vali_loader is None else self.vali_loader

    def _select_optimizer(self):
        self.optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.args.lr)
        self.scheduler = torch.optim.lr_scheduler.OneCycleLR(
            self.optimizer, max_lr=self.args.lr, steps_per_epoch=len(self.train_loader), pct_start=0.0, epochs=self.args.epochs)
        return self.optimizer

    def _select_criterion(self):
        self.criterion = torch.nn.MSELoss()

    def _save(self, name=''):
        torch.save(self.model.state_dict(), os.path.join(
            self.checkpoints_path, name + '.pth'))
        torch.save(self.ema_model.state_dict(), os.path.join(
            self.checkpoints_path, name + '.pth'))
        state = self.scheduler.state_dict()
        fw = open(os.path.join(self.checkpoints_path, name + '.pkl'), 'wb')
        pickle.dump(state, fw)

    def train(self, args):
        config = args.__dict__
        recorder = Recorder(verbose=True)
        for epoch in range(config['epochs']):
            train_loss = []
            self.model.train()
            train_pbar = tqdm(self.train_loader)
            for batch_x, batch_y  in train_pbar:
                batch_x, batch_y = batch_x.to(self.device), batch_y.to(self.device)
                pred_list = []
                for times in range(12):
                    self.optimizer.zero_grad()
                    t = torch.tensor(times*100).repeat(batch_x.shape[0]).cuda()#时间编码在批大小上重复
                    pred_y = self.model(batch_x, pred_list, t)
                    loss = self.criterion(pred_y, batch_y[:, times,:, :,:])
                    train_loss.append(loss.item())
                    train_pbar.set_description('train loss: {:.4f}'.format(loss.item()))
                    loss.backward()
                    pred_list.append(pred_y.detach())
                    self.optimizer.step()
                self.scheduler.step()

                if self.step % self.update_ema_every == 0:
                    self.step_ema()
                self.step += 1

            train_loss = np.average(train_loss)

            if epoch % args.log_step == 0:
                with torch.no_grad():
                    vali_loss = self.vali(self.vali_loader)
                    if epoch % (args.log_step * 100) == 0:
                        self._save(name=str(epoch))
                print_log("Epoch: {0} | Train Loss: {1:.4f} Vali Loss: {2:.4f}\n".format(
                    epoch + 1, train_loss, vali_loss))
                recorder(vali_loss, self.model, self.path)

        best_model_path = self.path + '/' + 'wind_v_checkpoint.pth'  # todo: step2，更改文件名称
        self.model.load_state_dict(torch.load(best_model_path))
        return self.model

    def vali(self, vali_loader=None ):
        self.model.eval()
        preds_lst, trues_lst, total_loss = [], [], []
        vali_pbar = tqdm(vali_loader)
        for i, (batch_x, batch_y) in enumerate(vali_pbar):
            if i * batch_x.shape[0] > 1000:
                break

            batch_x, batch_y = batch_x.to(self.device), batch_y.to(self.device)
            pred_list = []
            for timestep in range(12):
                t = torch.tensor(timestep*100).repeat(batch_x.shape[0]).cuda()
                pred_y = self.ema_model(batch_x, pred_list, t)##
                pred_list.append(pred_y)
            pred_y = torch.cat(pred_list, dim=1).unsqueeze(2)

            list(map(lambda data, lst: lst.append(data.detach().cpu().numpy()), [
                 pred_y, batch_y], [preds_lst, trues_lst]))

            loss = self.criterion(pred_y, batch_y)
            vali_pbar.set_description(
                'vali loss: {:.4f}'.format(loss.mean().item()))
            total_loss.append(loss.mean().item())

        total_loss = np.average(total_loss)
        preds = np.concatenate(preds_lst, axis=0)
        trues = np.concatenate(trues_lst, axis=0)
        #mse, mae, t_mae, ssim, psnr = metric(preds, trues, vali_loader.dataset.mean, vali_loader.dataset.std, False)
        mse, mae = metric(preds, trues, vali_loader.dataset.mean, vali_loader.dataset.std, False)
        #self.t_sample = t_mae/np.sum(t_mae) <- only need for long epoch

        #print_log('vali mse:{:.4f}, mae:{:.4f}, ssim:{:.4f}, psnr:{:.4f}'.format(mse, mae, ssim, psnr))
        print_log('vali mse:{:.4f}, mae:{:.4f}'.format(mse, mae))
        self.model.train()
        return total_loss

    def test(self, args):
        self.model.eval()

        inputs_lst, trues_lst, preds_lst = [], [], []
        for batch_x, batch_y in self.test_loader:
            pred_y = self.model(batch_x.to(self.device))
            list(map(lambda data, lst: lst.append(data.detach().cpu().numpy()), [
                 batch_x, batch_y, pred_y], [inputs_lst, trues_lst, preds_lst]))

        inputs, trues, preds = map(lambda data: np.concatenate(
            data, axis=0), [inputs_lst, trues_lst, preds_lst])

        folder_path = self.path+'/results/{}/sv/'.format(args.ex_name)
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        mse, mae = metric(preds, trues, self.test_loader.dataset.mean, self.test_loader.dataset.std, False)
        #mse, mae, ssim, psnr = metric(preds, trues, self.test_loader.dataset.mean, self.test_loader.dataset.std, True)
       # print_log('mse:{:.4f}, mae:{:.4f}, ssim:{:.4f}, psnr:{:.4f}'.format(mse, mae, ssim, psnr))
        print_log('mse:{:.4f}, mae:{:.4f}'.format(mse, mae))
        for np_data in ['inputs', 'trues', 'preds']:
            np.save(osp.join(folder_path, np_data + '.npy'), vars()[np_data])
        return mse

    def mytest(self):
        with torch.no_grad():
            args = self.args
            model = IAM4VP(tuple(args.in_shape), args.hid_S,
                                args.hid_T, args.N_S, args.N_T).to(self.device)
            if os.path.exists('wind_v_checkpoint.pth'):
                print("load_model!")
                model.load_state_dict(torch.load('wind_v_checkpoint.pth'))
            model.eval()
            vali_loader = self.vali_loader
            preds_lst, trues_lst, total_loss = [], [], []
            vali_pbar = tqdm(vali_loader)
            for i, (batch_x, batch_y) in enumerate(vali_pbar):
                batch_x, batch_y = batch_x.to(self.device), batch_y.to(self.device)
                pred_list = []
                for timestep in range(12):
                    t = torch.tensor(timestep * 100).repeat(batch_x.shape[0]).cuda()
                    pred_y =model(batch_x, pred_list, t)  ##
                    pred_list.append(pred_y)
                pred_y = torch.cat(pred_list, dim=1).unsqueeze(2)
                list(map(lambda data, lst: lst.append(data.detach().cpu().numpy()), [
                    pred_y, batch_y], [preds_lst, trues_lst]))
                loss = self.criterion(pred_y, batch_y)
                vali_pbar.set_description(
                    'vali loss: {:.4f}'.format(loss.mean().item()))
                total_loss.append(loss.mean().item())
            total_loss = np.average(total_loss)
            preds = np.concatenate(preds_lst, axis=0)
            trues = np.concatenate(trues_lst, axis=0)
            # mse, mae, t_mae, ssim, psnr = metric(preds, trues, vali_loader.dataset.mean, vali_loader.dataset.std, False)
            mse, mae = metric(preds, trues, vali_loader.dataset.mean, vali_loader.dataset.std, False)
            # self.t_sample = t_mae/np.sum(t_mae) <- only need for long epoch

            # print_log('vali mse:{:.4f}, mae:{:.4f}, ssim:{:.4f}, psnr:{:.4f}'.format(mse, mae, ssim, psnr))
            print_log('vali mse:{:.4f}, mae:{:.4f}'.format(mse, mae))
        return mse
