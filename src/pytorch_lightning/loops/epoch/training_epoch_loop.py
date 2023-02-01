# Copyright The Lightning team.
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
import math
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Union

import torch

from pytorch_lightning import loops  # import as loops to avoid circular imports
from pytorch_lightning.loops.optimization import _AutomaticOptimization, _ManualOptimization
from pytorch_lightning.loops.optimization.automatic import _OUTPUTS_TYPE as _OPTIMIZER_LOOP_OUTPUTS_TYPE
from pytorch_lightning.loops.optimization.manual import _OUTPUTS_TYPE as _MANUAL_LOOP_OUTPUTS_TYPE
from pytorch_lightning.loops.progress import BatchProgress, SchedulerProgress
from pytorch_lightning.loops.utilities import _is_max_limit_reached
from pytorch_lightning.trainer.connectors.logger_connector.result import _ResultCollection
from pytorch_lightning.utilities.exceptions import MisconfigurationException, SIGTERMException
from pytorch_lightning.utilities.fetching import AbstractDataFetcher, DataLoaderIterDataFetcher
from pytorch_lightning.utilities.model_helpers import is_overridden
from pytorch_lightning.utilities.rank_zero import rank_zero_warn, WarningCache
from pytorch_lightning.utilities.signature_utils import is_param_in_hook_signature

_BATCH_OUTPUTS_TYPE = Optional[Union[_OPTIMIZER_LOOP_OUTPUTS_TYPE, _MANUAL_LOOP_OUTPUTS_TYPE]]
_OUTPUTS_TYPE = List[_BATCH_OUTPUTS_TYPE]


