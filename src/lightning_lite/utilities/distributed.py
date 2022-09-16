import logging
import os
from typing import Any, Iterable, Iterator, List, Optional, Sized, Tuple, Union

import torch
from torch import Tensor
from torch.nn import functional as F
from torch.utils.data import Dataset, DistributedSampler, Sampler

from lightning_lite.plugins.environments.cluster_environment import ClusterEnvironment
from lightning_lite.utilities.imports import _HPU_AVAILABLE, _TPU_AVAILABLE
from lightning_lite.utilities.rank_zero import rank_zero_info as new_rank_zero_info

if _TPU_AVAILABLE:
    import torch_xla.core.xla_model as xm


if torch.distributed.is_available():
    from torch.distributed import group, ReduceOp
else:

    class ReduceOp:  # type: ignore # (see https://github.com/python/mypy/issues/1153)
        SUM = None

    class group:  # type: ignore
        WORLD = None


log = logging.getLogger(__name__)


def gather_all_tensors(result: Tensor, group: Optional[Any] = None) -> List[Tensor]:
    """Function to gather all tensors from several DDP processes onto a list that is broadcasted to all processes.

    Works on tensors that have the same number of dimensions, but where each dimension may differ. In this case
    tensors are padded, gathered and then trimmed to secure equal workload for all processes.

    Args:
        result: The value to sync
        group: The process group to gather results from. Defaults to all processes (world)

    Return:
        gathered_result: List with size equal to the process group where
            gathered_result[i] corresponds to result tensor from process i
    """
    if group is None:
        group = torch.distributed.group.WORLD

    # Convert tensors to contiguous format
    result = result.contiguous()

    world_size = torch.distributed.get_world_size(group)
    torch.distributed.barrier(group=group)

    # If the tensor is scalar, things are easy
    if result.ndim == 0:
        return _simple_gather_all_tensors(result, group, world_size)

    # 1. Gather sizes of all tensors
    local_size = torch.tensor(result.shape, device=result.device)
    local_sizes = [torch.zeros_like(local_size) for _ in range(world_size)]
    torch.distributed.all_gather(local_sizes, local_size, group=group)
    max_size = torch.stack(local_sizes).max(dim=0).values
    all_sizes_equal = all(all(ls == max_size) for ls in local_sizes)

    # 2. If shapes are all the same, then do a simple gather:
    if all_sizes_equal:
        return _simple_gather_all_tensors(result, group, world_size)

    # 3. If not, we need to pad each local tensor to maximum size, gather and then truncate
    pad_dims = []
    pad_by = (max_size - local_size).detach().cpu()
    for val in reversed(pad_by):
        pad_dims.append(0)
        pad_dims.append(val.item())
    result_padded = F.pad(result, pad_dims)
    gathered_result = [torch.zeros_like(result_padded) for _ in range(world_size)]
    torch.distributed.all_gather(gathered_result, result_padded, group)
    for idx, item_size in enumerate(local_sizes):
        slice_param = [slice(dim_size) for dim_size in item_size]
        gathered_result[idx] = gathered_result[idx][slice_param]
    return gathered_result


def _simple_gather_all_tensors(result: Tensor, group: Any, world_size: int) -> List[Tensor]:
    gathered_result = [torch.zeros_like(result) for _ in range(world_size)]
    torch.distributed.all_gather(gathered_result, result, group)
    return gathered_result


def distributed_available() -> bool:
    return torch.distributed.is_available() and torch.distributed.is_initialized() or tpu_distributed()


def sync_ddp_if_available(
    result: Tensor, group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = None
) -> Tensor:
    """Function to reduce a tensor across worker processes during distributed training.

    Args:
        result: The value to sync and reduce (typically tensor or number)
        group: The process group to gather results from. Defaults to all processes (world)
        reduce_op: The reduction operation. Defaults to sum.
            Can also be a string of 'avg', 'mean' to calculate the mean during reduction.

    Return:
        reduced value
    """
    if distributed_available():
        return sync_ddp(result, group=group, reduce_op=reduce_op)
    return result


