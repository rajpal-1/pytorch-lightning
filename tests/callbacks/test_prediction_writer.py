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
from unittest.mock import Mock, call, ANY

import pytest
from torch.utils.data import DataLoader

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import BasePredictionWriter
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers import BoringModel, RandomDataset


class DummyPredictionWriter(BasePredictionWriter):
    def write_on_batch_end(self, *args, **kwargs):
        pass

    def write_on_epoch_end(self, *args, **kwargs):
        print(*args, **kwargs)


def test_prediction_writer_invalid_write_interval():
    with pytest.raises(MisconfigurationException, match=r"`write_interval` should be one of \['batch"):
        DummyPredictionWriter("something")


def test_prediction_writer_hook_call_intervals(tmpdir):
    DummyPredictionWriter.write_on_batch_end = Mock()
    DummyPredictionWriter.write_on_epoch_end = Mock()

    dataloader = DataLoader(RandomDataset(32, 64))

    model = BoringModel()
    cb = DummyPredictionWriter("batch_and_epoch")
    trainer = Trainer(limit_predict_batches=4, callbacks=cb)
    results = trainer.predict(model, dataloaders=dataloader)
    assert len(results) == 4
    assert cb.write_on_batch_end.call_count == 4
    assert cb.write_on_epoch_end.call_count == 1

    DummyPredictionWriter.write_on_batch_end.reset_mock()
    DummyPredictionWriter.write_on_epoch_end.reset_mock()

    cb = DummyPredictionWriter("batch_and_epoch")
    trainer = Trainer(limit_predict_batches=4, callbacks=cb)
    trainer.predict(model, dataloaders=dataloader, return_predictions=False)
    assert cb.write_on_batch_end.call_count == 4
    assert cb.write_on_epoch_end.call_count == 1

    DummyPredictionWriter.write_on_batch_end.reset_mock()
    DummyPredictionWriter.write_on_epoch_end.reset_mock()

    cb = DummyPredictionWriter("batch")
    trainer = Trainer(limit_predict_batches=4, callbacks=cb)
    trainer.predict(model, dataloaders=dataloader, return_predictions=False)
    assert cb.write_on_batch_end.call_count == 4
    assert cb.write_on_epoch_end.call_count == 0

    DummyPredictionWriter.write_on_batch_end.reset_mock()
    DummyPredictionWriter.write_on_epoch_end.reset_mock()

    cb = DummyPredictionWriter("epoch")
    trainer = Trainer(limit_predict_batches=4, callbacks=cb)
    trainer.predict(model, dataloaders=dataloader, return_predictions=False)
    assert cb.write_on_batch_end.call_count == 0
    assert cb.write_on_epoch_end.call_count == 1


def test_prediction_writer_batch_indices(tmpdir):
    DummyPredictionWriter.write_on_batch_end = Mock()
    DummyPredictionWriter.write_on_epoch_end = Mock()

    dataloader = DataLoader(RandomDataset(32, 64), batch_size=4, num_workers=2)
    model = BoringModel()
    writer = DummyPredictionWriter("batch_and_epoch")
    trainer = Trainer(limit_predict_batches=4, callbacks=writer)
    trainer.predict(model, dataloaders=dataloader)

    writer.write_on_batch_end.assert_has_calls(
        [
            call(trainer, model, ANY, [[0, 1, 2, 3]], ANY, 0, 0),
            call(trainer, model, ANY, [[4, 5, 6, 7]], ANY, 1, 0),
            call(trainer, model, ANY, [[8, 9, 10, 11]], ANY, 2, 0),
            call(trainer, model, ANY, [[12, 13, 14, 15]], ANY, 3, 0),
        ]
    )

    writer.write_on_epoch_end.assert_has_calls(
        [
            call(trainer, model, ANY, [[[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 15]]]),
        ]
    )
