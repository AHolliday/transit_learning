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

import logging as log
import time
from pathlib import Path
import pickle

from tqdm import tqdm
import torch
from torch_geometric.loader import DataLoader
from omegaconf import DictConfig, OmegaConf
import hydra

from torch_utils import reconstruct_all_paths, floyd_warshall, \
    get_route_edge_matrix, get_batch_tensor_from_routes, \
    get_route_leg_times, get_unselected_routes
# from learning.bee_colony import build_init_scenario
from simulation.citygraph_dataset import get_dataset_from_config
import learning.utils as lrnu


"""
Implementation of the method described in the paper "Solving urban transit 
route design problem using selection hyper-heuristics" by Ahmed et al. (2019).
"""


# # CPU is always gonna be faster with this.
# DEVICE = torch.device("cpu")


class HeuristicSequence:
    def __init__(self, init_size, batch_size=1, device=None, exp_size=10):
        self._seq = torch.full((batch_size, init_size), -1, dtype=int,
                               device=device)
        self._seq_idxs = torch.zeros(batch_size, dtype=int, device=device)
        self.batch_idxs = torch.arange(batch_size, device=device)
        self.exp_size = exp_size

    def add_heuristic(self, heuristic_idxs):
        if self._seq_idxs.max() == self._seq.shape[1]:
            # we need more memory to store the sequence, so add some
            new_cols = torch.full(
                (self._seq.shape[0], self.exp_size), -1, dtype=int,
                device=self._seq.device)
            self._seq = torch.cat((self._seq, new_cols), dim=1)

        self._seq[self.batch_idxs, self._seq_idxs] = heuristic_idxs
        self._seq_idxs += 1
    
    def get_sequences(self, mask=None):
        if mask is None:
            return self._seq
        return self._seq[mask]

    def reset_seqs(self, reset_mask):
        self._seq[reset_mask] = -1
        self._seq_idxs[reset_mask] = 0


