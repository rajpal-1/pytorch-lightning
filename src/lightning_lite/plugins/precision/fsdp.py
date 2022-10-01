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
from typing import Literal, Optional, TYPE_CHECKING

import torch

from lightning_lite.plugins.precision import NativeMixedPrecision
from lightning_lite.utilities.enums import PrecisionType
from lightning_lite.utilities.imports import _TORCH_GREATER_EQUAL_1_12

if TYPE_CHECKING:
    from torch.distributed.fsdp.fully_sharded_data_parallel import MixedPrecision
    from torch.distributed.fsdp.sharded_grad_scaler import ShardedGradScaler


class FSDPPrecision(NativeMixedPrecision):
    """AMP for Fully Sharded Data Parallel training."""

    def __init__(self, precision: Literal[16, "bf16"], device: str, scaler: Optional["ShardedGradScaler"] = None) -> None:
        if not _TORCH_GREATER_EQUAL_1_12:
            raise RuntimeError("`FSDPPrecision` is supported from PyTorch v1.12.0 onwards.")

        from torch.distributed.fsdp.sharded_grad_scaler import ShardedGradScaler

        super().__init__(
            precision=precision,
            device=device,
            scaler=(ShardedGradScaler() if scaler is None and precision == 16 else None),
        )

    @property
    def mixed_precision_config(self) -> MixedPrecision:
        from torch.distributed.fsdp.fully_sharded_data_parallel import MixedPrecision

        if self.precision == PrecisionType.HALF:
            dtype = torch.float16
        elif self.precision == PrecisionType.BFLOAT:
            dtype = torch.bfloat16
        else:
            raise ValueError(f"Was unable to infer precision type, received {self.precision!r}.")
        return MixedPrecision(
            param_dtype=dtype,
            reduce_dtype=dtype,
            buffer_dtype=dtype,
        )