def sync_ddp(result: Tensor, group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = None) -> Tensor:
    """Function to reduce the tensors from several DDP processes to one main process.

    Args:
        result: The value to sync and reduce (typically tensor or number)
        group: The process group to gather results from. Defaults to all processes (world)
        reduce_op: The reduction operation. Defaults to sum.
            Can also be a string of 'avg', 'mean' to calculate the mean during reduction.

    Return:
        reduced value
    """
    divide_by_world_size = False

    if group is None:
        group = torch.distributed.group.WORLD

    op: Optional[ReduceOp]
    if isinstance(reduce_op, str):
        if reduce_op.lower() in ("avg", "mean"):
            op = ReduceOp.SUM
            divide_by_world_size = True
        else:
            op = getattr(ReduceOp, reduce_op.upper())
    else:
        op = reduce_op

    # WA for HPU. HPU doesn't support Long types, forcefully set it to float
    if _HPU_AVAILABLE:
        is_hpu_backend = os.environ.get("HCCL_DISTRIBUTED_BACKEND") == "1"
        if is_hpu_backend:
            if (result.type() == "torch.LongTensor") or (result.type() == "torch.hpu.LongTensor"):
                new_rank_zero_info("Long tensor unsupported on HPU, casting to float")
                result = result.float()

    # Sync all processes before reduction
    torch.distributed.barrier(group=group)
    torch.distributed.all_reduce(result, op=op, group=group, async_op=False)

    if divide_by_world_size:
        result = result / torch.distributed.get_world_size(group)

    return result


class AllGatherGrad(torch.autograd.Function):
    @staticmethod
    def forward(  # type: ignore[override]
        ctx: Any,
        tensor: Tensor,
        group: Optional["torch.distributed.ProcessGroup"] = group.WORLD,
    ) -> Tensor:
        ctx.group = group

        gathered_tensor = [torch.zeros_like(tensor) for _ in range(torch.distributed.get_world_size())]

        torch.distributed.all_gather(gathered_tensor, tensor, group=group)
        gathered_tensor = torch.stack(gathered_tensor, dim=0)

        return gathered_tensor

    @staticmethod
    def backward(ctx: Any, *grad_output: Tensor) -> Tuple[Tensor, None]:
        grad_output = torch.cat(grad_output)

        torch.distributed.all_reduce(grad_output, op=torch.distributed.ReduceOp.SUM, async_op=False, group=ctx.group)

        return grad_output[torch.distributed.get_rank()], None


def all_gather_ddp_if_available(
    tensor: Tensor, group: Optional["torch.distributed.ProcessGroup"] = None, sync_grads: bool = False
) -> Tensor:
    """Function to gather a tensor from several distributed processes.

    Args:
        tensor: Tensor of shape (batch, ...)
        group: The process group to gather results from. Defaults to all processes (world)
        sync_grads: Flag that allows users to synchronize gradients for all_gather op

    Return:
        A tensor of shape (world_size, batch, ...)
    """
    group = group if group is not None else torch.distributed.group.WORLD
    if distributed_available():
        if sync_grads:
            return AllGatherGrad.apply(tensor, group)
        with torch.no_grad():
            return AllGatherGrad.apply(tensor, group)
    return tensor