def hyperheuristic(state, cost_obj, f_0, duration_s=None, n_steps=None, 
                   init_scenario=None, model=None, silent=False,
                   sum_writer=None):
    """
    graph_data -- A CityGraphData object containing the graph data.
    n_routes -- The number of routes in each candidate scenario, called NBL in
        the paper.
    cost_fn -- A function that determines the cost (badness) of a scenario.  In
        the paper, this is wieghted total travel time.
    """
    assert duration_s is not None or n_steps is not None
    heuristics = [
        heuristic_0,
        heuristic_1,
        heuristic_2,
        heuristic_3,
        heuristic_4,
        heuristic_5,
        heuristic_6,
        # heuristic_bee1,
        # heuristic_bee2,
        # heuristic_bee3,
    ]
    if model is not None:
        heuristics.append(lambda *args, **kwargs: neural_heuristic(*args, 
                                                                   state=state,
                                                                   model=model,
                                                                   **kwargs))
    n_hrstcs = len(heuristics)

    # add the special no-op heuristic, which isn't part of n_hrstcs
    heuristics.append(noop_heuristic)

    dev = state.device
    batch_size = state.batch_size

    # create the initial scenario
    shortest_paths, _ = reconstruct_all_paths(state.nexts.cpu())
    max_n_nodes = state.max_n_nodes
    demand = torch.zeros((batch_size, max_n_nodes+1, max_n_nodes+1), 
                         device=dev)
    demand[:, :-1, :-1] = state.demand
    no_given_init = init_scenario is None
    n_routes = state.n_routes_to_plan
    min_route_len = state.min_route_len
    max_route_len = state.max_route_len
    if not no_given_init:
        tmp = torch.full((batch_size, n_routes, max_n_nodes), -1)
        tmp[:, :, :init_scenario.shape[-1]] = init_scenario
        init_scenario = tmp

    init_scen_path = Path('hh_init_scenario.pkl')
    if init_scen_path.exists():
        log.info('Loading initial scenario from file')
        with init_scen_path.open('rb') as ff:
            scenario = pickle.load(ff)
    else:
        scenario = build_init_scenario(shortest_paths, state,
                                       init_scenario=init_scenario)     
        scenario = scenario.cpu()
        with init_scen_path.open('wb') as ff:
            pickle.dump(scenario, ff)

    best_scenario = scenario.clone()
    sp_lens = (shortest_paths > -1).sum(dim=-1).cpu()

    def cost_fn_wrapper(scenario, step):
        # we use CPU in the algorithm because it's faster, but the cost fn
         # is faster on GPU.
        if dev.type != 'cpu':
            state.replace_routes(scenario.to(dev))
            result = cost_obj(state, no_norm=True)
            cost = result.cost.cpu()
        else:
            state.replace_routes(scenario)
            result = cost_obj(state, no_norm=True)
            cost = result.cost
        
        nv = count_violations(scenario, max_n_nodes, n_routes, sp_lens, 
                              state.demand[0], min_route_len, max_route_len, 
                              state.symmetric_routes)
        cost[nv > 0] = float('inf')
        # cost = cost + nv * cost
        sum_writer.add_scalar('mean violations', nv.float().mean(), step)
        return cost, result.get_metrics_tensor()

    best_cost, best_metrics = cost_fn_wrapper(scenario, 0)
    print('starting metrics:', best_metrics)

    # multiply by 2 to allow more exploration at first
    delta_F = max(best_cost - f_0, 0) # * 2
    cost_history = [best_cost]

    metric_names = cost_obj.get_metric_names()

    # initialize variables
    heuristics_sequence = HeuristicSequence(max_n_nodes, batch_size=batch_size)
    curr_idxs = torch.randint(n_hrstcs, (batch_size,))
    heuristics_sequence.add_heuristic(curr_idxs)
    trans_mat = 1 - torch.eye(n_hrstcs + 1)
    trans_mat[-1, :-1] = 0
    trans_mat[:-1, -1] = 0
    trans_mat[-1, -1] = 1
    trans_mat = trans_mat.repeat(batch_size, 1, 1)
    seq_mat = torch.ones((batch_size, n_hrstcs + 1, 2), dtype=torch.long)
    # set the row corresponding to the noop heuristic to always end
    seq_mat[:, -1, 0] = 0

    # for step in tqdm(range(n_steps)):
    if duration_s is not None:
        time_elapsed_s = 0
        start_time_s = time.process_time()
        condition = lambda: time_elapsed_s < duration_s
        pbar = tqdm(total=duration_s, unit='process s', disable=silent)
    else:
        pbar = tqdm(total=n_steps, disable=silent)
        step = 0
        condition = lambda: step < n_steps

    while condition():
        # select a heuristic
        next_idxs = select_next(trans_mat, curr_idxs)
        heuristics_sequence.add_heuristic(next_idxs)
        is_seq_done = is_sequence_done(seq_mat, next_idxs)
        if is_seq_done.any():
            # apply the sequence of heuristics
            done_scens = scenario[is_seq_done]
            done_heurseqs = heuristics_sequence.get_sequences(is_seq_done)
            new_scenario = apply(done_scens, done_heurseqs, heuristics,
                                 state, shortest_paths[0])
            step_scenario = scenario.clone()
            step_scenario[is_seq_done] = new_scenario
            # we're just recomputing it for the ones that aren't done...
            new_cost, new_metrics = cost_fn_wrapper(step_scenario, step)

            # update the best scenarios that have improved over the old best
            is_improved = new_cost < best_cost
            best_cost[is_improved] = new_cost[is_improved]
            best_metrics[is_improved] = new_metrics[is_improved]
            best_scenario[is_improved] = step_scenario[is_improved]

            # update the matrices
            imp_hrstc_seq = heuristics_sequence.get_sequences(is_improved)
            imp_trans_mat = trans_mat[is_improved] 
            imp_seq_mat = seq_mat[is_improved]
            update_matrices(imp_hrstc_seq, imp_trans_mat, imp_seq_mat)
            trans_mat[is_improved] = imp_trans_mat
            seq_mat[is_improved] = imp_seq_mat

            # update the candidate scenario by the great deluge strategy
            if duration_s is not None:
                accepted = accept(new_cost, time_elapsed_s, duration_s, f_0, 
                                  delta_F)
            else:
                accepted = accept(new_cost, step, n_steps, f_0, delta_F)
            
            if accepted.any():
                print('accepted!')
            
            accepted[is_improved] = True
            accepted = accepted[:, None, None]
            scenario = step_scenario * accepted + scenario * ~accepted

            # reset to prepare for the next sequence
            heuristics_sequence.reset_seqs(is_seq_done)
            
        assert (next_idxs < n_hrstcs).all()
        curr_idxs = next_idxs
        cost_history.append(best_cost.clone())
        if sum_writer is not None:
            # log the various metrics
            for name, vals in zip(metric_names, best_metrics.unbind(-1)):
                sum_writer.add_scalar(f'mean {name}', vals.mean(), step)

            sum_writer.add_scalar('mean cost', best_cost.mean(), step)

        if duration_s is not None:
            new_time_elapsed_s = time.process_time() - start_time_s
            delta_t = new_time_elapsed_s - time_elapsed_s
            pbar.update(delta_t)
            time_elapsed_s = new_time_elapsed_s
        else:
            step += 1
            pbar.update(1)

    pbar.close()
    state.replace_routes(best_scenario.to(dev))
    return state, torch.stack(cost_history, dim=1).to(dev)


