#/usr/bin/env python3.6
import re
import argparse
import warnings
from pathlib import Path
from operator import itemgetter
from shutil import copytree, rmtree
import typing
from typing import Any, Callable, List, Tuple
import matplotlib.pyplot as plt
import torch
import numpy as np
import pandas as pd
import torch.nn.functional as F
from torch import Tensor
from torch.utils.data import DataLoader
from dice3d import dice3d, dice3dn
from networks import weights_init
from dataloader import get_loaders
from utils import map_, save_dict_to_file
from utils import dice_coef, dice_batch, save_images, tqdm_
from utils import probs2one_hot, probs2class, mask_resize, resize, haussdorf
from utils import adjust_learning_rate
import datetime
from itertools import cycle
import os

import matplotlib.pyplot as plt


def setup(args, n_class, dtype) -> Tuple[Any, Any, Any, List[Callable], List[float],List[Callable], List[float], Callable]:
    print(">>> Setting up")
    cpu: bool = args.cpu or not torch.cuda.is_available()
    device = torch.device("cpu") if cpu else torch.device("cuda")

    if args.model_weights:
        if cpu:
            net = torch.load(args.model_weights, map_location='cpu')
        else:
            net = torch.load(args.model_weights)
    else:
        net_class = getattr(__import__('networks'), args.network)
        net = net_class(1, n_class).type(dtype).to(device)
        net.apply(weights_init)
    net.to(device)

    optimizer = torch.optim.Adam(net.parameters(), lr=args.l_rate, betas=(0.9, 0.999))

    print(args.losses)
    losses = eval(args.losses)
    loss_fns: List[Callable] = []
    for loss_name, loss_params, _, _, fn, _ in losses:
        loss_class = getattr(__import__('losses'), loss_name)
        loss_fns.append(loss_class(**loss_params, dtype=dtype, fn=fn))

    loss_weights = map_(itemgetter(5), losses)

    print(args.losses_source)
    losses_source = eval(args.losses_source)
    loss_fns_source: List[Callable] = []
    for loss_name, loss_params, _, _, fn, _ in losses_source:
        loss_class = getattr(__import__('losses'), loss_name)
        loss_fns_source.append(loss_class(**loss_params, dtype=dtype, fn=fn))

    loss_weights_source = map_(itemgetter(5), losses_source)

    if args.scheduler:
        scheduler = getattr(__import__('scheduler'), args.scheduler)(**eval(args.scheduler_params))
    else:
        scheduler = ''

    return net, optimizer, device, loss_fns, loss_weights, loss_fns_source, loss_weights_source, scheduler


