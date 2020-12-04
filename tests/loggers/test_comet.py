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
from unittest.mock import patch

import pytest
import matplotlib.pyplot as plt

from pytorch_lightning import Trainer
from pytorch_lightning.loggers import CometLogger
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.base import EvalModelTemplate
import tests.base.plotting


def _patch_comet_atexit(monkeypatch):
    """ Prevent comet logger from trying to print at exit, since pytest's stdout/stderr redirection breaks it. """
    import atexit
    monkeypatch.setattr(atexit, "register", lambda _: None)


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_logger_online(comet):
    """Test comet online with mocks."""
    # Test api_key given
    with patch('pytorch_lightning.loggers.comet.CometExperiment') as comet_experiment:
        logger = CometLogger(api_key='key', workspace='dummy-test', project_name='general')

        _ = logger.experiment

        comet_experiment.assert_called_once_with(api_key='key', workspace='dummy-test', project_name='general')

    # Test both given
    with patch('pytorch_lightning.loggers.comet.CometExperiment') as comet_experiment:
        logger = CometLogger(save_dir='test', api_key='key', workspace='dummy-test', project_name='general')

        _ = logger.experiment

        comet_experiment.assert_called_once_with(api_key='key', workspace='dummy-test', project_name='general')

    # Test already exists
    with patch('pytorch_lightning.loggers.comet.CometExistingExperiment') as comet_existing:
        logger = CometLogger(
            experiment_key='test',
            experiment_name='experiment',
            api_key='key',
            workspace='dummy-test',
            project_name='general',
        )

        _ = logger.experiment

        comet_existing.assert_called_once_with(
            api_key='key', workspace='dummy-test', project_name='general', previous_experiment='test'
        )

        comet_existing().set_name.assert_called_once_with('experiment')

    with patch('pytorch_lightning.loggers.comet.API') as api:
        CometLogger(api_key='key', workspace='dummy-test', project_name='general', rest_api_key='rest')

        api.assert_called_once_with('rest')


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_logger_no_api_key_given(comet):
    """ Test that CometLogger fails to initialize if both api key and save_dir are missing. """
    with pytest.raises(MisconfigurationException):
        comet.config.get_api_key.return_value = None
        CometLogger(workspace='dummy-test', project_name='general')


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_logger_experiment_name(comet):
    """Test that Comet Logger experiment name works correctly."""

    api_key = "key"
    experiment_name = "My Name"

    # Test api_key given
    with patch('pytorch_lightning.loggers.comet.CometExperiment') as comet_experiment:
        logger = CometLogger(api_key=api_key, experiment_name=experiment_name,)

        assert logger._experiment is None

        _ = logger.experiment

        comet_experiment.assert_called_once_with(api_key=api_key, project_name=None)

        comet_experiment().set_name.assert_called_once_with(experiment_name)


@patch('pytorch_lightning.loggers.comet.CometOfflineExperiment')
@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_logger_dirs_creation(comet, comet_experiment, tmpdir, monkeypatch):
    """ Test that the logger creates the folders and files in the right place. """
    _patch_comet_atexit(monkeypatch)

    comet.config.get_api_key.return_value = None
    comet.generate_guid.return_value = "4321"

    logger = CometLogger(project_name='test', save_dir=tmpdir)
    assert not os.listdir(tmpdir)
    assert logger.mode == 'offline'
    assert logger.save_dir == tmpdir
    assert logger.name == 'test'
    assert logger.version == "4321"

    _ = logger.experiment

    comet_experiment.assert_called_once_with(offline_directory=tmpdir, project_name='test')

    # mock return values of experiment
    logger.experiment.id = '1'
    logger.experiment.project_name = 'test'

    model = EvalModelTemplate()
    trainer = Trainer(default_root_dir=tmpdir, logger=logger, max_epochs=1, limit_val_batches=3)
    trainer.fit(model)

    assert trainer.checkpoint_callback.dirpath == (tmpdir / 'test' / "1" / 'checkpoints')
    assert set(os.listdir(trainer.checkpoint_callback.dirpath)) == {'epoch=0.ckpt'}


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_name_default(comet):
    """ Test that CometLogger.name don't create an Experiment and returns a default value. """

    api_key = "key"

    with patch('pytorch_lightning.loggers.comet.CometExperiment'):
        logger = CometLogger(api_key=api_key)

        assert logger._experiment is None

        assert logger.name == "comet-default"

        assert logger._experiment is None


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_name_project_name(comet):
    """ Test that CometLogger.name does not create an Experiment and returns project name if passed. """

    api_key = "key"
    project_name = "My Project Name"

    with patch('pytorch_lightning.loggers.comet.CometExperiment'):
        logger = CometLogger(api_key=api_key, project_name=project_name)

        assert logger._experiment is None

        assert logger.name == project_name

        assert logger._experiment is None


@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_version_without_experiment(comet):
    """ Test that CometLogger.version does not create an Experiment. """

    api_key = "key"
    experiment_name = "My Name"
    comet.generate_guid.return_value = "1234"

    with patch('pytorch_lightning.loggers.comet.CometExperiment'):
        logger = CometLogger(api_key=api_key, experiment_name=experiment_name)

        assert logger._experiment is None

        first_version = logger.version
        assert first_version is not None

        assert logger.version == first_version

        assert logger._experiment is None

        _ = logger.experiment

        logger.reset_experiment()

        second_version = logger.version == "1234"
        assert second_version is not None
        assert second_version != first_version


@patch("pytorch_lightning.loggers.comet.CometExperiment")
@patch('pytorch_lightning.loggers.comet.comet_ml')
def test_comet_epoch_logging(comet, comet_experiment, tmpdir, monkeypatch):
    """ Test that CometLogger removes the epoch key from the metrics dict and passes it as argument. """
    _patch_comet_atexit(monkeypatch)
    logger = CometLogger(project_name="test", save_dir=tmpdir)
    logger.log_metrics({"test": 1, "epoch": 1}, step=123)
    logger.experiment.log_metrics.assert_called_once_with({"test": 1}, epoch=1, step=123)


@patch("pytorch_lightning.loggers.comet.CometExperiment")
@patch('pytorch_lightning.loggers.comet.comet_ml')
@patch('matplotlib.pyplot.close')
@pytest.mark.parametrize("close", [True, False])
def test_comet_log_figure(comet, comet_experiment, tmpdir, monkeypatch, close):

    _patch_comet_atexit(monkeypatch)
    logger = CometLogger(project_name="test", save_dir=tmpdir)
    f = tests.base.plotting.dummy_figure()
    logger.log_figure("dummy", f, step=123, close=close)
    logger.experiment.log_figure.assert_called_once_with(figure_name="dummy", figure=f, step=123)

    if close:
        plt.close.assert_called_once_with(f)
    else:
        plt.close.assert_not_called()
