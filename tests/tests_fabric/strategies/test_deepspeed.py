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
import json
import os
from re import escape
from unittest import mock
from unittest.mock import ANY, Mock

import pytest
import torch
from tests_fabric.helpers.runif import RunIf

from lightning_fabric.accelerators import CPUAccelerator
from lightning_fabric.strategies import DeepSpeedStrategy


@pytest.fixture
def deepspeed_config():
    return {
        "optimizer": {"type": "SGD", "params": {"lr": 3e-5}},
        "scheduler": {
            "type": "WarmupLR",
            "params": {"last_batch_iteration": -1, "warmup_min_lr": 0, "warmup_max_lr": 3e-5, "warmup_num_steps": 100},
        },
    }


@pytest.fixture
def deepspeed_zero_config(deepspeed_config):
    return {**deepspeed_config, "zero_allow_untested_optimizer": True, "zero_optimization": {"stage": 2}}


@RunIf(deepspeed=True)
def test_deepspeed_only_compatible_with_cuda():
    """Test that the DeepSpeed strategy raises an exception if an invalid accelerator is used."""
    strategy = DeepSpeedStrategy(accelerator=CPUAccelerator())
    with pytest.raises(RuntimeError, match="The DeepSpeed strategy is only supported on CUDA GPUs"):
        strategy.setup_environment()


@RunIf(deepspeed=True)
def test_deepspeed_with_invalid_config_path():
    """Test to ensure if we pass an invalid config path we throw an exception."""

    with pytest.raises(
        FileNotFoundError, match="You passed in a path to a DeepSpeed config but the path does not exist"
    ):
        DeepSpeedStrategy(config="invalid_path.json")


@RunIf(deepspeed=True)
def test_deepspeed_with_env_path(tmpdir, monkeypatch, deepspeed_config):
    """Test to ensure if we pass an env variable, we load the config from the path."""
    config_path = os.path.join(tmpdir, "temp.json")
    with open(config_path, "w") as f:
        f.write(json.dumps(deepspeed_config))
    monkeypatch.setenv("PL_DEEPSPEED_CONFIG_PATH", config_path)
    strategy = DeepSpeedStrategy()
    assert strategy.config == deepspeed_config


@RunIf(deepspeed=True)
def test_deepspeed_defaults():
    """Ensure that defaults are correctly set as a config for DeepSpeed if no arguments are passed."""
    strategy = DeepSpeedStrategy()
    assert strategy.config is not None
    assert isinstance(strategy.config["zero_optimization"], dict)
    assert strategy._backward_sync_control is None


@RunIf(deepspeed=True)
def test_deepspeed_custom_activation_checkpointing_params(tmpdir):
    """Ensure if we modify the activation checkpointing parameters, the deepspeed config contains these changes."""
    ds = DeepSpeedStrategy(
        partition_activations=True,
        cpu_checkpointing=True,
        contiguous_memory_optimization=True,
        synchronize_checkpoint_boundary=True,
    )
    checkpoint_config = ds.config["activation_checkpointing"]
    assert checkpoint_config["partition_activations"]
    assert checkpoint_config["cpu_checkpointing"]
    assert checkpoint_config["contiguous_memory_optimization"]
    assert checkpoint_config["synchronize_checkpoint_boundary"]


@RunIf(deepspeed=True)
def test_deepspeed_config_zero_offload(deepspeed_zero_config):
    """Test the various ways optimizer-offloading can be configured."""

    # default config
    strategy = DeepSpeedStrategy(config=deepspeed_zero_config)
    assert "offload_optimizer" not in strategy.config["zero_optimization"]

    # default config
    strategy = DeepSpeedStrategy()
    assert "offload_optimizer" not in strategy.config["zero_optimization"]

    # default config with `offload_optimizer` argument override
    strategy = DeepSpeedStrategy(offload_optimizer=True)
    assert strategy.config["zero_optimization"]["offload_optimizer"] == {
        "buffer_count": 4,
        "device": "cpu",
        "nvme_path": "/local_nvme",
        "pin_memory": False,
    }

    # externally configured through config
    deepspeed_zero_config["zero_optimization"]["offload_optimizer"] = False
    strategy = DeepSpeedStrategy(config=deepspeed_zero_config)
    assert strategy.config["zero_optimization"]["offload_optimizer"] is False