def build_init_scenario(shortest_paths, state, init_scenario=None):
    batch_size = shortest_paths.shape[0]
    scenarios = []
    for bi in range(batch_size):
        if init_scenario is not None:
            bis = init_scenario[bi]
        else:
            bis = None
        scenario = \
            _build_init_scenario_helper(shortest_paths[bi], state, 
                                        init_scenario=bis)
        scenarios.append(scenario)
    return torch.stack(scenarios, dim=0)


def _build_init_scenario_helper(shortest_paths, state, init_scenario=None):
    n_nodes = shortest_paths.shape[0]
    n_nodes = state.max_n_nodes
    min_stops = state.min_route_len[0]
    max_stops = state.max_route_len[0]
    symmetric_routes = state.symmetric_routes
    shortest_paths = shortest_paths.to(state.device)
    square_pathlens = (shortest_paths > -1).sum(dim=-1)[None]
    shortest_paths = shortest_paths.flatten(0, 1)
    path_lens = square_pathlens.flatten(0, 2)
    n_routes = state.n_routes_to_plan
    
    is_valid_len = path_lens >= min_stops
    if max_stops is not None:
        is_valid_len &= path_lens <= max_stops
    
    if init_scenario is not None:
        # use the provided initial scenario
        scenario = init_scenario.to(state.device)
        nv = count_violations(scenario, n_nodes, n_routes, square_pathlens, 
                              state.demand[0], min_stops, max_stops, 
                              symmetric_routes)[0]
    else:
        # assemble an initial scenario
        shortest_paths = shortest_paths[is_valid_len]
        path_lens = path_lens[is_valid_len]
        already_used = torch.zeros_like(path_lens, dtype=torch.bool)
        scenario = torch.full((n_routes, shortest_paths.shape[-1]), -1, 
                              device=shortest_paths.device)
                                
        nv = count_violations(scenario, n_nodes, n_routes, square_pathlens, 
                              state.demand[0], min_stops, max_stops,
                              symmetric_routes)[0]
        log.info(f'Building initial scenario')
        for route_idx in tqdm(range(n_routes)):
            # try possible routes
            new_nv_chunks = []
            for path_chunk in torch.split(shortest_paths, 100):
                scen_chunk = scenario[None].repeat(len(path_chunk), 1, 1)
                scen_chunk[:, route_idx, :path_chunk.shape[-1]] = path_chunk
                exp_lens = square_pathlens.repeat(len(path_chunk), 1, 1)
                new_nvs = count_violations(scen_chunk, n_nodes, n_routes, 
                                           exp_lens, state.demand[0], 
                                           min_stops, max_stops, 
                                           symmetric_routes)
                new_nv_chunks.append(new_nvs)
                
            new_nvs = torch.cat(new_nv_chunks)

            # don't pick ones that are already picked
            new_nvs[already_used] = nv + 1

            # pick the one with the fewest violations
            nv, best_path_idx = new_nvs.min(dim=0)
            choice = shortest_paths[best_path_idx]
            scenario[route_idx] = -1
            scenario[route_idx, :choice.shape[-1]] = choice
            already_used[best_path_idx] = True
        
        tmp = torch.full((n_routes, n_nodes), -1, device=scenario.device)
        tmp[:, :scenario.shape[-1]] = scenario
        scenario = tmp

    # repair procedure
    heuristics = [
        heuristic_0,
        heuristic_1,
        heuristic_2,
        heuristic_3,
        heuristic_4,
        heuristic_5,
        heuristic_6,
    ]

    log.info('Repairing initial scenarios')
    ii = 0
    while nv > 0:
        # simple random, improve or equal
        # select a hyperheuristic randomly
        heur_idx = torch.randint(len(heuristics), (1,))
        heuristic = heuristics[heur_idx]
        # apply it
        modified_scen = heuristic(scenario.clone(), state)
        new_nv = count_violations(modified_scen[None], n_nodes, n_routes, 
                                  square_pathlens, state.demand[0], min_stops, 
                                  max_stops, symmetric_routes)
        if new_nv <= nv:
            # accept the change if it doesn't make things worse
            scenario = modified_scen
            nv = new_nv
        ii += 1
        if ii % 1000 == 0:
            log.info(f'Iteration {ii}, violations: {nv.item()}')
                    
    return scenario.cpu()


