import inspect
import os
import os.path as osp
import sys
import time
import numpy as np
import warnings
import argparse

import mmcv
import torch
import torch.distributed as dist
from mmcv.runner import get_dist_info, init_dist
from mmcv.utils import Config, DictAction
from mmseg import __version__
from mmseg.apis import init_random_seed, set_random_seed
from mmseg.models import build_segmentor
from mmseg.utils import (collect_env, get_device, get_root_logger,
                         setup_multi_processes)


def uniform_soup(model, path, device = "cpu", by_name = False):
    if not isinstance(path, list):
        path = [path]
    model = model.to(device)
    model_dict = model.state_dict()
    soups = {key:[] for key in model_dict}
    for i, model_path in enumerate(path):
        weight = torch.load(model_path, map_location = device)
        weight_dict = weight.state_dict() if hasattr(weight, "state_dict") else weight
        if by_name:
            weight_dict = {k:v for k, v in weight_dict.items() if k in model_dict}
        for k, v in weight_dict.items():
            soups[k].append(v)
    if 0 < len(soups):
        soups = {k:(torch.sum(torch.stack(v), axis = 0) / len(v)).type(v[0].dtype) for k, v in soups.items() if len(v) != 0}
        model_dict.update(soups)
        model.load_state_dict(model_dict)
    return model

def greedy_soup(model, path, data, metric, device = "cpu", update_greedy = False, compare = np.greater_equal, by_name = False, digits = 4, verbose = True, y_true = "y_true"):
    if not isinstance(path, list):
        path = [path]
    score, soup = None, []
    model = model.to(device)
    model.eval()
    model_dict = model.state_dict()
    input_key = [key for key in inspect.getfullargspec(model.forward).args if key !=  "self"]
    input_cnt = len(input_key)
    for i, model_path in enumerate(path):
        if update_greedy:
            weight = torch.load(model_path, map_location = device)
            weight_dict = weight.state_dict() if hasattr(weight, "state_dict") else weight
            if by_name:
                weight_dict = {k:v for k, v in weight_dict.items() if k in model_dict}
            if len(soup) != 0:
                weight_dict = {key:(torch.sum(torch.stack([weight_dict[key], soup[key]]), axis = 0) / 2).type(weight_dict[key].dtype)  for key in model_dict.keys()}
            model_dict.update(weight_dict)
            model.load_state_dict(model_dict)
        else:
            model = uniform_soup(model, soup + [model_path], device = device, by_name = by_name)
                
        iterator = iter(data)
        history = []
        step = 0
        start_time = time.time()
        while True:
            try:
                text = ""
                iter_data = next(iterator)
                if not isinstance(iter_data, dict):
                    x = iter_data[:input_cnt]
                    y = list(iter_data[input_cnt:])
                    y = [d.to(device) if isinstance(d, torch.Tensor) else d for d in y]
                    d_cnt = len(y[0])
                else:
                    x = [iter_data[k] for k in input_key if k in iter_data]
                x = [d.to(device) if isinstance(d, torch.Tensor) else d for d in x]
                step += 1
                #del x

                with torch.no_grad():
                    logits = model(*x)
                    if isinstance(logits, torch.Tensor):
                        logits = [logits]
                        
                    if isinstance(iter_data, dict):
                        metric_key = [key for key in inspect.getfullargspec(func).args if key !=  "self"]
                        if len(metric_key) == 0:
                            metric_key = [y_true]
                        y = [iter_data[k] for k in metric_key if k in iter_data]
                        y = [d.to(device) if isinstance(d, torch.Tensor) else d for d in y]
                        d_cnt = len(y[0])
                    metric_val = np.array(metric(*(y + logits)))
                    if np.ndim(metric_val) == 0:
                        metric_val = [float(metric_val)] * d_cnt
                    history += list(metric_val)
                    #del y, logits

                if verbose:
                    sys.stdout.write("\r[{name}] step: {step} - time: {time:.2f}s - {key}: {val:.{digits}f}".format(name = os.path.basename(model_path), step = step, time = (time.time() - start_time), key = metric.__name__ if hasattr(metric, "__name__") else str(metric), val = np.nanmean(history), digits = digits))
                    sys.stdout.flush()
            except StopIteration:
                print("")
                #gc.collect()
                break
        if 0 < len(history) and (score is None or compare(np.nanmean(history), score)):
            score = np.nanmean(history)
            if update_greedy:
                soup = weight_dict
            else:
                soup += [model_path]
    if len(soup) != 0:
        if update_greedy:
            model_dict.update(soup)
            model.load_state_dict(model_dict)
        else:
            model = uniform_soup(model, soup, device = device, by_name = by_name)
        if verbose:
            print("greedy soup best score : {val:.{digits}f}".format(val = score, digits = digits))
    return model