def do_epoch(args, mode: str, net: Any, device: Any, loader: DataLoader, epc: int,
             loss_fns: List[Callable], loss_weights: List[float],loss_fns_source: List[Callable],
             loss_weights_source: List[float], new_w:int, num_steps:int, C: int, metric_axis:List[int], savedir: str = "",
             optimizer: Any = None, target_loader: Any = None):

    assert mode in ["train", "val"]
    L: int = len(loss_fns)
    indices = torch.tensor(metric_axis,device=device)
    if mode == "train":
        net.train()
        desc = f">> Training   ({epc})"
    elif mode == "val":
        net.eval()
        # net.train()
        desc = f">> Validation ({epc})"

    total_it_s, total_images = len(loader), len(loader.dataset)
    total_it_t, total_images_t = len(target_loader), len(target_loader.dataset)
    total_iteration = max(total_it_s, total_it_t)
    # Lazy add lines below because we will be cycling until the biggest length is reached
    total_images = max(total_images, total_images_t)
    total_images_t = total_images

    pho=1
    dtype = eval(args.dtype)

    all_dices: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    all_inter_card: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    all_card_gt: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    all_card_pred: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    loss_log: Tensor = torch.zeros((total_images), dtype=dtype, device=device)
    posim_log: Tensor = torch.zeros((total_images), dtype=dtype, device=device)
    haussdorf_log: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    all_grp: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    dice_3d_log: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)
    dice_3d_sd_log: Tensor = torch.zeros((total_images, C), dtype=dtype, device=device)


    tq_iter = tqdm_(enumerate(zip(loader, target_loader)), total=total_iteration, desc=desc)
    done: int = 0
    ratio_losses = 0
    n_warmup = 0
    mult_lw = [pho ** (epc - n_warmup + 1)] * len(loss_weights)
    if epc > 100:
        mult_lw = [pho ** 100] * len(loss_weights)
    mult_lw[0] = 1
    loss_weights = [a * b for a, b in zip(loss_weights, mult_lw)]
    losses_vec, source_vec, target_vec, baseline_target_vec = [], [], [], []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for j, (source_data, target_data) in tq_iter:
        #for j, target_data in tq_iter:
            source_data[1:] = [e.to(device) for e in source_data[1:]]  # Move all tensors to device
            filenames_source, source_image, source_gt = source_data[:3]
            target_data[1:] = [e.to(device) for e in target_data[1:]]  # Move all tensors to device
            filenames_target, target_image, target_gt = target_data[:3]
            labels = target_data[3:3+L]
            bounds = target_data[3+L:]
            assert len(labels) == len(bounds)
            B = len(target_image)
            # Reset gradients
            if optimizer:
                adjust_learning_rate(optimizer, 1, args.l_rate, num_steps, args.power)
                optimizer.zero_grad()

            # Forward
            with torch.set_grad_enabled(mode == "train"):
                pred_logits: Tensor = net(target_image)
                pred_logits_source: Tensor = net(source_image)
                pred_probs: Tensor = F.softmax(pred_logits, dim=1)
                pred_probs_source: Tensor = F.softmax(pred_logits_source, dim=1)
                if new_w > 0:
                    pred_probs = resize(pred_probs, new_w)
                    labels = [resize(label, new_w) for label in labels]
                    target = resize(target, new_w)
                predicted_mask: Tensor = probs2one_hot(pred_probs)  # Used only for dice computation    
            assert len(bounds) == len(loss_fns) == len(loss_weights)
            if epc < n_warmup:
                loss_weights = [1, 0]
            loss: Tensor = torch.zeros(1, requires_grad=True).to(device)
            loss_vec = []
            for loss_fn, label, w, bound in zip(loss_fns, labels, loss_weights, bounds):
                if w > 0:
                    if args.lin_aug_w:
                        if epc<70:
                            w=w*(epc+1)/70
                    loss =loss+ w * loss_fn(pred_probs, label, bound)

            for loss_fn, label, w, bound in zip(loss_fns_source, [source_gt], loss_weights_source, torch.randn(1)):
                if w > 0:
                    loss =loss+ w * loss_fn(pred_probs_source, label, bound)

            # Backward
            if optimizer:
                loss.backward()
                optimizer.step()

            # Compute and log metrics
            dices, inter_card, card_gt, card_pred = dice_coef(predicted_mask.detach(), target_gt.detach())
            assert dices.shape == (B, C), (dices.shape, B, C)
            
            sm_slice = slice(done, done + B)  # Values only for current batch
            all_dices[sm_slice, ...] = dices
            # # for 3D dice
            all_grp[sm_slice, ...] = int(re.split('_', filenames_target[0])[1]) * torch.ones([1, C])
            all_inter_card[sm_slice, ...] = inter_card
            all_card_gt[sm_slice, ...] = card_gt
            all_card_pred[sm_slice, ...] = card_pred
            loss_log[sm_slice] = loss.detach()
            
            # # Save images
            if savedir:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=UserWarning)
                    warnings.simplefilter("ignore") 
                    predicted_class: Tensor = probs2class(pred_probs)
                    save_images(predicted_class, filenames_target, savedir, mode, epc, True)
            
            # Logging
            big_slice = slice(0, done + B)  # Value for current and previous batches
            stat_dict = {"dice": torch.index_select(all_dices, 1, indices).mean(),
                         "loss": loss_log[big_slice].mean()}
            nice_dict = {k: f"{v:.4f}" for (k, v) in stat_dict.items()}
            
            done += B
            tq_iter.set_postfix(nice_dict)
        print(f"{desc} " + ', '.join(f"{k}={v}" for (k, v) in nice_dict.items()))

    dice_3d_log, dice_3d_sd_log = dice3dn(all_grp, all_inter_card, all_card_gt, all_card_pred,metric_axis,True)
    print("mean 3d_dice over all patients:",dice_3d_log)
    target_vec = [ dice_3d_log, dice_3d_sd_log]

    losses_vec = [loss_log.mean().item()]
    return losses_vec, target_vec


