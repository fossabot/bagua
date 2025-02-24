import torch
import copy
from bagua.torch_api.utils import collocate_params, flatten_module_params

__all__ = ["FusedOptimizer"]


class FusedOptimizer(torch.optim.Optimizer):
    """Convert any optimizer into a backward efficiency fused optimizer.

    This fused optimizer fuses multiple kernel launches that do module parameter updates
    into one or a few kernel launches, by flattening parameter tensors into one or more
    contiguous ones.

    It can be used in conjunction with :func:`bagua.torch_api.bagua_init`. In such a case,
    `Bagua` will do the fusions automatically, otherwise, pass ``True`` to `do_flatten`
    to perform these fusions explicitly.

    Args:
        optimizer (torch.optim.Optimizer): Any PyTorch optimizer.
        do_flatten (bool): Whether to flatten the parameters. Default: ``False``.

    Returns:
        Fused optimizer.


    Example::
        To use in conjunction with :func:`bagua.torch_api.bagua_init`:

        >>> optimizer = torch.optim.Adadelta(model.parameters(), ....)
        >>> optimizer = bagua.torch_api.FusedOptimizer(optimizer)
        >>> model, optimizer = bagua.bagua_init(model, optimizer, ...)

        To use alone or with :class:`torch.nn.parallel.DistributedDataParallel`, set `do_flatten` to be ``True``:

        >>> optimizer = torch.optim.Adadelta(model.parameters(), ....)
        >>> optimizer = bagua.torch_api.FusedOptimizer(optimizer, do_flatten=True)
    """

    def __init__(self, optimizer: torch.optim.Optimizer, do_flatten: bool = False):
        self.optimizer = copy.copy(optimizer)
        super(FusedOptimizer, self).__init__(optimizer.param_groups, optimizer.defaults)

        if do_flatten:
            f32_params = [
                param
                for group in self.optimizer.param_groups
                for param in group["params"]
                if param.type() == "torch.cuda.FloatTensor"
            ]
            f16_params = [
                param
                for group in self.optimizer.param_groups
                for param in group["params"]
                if param.type() == "torch.cuda.HalfTensor"
            ]

            flatten_module_params(f32_params, align_bytes=1)
            flatten_module_params(f16_params, align_bytes=1)

    def step(self, closure=None):
        for group in self.optimizer.param_groups:
            params = group["params"]
            grouped_params = group_params_by_storage(params)

            fused_params = []

            for _, group_p in grouped_params.items():
                fused_params.extend(reorder_params(group_p))

            group["params"] = fused_params

        return self.optimizer.step(closure)


def reorder_params(params):
    """Input params share same storage, reorder them by their storage offset"""

    sorted_params = sorted(params, key=lambda x: x.storage_offset())

    grouped = []
    tmp_params = []

    for p in sorted_params:
        if len(tmp_params) > 0 and not is_contiguous_param(p, tmp_params[-1]):
            grouped.append(collocate_params(tmp_params))
            tmp_params = []

        tmp_params.append(p)

    if len(tmp_params) > 0:
        grouped.append(collocate_params(tmp_params))

    return grouped


def is_contiguous_param(a, b):
    allocate_size_a = (
        a.bagua_tensor.num_elem_allocated() if hasattr(a, "bagua_tensor") else a.numel()
    )
    allocate_size_b = (
        b.bagua_tensor.num_elem_allocated() if hasattr(b, "bagua_tensor") else b.numel()
    )
    return (
        a.data.storage_offset() == b.data.storage_offset() + allocate_size_b
        and a.grad.data.storage_offset()
        == b.grad.data.storage_offset() + allocate_size_b
    ) or (
        b.data.storage_offset() == a.data.storage_offset() + allocate_size_a
        and b.grad.data.storage_offset()
        == a.grad.data.storage_offset() + allocate_size_a
    )


def group_params_by_storage(params):
    grouped_params = {}
    for p in params:
        weight_storage = p.data.storage().data_ptr()
        l = grouped_params.get(weight_storage, [])
        l.append(p)
        grouped_params[weight_storage] = l

    return grouped_params