def count_violations(scenario, n_nodes, required_n_routes, sp_lens,
                     demand, min_stops=2, max_stops=None, 
                     symmetric_routes=True):
    if scenario.ndim == 2:
        batch_size = 1
        scenario = scenario[None]
    else:
        batch_size = scenario.shape[0]
    # count uncovered nodes
    is_covered = torch.zeros((batch_size, n_nodes + 1), dtype=torch.bool, 
                             device=scenario.device)
    batch_idxs = torch.arange(batch_size, device=scenario.device)
    for scen_route in scenario.transpose(0, 1):
        is_covered[batch_idxs[:, None], scen_route] = True
    # is_covered[batch_idxs, scenario] = True
    n_uncovered = n_nodes - is_covered[:, :-1].sum(dim=-1)
    assert (n_uncovered >= 0).all()

    # count duplicate stops in routes
    n_duplicates = torch.zeros((batch_size,), dtype=torch.long, 
                               device=scenario.device)
    for bi, batch_elem in enumerate(scenario):
        for route in batch_elem:
            valid_route = route[route > -1]
            n_unique = valid_route.unique().shape[0]
            n_stops = valid_route.shape[0]
            n_duplicates[bi] += n_stops - n_unique

    n_violations = n_uncovered + n_duplicates
    assert (n_duplicates >= 0).all()

    # count un-connected vertices.  If more than 2 transfers, doesn't count.
    dummy_dtm = torch.ones((batch_size, n_nodes + 1, n_nodes + 1),
                           device=scenario.device)
    route_matrix = get_route_edge_matrix(scenario, dummy_dtm, 0,
                                         symmetric_routes=symmetric_routes)

    # direct_link = route_matrix.isfinite().float()
    # upto1transfer = direct_link.bmm(direct_link)
    # upto2transfers = upto1transfer.bmm(direct_link)
    # unconnected = ~(upto2transfers.bool())
    # n_unconnected_pairs = unconnected.sum(dim=(-1, -2))

    # run floyd-warshall
    route_matrix = route_matrix[:, :-1, :-1]
    _, dists = floyd_warshall(route_matrix)

    # for each pair of nodes with no path, add one
    unconnected = dists.isinf() & (demand > 0)
    n_unconnected_pairs = unconnected.sum(dim=(-1, -2))
    if symmetric_routes:
        # divide by 2 because we double-counted
        n_unconnected_pairs = n_unconnected_pairs // 2
    n_violations += n_unconnected_pairs
    assert (n_unconnected_pairs >= 0).all()

    # count out-of-bounds stops
    route_lens = (scenario != -1).sum(dim=-1)
    zero = torch.zeros(1, dtype=int, device=scenario.device)
    n_route_stops_under = (min_stops - route_lens).maximum(zero)
    if max_stops is None:
        max_stops = route_lens
    n_route_stops_over = (route_lens - max_stops).maximum(zero)
    n_route_stops_oob = n_route_stops_under + n_route_stops_over
    # don't count non-existent "dummy" routes as violations
    n_route_stops_oob[route_lens == 0] = 0
    n_stops_oob = n_route_stops_oob.sum(-1)
    n_violations += n_stops_oob
    assert (n_stops_oob >= 0).all()

    # count number of routes
    n_routes = (route_lens > 0).sum(dim=-1)
    n_routes_delta = (n_routes - required_n_routes).abs()
    n_violations += n_routes_delta
    assert (n_routes_delta >= 0).all()

    # count how many edges routes are away from being valid.  This isn't 
     # explicit in the paper but based on the results, and then on 
     # correspondence with one of the authors, it is what they do.
    path_n_skips = sp_lens - 2
    path_n_skips.clamp_(min=0)
    leg_n_skips = get_route_leg_times(scenario, path_n_skips)
    total_n_skips = leg_n_skips.sum(dim=(1, 2))
    assert (total_n_skips >= 0).all()
    n_violations += total_n_skips

    return n_violations


