# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Dict, Generator, List, Optional, TYPE_CHECKING, Union

import torch
from torch import Tensor
from torch.distributed import default_pg_timeout
from torch.nn import Module
from torch.optim import Optimizer

from lightning_lite.accelerators import Accelerator
from lightning_lite.plugins import CheckpointIO, ClusterEnvironment, Precision
from lightning_lite.plugins.precision.fsdp import FSDPPrecision
from lightning_lite.strategies.launchers.subprocess_script import _SubprocessScriptLauncher
from lightning_lite.strategies.parallel import ParallelStrategy
from lightning_lite.strategies.strategy import TBroadcast
from lightning_lite.utilities.distributed import distributed_available, get_default_process_group_backend_for_device
from lightning_lite.utilities.distributed import group as _group
from lightning_lite.utilities.distributed import init_dist_connection, ReduceOp, sync_ddp_if_available
from lightning_lite.utilities.imports import _TORCH_GREATER_EQUAL_1_12
from lightning_lite.utilities.rank_zero import rank_zero_only
from lightning_lite.utilities.seed import reset_seed

if TYPE_CHECKING:
    from torch.distributed.fsdp.fully_sharded_data_parallel import (
        BackwardPrefetch,
        CPUOffload,
        FullyShardedDataParallel,
        MixedPrecision,
    )
    from torch.distributed.fsdp.wrap import enable_wrap  # noqa: F401

_FSDP_ALIASES = ("fsdp", "fsdp_full_shard_offload")