def run(args: argparse.Namespace) -> None:
    # save args to dict
    d = vars(args)
    d['time'] = str(datetime.datetime.now())
    save_dict_to_file(d,args.workdir)

    temperature: float = 0.1
    n_class: int = args.n_class
    metric_axis: List = args.metric_axis
    lr: float = args.l_rate
    dtype = eval(args.dtype)

    # Proper params
    savedir: str = args.workdir
    n_epoch: int = args.n_epoch

    net, optimizer, device, loss_fns, loss_weights, loss_fns_source, loss_weights_source, scheduler = setup(args, n_class, dtype)
    print(f'> Loss weights cons: {loss_weights}, Loss weights source:{loss_weights_source}')
    shuffle = False
    if args.mix:
        shuffle = True
    #print("args.dataset",args.dataset)
    loader, loader_val = get_loaders(args, args.dataset,args.folders,
                                           args.batch_size, n_class,
                                           args.debug, args.in_memory, dtype, False,fix_size=[0,0])

    target_loader, target_loader_val = get_loaders(args, args.target_dataset,args.target_folders,
                                           args.batch_size, n_class,
                                           args.debug, args.in_memory, dtype, shuffle,fix_size=[0,0])

    num_steps = n_epoch * len(loader)
    #print(num_steps)
    print("metric axis",metric_axis)
    best_dice_pos: Tensor = np.zeros(1)
    best_dice: Tensor = np.zeros(1)
    best_3d_dice: Tensor = np.zeros(1)

    print("Results saved in ", savedir)
    print(">>> Starting the training")
    for i in range(n_epoch):

        tra_losses_vec, tra_target_vec                                    = do_epoch(args, "train", net, device,
                                                                                           loader, i, loss_fns,
                                                                                           loss_weights,
                                                                                           loss_fns_source,
                                                                                           loss_weights_source,
                                                                                           args.resize,
                                                                                           num_steps, n_class, metric_axis,
                                                                                           savedir="",
                                                                                           optimizer=optimizer,
                                                                                           target_loader=target_loader)

        with torch.no_grad():
            val_losses_vec, val_target_vec                                        = do_epoch(args, "val", net, device,
                                                                                               loader_val, i, loss_fns,
                                                                                               loss_weights,
                                                                                               loss_fns_source,
                                                                                               loss_weights_source,
                                                                                               args.resize,
                                                                                               num_steps, n_class,metric_axis,
                                                                                               savedir=savedir,
                                                                                               target_loader=target_loader_val)


        df_t_tmp = pd.DataFrame({
            "tra_loss":tra_losses_vec,
            "val_loss":val_losses_vec,
            "tra_dice_3d": [tra_target_vec[0]],
            "tra_dice_3d_sd": [tra_target_vec[1]],
            "val_dice_3d": [val_target_vec[0]],
            "val_dice_3d_sd": [val_target_vec[1]]})

        if i == 0:
            df_t = df_t_tmp
        else:
            df_t = df_t.append(df_t_tmp)

        df_t.to_csv(Path(savedir, "_".join(("target", args.csv))), float_format="%.4f", index_label="epoch")

        # Save model if better
        current_val_target_3d_dice = val_target_vec[0]

        if current_val_target_3d_dice > best_3d_dice:
            best_epoch = i
            best_3d_dice = current_val_target_3d_dice
            with open(Path(savedir, "best_epoch_3d.txt"), 'w') as f:
                f.write(str(i))
            best_folder_3d = Path(savedir, "best_epoch_3d")
            if best_folder_3d.exists():
                rmtree(best_folder_3d)
            copytree(Path(savedir, f"iter{i:03d}"), Path(best_folder_3d))
            torch.save(net, Path(savedir, "best_3d.pkl"))

        if i == n_epoch - 1:
            with open(Path(savedir, "last_epoch.txt"), 'w') as f:
                f.write(str(i))
            last_folder = Path(savedir, "last_epoch")
            if last_folder.exists():
                rmtree(last_folder)
            copytree(Path(savedir, f"iter{i:03d}"), Path(last_folder))
            torch.save(net, Path(savedir, "last.pkl"))

        # remove images from iteration
        rmtree(Path(savedir, f"iter{i:03d}"))

        if args.flr==False:
            adjust_learning_rate(optimizer, i, args.l_rate, n_epoch, 0.9)
    print("Results saved in ", savedir)


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Hyperparams')
    parser.add_argument('--dataset', type=str, required=True)
    parser.add_argument('--target_dataset', type=str, required=True)
    parser.add_argument("--workdir", type=str, required=True)
    parser.add_argument("--losses", type=str, required=True,
                        help="List of (loss_name, loss_params, bounds_name, bounds_params, fn, weight)")
    parser.add_argument("--losses_source", type=str, required=True,
                        help="List of (loss_name, loss_params, bounds_name, bounds_params, fn, weight)")
    parser.add_argument("--folders", type=str, required=True,
                        help="List of (subfolder, transform, is_hot)")
    parser.add_argument("--target_folders", type=str, required=True,
                        help="List of (subfolder, transform, is_hot)")
    parser.add_argument("--network", type=str, required=True, help="The network to use")
    parser.add_argument("--grp_regex", type=str, required=True)
    parser.add_argument("--n_class", type=int, required=True)

    parser.add_argument("--lin_aug_w", action="store_true")
    parser.add_argument("--flr", action="store_true")
    parser.add_argument("--augment", action="store_true")
    parser.add_argument("--mix", type=bool, default=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--csv", type=str, default='metrics.csv')
    parser.add_argument("--model_weights", type=str, default='')
    parser.add_argument("--cpu", action='store_true')
    parser.add_argument("--in_memory", action='store_true')
    parser.add_argument("--resize", type=int, default=0)
    parser.add_argument("--pho", nargs='?', type=float, default=1,
                        help='augment')
    parser.add_argument('--n_epoch', nargs='?', type=int, default=200,
                        help='# of the epochs')
    parser.add_argument('--l_rate', nargs='?', type=float, default=5e-4,
                        help='Learning Rate')
    parser.add_argument('--weight_decay', nargs='?', type=float, default=1e-5,
                        help='L2 regularisation of network weights')
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument("--dtype", type=str, default="torch.float32")
    parser.add_argument("--scheduler", type=str, default="DummyScheduler")
    parser.add_argument("--scheduler_params", type=str, default="{}")
    parser.add_argument("--bounds_on_fgt", type=bool, default=False)
    parser.add_argument("--bounds_on_train_stats", type=str, default='')
    parser.add_argument("--power",type=float, default=0.9)
    parser.add_argument("--metric_axis",type=int, nargs='*', required=True, help="Classes to display metrics. \
        Display only the average of everything if empty")
    args = parser.parse_args()
    print(args)

    return args


if __name__ == '__main__':
    run(get_args())