@RunIf(deepspeed=True)
@mock.patch("deepspeed.initialize")
def test_deepspeed_setup_module(init_mock):
    """Test that the DeepSpeed strategy can set up the model for inference (no optimizer required)."""
    model = Mock()
    model.parameters.return_value = []
    strategy = DeepSpeedStrategy()
    strategy.parallel_devices = [torch.device("cuda", 1)]
    init_mock.return_value = [Mock()] * 4  # mock to make tuple unpacking work

    strategy.setup_module(model)
    init_mock.assert_called_with(
        args=ANY,
        config=strategy.config,
        model=model,
        model_parameters=ANY,
        optimizer=None,
        dist_init_required=False,
    )


@RunIf(deepspeed=True)
def test_deepspeed_requires_joint_setup():
    """Test that the DeepSpeed strategy does not support setting up model and optimizer independently."""
    strategy = DeepSpeedStrategy()
    with pytest.raises(
        NotImplementedError, match=escape("does not support setting up the module and optimizer(s) independently")
    ):
        strategy.setup_optimizer(Mock())


@RunIf(deepspeed=True)
def test_deepspeed_save_checkpoint_storage_options(tmp_path):
    """Test that the DeepSpeed strategy does not accept storage options for saving checkpoints."""
    strategy = DeepSpeedStrategy()
    with pytest.raises(TypeError, match=escape("DeepSpeedStrategy.save_checkpoint(..., storage_options=...)` is not")):
        strategy.save_checkpoint(path=tmp_path, state=Mock(), storage_options=Mock())


@RunIf(deepspeed=True)
def test_deepspeed_save_checkpoint_one_deepspeed_engine_required(tmp_path):
    """Test that the DeepSpeed strategy can only save one DeepSpeedEngine per checkpoint."""
    from deepspeed import DeepSpeedEngine

    strategy = DeepSpeedStrategy()

    # missing DeepSpeedEngine
    with pytest.raises(ValueError, match="Could not find a DeepSpeed model in the provided checkpoint state."):
        strategy.save_checkpoint(path=tmp_path, state={})
    with pytest.raises(ValueError, match="Could not find a DeepSpeed model in the provided checkpoint state."):
        strategy.save_checkpoint(path=tmp_path, state={"model": torch.nn.Linear(3, 3)})

    # multiple DeepSpeedEngine
    model1 = Mock(spec=torch.nn.Module)
    model1.modules.return_value = [Mock(spec=DeepSpeedEngine)]
    model2 = Mock(spec=torch.nn.Module)
    model2.modules.return_value = [Mock(spec=DeepSpeedEngine)]
    with pytest.raises(ValueError, match="Found multiple DeepSpeed engine modules in the given state."):
        strategy.save_checkpoint(path=tmp_path, state={"model1": model1, "model2": model2})


@RunIf(deepspeed=True)
def test_deepspeed_save_checkpoint_client_state_separation(tmp_path):
    """Test that the DeepSpeed engine and optimizer get separated from the client state."""
    from deepspeed import DeepSpeedEngine

    strategy = DeepSpeedStrategy()
    optimizer = Mock()
    model = Mock(spec=DeepSpeedEngine, optimizer=optimizer)
    model.modules.return_value = [model]
    strategy.save_checkpoint(path=tmp_path, state={"model": model, "optimizer": optimizer, "test": "data"})
    # the client_state should not contain any deepspeed engine or deepspeed optimizer
    model.save_checkpoint.assert_called_with(tmp_path, client_state={"test": "data"}, tag="checkpoint")