def init_dist_connection(
    cluster_environment: "ClusterEnvironment",
    torch_distributed_backend: str,
    global_rank: Optional[int] = None,
    world_size: Optional[int] = None,
    **kwargs: Any,
) -> None:
    """Utility function to initialize distributed connection by setting env variables and initializing the
    distributed process group.

    Args:
        cluster_environment: ``ClusterEnvironment`` instance
        torch_distributed_backend: Backend to use (includes `nccl` and `gloo`)
        global_rank: Rank of the current process
        world_size: Number of processes in the group
        kwargs: Kwargs for ``init_process_group``

    Raises:
        RuntimeError:
            If ``torch.distributed`` is not available
    """
    if not torch.distributed.is_available():
        raise RuntimeError("torch.distributed is not available. Cannot initialize distributed process group")
    if torch.distributed.is_initialized():
        log.debug("torch.distributed is already initialized. Exiting early")
        return
    global_rank = global_rank if global_rank is not None else cluster_environment.global_rank()
    world_size = world_size if world_size is not None else cluster_environment.world_size()
    os.environ["MASTER_ADDR"] = cluster_environment.main_address
    os.environ["MASTER_PORT"] = str(cluster_environment.main_port)
    log.info(f"Initializing distributed: GLOBAL_RANK: {global_rank}, MEMBER: {global_rank + 1}/{world_size}")
    torch.distributed.init_process_group(torch_distributed_backend, rank=global_rank, world_size=world_size, **kwargs)

    # On rank=0 let everyone know training is starting
    new_rank_zero_info(
        f"{'-' * 100}\n"
        f"distributed_backend={torch_distributed_backend}\n"
        f"All distributed processes registered. Starting with {world_size} processes\n"
        f"{'-' * 100}\n"
    )


def tpu_distributed() -> bool:
    return _TPU_AVAILABLE and xm.xrt_world_size() > 1


def get_default_process_group_backend_for_device(device: torch.device) -> str:
    return "nccl" if device.type == "cuda" else "gloo"


# TODO(lite): The error messages refer to 'replace_sampler_ddp' in PL but Lite has it named 'replace_sampler'
class _DatasetSamplerWrapper(Dataset):
    """Dataset to create indexes from `Sampler` or `Iterable`"""

    def __init__(self, sampler: Union[Sampler, Iterable]) -> None:
        if not isinstance(sampler, Sized):
            raise TypeError(
                "You seem to have configured a sampler in your DataLoader which"
                " does not provide `__len__` method. The sampler was about to be"
                " replaced by `DistributedSamplerWrapper` since `replace_sampler_ddp`"
                " is True and you are using distributed training. Either provide `__len__`"
                " method in your sampler, remove it from DataLoader or set `replace_sampler_ddp=False`"
                " if you want to handle distributed sampling yourself."
            )
        if len(sampler) == float("inf"):
            raise TypeError(
                "You seem to have configured a sampler in your DataLoader which"
                " does not provide finite `__len__` method. The sampler was about to be"
                " replaced by `DistributedSamplerWrapper` since `replace_sampler_ddp`"
                " is True and you are using distributed training. Either provide `__len__`"
                " method in your sampler which returns a finite number, remove it from DataLoader"
                " or set `replace_sampler_ddp=False` if you want to handle distributed sampling yourself."
            )
        self._sampler = sampler
        # defer materializing an iterator until it is necessary
        self._sampler_list: Optional[List[Any]] = None

    def __getitem__(self, index: int) -> Any:
        if self._sampler_list is None:
            self._sampler_list = list(self._sampler)
        return self._sampler_list[index]

    def __len__(self) -> int:
        return len(self._sampler)

    def reset(self) -> None:
        """Reset the sampler list in order to get new sampling."""
        self._sampler_list = list(self._sampler)


class DistributedSamplerWrapper(DistributedSampler):
    """Wrapper over ``Sampler`` for distributed training.

    Allows you to use any sampler in distributed mode. It will be automatically used by Lightning in distributed mode if
    sampler replacement is enabled.
    """

    def __init__(self, sampler: Union[Sampler, Iterable], *args: Any, **kwargs: Any) -> None:
        super().__init__(_DatasetSamplerWrapper(sampler), *args, **kwargs)

    def __iter__(self) -> Iterator:
        self.dataset.reset()
        return (self.dataset[index] for index in super().__iter__())
