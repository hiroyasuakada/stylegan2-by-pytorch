import time
import random
import math
import os
from tensorboardX import SummaryWriter

import numpy as np
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F

from model_networks import Generator, Discriminator
from distributed import (
    get_rank,
    synchronize,
    reduce_loss_dict,
    reduce_sum,
    get_world_size,
)

# from model_loss import                        
# from dataset import Dataset


###################################################################################################
###                                         StyleGAN2                                           ###
###################################################################################################


class StyleGAN2(object):
    def __init__(self, log_dir='logs', device='cuda:0', gpu_ids=[0, 1, 2, 3], 
                 batch_size=1, n_sample=16, lr=0.002, r1=10, path_regularize=2, path_batch_shrink=2, 
                 g_reg_every=4, d_reg_every=16, mixing=0.9, mode_train=True):
        
        self.batch_size = batch_size
        self.n_sample = n_sample
        self.log_dir = log_dir
        self.device = device
        print(torch.cuda.is_available())
        
        self.lr = lr
        self.g_reg_every = g_reg_every
        self.d_reg_every = d_reg_every
        self.latents_dim = 512
        
        self.r1 = r1
        self.path_regularize = path_regularize
        self.path_batch_shrink = path_batch_shrink
        self.mixing = mixing
        self.path_lengths = torch.tensor(0.0, device=self.device)
        self.mean_path_length = 0
        self.mean_path_length_avg = 0

        self.sample_z = torch.randn(self.n_sample, self.latents_dim, device=self.device)
     
        if mode_train:
            self.gpu_ids = gpu_ids  # [0, 1, 2, 3] for DLB
        else:
            self.gpu_ids = [0]
        
        # load networks
        self.G = Generator().to(self.device)
        self.D = Discriminator().to(self.device)

        # set multi-GPUs
        self.G = torch.nn.DataParallel(self.G, self.gpu_ids)
        self.D = torch.nn.DataParallel(self.D, self.gpu_ids)
        
        # initialize loss functions
        self.d_adv_loss = torch.tensor(0.0, device=self.device)
        self.r1_loss = torch.tensor(0.0, device=self.device)
        self.g_adv_loss = torch.tensor(0.0, device=self.device)
        self.path_loss = torch.tensor(0.0, device=self.device)

        # optimize params for G and D
        g_reg_ratio = g_reg_every / (g_reg_every + 1)
        d_reg_ratio = d_reg_every / (d_reg_every + 1)
        self.optimizer_G = torch.optim.Adam(self.G.parameters(), 
                                            lr=self.lr * g_reg_ratio, 
                                            betas=(0 ** g_reg_ratio, 
                                                   0.99 ** g_reg_ratio))
        self.optimizer_D = torch.optim.Adam(self.D.parameters(), 
                                            lr=self.lr * d_reg_ratio, 
                                            betas=(0 ** d_reg_ratio, 
                                                   0.99 ** d_reg_ratio))

    def mixing_noise(self, batch_size, latents_dim, prob):
        if prob > 0 and random.random() < prob:
            noise = torch.randn(2, batch_size, latents_dim, device=self.device).unbind(0)
        else:
            noise = torch.randn(batch, latents_dim, device=self.device)

        return noise

    def g_path_regularize(fake_imgs, latents, mean_path_length, decay=0.01):
        moise = torch.randn_like(fake_imgs) / math.sqrt(fake_imgs.shape[2] * fake_imgs.shape[3])
        grad, = autograd.grad(outputs=(fake_imgs * noise).sum(), inputs=latents, create_graph=True)
        path_lengths = torch.sqrt(grad.pow(2).sum(2).mean(1))

        path_mean = mean_path_length + decay * (path_lengths.mean() - mean_path_length)
        path_loss = (path_lengths - path_mean).pow(2).mean()

        return path_loss, path_mean.detach(), path_lengths

    def set_input(self, data):
        data_D = data
        data_G = data

        return data_D, data_G

    def backward_D_adv(self, real_imgs, batch_size, latents_dim, ,mixing, device):
        # make noise as an input for Generator
        noise = self.mixing_noise(batch_size, latents_dim, mixing, device)
        
        # create fake images
        fake_imgs, _ = self.G(noise)

        # predict real or fake
        fake_pred = self.D(fake_imgs)
        real_pred = self.D(real_imgs)

        # calculate an adversarial loss (logistic loss) / D tries to distinguish real and fake
        real_loss = F.softplus(-real_pred)
        fake_loss = F.softplus(fake_pred)
        d_adv_loss = real_loss.mean() + fake_loss.mean()

        # backward
        d_adv_loss.backward()

        return d_adv_loss

    def backward_D_r1(self, real_imgs, r1, d_reg_every):
        # predict real
        real_imgs.requires_grad = True
        real_pred = self.D(real_imgs)

        # calculate gradient penalty as a r1 loss
        grad_real, = torch.autograd.grad(outputs=real_pred.sum(), inputs=real_imgs, create_graph=True)
        r1_loss = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()

        # backward
        (r1 / 2 * r1_loss * d_reg_every + 0 * real_pred[0]).backward()

        return r1_loss

    def backward_G_adv(self, batch_size, latents_dim, mixing, device):
        # make noise as an input for Generator
        noise = self.mixing_noise(batch_size, latents_dim, mixing, device)
        
        # create fake images
        fake_imgs, _ = self.G(noise)

        # predict real or fake
        fake_pred = self.D(fake_imgs)

        # calculate an adversarial loss / G tries to fool D
        g_adv_loss = F.softplus(-fake_pred).mean()

        # backward
        g_adv_loss.backward()

        return g_adv_loss

    def backward_G_path(self, batch_size, latents_dim, mixing, 
                        path_batch_shrink, mean_path_length, path_regularize, g_reg_every, device):
        path_batch_size = max(1, batch_size // path_batch_shrink)
        noise = self.mixing_noise(path_batch_size, latents_dim, mixing, device)
        fake_imgs, latents = self.G(noise, return_latents=True)

        path_loss, mean_path_length, path_lengths = g_path_regularize(fake_imgs, latents, mean_path_length)

        g_weighted_path_loss = path_regularize * g_reg_every * path_loss

        # reduce
        if path_batch_shrink:
            g_weighted_path_loss += 0 * fake_imgs[0, 0, 0, 0]

        # backward
        g_weighted_path_loss.backward()

        return path_loss, mean_path_length, path_lengths

    def optimize(self, batch_idx, data):
        data_D, data_G = self.set_input(data)

        # update Discriminator
        self.optimizer_D.zero_grad()
        self.d_adv_loss = self.backward_D_adv(
                real_imgs=data, batch_size=self.batch_size, latents_dim=self.latents_dim, 
                mixing=self.mixing, device=self.device
                )
        self.optimizer_D.step

        # apply r1 regularization to Discriminator
        if batch_idx % self.d_reg_every == 0:
            self.optimizer_D.zero_grad()
            self.r1_loss = self.backward_D_r1(
                    real_imgs=data, r1=self.r1, d_reg_every=self.d_reg_every
                    )
            self.optimizer_D.step
        
        # update Generator
        self.optimizer_G.zero_grad()
        self.g_adv_loss = self.backward_G_adv(
                batch_size=self.batch_size, latents_dim=self.latents_dim, 
                mixing=self.mixing, device=self.device
                )
        self.optimizer_G.step

        # apply path length regularization to Generator
        if batch_idx % self.g_reg_every == 0:
            self.optimizer_G.zero_grad()
            self.path_loss, self.mean_path_length, self.path_lengths = self.backward_G_path(
                    batch_size=self.batch_size, latents_dim=self.latents_dim, mixing=self.mixing,
                    path_batch_shrink=self.path_batch_shrink, mean_path_length=self.mean_path_length, 
                    path_regularize=self.path_regularize, device=self.device
                    )
            self.optimizer_G.step

            self.mean_path_length_avg = (reduce_sum(self.mean_path_length).item() / get_world_size())

        losses = [self.d_adv_loss, 
                  self.r1_loss, 
                  self.g_adv_loss, 
                  self.path_loss,
                  self.path_lengths,
                  self.mean_path_length_avg]

        return np.array(losses).astype(np.float32)
        
    def train(self, data_loader):
        running_loss = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        time_list = []

        for batch_idx, data in enumerate(data_loader):
            # count time 1
            t1 = time.perf_counter()

            # get losses
            losses = self.optimize(batch_idx, data)
            running_loss += losses

            # count time 2 
            t2 = time.perf_counter()
            get_processing_time = t2 - t1
            time_list.append(get_processing_time)

            # print batch and processing time, when idx == 500 
            if batch_idx % 500 == 0:
                print('batch: {} / elapsed_time: {} sec'.format(batch_idx, sum(time_list)))
                time_list = []

        running_loss /= len(data_loader)
        return running_loss

    def save_network(network, network_label, epoch_label):
        # path to files
        save_filename = '{}_net_{}.pth'.format(network_label, epoch_label)
        save_path = os.path.join(self.log_dir, save_filename)

        # save models on CPU
        torch.save(network.cpu().state_dict(), save_path)

        # return models to GPU
        network.to(self.device)

    def load_network(network, network_label, epoch_label):
        # path to files
        load_filename = '{}_net_{}.pth'.format(network_label, epoch_label)
        load_path = os.path.join(self.log_dir, load_filename)

        # load models
        network.load_state_dict(torch.load(load_path))

    def save(self, epoch_label):
        self.save_network(self.G, 'Generator', epoch_label)
        self.save_network(self.D, 'Discriminator', epoch_label)

    def load(self, epoch_label):
        self.load_network(self.G, 'Generator', epoch_label)
        self.load_network(self.D, 'Discriminator', epoch_label)