def select_next(trans_mat, curr_idxs):
    """
    trans_mat: (batch_size, n_hrstcs, n_hrstcs)
    curr_idxs: (batch_size,)
    """
    n_heuristics = trans_mat.shape[-1]
    gather_idxs = curr_idxs.clone()
    gather_idxs[gather_idxs == -1] = trans_mat.shape[-1] - 1
    gather_idxs = gather_idxs[..., None, None].expand(-1, -1, n_heuristics)
    # batch_size x n_hrstcs
    next_scores = trans_mat.gather(1, gather_idxs).squeeze(1)
    next_probs = next_scores / next_scores.sum(dim=-1, keepdim=True)
    next_idxs = next_probs.multinomial(1).squeeze(-1)

    assert (next_idxs < 7).all()

    return next_idxs


def is_sequence_done(seq_mat, next_idxs):
    gather_idxs = next_idxs.clone()
    gather_idxs[gather_idxs == -1] = seq_mat.shape[-1] - 1
    gather_idxs = gather_idxs[..., None, None].expand(-1, -1, 2)
    act_scores = seq_mat.gather(1, gather_idxs).squeeze(1)
    done_probs = act_scores[:, 1] / act_scores.sum(dim=-1)
    return done_probs > torch.rand_like(done_probs)


def apply(scenario, hrstc_idx_seq, heuristics, state, shortest_paths):
    """Apply a sequence of heuristics to a scenario.

    sequence -- A list of indices into the heuristics list.
    scenario -- A tensor of routes.
    heuristics -- A list of heuristic functions.
    """
    scenario = scenario.clone()
    for idxs in hrstc_idx_seq.T:
        for bi, hi in enumerate(idxs):
            post_scen = heuristics[hi](scenario[bi], state=state, 
                                       shortest_paths=shortest_paths)
            scenario[bi] = post_scen

    return scenario


def update_matrices(hrstc_idx_seq, trans_mat, seq_mat):
    # batch_size x seq_len
    batch_idxs = torch.arange(trans_mat.shape[0], device=trans_mat.device)
    # update the transition matrix
    # first heuristic is never a no-op
    curr_is_noop = torch.zeros_like(batch_idxs, dtype=torch.bool)
    for ii, step_idxs in enumerate(hrstc_idx_seq.T[:-1]):
        # update the transition matrix
        next_idxs = hrstc_idx_seq[:, ii + 1]
        trans_mat[batch_idxs, step_idxs, next_idxs] += 1

        # update the sequence matrix
        next_is_noop = next_idxs == -1
        is_last = next_is_noop & ~curr_is_noop
        seq_mat[batch_idxs[is_last], step_idxs[is_last], 1] += 1
        seq_mat[batch_idxs[~is_last], step_idxs[~is_last], 0] += 1

        curr_is_noop = next_is_noop
    
    # handle the final step
    last_step_idxs = hrstc_idx_seq[..., -1]
    # the last step must update the 'end' probability
    seq_mat[batch_idxs[~curr_is_noop], last_step_idxs[~curr_is_noop], 1] += 1

    # force no-op row to always pick end
    seq_mat[:, -1, 0] = 0
    seq_mat[:, -1, 1] = 1
    # force no transitions to "end" state
    trans_mat[:, :-1, -1] = 0
    trans_mat[:, -1, :-1] = 0


