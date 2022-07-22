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
r"""
LRFinderCallback
===============

Finds optimal learning rate
"""

from typing import Callable, Optional

import pytorch_lightning as pl
from pytorch_lightning.callbacks.callback import Callback
from pytorch_lightning.tuner.lr_finder import lr_find
from pytorch_lightning.utilities.exceptions import _TunerExitException, MisconfigurationException


class LRFinderCallback(Callback):
    SUPPORTED_MODES = ("linear", "exponential")

    """LRFinderCallback enables the user to do a range test of good initial learning rates, to reduce the amount of
    guesswork in picking a good starting learning rate.

    Args:
        min_lr: minimum learning rate to investigate

        max_lr: maximum learning rate to investigate

        num_training: number of learning rates to test

        mode: Search strategy to update learning rate after each batch:

            - ``'exponential'`` (default): Will increase the learning rate exponentially.
            - ``'linear'``: Will increase the learning rate linearly.

        early_stop_threshold: threshold for stopping the search. If the
            loss at any point is larger than early_stop_threshold*best_loss
            then the search is stopped. To disable, set to None.

        update_attr: Whether to update the learning rate attribute or not.

    Raises:
        MisconfigurationException:
            If learning rate/lr in ``model`` or ``model.hparams`` isn't overridden when ``auto_lr_find=True``,
            or if you are using more than one optimizer.
    """

    def __init__(
        self,
        min_lr: float = 1e-8,
        max_lr: float = 1,
        num_training: int = 100,
        mode: str = "exponential",
        early_stop_threshold: float = 4.0,
        update_attr: bool = False,
    ) -> None:
        mode = mode.lower()
        if mode not in self.SUPPORTED_MODES:
            raise MisconfigurationException(f"`mode` should be either of {self.SUPPORTED_MODES}")

        self._min_lr = min_lr
        self._max_lr = max_lr
        self._num_training = num_training
        self._mode = mode
        self._early_stop_threshold = early_stop_threshold
        self._update_attr = update_attr

        self._early_exit = False
        self.optimal_lr = None

    def lr_find(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule"):
        self.optimal_lr = lr_find(
            trainer,
            pl_module,
            min_lr=self._min_lr,
            max_lr=self._max_lr,
            num_training=self._num_training,
            mode=self._mode,
            early_stop_threshold=self._early_stop_threshold,
            update_attr=self._update_attr,
        )

        if self._early_exit:
            raise _TunerExitException()

    def on_fit_start(self, trainer: "pl.Trainer", pl_module: "pl.LightningModule"):
        self.lr_find(trainer, pl_module)
