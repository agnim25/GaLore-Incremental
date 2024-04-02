# copy dependencies from transformers/optimization.py
import math
import warnings
from typing import Callable, Iterable, Tuple

import torch
from torch import nn
from torch.optim import Optimizer
import torch.nn.functional as F

from transformers.utils.versions import require_version

from .galore_projector import GaLoreProjector


class GaLoreAdamWInRank(Optimizer):
    """
    Implements Adam algorithm with weight decay fix as introduced in [Decoupled Weight Decay
    Regularization](https://arxiv.org/abs/1711.05101).

    Parameters:
        params (`Iterable[nn.parameter.Parameter]`):
            Iterable of parameters to optimize or dictionaries defining parameter groups.
        lr (`float`, *optional*, defaults to 0.001):
            The learning rate to use.
        betas (`Tuple[float,float]`, *optional*, defaults to `(0.9, 0.999)`):
            Adam's betas parameters (b1, b2).
        eps (`float`, *optional*, defaults to 1e-06):
            Adam's epsilon for numerical stability.
        weight_decay (`float`, *optional*, defaults to 0.0):
            Decoupled weight decay to apply.
        correct_bias (`bool`, *optional*, defaults to `True`):
            Whether or not to correct bias in Adam (for instance, in Bert TF repository they use `False`).
        no_deprecation_warning (`bool`, *optional*, defaults to `False`):
            A flag used to disable the deprecation warning (set to `True` to disable the warning).
    """

    def __init__(
        self,
        params: Iterable[nn.parameter.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float] = (0.9, 0.999),
        eps: float = 1e-6,
        weight_decay: float = 0.0,
        correct_bias: bool = True,
        no_deprecation_warning: bool = False,
    ):
        if not no_deprecation_warning:
            warnings.warn(
                "This implementation of AdamW is deprecated and will be removed in a future version. Use the PyTorch"
                " implementation torch.optim.AdamW instead, or set `no_deprecation_warning=True` to disable this"
                " warning",
                FutureWarning,
            )
        require_version("torch>=1.5.0")  # add_ with alpha
        if lr < 0.0:
            raise ValueError(f"Invalid learning rate: {lr} - should be >= 0.0")
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError(f"Invalid beta parameter: {betas[0]} - should be in [0.0, 1.0)")
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError(f"Invalid beta parameter: {betas[1]} - should be in [0.0, 1.0)")
        if not 0.0 <= eps:
            raise ValueError(f"Invalid epsilon value: {eps} - should be >= 0.0")
        defaults = {"lr": lr, "betas": betas, "eps": eps, "weight_decay": weight_decay, "correct_bias": correct_bias}
        super().__init__(params, defaults)

        self.galore_group = None
        for group in self.param_groups:
            if "rank" in group:
                self.galore_group = group

        if not self.galore_group:
            raise ValueError("Did not provide galore params when initializing optimizer")

        self.param_ranks = [2 for p in self.galore_group["params"]]
        self.current_explained_ratios = [0 for p in self.galore_group["params"]]
        self.rank_buffer = 100
        self.explained_ratio_threshold = 0.9
        

    @torch.no_grad()
    def step(self, closure: Callable = None):
        """
        Performs a single optimization step.

        Arguments:
            closure (`Callable`, *optional*): A closure that reevaluates the model and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        # print current rank values
        # for group in self.param_groups:
        #     if "rank" in group:
        #         print(self.param_ranks)

        for group in self.param_groups:
            for i, p in enumerate(group["params"]):
                if p.grad is None:
                    continue
                grad = p.grad
                if grad.is_sparse:
                    raise RuntimeError("Adam does not support sparse gradients, please consider SparseAdam instead")

                state = self.state[p]
                
                if "step" not in state:
                    state["step"] = 0

                # GaLore Projection
                if "rank" in group:
                    # InRank update
                    # prev: store a rank for each param (init 2), b = 100
                    # 1. compute top (r + b) singular vectors of grad
                    curr_rank = self.param_ranks[i]
                    matrix = grad[:curr_rank + self.rank_buffer, :]
                    if matrix.dtype != torch.float:
                        matrix = matrix.float()
                    s = torch.linalg.svdvals(matrix)
                    
                    # 2. increment rank until explained ratio >= threshold
                    while self.compute_explained_ratio(curr_rank, s) < self.explained_ratio_threshold:
                        curr_rank += 1
                    self.current_explained_ratios[i] = self.compute_explained_ratio(curr_rank, s)
                    self.param_ranks[i] = curr_rank

                    if "projector" not in state or self.param_ranks[i] != state["projector"].rank:
                        state["projector"] = GaLoreProjector(self.param_ranks[i], update_proj_gap=group["update_proj_gap"], scale=group["scale"], proj_type=group["proj_type"])

                    grad = state["projector"].project(grad, state["step"])

                # State initialization
                if "exp_avg" not in state:
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(grad)
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(grad)
                # if rank has increased, update state to match dimensions
                elif "rank" in group:
                    rank_dim = 0 if grad.size(dim=0) < grad.size(dim=1) else 1
                    if self.param_ranks[i] > state["exp_avg"].size(dim=rank_dim):
                        rank_diff = self.param_ranks[i] - state["exp_avg"].size(dim=rank_dim)
                        update_shape = (0, 0, 0, rank_diff) if rank_dim == 0 else (0, rank_diff)
                        state["exp_avg"] = F.pad(state["exp_avg"], update_shape, value=0)
                        state["exp_avg_sq"] = F.pad(state["exp_avg_sq"], update_shape, value=0)

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
                beta1, beta2 = group["betas"]

                state["step"] += 1

                # Decay the first and second moment running average coefficient
                # In-place operations to update the averages at the same time
                exp_avg.mul_(beta1).add_(grad, alpha=(1.0 - beta1))
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1.0 - beta2)
                denom = exp_avg_sq.sqrt().add_(group["eps"])

                step_size = group["lr"]
                if group["correct_bias"]:  # No bias correction for Bert
                    bias_correction1 = 1.0 - beta1 ** state["step"]
                    bias_correction2 = 1.0 - beta2 ** state["step"]
                    step_size = step_size * math.sqrt(bias_correction2) / bias_correction1

                # compute norm gradient
                norm_grad = exp_avg / denom
                
                # GaLore Projection Back
                if "rank" in group:
                    norm_grad = state["projector"].project_back(norm_grad)
                
                p.add_(norm_grad, alpha=-step_size)

                # Just adding the square of the weights to the loss function is *not*
                # the correct way of using L2 regularization/weight decay with Adam,
                # since that will interact with the m and v parameters in strange ways.
                #
                # Instead we want to decay the weights in a manner that doesn't interact
                # with the m/v parameters. This is equivalent to adding the square
                # of the weights to the loss with plain (non-momentum) SGD.
                # Add weight decay at the end (fixed version)
                if group["weight_decay"] > 0.0:
                    p.add_(p, alpha=(-group["lr"] * group["weight_decay"]))

        return loss

    
    def compute_explained_ratio(self, s_max, s):
        s_current = s.clone()
        s_current[s_max:] = 0
        return 1 - torch.var(s - s_current) / torch.var(s)