def accept(new_cost, progress, duration, f_0, delta_F):
    """Great Deluge acceptance strategy."""
    level = f_0 + delta_F * (1 - progress / duration)
    return new_cost <= level


def neural_heuristic(scenario, state, model, *args, **kwargs):
    """Use a neural network to plan a new route."""
    # drop a random route
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    remaining_scenario = get_unselected_routes(scenario[None], route_idx)
    state.replace_routes(remaining_scenario)
    result = model.step(state, greedy=False)
    return result.routes_tensor[0]


def heuristic_0(scenario, state, **kwargs):
    """select a random route and add a random new node at a random position"""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"

    # select a route that isn't already full
    route_lens = (scenario != -1).sum(dim=-1)

    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    route_len = route_lens[route_idx][0]
    route = scenario[route_idx][0]

    n_nodes_available = state.max_n_nodes - route_len
    if n_nodes_available == 0:
        # route is already full, so just return the original scenario
        return scenario
    new_node_subidx = torch.randint(n_nodes_available, (1,),
                                    device=scenario.device)
    unused_nodes = list(set(range(state.max_n_nodes)) - set(route.tolist()))
    new_node = unused_nodes[new_node_subidx]

    new_pos_floats = torch.rand((1,), device=scenario.device)
    new_pos = ((route_len + 1) * new_pos_floats).to(int)

    # insert nodes at the new positions
    inplace_insert_node_at(route, new_pos, new_node)

    scenario[route_idx] = route
    check_for_inner_negs(route)
    return scenario


def heuristic_1(scenario, *args, **kwargs):
    """delete a random node from a random route"""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"

    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    route_lens = (scenario != -1).sum(dim=-1)
    route_len = route_lens[route_idx][0]
    if route_len == 0:
        # can't delete, just return the existing scenario
        return scenario
    target_node = torch.randint(route_len, (1,), device=scenario.device)
    route = scenario[route_idx][0]
    inplace_remove_stop_at(route, target_node)

    scenario[route_idx] = route
    check_for_inner_negs(route)
    return scenario


def heuristic_2(scenario, *args, **kwargs):
    """swap two random nodes in a random route"""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    route_len = (scenario != -1).sum(dim=-1)[route_idx]
    # pick nodes to swap
    dev = scenario.device
    if route_len < 2:
        # can't swap, just return the existing scenario
        return scenario
    
    node_idxs = torch.ones(route_len, device=dev).\
        multinomial(2, replacement=False)

    # swap nodes
    route = scenario[route_idx][0]
    old_node = route[node_idxs[0]]
    route[node_idxs[0]] = route[node_idxs[1]]
    route[node_idxs[1]] = old_node

    scenario[route_idx] = route
    check_for_inner_negs(route)
    return scenario


def heuristic_3(scenario, *args, **kwargs):
    """move a random node to a random new position in a random route"""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    route_len = (scenario != -1).sum(dim=-1)[route_idx]
    if route_len < 2:
        # can't move any nodes, just return the existing scenario
        return scenario
    # pick nodes to swap
    dev = scenario.device
    old_node_pos, new_node_pos = torch.ones(route_len, device=dev).\
        multinomial(2, replacement=False)
    old_node = scenario[route_idx, old_node_pos]
    route = scenario[route_idx][0]
    inplace_remove_stop_at(route, old_node_pos)
    inplace_insert_node_at(route, new_node_pos, old_node)

    scenario[route_idx] = route
    check_for_inner_negs(route)

    return scenario


