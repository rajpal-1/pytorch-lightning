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
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Dict, Optional, Union

import torch
from torch import Tensor
from torch.optim import Optimizer
from typing_extensions import OrderedDict

from pytorch_lightning.loops.loop import _Loop
from pytorch_lightning.accelerators import TPUAccelerator
from pytorch_lightning.core.optimizer import LightningOptimizer
from pytorch_lightning.loops.optimization.closure import AbstractClosure, OutputResult
from pytorch_lightning.loops.progress import OptimizationProgress
from pytorch_lightning.loops.utilities import _block_parallel_sync_behavior
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.rank_zero import WarningCache
from pytorch_lightning.utilities.types import STEP_OUTPUT


@dataclass
class ClosureResult(OutputResult):
    """A container to hold the result of a :class:`Closure` call.

    It is created from the output of :meth:`~pytorch_lightning.core.module.LightningModule.training_step`.

    Attributes:
        closure_loss: The loss with a graph attached.
        loss: A detached copy of the closure loss.
        extra: Any keys other than the loss returned.
    """

    closure_loss: Optional[Tensor]
    loss: Optional[Tensor] = field(init=False, default=None)
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._clone_loss()

    def _clone_loss(self) -> None:
        if self.closure_loss is not None:
            # the loss will get scaled for amp. avoid any modifications to it
            self.loss = self.closure_loss.detach().clone()

    @classmethod
    def from_training_step_output(
        cls, training_step_output: Optional[STEP_OUTPUT], normalize: int = 1
    ) -> "ClosureResult":
        closure_loss, extra = None, {}

        if isinstance(training_step_output, dict):
            # this should not modify the `training_step_output`, as the user could be using it after `training_step_end`
            closure_loss = training_step_output.get("loss")
            if closure_loss is None:
                raise MisconfigurationException(
                    "In automatic_optimization, when `training_step` returns a dict, the 'loss' key needs to be present"
                )
            extra = {k: v for k, v in training_step_output.items() if k != "loss"}
        elif isinstance(training_step_output, Tensor):
            closure_loss = training_step_output
        elif training_step_output is not None:
            raise MisconfigurationException(
                "In automatic optimization, `training_step` must return a Tensor, "
                "a dict, or None (where the step will be skipped)."
            )

        if closure_loss is not None:
            # accumulate the loss. If ``accumulate_grad_batches == 1``, no effect
            # note: avoid in-place operation `x /= y` here on purpose
            closure_loss = closure_loss / normalize

        return cls(closure_loss, extra=extra)

    def asdict(self) -> Dict[str, Any]:
        return {"loss": self.loss, **self.extra}