class FSDPStrategy(ParallelStrategy):
    r"""Strategy for Fully Sharded Data Parallel provided by torch.distributed.

    .. warning:: ``FSDPStrategy`` is in BETA and subject to change. The interface can
        bring breaking changes and new features with the next release of PyTorch.

    Fully Sharded Training shards the entire model across all available GPUs, allowing you to scale model
    size, whilst using efficient communication to reduce overhead. In practice, this means we can remain
    at parity with PyTorch DDP, whilst scaling our model sizes dramatically. The technique is similar
    to ZeRO-Stage 3.

    For more information `check out <https://pytorch.org/blog/introducing-pytorch-fully-sharded-data-parallel-api>`__.

    Defaults have been set and options have been exposed, but may require configuration
    based on your level of memory/speed efficiency. We suggest having a look at
    `this tutorial <https://pytorch.org/tutorials/intermediate/FSDP_tutorial.html>`__ for more information.

    Arguments:
        cpu_offload: CPU offloading config. Currently, only parameter and gradient CPU offload is supported. It
            can be enabled via passing in ``cpu_offload=CPUOffload(offload_params=True)``. Note that this currently
            implicitly enables gradient offloading to CPU in order for parameters and gradients to be on same device
            to work with the optimizer. This API is subject to change. Default is ``None`` in which case there
            will be no offloading.
        backward_prefetch: This is an experimental feature that is subject to change in the near future. It allows
            users to enable two different backward prefetching algorithms to help backward communication and
            computation overlapping. The pros and cons of each algorithm is explained in the class ``BackwardPrefetch``.
        mixed_precision: Mixed Precision config. By default, Lightning will enable FP16 if ``precision=16`` or BF16
            if ``precision=bf16`` unless a config is passed in. This is only available in PyTorch 1.12 and later.
        \**kwargs: Optional keywoard arguments passed to the FSDP context manager which will configure the FSDP class
            when wrapping modules.
    """

    def __init__(
        self,
        accelerator: Optional[Accelerator] = None,
        parallel_devices: Optional[List[torch.device]] = None,
        cluster_environment: Optional[ClusterEnvironment] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision_plugin: Optional[Precision] = None,
        process_group_backend: Optional[str] = None,
        timeout: Optional[timedelta] = default_pg_timeout,
        cpu_offload: Optional["CPUOffload"] = None,
        backward_prefetch: Optional["BackwardPrefetch"] = None,
        mixed_precision: Optional["MixedPrecision"] = None,
        **kwargs: Any,
    ) -> None:
        if not _TORCH_GREATER_EQUAL_1_12:
            raise NotImplementedError("`FSDPStrategy` is supported from PyTorch v1.12.0 onwards.")

        super().__init__(
            accelerator=accelerator,
            parallel_devices=parallel_devices,
            cluster_environment=cluster_environment,
            checkpoint_io=checkpoint_io,
            precision_plugin=precision_plugin,
        )
        self._num_nodes = 1
        self._process_group_backend: Optional[str] = process_group_backend
        self._timeout: Optional[timedelta] = timeout
        self._ddp_kwargs = kwargs

        self.cpu_offload = cpu_offload
        self.backward_prefetch = backward_prefetch
        self.mixed_precision = mixed_precision

    @property
    def root_device(self) -> torch.device:
        assert self.parallel_devices is not None
        return self.parallel_devices[self.local_rank]

    @property
    def is_distributed(self) -> bool:
        return True

    @property
    def num_nodes(self) -> int:
        return self._num_nodes

    @num_nodes.setter
    def num_nodes(self, num_nodes: int) -> None:
        self._num_nodes = num_nodes

    @property
    def num_processes(self) -> int:
        return len(self.parallel_devices) if self.parallel_devices is not None else 0

    @property
    def distributed_sampler_kwargs(self) -> Dict:
        return dict(num_replicas=(self.num_nodes * self.num_processes), rank=self.global_rank)

    @property
    def process_group_backend(self) -> Optional[str]:
        return self._process_group_backend

    @property
    def mixed_precision_config(self) -> Optional["MixedPrecision"]:
        if self.mixed_precision:
            return self.mixed_precision
        plugin = self.precision_plugin
        if isinstance(plugin, FSDPPrecision):
            return plugin.mixed_precision_config

    def _configure_launcher(self) -> None:
        assert self.cluster_environment is not None
        if not self.cluster_environment.creates_processes_externally:
            self._launcher = _SubprocessScriptLauncher(self.cluster_environment, self.num_processes, self.num_nodes)

    def setup_environment(self) -> None:
        self._setup_distributed()
        super().setup_environment()

    def setup_module(self, module: Module) -> "FullyShardedDataParallel":
        """Wraps the model into a
        :class:`~torch.distributed.fsdp.fully_sharded_data_parallel.FullyShardedDataParallel` module."""
        from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel

        if (
            any(isinstance(mod, FullyShardedDataParallel) for mod in module.modules())
            and "auto_wrap_policy" in self._ddp_kwargs
        ):
            # If model is already wrapped, we need to avoid sending the `auto_wrap_policy`
            del self._ddp_kwargs["auto_wrap_policy"]
        return FullyShardedDataParallel(
            module=module,
            cpu_offload=self.cpu_offload,
            backward_prefetch=self.backward_prefetch,
            mixed_precision=self.mixed_precision_config,
            device_id=self.root_device.index,
            **self._ddp_kwargs,
        )

    def setup_optimizer(self, optimizer: Optimizer) -> Optimizer:
        from torch.distributed.fsdp import FlatParameter

        if len(optimizer.param_groups) > 1:
            raise ValueError("Optimizers used with FSDP do not support multiple param groups.")

        if any(isinstance(param, FlatParameter) for param in optimizer.param_groups[0].values()):
            return optimizer
        raise ValueError("The optimizer does not seem to reference any flat FSDP parameters.")

    def module_to_device(self, module: Module) -> None:
        pass

    @contextmanager
    def module_sharded_context(self) -> Generator:
        from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel
        from torch.distributed.fsdp.wrap import enable_wrap

        with enable_wrap(
            wrapper_cls=FullyShardedDataParallel,
            cpu_offload=self.cpu_offload,
            backward_prefetch=self.backward_prefetch,
            mixed_precision=self.mixed_precision_config,
            device_id=self.root_device.index,
            **self._ddp_kwargs,
        ):
            yield

    def reduce(
        self, tensor: Tensor, group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = "mean"
    ) -> Tensor:
        if isinstance(tensor, Tensor):
            tensor = sync_ddp_if_available(tensor, group, reduce_op=reduce_op)
        return tensor

    def barrier(self, *args: Any, **kwargs: Any) -> None:
        if not distributed_available():
            return
        if torch.distributed.get_backend() == "nccl":
            torch.distributed.barrier(device_ids=[self.root_device.index])
        else:
            torch.distributed.barrier()

    def broadcast(self, obj: TBroadcast, src: int = 0) -> TBroadcast:
        obj = [obj]
        if self.global_rank != src:
            obj = [None]  # type: ignore[list-item]
        torch.distributed.broadcast_object_list(obj, src, group=_group.WORLD)
        return obj[0]

    @classmethod
    def register_strategies(cls, strategy_registry: Dict) -> None:
        from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload

        strategy_registry.register(
            "fsdp",
            cls,
            description="Fully Sharded Data Parallel training from torch.distributed.",
        )
        strategy_registry.register(
            "fsdp_full_shard_offload",
            cls,
            description="Native FSDP with Full Sharding and CPU Offloading",
            cpu_offload=CPUOffload(offload_params=True),
        )

    def _setup_distributed(self) -> None:
        reset_seed()
        self._set_world_ranks()
        rank_zero_only.rank = self.global_rank
        self._process_group_backend = self._get_process_group_backend()
        assert self.cluster_environment is not None
        init_dist_connection(self.cluster_environment, self._process_group_backend, timeout=self._timeout)

    def _get_process_group_backend(self) -> str:
        return self._process_group_backend or get_default_process_group_backend_for_device(self.root_device)

    def _set_world_ranks(self) -> None:
        if self.cluster_environment is None:
            return
        self.cluster_environment.set_global_rank(self.node_rank * self.num_processes + self.local_rank)
        self.cluster_environment.set_world_size(self.num_nodes * self.num_processes)
        rank_zero_only.rank = self.cluster_environment.global_rank()