def heuristic_4(scenario, state, *args, **kwargs):
    """replace a random node in a random route with another random node"""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"
    route_lens = (scenario != -1).sum(dim=-1)

    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device)
    route_len = route_lens[route_idx][0]
    if route_len == 0:
        # can't replace anything, just return the existing scenario
        return scenario
    replacement_idx = torch.randint(route_len, (1,), device=scenario.device)

    n_nodes_not_on_route = state.max_n_nodes - route_len
    if n_nodes_not_on_route == 0:
        # can't replace with anything, just return the existing scenario
        return scenario
    new_node_subidx = torch.randint(state.max_n_nodes - route_len, (1,), 
                                    device=scenario.device)
    route = scenario[route_idx][0]
    unused_nodes = list(set(range(state.max_n_nodes)) - set(route.tolist()))
    new_node = unused_nodes[new_node_subidx]

    route[replacement_idx] = new_node

    scenario[route_idx] = route
    check_for_inner_negs(route)
    return scenario


def heuristic_5(scenario, state, *args, **kwargs):
    """select two random routes and a random position on each; move node at the 
        first pos on the first route to the second pos on the second route."""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"
    n_routes = scenario.shape[0]
    if n_routes < 2:
        # makes no sense to swap nodes between two routes if there is only one
        return scenario

    route_lens = (scenario != -1).sum(dim=-1)
    can_shrink = route_lens > 2
    if not can_shrink.any():
        return scenario
    route1_idx = can_shrink.to(float).multinomial(1)

    can_grow = route_lens < state.max_n_nodes
    if not can_grow.any():
        return scenario
    route2_idx = can_grow.to(float).multinomial(1)

    route1 = scenario[route1_idx][0]
    route2 = scenario[route2_idx][0]
    route1_len = (route1 != -1).sum(dim=-1)
    route2_len = (route2 != -1).sum(dim=-1)
    node1_idx = (torch.rand(1, device=scenario.device) * route1_len).to(int)
    node2_idx = (torch.rand(1, device=scenario.device) * route2_len).to(int)
    old_node_1 = route1[node1_idx[0]]
    assert old_node_1 >= 0
    inplace_insert_node_at(route2, node2_idx, old_node_1)
    inplace_remove_stop_at(route1, node1_idx)

    check_for_inner_negs(route1)
    check_for_inner_negs(route2)

    scenario[route1_idx] = route1
    scenario[route2_idx] = route2
    return scenario


def heuristic_6(scenario, *args, **kwargs):
    """select two random routes and a random node on each, and swap those two 
        nodes."""
    assert scenario.ndim == 2, "batched scenarios not supported yet!"
    n_routes = scenario.shape[0]
    if n_routes < 2:
        # makes no sense to swap nodes between two routes if there is only one
        return scenario
    dist = torch.ones(n_routes, device=scenario.device)
    route_idxs = dist.multinomial(2, replacement=False)
    old_scenario = scenario.clone()
    routes = old_scenario[route_idxs]
    route_lens = (routes != -1).sum(dim=-1)
    if 0 in route_lens:
        # can't swap any nodes, just return the existing scenario
        return scenario
    node_idxs = (torch.rand(2, device=scenario.device) * route_lens).to(int)
    old_node_1 = routes[0, node_idxs[0]].item()
    old_node_2 = routes[1, node_idxs[1]].item()

    scenario[route_idxs[0], node_idxs[0]] = old_node_2
    scenario[route_idxs[1], node_idxs[1]] = old_node_1
    check_for_inner_negs(scenario[route_idxs[0]])
    check_for_inner_negs(scenario[route_idxs[1]])
    return scenario


def heuristic_bee1(scenario, state, shortest_paths, *args, **kwargs):
    # select a route
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device).squeeze()
    # select which terminal will be kept
    keep_start = torch.rand(1) > 0.5
    route = scenario[route_idx]
    if keep_start:
        kept_node = route[0]
        options = shortest_paths[kept_node]
    else:
        route_len = (route != -1).sum()
        kept_node = route[route_len - 1]
        options = shortest_paths[:, kept_node]

    # select a new route at random
    option_lens = (options > -1).sum(dim=1)
    is_valid = option_lens >= state.min_route_len[0]
    is_valid &= option_lens <= state.max_route_len[0]
    assert is_valid.ndim == 1
    options = options[is_valid]
    selection_idx = torch.randint(options.shape[0], (1,)).squeeze()
    new_route = options[selection_idx]
    scenario[route_idx, :len(new_route)] = new_route

    return scenario


