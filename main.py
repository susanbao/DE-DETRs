# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler

import datasets
import util.misc as utils
import datasets.samplers as samplers
from datasets import build_dataset, get_coco_api_from_dataset
from engine import evaluate, train_one_epoch
from models import build_model

import os
import wandb
import warnings
import ipdb


def get_args_parser():
    # ipdb.set_trace()
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--wandb', action='store_true', help="turn on wandb for logging")
    # label augmentation
    parser.add_argument('--repeat_label', type=int, default=None, help="repeat positive labels for n times")
    parser.add_argument('--repeat_ratio', type=float, default=None,
                        help="resample positive labels to make pos:all=ratio, e.g. 0.25")
    parser.add_argument('--two_stage_match', action='store_true', help="two stage matching for the repeated label")
    # nms for label augmentation
    parser.add_argument('--nms', action='store_true', help="use nms for postprocessing")
    parser.add_argument('--nms_thresh', type=float, default=0.7, help="IoU threshold for nms")
    parser.add_argument('--nms_remove', type=float, default=0.01, help="score multiplier for removed preds")

    # for DE-DETR model
    parser.add_argument('--pool_res', type=int, default=4, help="roi resolution)")
    parser.add_argument('--init_ref_dim', type=int, default=2, help="dimension of init reference", choices=[2, 4])
    parser.add_argument('--no_box_refine', action='store_false', dest='box_refine',
                        help="remove bbox refinement (as did in Cascaded RCNN)")
    parser.add_argument('--no_ms_roi', action='store_false', dest='ms_roi',
                        help="update memory and pos by roi align on single-scale feature (32x down-sampled)")

    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--lr_drop', default=40, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")
    parser.add_argument('--num_feature_levels', default=3, type=int, help='number of feature levels')

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=100, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')

    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    # * Matcher
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help="Relative classification weight of the no-object class")

    # dataset parameters
    # * down-sample dataset
    parser.add_argument('--sample_rate', default=None, type=float, help="sample rate for downsampled dataset")
    parser.add_argument('--sample_repeat', action='store_true',
                        help="repeat the dataset 1/sample_rate times, to maintain the computational cost")
    # * other dataset params
    parser.add_argument('--dataset_file', default='coco')
    parser.add_argument('--coco_path', type=str)
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--model', required=True, type=str, help='model name')
    parser.add_argument('--output_dir', default=None,
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=None, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=2, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    parser.add_argument('--cache_mode', default=False, action='store_true', help='whether to cache images on memory')
    return parser


def get_dataset_name(args):
    """name for down-sampled dataset: [dataset_file]down[ratio]rep"""
    if args.sample_rate is not None:
        assert 'down' in args.dataset_file, "sample_rate only works with down-sampled dataset!"
    dataset_name = args.dataset_file if args.sample_rate is None else args.dataset_file + str(args.sample_rate)
    dataset_name = dataset_name + 'rep' if args.sample_repeat else dataset_name
    return dataset_name


def main(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    if args.ms_roi is False:
        assert args.num_feature_levels == 1
    print(args)

    device = torch.device(args.device)

    # make seed random
    if args.seed is None:
        args.seed = random.randint(1, 10000)
    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    dataset_name = get_dataset_name(args)
    run_name = '_'.join([
        dataset_name, args.model, 'bs{}x{}'.format(args.world_size, args.batch_size),
        'seed{}'.format(args.seed),
    ])
    if args.output_dir is None:
        args.output_dir = os.path.join('work_dirs', run_name)
    # log with wandb
    if utils.get_rank() == 0:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        if args.wandb:
            wandb.init(config=args, project="DE-DETR")
            wandb.run.name = run_name
        else:
            warnings.warn("wandb is turned off")

    model, criterion, postprocessors = build_model(args)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)

    param_dicts = [
        {"params": [p for n, p in model_without_ddp.named_parameters() if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

    dataset_train = build_dataset(image_set='train', args=args)
    dataset_val = build_dataset(image_set='val', args=args)

    test_val = True
    if args.distributed:
        if args.cache_mode:
            sampler_train = samplers.NodeDistributedSampler(dataset_train)
            if test_val:
                sampler_val = samplers.NodeDistributedSampler(dataset_val, shuffle=False)
            else:
                sampler_val = samplers.NodeDistributedSampler(dataset_train, shuffle=False)
        else:
            sampler_train = samplers.DistributedSampler(dataset_train)
            if test_val:
                sampler_val = samplers.DistributedSampler(dataset_val, shuffle=False)
            else:
                sampler_val = samplers.DistributedSampler(dataset_train, shuffle=False)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        if test_val:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_train)

    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn, num_workers=args.num_workers,
                                   pin_memory=True)
    if test_val:
        data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                     drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers,
                                     pin_memory=True)
    else:
        data_loader_val = DataLoader(dataset_train, args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers,
                                 pin_memory=True)

    if args.dataset_file == "coco_panoptic":
        # We also evaluate AP during panoptic training, on original coco DS
        coco_val = datasets.coco.build("val", args)
        base_ds = get_coco_api_from_dataset(coco_val)
    else:
        if test_val:
            base_ds = get_coco_api_from_dataset(dataset_val)
        else:
            base_ds = get_coco_api_from_dataset(dataset_train)

    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    output_dir = Path(args.output_dir)
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

    if args.eval:
        test_stats, coco_evaluator = evaluate(model, criterion, postprocessors,
                                              data_loader_val, base_ds, device, args.output_dir)
        if args.output_dir:
            utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
        return

    print("Start training")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            sampler_train.set_epoch(epoch)
        print(run_name)  # remind the run name each epoch
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer, device, epoch,
            args.clip_max_norm)
        lr_scheduler.step()
        if args.output_dir:
            checkpoint_paths = [output_dir / 'checkpoint.pth']
            # extra checkpoint before LR drop and every 100 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)

        test_stats, coco_evaluator = evaluate(
            model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir
        )

        results = coco_evaluator.coco_eval['bbox'].stats
        if utils.get_rank() == 0 and args.wandb:
            info = {
                'Average Precision(AP) @ [IoU = 0.50:0.95 | area = all | maxDets = 100]': results[0],
                'Average Precision(AP) @ [IoU = 0.50 | area = all | maxDets = 100]': results[1],
                'Average Precision(AP) @ [IoU = 0.75 | area = all | maxDets = 100]': results[2],
                'Average Precision(AP) @ [IoU = 0.50:0.95 | area = small | maxDets = 100]': results[3],
                'Average Precision(AP) @ [IoU = 0.50:0.95 | area = medium | maxDets = 100]': results[4],
                'Average Precision(AP) @ [IoU = 0.50:0.95 | area = large | maxDets = 100]': results[5],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = all | maxDets = 1]': results[6],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = all | maxDets = 10]': results[7],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = all | maxDets = 100]': results[8],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = small | maxDets = 100]': results[9],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = medium | maxDets = 100]': results[10],
                'Average Recall(AR) @ [IoU = 0.50:0.95 | area = large | maxDets = 100]': results[11],
            }
            wandb.log(info, step=epoch+1)

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     **{f'test_{k}': v for k, v in test_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}

        if args.output_dir and utils.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")

            # for evaluation logs
            if coco_evaluator is not None:
                (output_dir / 'eval').mkdir(exist_ok=True)
                if "bbox" in coco_evaluator.coco_eval:
                    filenames = ['latest.pth']
                    if epoch % 50 == 0:
                        filenames.append(f'{epoch:03}.pth')
                    for name in filenames:
                        torch.save(coco_evaluator.coco_eval["bbox"].eval,
                                   output_dir / "eval" / name)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    main(args)
