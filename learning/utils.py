# Copyright 2023 Andrew Holliday
# 
# This file is part of the Transit Learning project.
#
# Transit Learning is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free 
# Software Foundation, either version 3 of the License, or (at your option) any 
# later version.
# 
# Transit Learning is distributed in the hope that it will be useful, but 
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or 
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more 
# details.
#
# You should have received a copy of the GNU General Public License along with 
# Transit Learning. If not, see <https://www.gnu.org/licenses/>.

from datetime import datetime
from pathlib import Path
import logging as log
import time
import random
import pickle

import torch
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
import numpy as np
from omegaconf import DictConfig
import matplotlib.pyplot as plt

from learning import models
from learning.initialization import init_from_cfg
from simulation.transit_time_estimator import NikolicCostModule, MyCostModule,\
    RouteGenBatchState
from simulation.drawing import draw_coalesced_routes, plot_routes_in_groups
from simulation.citygraph_dataset import STOP_KEY
from torch_utils import load_routes_tensor


PM_TERMSFIRST = "termsfirst"
PM_ROUTESFIRST = "routesfirst"
PM_SHORTESTPATH = "shortestpath"
PM_WSP = "weightedsp"
PM_BEAM = "beamsearch"
PM_COMB = "combiner"


STOP_FEAT_DIM = 8


def build_model_from_cfg(cfg):
    common_cfg = cfg.model.common

    backbone_gn = get_graphnet_from_cfg(cfg.model.backbone_gn, common_cfg)

    mean_stop_time_s = cfg.experiment.cost_function.kwargs.mean_stop_time_s
    gen_type = cfg.model.route_generator.type
    if gen_type == "PathCombiningRouteGenerator":
        gen_class = models.PathCombiningRouteGenerator
    elif gen_type == "RandomPathCombiningRouteGenerator":
        gen_class = models.RandomPathCombiningRouteGenerator
    elif gen_type == "UnbiasedPathCombiner":
        gen_class = models.UnbiasedPathCombiner
    elif gen_type == "NodeWalker":
        gen_class = models.NodeWalker

    low_mem_mode = cfg.experiment.get('low_memory_mode', False)

    model = gen_class(backbone_net=backbone_gn, 
                      mean_stop_time_s=mean_stop_time_s, 
                      symmetric_routes=cfg.experiment.symmetric_routes,
                      low_memory_mode=low_mem_mode, **cfg.model.common, 
                      **cfg.model.route_generator.kwargs)
                
    return model


def get_graphnet_from_cfg(net_cfg, common_cfg):
    kwargs = net_cfg.get('kwargs', {})
    net_type = net_cfg.net_type
    if net_type == 'none':
        return models.NoOpGraphNet(**kwargs)
    elif net_type == 'sgc':
        sgc_common_cfg = common_cfg.copy()
        del sgc_common_cfg.embed_dim
        return models.SimplifiedGcn(out_dim=common_cfg.embed_dim, 
                                    **sgc_common_cfg, **kwargs)
    elif net_type == 'graph conv':
        return models.Gcn(**common_cfg, **kwargs)
    elif net_type == 'graph attn':
        return models.GraphAttnNet(**common_cfg, **kwargs)
    elif net_type == 'edge graph':
        return models.EdgeGraphNet(**common_cfg, **kwargs)
    assert False, f'Unknown net type {net_type}'


def log_config(cfg, sumwriter, prefix=''):
    for key, val in cfg.items():
        if type(val) is DictConfig:
            log_config(val, sumwriter, prefix + key + '.')
        else:
            name = prefix + key
            sumwriter.add_text(name, str(val), 0)