class Closure(AbstractClosure[ClosureResult]):
    """An implementation of a :class:`AbstractClosure` for automatic optimization in Lightning that combines three
    elementary closures into one: ``training_step``, ``backward`` and ``zero_grad``.

    The Closure gets created by the training loop(s) and is then passed to the
    :meth:`torch.optim.Optimizer.step` method. An optimizer is responsible for calling the closure and optionally
    do something with the output.

    Args:
        step_fn: This is typically the :meth:`pytorch_lightning.core.module.LightningModule.training_step
            wrapped with processing for its outputs
        backward_fn: A function that takes a loss value as input, performs back-propagation and returns the loss value.
            Can be set to ``None`` to skip the backward operation.
        zero_grad_fn: A function that zeroes the gradients. Can be set to ``None`` to skip zero_grad, for example
            when accumulating gradients.

    Example:

        closure = Closure()
        optimizer = torch.optim.Adam(...)
        optimizer.step(closure)
    """

    warning_cache = WarningCache()

    def __init__(
        self,
        step_fn: Callable[[], ClosureResult],
        backward_fn: Optional[Callable[[Tensor], None]] = None,
        zero_grad_fn: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self._step_fn = step_fn
        self._backward_fn = backward_fn
        self._zero_grad_fn = zero_grad_fn

    def closure(self, *args: Any, **kwargs: Any) -> ClosureResult:
        step_output = self._step_fn()

        if step_output.closure_loss is None:
            self.warning_cache.warn("`training_step` returned `None`. If this was on purpose, ignore this warning...")

        if self._zero_grad_fn is not None:
            self._zero_grad_fn()

        if self._backward_fn is not None and step_output.closure_loss is not None:
            self._backward_fn(step_output.closure_loss)

        return step_output

    def __call__(self, *args: Any, **kwargs: Any) -> Optional[Tensor]:
        self._result = self.closure(*args, **kwargs)
        return self._result.loss


_OUTPUTS_TYPE = Dict[str, Any]


class _AutomaticOptimization(_Loop):
    """Performs automatic optimization (forward, zero grad, backward, optimizer step)"""

    output_result_cls = ClosureResult

    def __init__(self) -> None:
        super().__init__()
        self.optim_progress: OptimizationProgress = OptimizationProgress()
        self._skip_backward: bool = False

    def run(self, optimizer: Optimizer, kwargs: OrderedDict) -> _OUTPUTS_TYPE:
        """Runs closure (train step + backward) together with optimization if necessary.

        Args:
            kwargs: the kwargs passed down to the hooks
            optimizer: the optimizer
        """
        closure = self._make_closure(kwargs, optimizer)

        if (
            # when the strategy handles accumulation, we want to always call the optimizer step
            not self.trainer.strategy.handles_gradient_accumulation
            and self.trainer.fit_loop._should_accumulate()
        ):
            # For gradient accumulation

            # -------------------
            # calculate loss (train step + train step end)
            # -------------------
            # automatic_optimization=True: perform ddp sync only when performing optimizer_step
            with _block_parallel_sync_behavior(self.trainer.strategy, block=True):
                closure()

        # ------------------------------
        # BACKWARD PASS
        # ------------------------------
        # gradient update with accumulated gradients
        else:
            self._optimizer_step(optimizer, kwargs.get("batch_idx", 0), closure)

        result = closure.consume_result()
        return result.asdict()

    def _make_closure(self, kwargs: OrderedDict, optimizer: Optimizer) -> Closure:
        """Build a closure object that captures the given arguments and runs the `training_step` function and
        optionally other functions such as `backward` and `zero_grad`."""
        step_fn = self._make_step_fn(kwargs)
        backward_fn = self._make_backward_fn(optimizer)
        zero_grad_fn = self._make_zero_grad_fn(kwargs.get("batch_idx", 0), optimizer)
        return Closure(step_fn=step_fn, backward_fn=backward_fn, zero_grad_fn=zero_grad_fn)

    def _make_step_fn(self, kwargs: OrderedDict) -> Callable[[], ClosureResult]:
        """Build the step function that runs the `training_step` and processes its output."""
        return partial(self._training_step, kwargs)

    def _make_zero_grad_fn(self, batch_idx: int, optimizer: Optimizer) -> Optional[Callable[[], None]]:
        """Build a `zero_grad` function that zeroes the gradients before back-propagation.

        Returns ``None`` in the case backward needs to be skipped.
        """

        if self._skip_backward:
            return None

        is_first_batch_to_accumulate = batch_idx % self.trainer.accumulate_grad_batches == 0
        if not is_first_batch_to_accumulate:
            return None

        def zero_grad_fn() -> None:
            self._on_before_zero_grad(optimizer)
            self._optimizer_zero_grad(batch_idx, optimizer)

        return zero_grad_fn

    def _make_backward_fn(self, optimizer: Optimizer) -> Optional[Callable[[Tensor], None]]:
        """Build a `backward` function that handles back-propagation through the output produced by the
        `training_step` function.

        Returns ``None`` in the case backward needs to be skipped.
        """
        if self._skip_backward:
            return None

        def backward_fn(loss: Tensor) -> None:
            self.trainer._call_strategy_hook("backward", loss, optimizer)

        return backward_fn

    def _optimizer_step(
        self,
        optimizer: Union[Optimizer, LightningOptimizer],
        batch_idx: int,
        train_step_and_backward_closure: Callable[[], Optional[Tensor]],
    ) -> None:
        """Performs the optimizer step and some sanity checking.

        Args:
            optimizer: the optimizer to perform the step with
            batch_idx: the index of the current batch
            train_step_and_backward_closure: the closure function performing the train step and computing the
                gradients. By default, called by the optimizer (if possible)
        """
        is_lbfgs = isinstance(optimizer, torch.optim.LBFGS)

        # wraps into LightningOptimizer only for running step
        optimizer = self.trainer.strategy._lightning_optimizers[0]

        # if `strategy.handles_gradient_accumulation`, this method will be called to route into the strategy, but we
        # need to check again if `should_accumulate` before increasing the counters
        should_accumulate = self.trainer.fit_loop._should_accumulate()
        if not should_accumulate:
            self.optim_progress.optimizer.step.increment_ready()

        # model hook
        self.trainer._call_lightning_module_hook(
            "optimizer_step",
            self.trainer.current_epoch,
            batch_idx,
            optimizer,
            train_step_and_backward_closure,
            on_tpu=isinstance(self.trainer.accelerator, TPUAccelerator),
            using_lbfgs=is_lbfgs,
        )

        if not should_accumulate:
            self.optim_progress.optimizer.step.increment_completed()

    def _on_before_zero_grad(self, optimizer: torch.optim.Optimizer) -> None:
        """Calls the ``on_before_zero_grad`` hook.

        Args:
            optimizer: the current optimizer
        """
        self.optim_progress.optimizer.zero_grad.increment_ready()
        self.trainer._call_callback_hooks("on_before_zero_grad", optimizer)
        self.trainer._call_lightning_module_hook("on_before_zero_grad", optimizer)
        self.optim_progress.optimizer.zero_grad.increment_started()

    def _optimizer_zero_grad(self, batch_idx: int, optimizer: torch.optim.Optimizer) -> None:
        """Zeroes out all gradients of parameters optimized by the current optimizer.

        Args:
            batch_idx: the index of the current batch
            optimizer: the current optimizer
        """
        self.trainer._call_lightning_module_hook(
            "optimizer_zero_grad", self.trainer.current_epoch, batch_idx, optimizer
        )
        self.optim_progress.optimizer.zero_grad.increment_completed()

    def _training_step(self, kwargs: OrderedDict) -> ClosureResult:
        """Performs the actual train step with the tied hooks.

        Args:
            kwargs: the kwargs passed down to the hooks.

        Returns:
            A ``ClosureResult`` containing the training step output.
        """
        # manually capture logged metrics
        training_step_output = self.trainer._call_strategy_hook("training_step", *kwargs.values())
        self.trainer.strategy.post_training_step()

        model_output = self.trainer._call_lightning_module_hook("training_step_end", training_step_output)
        strategy_output = self.trainer._call_strategy_hook("training_step_end", training_step_output)
        training_step_output = strategy_output if model_output is None else model_output

        result = self.output_result_cls.from_training_step_output(
            training_step_output, self.trainer.accumulate_grad_batches
        )
        return result
