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
import os
import signal
from time import sleep
from unittest import mock

import pytest

from pytorch_lightning import Trainer
from pytorch_lightning.trainer.connectors.signal_connector import SignalConnector
from pytorch_lightning.utilities.exceptions import ExitGracefullyException
from tests.helpers import BoringModel
from tests.helpers.runif import RunIf


@pytest.mark.parametrize("register_handler", [False, True])
@pytest.mark.parametrize("terminate_gracefully", [False, True])
@RunIf(min_torch="1.7.0", skip_windows=True)
def test_fault_tolerant_sig_handler(register_handler, terminate_gracefully, tmpdir):

    # hack to reset the signal
    signal.signal(signal.SIGUSR1, 0)

    if register_handler:

        def handler(*_):
            pass

        signal.signal(signal.SIGUSR1, handler)

    class TestModel(BoringModel):
        def training_step(self, batch, batch_idx):
            if terminate_gracefully or register_handler:
                os.kill(os.getpid(), signal.SIGUSR1)
                sleep(0.1)
            return super().training_step(batch, batch_idx)

    model = TestModel()

    with mock.patch.dict(os.environ, {"PL_FAULT_TOLERANT_TRAINING": str(int(terminate_gracefully))}):

        trainer = Trainer(default_root_dir=tmpdir, max_epochs=1, limit_train_batches=2, limit_val_batches=0)
        if terminate_gracefully and not register_handler:
            with pytest.raises(ExitGracefullyException):
                trainer.fit(model)
        else:
            trainer.fit(model)
        assert trainer._terminate_gracefully == (False if register_handler else terminate_gracefully)


def signal_handler():
    pass


class SignalHandlers:
    def signal_handler(self):
        pass


@pytest.mark.parametrize(
    ["handler", "expected_return"],
    [
        (signal.Handlers.SIG_DFL, False),
        (signal_handler, True),
        (SignalHandlers().signal_handler, True),
    ],
)
def test_has_already_handler(handler, expected_return):
    """Test that the SignalConnector detects whether a signal handler is already attached."""
    trainer = Trainer()
    connector = SignalConnector(trainer)
    with mock.patch("signal.getsignal", return_value=handler):
        assert connector._has_already_handler(signal.SIGTERM) is expected_return
