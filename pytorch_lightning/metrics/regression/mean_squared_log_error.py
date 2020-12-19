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
import torch
from typing import Any, Callable, Optional

from pytorch_lightning.metrics.metric import Metric
from pytorch_lightning.metrics.functional.mean_squared_log_error import (
    _mean_squared_log_error_update,
    _mean_squared_log_error_compute
)


class MeanSquaredLogError(Metric):
    r"""
    Computes `mean squared logarithmic error
    <https://scikit-learn.org/stable/modules/model_evaluation.html#mean-squared-log-error>`_
    (MSLE):

    .. math:: \text{MSLE} = \frac{1}{N}\sum_i^N (\log_e(1 + y_i) - \log_e(1 + \hat{y_i}))^2

    Where :math:`y` is a tensor of target values, and :math:`\hat{y}` is a tensor of predictions.

    Args:
        compute_on_step:
            Forward only calls ``update()`` and return None if this is set to False. default: True
        dist_sync_on_step:
            Synchronize metric state across processes at each ``forward()``
            before returning the value at the step. default: False
        process_group:
            Specify the process group on which synchronization is called. default: None (which selects the entire world)
        dist_sync_fn:
            Callback that performs the allgather operation on the metric state. When `None`, DDP
            will be used to perform the allgather. default: None
        auto_reset_on_compute:
            Specify if ``reset()`` should be called automatically after each ``compute()``.
            Disabling it allows for calculate running accumulated metrics. default: True

    Example:

        >>> from pytorch_lightning.metrics import MeanSquaredLogError
        >>> target = torch.tensor([2.5, 5, 4, 8])
        >>> preds = torch.tensor([3, 5, 2.5, 7])
        >>> mean_squared_log_error = MeanSquaredLogError()
        >>> mean_squared_log_error(preds, target)
        tensor(0.0397)

    """

    def __init__(
        self,
        compute_on_step: bool = True,
        dist_sync_on_step: bool = False,
        process_group: Optional[Any] = None,
        dist_sync_fn: Callable = None,
        auto_reset_on_compute: bool = True
    ):
        super().__init__(
            compute_on_step=compute_on_step,
            dist_sync_on_step=dist_sync_on_step,
            process_group=process_group,
            dist_sync_fn=dist_sync_fn,
            auto_reset_on_compute=auto_reset_on_compute
        )

        self.add_state("sum_squared_log_error", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.add_state("total", default=torch.tensor(0), dist_reduce_fx="sum")

    def update(self, preds: torch.Tensor, target: torch.Tensor):
        """
        Update state with predictions and targets.

        Args:
            preds: Predictions from model
            target: Ground truth values
        """
        sum_squared_log_error, n_obs = _mean_squared_log_error_update(preds, target)

        self.sum_squared_log_error += sum_squared_log_error
        self.total += n_obs

    def compute(self):
        """
        Compute mean squared logarithmic error over state.
        """
        return _mean_squared_log_error_compute(self.sum_squared_log_error, self.total)