def process_standard_experiment_cfg(cfg, run_name_prefix='', 
                                    weights_required=False):
    """Performs some standard setup using learning arguments.
    
    Returns:
    - a torch device object
    - a string name for the current run
    - a summary writer object for logging results from training
    - a model, if one is specified in the config; None otherwise
    - a cost function C(routes, graph_data)
    """
    exp_cfg = cfg.experiment
    if 'seed' in exp_cfg:
        # seed all random number generators
        seed = exp_cfg.seed
        log.debug(f"setting random seed to {seed}")
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)
        log.info(f"seed is {seed}")
    else:
        seed = None
        log.info("not seeding random number generators")

    # determine the device
    if exp_cfg.get('cpu', False):
        device = torch.device("cpu")
    else:
        device = torch.device("cuda")
    log.info(f"device is {device}")

    # determine the logging directory and run name
    run_name = cfg.get('run_name', 
                       datetime.now().strftime("%d_%m_%Y_%H:%M:%S"))
    run_name = run_name_prefix + run_name
    if 'logdir' in exp_cfg and exp_cfg.logdir is not None:
        log_path = Path(exp_cfg.logdir)
        # and set up summary writer
        summary_writer = SummaryWriter(log_path / run_name)
        summary_writer.add_text('device type', device.type, 0)
        summary_writer.add_text("random seed", str(seed), 0)
        log_config(cfg, summary_writer)

    else:
        summary_writer = None

    # set anomaly detection if requested
    torch.autograd.set_detect_anomaly(exp_cfg.get('anomaly', False))

    if 'model' in cfg:
        # setup the model
        model = build_model_from_cfg(cfg)
        if 'weights' in cfg.model:
            model.load_state_dict(torch.load(cfg.model.weights,
                                             map_location=device))
        elif weights_required and cfg.model.route_generator.type != \
                'RandomPathCombiningRouteGenerator':
            raise ValueError("model weights are required but not provided")
    else:
        model = None

    # setup the cost function
    low_mem_mode = exp_cfg.get('low_memory_mode', False)
    if exp_cfg.cost_function.type == 'nikolic':
        cost_obj = NikolicCostModule(low_memory_mode=low_mem_mode, 
                                     symmetric_routes=exp_cfg.symmetric_routes,
                                     **exp_cfg.cost_function.kwargs)
    elif exp_cfg.cost_function.type == 'mine':
        cost_obj = MyCostModule(low_memory_mode=low_mem_mode, 
                                symmetric_routes=exp_cfg.symmetric_routes,
                                min_route_len=cfg.eval.min_route_len,
                                max_route_len=cfg.eval.max_route_len,
                                **exp_cfg.cost_function.kwargs)

    # move torch objects to the device
    cost_obj.to(device)
    if model is not None:
        model.to(device)

    return device, run_name, summary_writer, cost_obj, model


def log_stops_per_route(batch_routes, sum_writer, ep_count, prefix=''):
    elem1 = batch_routes[0][0]
    no_batch = type(elem1) is int or \
        (type(elem1) is torch.Tensor and elem1.ndim == 0)
    if no_batch:
        # add a batch around this single network
        batch_routes = [batch_routes]
    route_lens = [[len(rr) for rr in routes] for routes in batch_routes]
    avg_stops_per_network = [torch.tensor(rls, dtype=float).mean() 
                              for rls in route_lens]
    avg_stops = torch.stack(avg_stops_per_network).mean()
    sum_writer.add_scalar(prefix + ' # stops per route', avg_stops, ep_count)


def rewards_to_returns(rewards, discount_rate=1):
    if discount_rate == 1:
        return rewards.flip([-1]).cumsum(-1).flip([-1])

    flipped_rewards = rewards.flip([-1])
    flipped_returns = torch.zeros(rewards.shape, device=rewards.device)
    return_t = torch.zeros(rewards.shape[:-1], device=rewards.device)
    for tt in range(flipped_rewards.shape[-1]):
        reward_t = flipped_rewards[..., tt]
        return_t = return_t * discount_rate + reward_t
        flipped_returns[..., tt] = return_t
    return flipped_returns.flip([-1])


