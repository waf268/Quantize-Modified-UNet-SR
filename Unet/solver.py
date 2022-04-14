from __future__ import print_function

from math import log10

import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn

#from FilterCNN.model import Net
from progress_bar import progress_bar
from Unet.Umodel import UNet8
from Unet.Umodel import UNet4
from Unet.Umodel import UNet2
from Unet.GraLoss import GradientLoss

from torchvision.transforms.functional import to_pil_image
from pytorch_msssim import ssim
import numpy as np
import os
import logging
import sys
import copy

from quantization_utils.quant_modules import *


class unetTrainer(object):
    def __init__(self, config, training_loader, testing_loader):
        super(unetTrainer, self).__init__()
        self.CUDA = torch.cuda.is_available()
        self.device = torch.device('cuda' if self.CUDA else 'cpu')

        self.model = None
        self.model_teacher = None

        self.lr = config.lr
        self.nEpochs = config.nEpochs
        self.criterion = None
        self.optimizer = None
        self.scheduler = None
        self.seed = config.seed
        self.upscale_factor = config.upscale_factor
        self.training_loader = training_loader
        self.testing_loader = testing_loader

        self.save_path = os.path.join("result")
        self.trained_model_path = "result_BSD300_mixGE/my_model.pth"

        self.logger = self.set_logger()

    def set_logger(self):
        logger = logging.getLogger('baseline')
        file_formatter = logging.Formatter('%(message)s')
        console_formatter = logging.Formatter('%(message)s')

        # file log
        file_handler = logging.FileHandler(os.path.join(self.save_path, "train_test.log"))
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # console log
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        logger.setLevel(logging.INFO)
        return logger

    def build_model(self):
        #self.model = Net(num_channels=3, base_filter=64, upscale_factor=self.upscale_factor).to(self.device)
        if self.upscale_factor==2:
            self.model=UNet2(3,3).to(self.device)
            self.model_teacher=UNet2(3,3).to(self.device)
        if self.upscale_factor==4:
            self.model=UNet4(3,3).to(self.device)
            self.model_teacher=UNet4(3,3).to(self.device)
        if self.upscale_factor==8:
            self.model=UNet8(3,3).to(self.device)
            self.model_teacher=UNet8(3,3).to(self.device)
        self.model.weight_init(mean=0.0, std=0.01)
        self.model_teacher.weight_init(mean=0.0, std=0.01)
        self.criterion = torch.nn.MSELoss()
        self.criterion_3=torch.nn.L1Loss()
        self.criterion_2 = GradientLoss()
        torch.manual_seed(self.seed)

        self.logger.info('# model parameters:', sum(param.numel() for param in self.model.parameters()))

        # quantize model
        self.model.load_state_dict(torch.load(self.trained_model_path))
        self.model = self.quantize_model(self.model)
        self.model.to(self.device)

        if self.CUDA:
            torch.cuda.manual_seed(self.seed)
            cudnn.benchmark = True
            self.criterion.cuda()
            self.criterion_2.cuda()

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr,weight_decay=1e-6)#,weight_decay=1e-4
        self.scheduler = torch.optim.lr_scheduler.MultiStepLR(self.optimizer, milestones=[50,100,150,200,300,400,500,1000], gamma=0.5)

    def save_model(self):
        model_name = "my_model.pth"
        torch.save(self.model.state_dict(), os.path.join(self.save_path, model_name))
        print("Model saved.")

    def quantize_model(self, model):
        """
        Recursively quantize a pretrained single-precision model to int8 quantized model
        model: pretrained single-precision model
        """
        
        weight_bit = 8
        act_bit = 8
        
        # quantize convolutional and linear layers
        if type(model) == nn.Conv2d:
            quant_mod = Quant_Conv2d(weight_bit=weight_bit)
            quant_mod.set_param(model)
            return quant_mod
        elif type(model) == nn.Linear:
            quant_mod = Quant_Linear(weight_bit=weight_bit)
            quant_mod.set_param(model)
            return quant_mod
        elif type(model) == nn.ConvTranspose2d:
            quant_mod = Quant_ConvTranspose2d(weight_bit=weight_bit)
            quant_mod.set_param(model)
            return quant_mod
        
        # quantize all the activation
        elif type(model) == nn.ReLU or type(model) == nn.ReLU6:
            return nn.Sequential(*[model, QuantAct(activation_bit=act_bit)])
        
        # recursively use the quantized module to replace the single-precision module
        elif type(model) == nn.Sequential:
            mods = []
            for n, m in model.named_children():
                mods.append(self.quantize_model(m))
            return nn.Sequential(*mods)
        else:
            q_model = copy.deepcopy(model)
            for attr in dir(model):
                mod = getattr(model, attr)
                if isinstance(mod, nn.Module) and 'norm' not in attr:
                    setattr(q_model, attr, self.quantize_model(mod))
            return q_model
    
    def train(self):
        self.model.train()
        #self.model_teacher.eval()
        train_loss = 0
        for batch_num, (data, target) in enumerate(self.training_loader):

            data, target = data.to(self.device), target.to(self.device)
            #data = data.to(self.device)

            self.optimizer.zero_grad()

            '''
            # predict
            prediction = self.model(data)
            prediction_teacher = self.model_teacher(data)

            # calculate kd loss(L1)
            loss = self.criterion_3(prediction, prediction_teacher)

            # MSE loss
            #loss = self.criterion(prediction, prediction_teacher)

            # MixGE loss
            #mseLoss = self.criterion(prediction, prediction_teacher)
            #geLoss = self.criterion_2(prediction, prediction_teacher)
            #loss = mseLoss + 0.1 * geLoss
            '''

            prediction = self.model(data)

            # L1 loss
            #loss = self.criterion_3(prediction, target)

            # MSE loss
            #loss = self.criterion(prediction, target)

            # MixGE loss
            mseLoss = self.criterion(prediction, target)
            geLoss = self.criterion_2(prediction, target)
            loss = mseLoss + 0.1 * geLoss

            # new MixGE loss 
            # Van Der Jeught, S., Muyshondt, P. G., & Lobato, I. (2021). Optimized loss function in deep learning profilometry for improved prediction performance. Journal of Physics: Photonics, 3(2), 024014.
            #l1Loss = self.criterion_3(prediction, target)
            #geLoss = self.criterion_2(prediction, target)
            #loss = 0.5 * l1Loss + 0.5 * geLoss

            #print(str(loss1.cpu().detach().numpy())+'  '+str(loss_ssim.cpu().detach().numpy()))
            

            train_loss += loss.item()
            loss.backward()
            self.optimizer.step()
            #progress_bar(batch_num, len(self.training_loader), 'Loss: %.4f' % (train_loss / (batch_num + 1)))

        self.logger.info("Training: Average Loss: {:.4f}".format(train_loss / len(self.training_loader)))

    def test(self, epoch):
        self.model.eval()
        avg_psnr = 0
        avg_ssim = 0
        with torch.no_grad():
            for batch_num, (data, target) in enumerate(self.testing_loader):
                data, target = data.to(self.device), target.to(self.device)
                prediction = self.model(data)
                mse = self.criterion(prediction, target)
                psnr = 10 * log10(1 / mse.item())
                avg_psnr += psnr
                ssim_value = ssim(prediction, target, data_range=1)
                #print(ssim_value)
                avg_ssim += ssim_value

                if epoch == self.nEpochs:
                    # change output to image
                    output = prediction.squeeze()
                    output = to_pil_image(output).convert('RGB')

                    # save output
                    output.save(os.path.join(self.save_path, "prediction", f"prediction_{batch_num}.jpg"))

                    # save target
                    target = target.squeeze()
                    target = to_pil_image(target).convert('RGB')
                    target.save(os.path.join(self.save_path, "original", f"original_{batch_num}.jpg"))
                #progress_bar(batch_num, len(self.testing_loader), 'PSNR: %.4f | SSIM: %.4f' % ((avg_psnr / (batch_num + 1)),avg_ssim / (batch_num + 1)))

        avg_psnr /= len(self.testing_loader)
        avg_ssim /= len(self.testing_loader)
        self.logger.info(f"Testing:  Average PSNR: {avg_psnr:.4f}  SSIM: {avg_ssim:.4f}")

    def test_teacher(self):
        self.model_teacher.eval()
        avg_psnr = 0
        avg_ssim = 0
        with torch.no_grad():
            for batch_num, (data, target) in enumerate(self.testing_loader):
                data, target = data.to(self.device), target.to(self.device)

                prediction = self.model_teacher(data)

                mse = self.criterion(prediction, target)

                psnr = 10 * log10(1 / mse.item())
                avg_psnr += psnr
                ssim_value = ssim(prediction, target, data_range=1)
                
                avg_ssim += ssim_value
                #progress_bar(batch_num, len(self.testing_loader), 'PSNR: %.4f | SSIM: %.4f' % ((avg_psnr / (batch_num + 1)),avg_ssim / (batch_num + 1)))

        avg_psnr /= len(self.testing_loader)
        avg_ssim /= len(self.testing_loader)
        self.logger.info(f"\nTesting Teacher Model:  Average PSNR: {avg_psnr:.4f}  SSIM: {avg_ssim:.4f}\n")

    def run(self):
        # load model
        self.build_model()

        self.model_teacher.load_state_dict(torch.load(self.trained_model_path))
        self.model_teacher.to(self.device)

        self.logger.info(self.model)

        # test teacher model
        self.test_teacher()

        # train
        for epoch in range(1, self.nEpochs + 1):
            self.logger.info("\n===> Epoch {} starts:".format(epoch))
            self.train()
            self.test(epoch)
            self.scheduler.step(epoch)
            if epoch == self.nEpochs:
                self.save_model()

    def testOnly(self):
        self.build_model()
        self.model.load_state_dict(torch.load("result/my_model.pth"))
        self.model.to(self.device)
        self.test(self.nEpochs)