class _TrainingEpochLoop(loops._Loop):
    """
    Iterates over all batches in the dataloader (one epoch) that the user returns in their
    :meth:`~pytorch_lightning.core.module.LightningModule.train_dataloader` method.

    Its main responsibilities are calling the ``*_epoch_{start,end}`` hooks, accumulating outputs if the user request
    them in one of these hooks, and running validation at the requested interval.

    The validation is carried out by yet another loop,
    :class:`~pytorch_lightning.loops.epoch.validation_epoch_loop.ValidationEpochLoop`.

    In the ``run()`` method, the training epoch loop could in theory simply call the
    ``LightningModule.training_step`` already and perform the optimization.
    However, Lightning has built-in support for automatic optimization with multiple optimizers.
    For this reason there are actually two more loops nested under
    :class:`~pytorch_lightning.loops.epoch.training_epoch_loop.TrainingEpochLoop`.

    Args:
        min_steps: The minimum number of steps (batches) to process
        max_steps: The maximum number of steps (batches) to process
    """

    def __init__(self, min_steps: Optional[int] = None, max_steps: int = -1) -> None:
        super().__init__()
        if max_steps < -1:
            raise MisconfigurationException(
                f"`max_steps` must be a non-negative integer or -1 (infinite steps). You passed in {max_steps}."
            )
        self.min_steps = min_steps
        self.max_steps = max_steps

        self.batch_progress = BatchProgress()
        self.scheduler_progress = SchedulerProgress()

        self.optimizer_loop = _AutomaticOptimization()
        self.manual_loop = _ManualOptimization()

        self.val_loop = loops._EvaluationLoop(verbose=False)

        self._results = _ResultCollection(training=True)
        self._outputs: _OUTPUTS_TYPE = []
        self._warning_cache = WarningCache()
        self._batches_that_stepped: int = 0

    @property
    def total_batch_idx(self) -> int:
        """Returns the current batch index (across epochs)"""
        # use `ready` instead of `completed` in case this is accessed after `completed` has been increased
        # but before the next `ready` increase
        return self.batch_progress.total.ready - 1

    @property
    def batch_idx(self) -> int:
        """Returns the current batch index (within this epoch)"""
        # use `ready` instead of `completed` in case this is accessed after `completed` has been increased
        # but before the next `ready` increase
        return self.batch_progress.current.ready - 1

    @property
    def global_step(self) -> int:
        lightning_module = self.trainer.lightning_module
        if lightning_module is None or lightning_module.automatic_optimization:
            return self.optimizer_loop.optim_progress.optimizer_steps
        return self.manual_loop.optim_step_progress.total.completed

    @property
    def _is_training_done(self) -> bool:
        max_steps_reached = _is_max_limit_reached(self.global_step, self.max_steps)
        return max_steps_reached or self._num_ready_batches_reached()

    @property
    def _is_validation_done(self) -> bool:
        # when we are restarting we want to check whether the val loop has finished
        return not self.restarting or self.val_loop.done

    @property
    def done(self) -> bool:
        """Evaluates when to leave the loop."""
        if self._is_training_done and self._is_validation_done:
            return True

        if self.trainer.should_stop:
            # early stopping
            min_epochs = self.trainer.fit_loop.min_epochs
            should_stop_early = self.trainer.fit_loop._should_stop_early
            if not should_stop_early:
                self._warning_cache.info(
                    f"Trainer was signaled to stop but the required `min_epochs={min_epochs!r}` or"
                    f" `min_steps={self.min_steps!r}` has not been met. Training will continue..."
                )
            return should_stop_early

        return False

    def run(self, data_fetcher: AbstractDataFetcher) -> _OUTPUTS_TYPE:
        self.reset()
        self.on_run_start(data_fetcher)
        while not self.done:
            try:
                self.advance(data_fetcher)
                self.on_advance_end()
                self._restarting = False
            except StopIteration:
                break
        self._restarting = False
        return self.on_run_end()

    def reset(self) -> None:
        """Resets the internal state of the loop for a new run."""
        if self.restarting:
            self.batch_progress.reset_on_restart()
            self.scheduler_progress.reset_on_restart()
            self.optimizer_loop.optim_progress.reset_on_restart()

            trainer = self.trainer
            if trainer.num_training_batches != float("inf"):
                expected_steps = math.ceil(trainer.num_training_batches / trainer.accumulate_grad_batches)
                if self.global_step % expected_steps != 0:
                    rank_zero_warn(
                        "You're resuming from a checkpoint that ended before the epoch ended. This can cause unreliable"
                        " results if further training is done. Consider using an end-of-epoch checkpoint"
                    )
        else:
            self.batch_progress.reset_on_run()
            self.scheduler_progress.reset_on_run()
            self.optimizer_loop.optim_progress.reset_on_run()
            # when the epoch starts, the total val batch progress should be reset as it's supposed to count the batches
            # seen per epoch, this is useful for tracking when validation is run multiple times per epoch
            self.val_loop.epoch_loop.batch_progress.total.reset()

        self._outputs = []

    def on_run_start(self, data_fetcher: AbstractDataFetcher) -> None:
        _ = iter(data_fetcher)  # creates the iterator inside the fetcher
        # add the previous `fetched` value to properly track `is_last_batch` with no prefetching
        data_fetcher.fetched += self.batch_progress.current.ready

        data_fetcher._start_profiler = self._on_before_fetch
        data_fetcher._stop_profiler = self._on_after_fetch

    def _on_before_fetch(self) -> None:
        self.trainer.profiler.start(f"[{self.__class__.__name__}].train_dataloader_next")

    def _on_after_fetch(self) -> None:
        self.trainer.profiler.stop(f"[{self.__class__.__name__}].train_dataloader_next")

    def advance(self, data_fetcher: AbstractDataFetcher) -> None:
        """Runs a single training batch.

        Raises:
            StopIteration: When the epoch is canceled by the user returning -1
        """
        if self.restarting and self._should_check_val_fx():
            # skip training and run validation in `on_advance_end`
            return
        # we are going to train first so the val loop does not need to restart
        self.val_loop.restarting = False

        if not isinstance(data_fetcher, DataLoaderIterDataFetcher):
            batch_idx = self.batch_idx + 1
            batch = next(data_fetcher)
        else:
            batch_idx, batch = next(data_fetcher)
        self.batch_progress.is_last_batch = data_fetcher.done

        kwargs = self._build_kwargs(OrderedDict(), batch, batch_idx)

        self.batch_progress.increment_ready()

        self.trainer._logger_connector.on_batch_start(batch, batch_idx)

        batch_output: _BATCH_OUTPUTS_TYPE = None  # for mypy
        if batch is None:
            self._warning_cache.warn("train_dataloader yielded None. If this was on purpose, ignore this warning...")
        else:
            # hook
            self.trainer._call_callback_hooks("on_train_batch_start", batch, batch_idx)
            response = self.trainer._call_lightning_module_hook("on_train_batch_start", batch, batch_idx)
            self.trainer._call_strategy_hook("on_train_batch_start", batch, batch_idx)
            if response == -1:
                self.batch_progress.increment_processed()
                raise StopIteration

            self.batch_progress.increment_started()

            with self.trainer.profiler.profile("run_training_batch"):
                if self.trainer.lightning_module.automatic_optimization:
                    # in automatic optimization, there can only be one optimizer
                    batch_output = self.optimizer_loop.run(self.trainer.optimizers[0], kwargs)
                else:
                    batch_output = self.manual_loop.run(kwargs)

        self.batch_progress.increment_processed()

        # update non-plateau LR schedulers
        # update epoch-interval ones only when we are at the end of training epoch
        self.update_lr_schedulers("step", update_plateau_schedulers=False)
        if self._num_ready_batches_reached():
            self.update_lr_schedulers("epoch", update_plateau_schedulers=False)

        self.trainer._call_callback_hooks("on_train_batch_end", batch_output, batch, batch_idx)
        self.trainer._call_lightning_module_hook("on_train_batch_end", batch_output, batch, batch_idx)
        self.trainer._logger_connector.on_batch_end()

        self.batch_progress.increment_completed()

        if batch_output and is_overridden("training_epoch_end", self.trainer.lightning_module):
            # batch_output may be empty
            # automatic: can be empty if all optimizers skip their batches
            # manual: #9052 added support for raising `StopIteration` in the `training_step`. If that happens,
            # then `advance` doesn't finish and an empty dict is returned
            self._outputs.append(batch_output)

        # -----------------------------------------
        # SAVE METRICS TO LOGGERS AND PROGRESS_BAR
        # -----------------------------------------
        self.trainer._logger_connector.update_train_step_metrics()

    def on_advance_end(self) -> None:
        # -----------------------------------------
        # VALIDATE IF NEEDED
        # -----------------------------------------
        should_check_val = self._should_check_val_fx()
        if should_check_val:
            self.trainer.validating = True
            self._run_validation()
            self.trainer.training = True

        # update plateau LR scheduler after metrics are logged
        self.update_lr_schedulers("step", update_plateau_schedulers=True)

        if not self._should_accumulate():
            # this is increased once per batch disregarding multiple optimizers on purpose for loggers
            self._batches_that_stepped += 1
        # this will save based on the `batches_that_stepped` value
        self._save_loggers_on_train_batch_end()

        # if training finished, defer exit to the parent. this assumes there will be enough time in between
        # which might not be the case depending on what's in the `*_epoch_end` hooks
        if not self._is_training_done and self.trainer.received_sigterm:
            raise SIGTERMException

    def on_run_end(self) -> _OUTPUTS_TYPE:
        outputs, self._outputs = self._outputs, []
        return outputs

    def teardown(self) -> None:
        self._results.cpu()
        self.val_loop.teardown()

    def on_save_checkpoint(self) -> Dict:
        state_dict = super().on_save_checkpoint()
        state_dict["_batches_that_stepped"] = self._batches_that_stepped
        return state_dict

    def on_load_checkpoint(self, state_dict: Dict) -> None:
        self._batches_that_stepped = state_dict.get("_batches_that_stepped", 0)

    def _run_validation(self) -> None:
        # reload dataloaders
        self.val_loop._reload_evaluation_dataloaders()

        with torch.no_grad():
            self.val_loop.run()

    def _accumulated_batches_reached(self) -> bool:
        """Determine if accumulation will be finished by the end of the current batch."""
        return self.batch_progress.current.ready % self.trainer.accumulate_grad_batches == 0

    def _num_ready_batches_reached(self) -> bool:
        """Checks if we are in the last batch or if there are more batches to follow."""
        epoch_finished_on_ready = self.batch_progress.current.ready == self.trainer.num_training_batches
        return epoch_finished_on_ready or self.batch_progress.is_last_batch

    def _num_completed_batches_reached(self) -> bool:
        epoch_finished_on_completed = self.batch_progress.current.completed == self.trainer.num_training_batches
        dataloader_consumed_successfully = self.batch_progress.is_last_batch and self._has_completed()
        return epoch_finished_on_completed or dataloader_consumed_successfully

    def _has_completed(self) -> bool:
        return self.batch_progress.current.ready == self.batch_progress.current.completed

    def _should_accumulate(self) -> bool:
        """Checks if the optimizer step should be performed or gradients should be accumulated for the current
        step."""
        accumulation_done = self._accumulated_batches_reached()
        # Lightning steps on the final batch
        is_final_batch = self._num_ready_batches_reached()
        # but the strategy might not
        strategy_accumulates_on_final_batch = self.trainer.strategy.handles_gradient_accumulation or not is_final_batch
        return not accumulation_done and strategy_accumulates_on_final_batch

    def update_lr_schedulers(self, interval: str, update_plateau_schedulers: bool) -> None:
        """updates the lr schedulers based on the given interval."""
        if interval == "step" and self._should_accumulate():
            return
        self._update_learning_rates(interval=interval, update_plateau_schedulers=update_plateau_schedulers)

    def _update_learning_rates(self, interval: str, update_plateau_schedulers: bool) -> None:
        """Update learning rates.

        Args:
            interval: either 'epoch' or 'step'.
            update_plateau_schedulers: control whether ``ReduceLROnPlateau`` or non-plateau schedulers get updated.
                This is used so non-plateau schedulers can be updated before running validation. Checkpoints are
                commonly saved during validation, however, on-plateau schedulers might monitor a validation metric
                so they have to be updated separately.
        """
        if not self.trainer.lr_scheduler_configs or not self.trainer.lightning_module.automatic_optimization:
            return

        for config in self.trainer.lr_scheduler_configs:
            if update_plateau_schedulers ^ config.reduce_on_plateau:
                continue

            current_idx = self.batch_idx if interval == "step" else self.trainer.current_epoch
            current_idx += 1  # account for both batch and epoch starts from 0
            # Take step if call to update_learning_rates matches the interval key and
            # the current step modulo the schedulers frequency is zero
            if config.interval == interval and current_idx % config.frequency == 0:
                monitor_val = None
                if config.reduce_on_plateau:
                    monitor_key = config.monitor
                    assert monitor_key is not None
                    monitor_val = self._get_monitor_value(monitor_key)
                    if monitor_val is None:
                        if config.strict:
                            avail_metrics = list(self.trainer.callback_metrics)
                            raise MisconfigurationException(
                                f"ReduceLROnPlateau conditioned on metric {monitor_key}"
                                f" which is not available. Available metrics are: {avail_metrics}."
                                " Condition can be set using `monitor` key in lr scheduler dict"
                            )
                        rank_zero_warn(
                            f"ReduceLROnPlateau conditioned on metric {monitor_key}"
                            " which is not available but strict is set to `False`."
                            " Skipping learning rate update.",
                            category=RuntimeWarning,
                        )
                        continue

                self.scheduler_progress.increment_ready()

                # update LR
                self.trainer._call_lightning_module_hook(
                    "lr_scheduler_step",
                    config.scheduler,
                    monitor_val,
                )
                self.scheduler_progress.increment_completed()

    def _get_monitor_value(self, key: str) -> Optional[Any]:
        # this is a separate method to aid in testing
        return self.trainer.callback_metrics.get(key)

    def _should_check_val_epoch(self) -> bool:
        return self.trainer.enable_validation and (
            self.trainer.check_val_every_n_epoch is None
            or (self.trainer.current_epoch + 1) % self.trainer.check_val_every_n_epoch == 0
        )

    def _should_check_val_fx(self) -> bool:
        """Decide if we should run validation."""
        if not self._should_check_val_epoch():
            return False

        # val_check_batch is inf for iterable datasets with no length defined
        is_infinite_dataset = self.trainer.val_check_batch == float("inf")
        is_last_batch = self.batch_progress.is_last_batch
        if is_last_batch and is_infinite_dataset:
            return True

        if self.trainer.should_stop:
            return True

        # TODO: let training/eval loop handle logic around limit_*_batches and val_check_batch
        is_val_check_batch = is_last_batch
        if isinstance(self.trainer.limit_train_batches, int) and is_infinite_dataset:
            is_val_check_batch = (self.batch_idx + 1) % self.trainer.limit_train_batches == 0
        elif self.trainer.val_check_batch != float("inf"):
            # if `check_val_every_n_epoch is `None`, run a validation loop every n training batches
            # else condition it based on the batch_idx of the current epoch
            current_iteration = self.total_batch_idx if self.trainer.check_val_every_n_epoch is None else self.batch_idx
            is_val_check_batch = (current_iteration + 1) % self.trainer.val_check_batch == 0

        return is_val_check_batch

    def _save_loggers_on_train_batch_end(self) -> None:
        """Flushes loggers to disk."""
        if self.trainer.should_stop:
            for logger in self.trainer.loggers:
                logger.save()

    def _build_kwargs(self, kwargs: OrderedDict, batch: Any, batch_idx: int) -> OrderedDict:
        """Helper method to build the arguments for the current step.

        Args:
            kwargs: The kwargs passed down to the hooks.
            batch: The current batch to run through the step.
            batch_idx: The current batch idx.

        Returns:
            The kwargs passed down to the hooks.
        """
        kwargs["batch"] = batch
        training_step_fx = getattr(self.trainer.lightning_module, "training_step")
        # the `batch_idx` is optional, but its name can be anything
        # as long as there are two argumetns after 'self', we assume they are the `batch` and `batch_idx`
        if is_param_in_hook_signature(training_step_fx, "batch_idx", min_args=2):
            kwargs["batch_idx"] = batch_idx
        return kwargs