@torch.no_grad()
def test_method(method_fn, dataloader, eval_cfg, init_cfg, cost_obj, 
                sum_writer=None, silent=False, return_routes=False, 
                device=None, iter_num=0, *method_args, **method_kwargs):
    log.debug(f"evaluating {method_fn.__name__} on dataset")
    cost_histories = []
    final_costs = []
    all_metrics = None

    for data in tqdm(dataloader, disable=silent):
        if device is not None and device.type != 'cpu':
            data = data.cuda()
        
        start_time = time.time()

        fixed_routes = None
        dataset = eval_cfg.get('dataset', None)
        if dataset is not None:
            fixed_routes_file = dataset.get('fixed_routes', None)
            if fixed_routes_file is not None:
                fixed_routes_path = Path(dataset.path) / fixed_routes_file
                # load the fixed routes from the file
                fixed_routes = load_routes_tensor(fixed_routes_path, device)

        state = RouteGenBatchState(data, cost_obj, eval_cfg.n_routes, 
                                   eval_cfg.min_route_len, 
                                   eval_cfg.max_route_len, 
                                   fixed_routes=fixed_routes)
        
        init_network = init_from_cfg(state, init_cfg)
        assert init_network is None or init_network.shape[1] == eval_cfg.n_routes, \
            "initial solution has wrong number of routes "\
            f"{init_network.shape[1]}, should be {eval_cfg.n_routes}"

        if method_fn is not None:
            state, cost_history = method_fn(state, cost_obj, silent=silent,
                                            init_network=init_network,
                                            sum_writer=sum_writer, *method_args, 
                                            **method_kwargs)
        else:
            # just evaluate the initial solution
            state.add_new_routes(init_network)
            cost_history = None
        if eval_cfg.get('draw', False):
            for city, city_routes in zip(data.to_data_list(), state.routes):
                # first, draw city and coalesced routes side-by-side
                fig, axes = plt.subplots(1, 2)
                city.draw(axes[0])
                axes[0].set_title('city')
                draw_coalesced_routes(city[STOP_KEY].pos, city_routes, axes[1])
                axes[1].set_title('route graph')

                # then, draw the routes in subfigures
                plot_routes_in_groups(city[STOP_KEY].pos, city_routes)

                plt.tight_layout()
                plt.show()

        duration = time.time() - start_time
        if cost_history is not None:
            n_iters = cost_history.shape[1]
            cost_histories.append(cost_history)
            final_costs.append(cost_history[:, -1])
        else:
            final_costs.append(cost_obj(state).cost)
            n_iters = 0

        # compute the metrics for this iteration
        result = cost_obj(state)
        metrics = result.get_metrics()
        metrics['average wall-clock duration'] = \
            torch.full_like(metrics['ATT'], duration)
        metrics['average # iterations'] = \
            torch.full_like(metrics['ATT'], n_iters)
        # metrics['demand weight'] = cost_obj.demand_time_weight
        # metrics['route weight'] = cost_obj.route_time_weight
        # metrics['unserved weight'] = cost_obj.unserved_weight

        # add this iteration's metrics to the collection of metrics        
        if all_metrics is None:
            all_metrics = metrics
        else:
            for key, val in metrics.items():
                all_metrics[key] = torch.cat((all_metrics[key], val))

    # compute some aggregate statistics
    final_costs = torch.cat(final_costs)
    mean_metrics = {key: val.mean() for key, val in all_metrics.items()}
    if sum_writer is not None:
        # log the aggregate statistics to tensorboard
        sum_writer.add_scalar("val cost", final_costs.mean(), iter_num)
        for name, stat_value in mean_metrics.items():
            sum_writer.add_scalar(name, stat_value, iter_num)

    if not silent:
        if eval_cfg.csv:
            # print statistics in csv format
            parts = [eval_cfg.n_routes]
            for stat in metrics.values():
                parts.append(stat.mean().item())
                if stat.numel() > 1: 
                    parts.append(stat.std().item())
            csv_row = ',' + ','.join([f"{pp:.3f}" for pp in parts])
            print(csv_row)
        else:
            # print overall statistics normally
            print(f"average cost: {final_costs.mean():.3f}")
            for name, stat_value in mean_metrics.items():
                print(f"{name}: {stat_value:.3f}")

    out_stats = (final_costs.mean(), final_costs.std(), metrics)
    if return_routes:
        return out_stats + (state.routes,)
    else:
        return out_stats