def heuristic_bee2(scenario, state, *args, **kwargs):
    # shorten, same as in BCO
    # select a route at random
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device).squeeze()

    # select which terminal will be modified
    modify_start = torch.rand(1) > 0.5

    route = scenario[route_idx]
    route_len = (route != -1).sum()
    can_shorten = len(route) > state.min_route_len[0]
    if not can_shorten:
        # we can't modify this route, so just leave it
        return scenario
    else:
        # cut off the chosen end
        if modify_start:
            # shift the route "back" one index, erasing the first node
            route[:-1] = route[1:].clone()

        # either way, erase the old last node, since it's either what we want
         # to remove or it's a duplicate after the shift
        route[route_len - 1] = -1

    # add the modified route back to the scenario
    scenario[route_idx] = route
    return scenario


def heuristic_bee3(scenario, state, *args, **kwargs):
    # extend, same as in BCO
    # select a route at random
    n_routes = scenario.shape[0]
    route_idx = torch.randint(n_routes, (1,), device=scenario.device).squeeze()

    # select which terminal will be modified
    modify_start = torch.rand(1) > 0.5
    extend_choices = torch.ones(state.max_n_nodes, device=scenario.device)
    # don't add any duplicate routes
    route = scenario[route_idx]
    route_len = (route != -1).sum()
    extend_choices[route[:route_len]] = 0.0

    can_extend = (extend_choices > 0).any() and \
        route_len < state.max_route_len[0]
    if not can_extend:
        # we can't modify this route, so just leave it
        return scenario
    else:
        # add a random node at the chosen end
        extension = extend_choices.to(dtype=torch.float32).multinomial(1)
        if modify_start:
            route[1:] = route[:-1].clone()
            route[0] = extension
        else:
            route[route_len] = extension

    # add the modified route back to the scenario
    scenario[route_idx] = route
    return scenario


def noop_heuristic(scenario, *args, **kwargs):
    return scenario


def check_for_inner_negs(route):
    is_neg = route < 0
    if is_neg.any():
        n_nonneg = (~is_neg).sum()
        neg_idxs = torch.where(is_neg)[0]
        first_neg_idx = neg_idxs[0]
        assert first_neg_idx == n_nonneg


def inplace_remove_stop_at(route, stop_idx):
    """remove the stop at the given index from the route"""
    route_len = (route != -1).sum()
    route[stop_idx:-1] = route[stop_idx + 1:].clone()
    route[route_len - 1] = -1


def inplace_insert_node_at(route, node_idx, new_node):
    """insert a new node at the given position in the given route"""
    route[node_idx + 1:] = route[node_idx:-1].clone()
    route[node_idx] = new_node


@hydra.main(version_base=None, config_path="../cfg", 
            config_name="hyperheuristic")
def main(cfg: DictConfig):
    global DEVICE
    DEVICE, run_name, sum_writer, cost_fn, model = \
        lrnu.process_standard_experiment_cfg(cfg, 'hh_', 
                                             weights_required=True)

    # read in the dataset
    test_ds = get_dataset_from_config(cfg.eval.dataset)
    test_dl = DataLoader(test_ds, batch_size=cfg.batch_size)

    assert 'n_iterations' in cfg or 'duration_s' in cfg, \
        "Must provide either n_iterations or duration_s in config!"
    n_steps = cfg.get('n_iterations', None)
    duration_s = cfg.get('duration_s', None)
    draw = cfg.eval.get('draw', False)

    # TODO load the model and pass it in if the config file specifies it.

    routes = \
        lrnu.test_method(hyperheuristic, test_dl, cfg.eval.n_routes, 
        cfg.eval.min_route_len, cfg.eval.max_route_len, cost_fn, 
        sum_writer=sum_writer, silent=False, duration_s=duration_s, 
        n_steps=n_steps, f_0=cfg.f_0, init_model=model, device=DEVICE, 
        csv=cfg.eval.csv, draw=draw, return_routes=True)[-1]
    if type(routes) is not torch.Tensor:
        routes = get_batch_tensor_from_routes(routes)

    # save the final routes that were produced
    lrnu.dump_routes(run_name, routes.cpu())


if __name__ == "__main__":
    main()
