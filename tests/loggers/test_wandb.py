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
import pickle
from unittest import mock

import pytest
import matplotlib.pyplot as plt

from pytorch_lightning import Trainer
from pytorch_lightning.loggers import WandbLogger
from tests.base import EvalModelTemplate
import tests.base.plotting


@mock.patch('pytorch_lightning.loggers.wandb.wandb')
def test_wandb_logger(wandb):
    """Verify that basic functionality of wandb logger works.
    Wandb doesn't work well with pytest so we have to mock it out here."""
    logger = WandbLogger(anonymous=True, offline=True)

    logger.log_metrics({'acc': 1.0})
    wandb.init().log.assert_called_once_with({'acc': 1.0})

    wandb.init().log.reset_mock()
    logger.log_metrics({'acc': 1.0}, step=3)
    wandb.init().log.assert_called_once_with({'global_step': 3, 'acc': 1.0})

    logger.log_hyperparams({'test': None, 'nested': {'a': 1}, 'b': [2, 3, 4]})
    wandb.init().config.update.assert_called_once_with(
        {'test': 'None', 'nested/a': 1, 'b': [2, 3, 4]},
        allow_val_change=True,
    )

    logger.watch('model', 'log', 10)
    wandb.init().watch.assert_called_once_with('model', log='log', log_freq=10)

    assert logger.name == wandb.init().project_name()
    assert logger.version == wandb.init().id


@mock.patch('pytorch_lightning.loggers.wandb.wandb')
@pytest.mark.parametrize("close", [True, False])
def test_wandb_logger_log_figure(wandb, close):
    logger = WandbLogger(anonymous=True, offline=True)
    f = tests.base.plotting.dummy_figure()

    with mock.patch('matplotlib.pyplot.close') as plt_close:
        logger.log_figure("dummy", f, step=123, close=close)
    logger.experiment.log.assert_called_once_with({"dummy": wandb.Image(f)}, step=123)

    if close:
        plt_close.assert_called_once_with(f)
    else:
        plt_close.assert_not_called()


@mock.patch('pytorch_lightning.loggers.wandb.wandb')
def test_wandb_pickle(wandb, tmpdir):
    """
    Verify that pickling trainer with wandb logger works.
    Wandb doesn't work well with pytest so we have to mock it out here.
    """
    class Experiment:
        """ """
        id = 'the_id'

        def project_name(self):
            return 'the_project_name'

    wandb.init.return_value = Experiment()
    logger = WandbLogger(id='the_id', offline=True)

    trainer = Trainer(
        default_root_dir=tmpdir,
        max_epochs=1,
        logger=logger,
    )
    # Access the experiment to ensure it's created
    assert trainer.logger.experiment, 'missing experiment'
    pkl_bytes = pickle.dumps(trainer)
    trainer2 = pickle.loads(pkl_bytes)

    assert os.environ['WANDB_MODE'] == 'dryrun'
    assert trainer2.logger.__class__.__name__ == WandbLogger.__name__
    assert trainer2.logger.experiment, 'missing experiment'

    wandb.init.assert_called()
    assert 'id' in wandb.init.call_args[1]
    assert wandb.init.call_args[1]['id'] == 'the_id'

    del os.environ['WANDB_MODE']


@mock.patch('pytorch_lightning.loggers.wandb.wandb')
def test_wandb_logger_dirs_creation(wandb, tmpdir):
    """ Test that the logger creates the folders and files in the right place. """
    logger = WandbLogger(save_dir=str(tmpdir), offline=True)
    assert logger.version is None
    assert logger.name is None

    # mock return values of experiment
    logger.experiment.id = '1'
    logger.experiment.project_name.return_value = 'project'

    for _ in range(2):
        _ = logger.experiment

    assert logger.version == '1'
    assert logger.name == 'project'
    assert str(tmpdir) == logger.save_dir
    assert not os.listdir(tmpdir)

    version = logger.version
    model = EvalModelTemplate()
    trainer = Trainer(default_root_dir=tmpdir, logger=logger, max_epochs=1, limit_val_batches=3)
    trainer.fit(model)

    assert trainer.checkpoint_callback.dirpath == str(tmpdir / 'project' / version / 'checkpoints')
    assert set(os.listdir(trainer.checkpoint_callback.dirpath)) == {'epoch=0.ckpt'}
