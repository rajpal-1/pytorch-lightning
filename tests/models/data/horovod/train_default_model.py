"""
This script is meant to be executed from `../../test_horovod.py`.

Because Horovod uses a parallel programming model similar to MPI, unit tests for collective
ops like allreduce need to be run in parallel. The most common approach for running parallel
Horovod workers is to launch multiple replicas of the training script via the `horovodrun`
command-line tool:

.. code-block:: bash

    horovodrun -np 2 python train_default_model.py ...

Individual test parameters are configured by the serialized `--trainer-options` JSON object.

An non-zero exit code from this script on any rank will indicate failure, while a zero exit code
across all ranks indicates success.
"""

import argparse
import json
import os
import sys


try:
    import horovod.torch as hvd
except (ModuleNotFoundError, ImportError):
    print('You requested to import Horovod which is missing or not supported for your OS.')

PATH_HERE = os.path.abspath(os.path.dirname(__file__))
PATH_ROOT = os.path.abspath(os.path.join(PATH_HERE, '..', '..', '..', '..'))
sys.path.insert(0, os.path.abspath(PATH_ROOT))

from pytorch_lightning import Trainer  # noqa: E402
from pytorch_lightning.callbacks import ModelCheckpoint  # noqa: E402

# Move project root to the front of the search path, as some imports
# may have reordered things
idx = sys.path.index(PATH_ROOT)
sys.path[0], sys.path[idx] = sys.path[idx], sys.path[0]

from tests.base import EvalModelTemplate  # noqa: E402
from tests.base.develop_pipelines import run_prediction  # noqa: E402
from tests.base.develop_utils import set_random_master_port, reset_seed  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument('--trainer-options', required=True)
parser.add_argument('--on-gpu', action='store_true', default=False)


def run_test_from_config(trainer_options):
    """Trains the default model with the given config."""
    set_random_master_port()
    reset_seed()

    ckpt_path = trainer_options['weights_save_path']
    trainer_options.update(checkpoint_callback=ModelCheckpoint(ckpt_path))

    model = EvalModelTemplate()

    trainer = Trainer(**trainer_options)
    result = trainer.fit(model)
    assert result == 1

    # Horovod should be initialized following training. If not, this will raise an exception.
    assert hvd.size() == 2

    if trainer.global_rank > 0:
        # on higher ranks the checkpoint location is unknown
        # we want to test checkpointing on rank 0 only
        assert not trainer.checkpoint_callback.best_model_path
        return

    # test model loading
    pretrained_model = EvalModelTemplate.load_from_checkpoint(trainer.checkpoint_callback.best_model_path)

    # test new model accuracy
    test_loaders = model.test_dataloader()
    if not isinstance(test_loaders, list):
        test_loaders = [test_loaders]

    for dataloader in test_loaders:
        run_prediction(dataloader, pretrained_model)

    # test HPC loading / saving
    trainer.checkpoint_connector.hpc_save(ckpt_path, trainer.logger)
    trainer.checkpoint_connector.hpc_load(ckpt_path, on_gpu=args.on_gpu)

    if args.on_gpu:
        trainer = Trainer(gpus=1, distributed_backend='horovod', max_epochs=1)
        # Test the root_gpu property
        assert trainer.root_gpu == hvd.local_rank()


if __name__ == "__main__":
    args = parser.parse_args()
    run_test_from_config(json.loads(args.trainer_options))