def parse_args():
    parser = argparse.ArgumentParser(description='Train a segmentor')
    parser.add_argument('config', help='train config file path')
    parser.add_argument('checkpoints_path', help='model checkpoints file path')
    parser.add_argument('--work-dir', help='the dir to save logs and models')
    parser.add_argument(
        '--load-from', help='the checkpoint file to load weights from')
    parser.add_argument(
        '--resume-from', help='the checkpoint file to resume from')
    parser.add_argument(
        '--no-validate',
        action='store_true',
        help='whether not to evaluate the checkpoint during training')
    group_gpus = parser.add_mutually_exclusive_group()
    group_gpus.add_argument(
        '--gpus',
        type=int,
        help='(Deprecated, please use --gpu-id) number of gpus to use '
        '(only applicable to non-distributed training)')
    group_gpus.add_argument(
        '--gpu-ids',
        type=int,
        nargs='+',
        help='(Deprecated, please use --gpu-id) ids of gpus to use '
        '(only applicable to non-distributed training)')
    group_gpus.add_argument(
        '--gpu-id',
        type=int,
        default=0,
        help='id of gpu to use '
        '(only applicable to non-distributed training)')
    parser.add_argument('--seed', type=int, default=145, help='random seed')
    parser.add_argument(
        '--diff_seed',
        action='store_true',
        help='Whether or not set different seeds for different ranks')
    parser.add_argument(
        '--deterministic',
        type=bool,
        default=True,
        help='whether to set deterministic options for CUDNN backend.')
    parser.add_argument(
        '--options',
        nargs='+',
        action=DictAction,
        help="--options is deprecated in favor of --cfg_options' and it will "
        'not be supported in version v0.22.0. Override some settings in the '
        'used config, the key-value pair in xxx=yyy format will be merged '
        'into config file. If the value to be overwritten is a list, it '
        'should be like key="[a,b]" or key=a,b It also allows nested '
        'list/tuple values, e.g. key="[(a,b),(c,d)]" Note that the quotation '
        'marks are necessary and that no white space is allowed.')
    parser.add_argument(
        '--cfg-options',
        nargs='+',
        action=DictAction,
        help='override some settings in the used config, the key-value pair '
        'in xxx=yyy format will be merged into config file. If the value to '
        'be overwritten is a list, it should be like key="[a,b]" or key=a,b '
        'It also allows nested list/tuple values, e.g. key="[(a,b),(c,d)]" '
        'Note that the quotation marks are necessary and that no white space '
        'is allowed.')
    parser.add_argument(
        '--launcher',
        choices=['none', 'pytorch', 'slurm', 'mpi'],
        default='none',
        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    parser.add_argument(
        '--auto-resume',
        action='store_true',
        help='resume from the latest checkpoint automatically.')
    
        
    args = parser.parse_args()
    if 'LOCAL_RANK' not in os.environ:
        os.environ['LOCAL_RANK'] = str(args.local_rank)

    if args.options and args.cfg_options:
        raise ValueError(
            '--options and --cfg-options cannot be both '
            'specified, --options is deprecated in favor of --cfg-options. '
            '--options will not be supported in version v0.22.0.')
    if args.options:
        warnings.warn('--options is deprecated in favor of --cfg-options. '
                      '--options will not be supported in version v0.22.0.')
        args.cfg_options = args.options

    return args

def main():
    args = parse_args()

    cfg = Config.fromfile(args.config)
    if args.cfg_options is not None:
        cfg.merge_from_dict(args.cfg_options)

    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = False

    # work_dir is determined in this priority: CLI > segment in file > filename
    if args.work_dir is not None:
        # update configs according to CLI args if args.work_dir is not None
        cfg.work_dir = args.work_dir
    elif cfg.get('work_dir', None) is None:
        # use config filename as default work_dir if cfg.work_dir is None
        cfg.work_dir = osp.join('./work_dirs',
                                osp.splitext(osp.basename(args.config))[0])
    if args.load_from is not None:
        cfg.load_from = args.load_from
    if args.resume_from is not None:
        cfg.resume_from = args.resume_from
    if args.gpus is not None:
        cfg.gpu_ids = range(1)
        warnings.warn('`--gpus` is deprecated because we only support '
                      'single GPU mode in non-distributed training. '
                      'Use `gpus=1` now.')
    if args.gpu_ids is not None:
        cfg.gpu_ids = args.gpu_ids[0:1]
        warnings.warn('`--gpu-ids` is deprecated, please use `--gpu-id`. '
                      'Because we only support single GPU mode in '
                      'non-distributed training. Use the first GPU '
                      'in `gpu_ids` now.')
    if args.gpus is None and args.gpu_ids is None:
        cfg.gpu_ids = [args.gpu_id]

    cfg.auto_resume = args.auto_resume

    # init distributed env first, since logger depends on the dist info.
    if args.launcher == 'none':
        distributed = False
    else:
        distributed = True
        init_dist(args.launcher, **cfg.dist_params)
        # gpu_ids is used to calculate iter when resuming checkpoint
        _, world_size = get_dist_info()
        cfg.gpu_ids = range(world_size)

    # create work_dir
    mmcv.mkdir_or_exist(osp.abspath(cfg.work_dir))
    # dump config
    cfg.dump(osp.join(cfg.work_dir, osp.basename(args.config)))
    # init the logger before other steps
    timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
    log_file = osp.join(cfg.work_dir, f'{timestamp}.log')
    logger = get_root_logger(log_file=log_file, log_level=cfg.log_level)

    # set multi-process settings
    setup_multi_processes(cfg)

    
    # Logging 해주는 부분
    
    # init the meta dict to record some important information such as    
    # environment info and seed, which will be logged
    meta = dict()
    # log env info
    env_info_dict = collect_env()
    env_info = '\n'.join([f'{k}: {v}' for k, v in env_info_dict.items()])
    dash_line = '-' * 60 + '\n'
    logger.info('Environment info:\n' + dash_line + env_info + '\n' +
                dash_line)
    meta['env_info'] = env_info

    # log some basic info
    logger.info(f'Distributed training: {distributed}')
    logger.info(f'Config:\n{cfg.pretty_text}')

    # set random seeds
    cfg.device = get_device()
    seed = init_random_seed(args.seed, device=cfg.device)
    seed = seed + dist.get_rank() if args.diff_seed else seed
    logger.info(f'Set random seed to {seed}, '
                f'deterministic: {args.deterministic}')
    set_random_seed(seed, deterministic=args.deterministic)
    cfg.seed = seed
    meta['seed'] = seed
    meta['exp_name'] = osp.basename(args.config) # config_file의 이름을 exp_name으로
    
    model = build_segmentor(
        cfg.model,
        train_cfg=cfg.get('train_cfg'),
        test_cfg=cfg.get('test_cfg'))
    model.init_weights()

    model = uniform_soup(model, args.checkpoints_path, device = "cpu", by_name = False)
    torch.save(model, args.checkpoints_path+"uniform_soup_result.pth")
