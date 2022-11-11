import os
from typing import Any, Dict, Optional

import pytest
import torch

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.demos.boring_classes import BoringModel
from pytorch_lightning.plugins.precision.fsdp_native_native_amp import FullyShardedNativeNativeMixedPrecisionPlugin
from pytorch_lightning.strategies import DDPFullyShardedNativeStrategy
from pytorch_lightning.utilities.exceptions import _ValueError
from pytorch_lightning.utilities.imports import _TORCH_GREATER_EQUAL_1_12
from tests_pytorch.helpers.runif import RunIf

if _TORCH_GREATER_EQUAL_1_12:
    from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel, MixedPrecision
    from torch.distributed.fsdp.wrap import wrap


def custom_auto_wrap_policy(
    module,
    recurse,
    unwrapped_params: int,
    min_num_params: int = int(1e8),
) -> bool:
    return unwrapped_params >= 2


@RunIf(min_torch="1.12")
def test_invalid_on_cpu(tmpdir):
    """Test to ensure that we raise ValueError for Native FSDP on CPU."""
    with pytest.raises(
        _ValueError,
        match=f"You selected strategy to be `{DDPFullyShardedNativeStrategy.strategy_name}`, "
        "but GPU accelerator is not used.",
    ):
        trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True, strategy="fsdp_native")
        assert isinstance(trainer.strategy, DDPFullyShardedNativeStrategy)
        trainer.strategy.setup_environment()


@RunIf(min_torch="1.12", min_cuda_gpus=1)
@pytest.mark.parametrize("precision, expected", [(16, torch.float16), ("bf16", torch.bfloat16)])
def test_precision_plugin_config(precision, expected):
    plugin = FullyShardedNativeNativeMixedPrecisionPlugin(precision=precision, device="cuda")
    config = plugin.mixed_precision_config
    assert config.param_dtype == expected
    assert config.buffer_dtype == expected
    assert config.reduce_dtype == expected


@RunIf(min_torch="1.12")
def test_fsdp_custom_mixed_precision(tmpdir):
    """Test to ensure that passing a custom mixed precision config works."""
    config = MixedPrecision()
    strategy = DDPFullyShardedNativeStrategy(mixed_precision=config)
    assert strategy.mixed_precision_config == config


class TestFSDPModel(BoringModel):
    def __init__(self):
        super().__init__()
        self.layer: Optional[torch.nn.Module] = None

    def _init_model(self) -> None:
        self.layer = torch.nn.Sequential(torch.nn.Linear(32, 32), torch.nn.ReLU(), torch.nn.Linear(32, 2))

    def setup(self, stage: str) -> None:
        if self.layer is None:
            self._init_model()

    def configure_sharded_model(self) -> None:
        # the model is already wrapped with FSDP: no need to wrap again!
        if isinstance(self.layer, FullyShardedDataParallel):
            return
        for i, layer in enumerate(self.layer):
            if i % 2 == 0:
                self.layer[i] = wrap(layer)
        self.layer = wrap(self.layer)

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        # when loading full state dict, we first need to create a new unwrapped model
        self._init_model()

    def configure_optimizers(self):
        return torch.optim.SGD(self.layer.parameters(), lr=0.1)

    def on_train_batch_end(self, outputs, batch, batch_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_test_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_validation_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_predict_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def _assert_layer_fsdp_instance(self) -> None:
        assert isinstance(self.layer, FullyShardedDataParallel)
        assert isinstance(self.trainer.strategy.precision_plugin, FullyShardedNativeNativeMixedPrecisionPlugin)
        # root should not be resharding
        assert self.layer.reshard_after_forward is False

        precision = torch.float16 if self.precision == 16 else torch.bfloat16
        assert self.layer.mixed_precision.param_dtype == precision
        assert self.layer.mixed_precision.reduce_dtype == precision
        assert self.layer.mixed_precision.buffer_dtype == precision

        for layer_num in [0, 2]:
            assert isinstance(self.layer.module[layer_num], FullyShardedDataParallel)
            # Assert that the nested layers are set reshard_after_forward to True
            assert self.layer.module[layer_num].reshard_after_forward is True

            assert self.layer[layer_num].mixed_precision.param_dtype == precision
            assert self.layer[layer_num].mixed_precision.reduce_dtype == precision
            assert self.layer[layer_num].mixed_precision.buffer_dtype == precision


class TestFSDPModelAutoWrapped(BoringModel):
    def __init__(self):
        super().__init__()
        self.layer = torch.nn.Sequential(torch.nn.Linear(32, 32), torch.nn.ReLU(), torch.nn.Linear(32, 2))

    def configure_optimizers(self):
        return torch.optim.SGD(self.trainer.model.parameters(), lr=0.1)

    def on_train_batch_end(self, outputs, batch, batch_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_test_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_validation_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def on_predict_batch_end(self, outputs, batch, batch_idx, dataloader_idx) -> None:
        self._assert_layer_fsdp_instance()

    def _assert_layer_fsdp_instance(self) -> None:
        assert isinstance(self.layer, torch.nn.Sequential)
        assert isinstance(self.trainer.strategy.precision_plugin, FullyShardedNativeNativeMixedPrecisionPlugin)

        precision = torch.float16 if self.precision == 16 else torch.bfloat16
        for layer_num in [0, 2]:
            assert isinstance(self.layer[layer_num], FullyShardedDataParallel)
            # Assert that the nested layers are set reshard_after_forward to True
            assert self.layer[layer_num].reshard_after_forward

            assert self.layer[layer_num].mixed_precision.param_dtype == precision
            assert self.layer[layer_num].mixed_precision.reduce_dtype == precision
            assert self.layer[layer_num].mixed_precision.buffer_dtype == precision


def _run_multiple_stages(trainer, model, model_path: Optional[str] = None):
    trainer.fit(model)
    model_path = trainer.strategy.broadcast(model_path)
    model_path = model_path if model_path else trainer.checkpoint_callback.last_model_path

    trainer.save_checkpoint(model_path, weights_only=True)

    _assert_save_equality(trainer, model_path, cls=model.__class__)

    # Test entry point
    trainer.test(model)  # model is wrapped, will not call `configure_sharded_model`

    # provide model path, will create a new unwrapped model and load and then call `configure_shared_model` to wrap
    trainer.test(ckpt_path=model_path)

    # Predict entry point
    trainer.predict(model)  # model is wrapped, will not call `configure_sharded_model`

    # provide model path, will create a new unwrapped model and load and then call `configure_shared_model` to wrap
    trainer.predict(ckpt_path=model_path)


def _assert_save_equality(trainer, ckpt_path, cls=TestFSDPModel):
    # Use FullySharded to get the state dict for the sake of comparison
    model_state_dict = trainer.strategy.lightning_module_state_dict()

    if trainer.is_global_zero:
        saved_model = cls.load_from_checkpoint(ckpt_path)

        # Assert model parameters are identical after loading
        for ddp_param, shard_param in zip(model_state_dict.values(), saved_model.state_dict().values()):
            assert torch.equal(ddp_param.float().cpu(), shard_param)


def custom_auto_wrap_policy(
    module,
    recurse,
    unwrapped_params: int,
    min_num_params: int = int(1e8),
) -> bool:
    return unwrapped_params >= 2


@RunIf(min_torch="1.12")
def test_invalid_on_cpu(tmpdir):
    """Test to ensure that we raise ValueError for Native FSDP on CPU."""
    with pytest.raises(
        _ValueError,
        match=f"You selected strategy to be `{DDPFullyShardedNativeStrategy.strategy_name}`, "
        "but GPU accelerator is not used.",
    ):
        trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True, strategy="fsdp_native")
        assert isinstance(trainer.strategy, DDPFullyShardedNativeStrategy)
        trainer.strategy.setup_environment()


@RunIf(min_torch="1.12", min_cuda_gpus=1)
@pytest.mark.parametrize("precision, expected", [(16, torch.float16), ("bf16", torch.bfloat16)])
def test_precision_plugin_config(precision, expected):
    plugin = FullyShardedNativeNativeMixedPrecisionPlugin(precision=precision, device="cuda")
    config = plugin.mixed_precision_config
    assert config.param_dtype == expected
    assert config.buffer_dtype == expected
    assert config.reduce_dtype == expected


@RunIf(min_torch="1.12")
def test_fsdp_custom_mixed_precision(tmpdir):
    """Test to ensure that passing a custom mixed precision config works."""
    config = MixedPrecision()
    strategy = DDPFullyShardedNativeStrategy(mixed_precision=config)
    assert strategy.mixed_precision_config == config


@RunIf(min_cuda_gpus=2, skip_windows=True, standalone=True, min_torch="1.12")
def test_fully_sharded_native_strategy_sync_batchnorm(tmpdir):
    """Test to ensure that sync_batchnorm works when using fsdp_native and GPU, and all stages can be run."""

    model = TestFSDPModel()
    trainer = Trainer(
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=2,
        strategy="fsdp_native",
        precision=16,
        max_epochs=1,
        sync_batchnorm=True,
    )
    _run_multiple_stages(trainer, model, os.path.join(tmpdir, "last.ckpt"))


@RunIf(min_cuda_gpus=1, skip_windows=True, standalone=True, min_torch="1.12")
@pytest.mark.parametrize("precision", (16, pytest.param("bf16", marks=RunIf(bf16_cuda=True))))
def test_fully_sharded_native_strategy_checkpoint(tmpdir, precision):
    """Test to ensure that checkpoint is saved correctly when using a single GPU, and all stages can be run."""
    model = TestFSDPModel()
    trainer = Trainer(
        default_root_dir=tmpdir, accelerator="gpu", devices=1, strategy="fsdp_native", precision=precision, max_epochs=1
    )
    _run_multiple_stages(trainer, model, os.path.join(tmpdir, "last.ckpt"))


@RunIf(min_cuda_gpus=2, skip_windows=True, standalone=True, min_torch="1.12")
@pytest.mark.parametrize(
    "model, strategy",
    [
        (TestFSDPModel(), "fsdp_native"),
        (TestFSDPModelAutoWrapped(), DDPFullyShardedNativeStrategy),
    ],
)
def test_fully_sharded_native_strategy_checkpoint_multi_gpus(tmpdir, model, strategy):
    """Test to ensure that checkpoint is saved correctly when using multiple GPUs, and all stages can be run."""

    ck = ModelCheckpoint(save_last=True)

    if not isinstance(strategy, str):
        strategy = strategy(auto_wrap_policy=custom_auto_wrap_policy)

    trainer = Trainer(
        default_root_dir=tmpdir,
        accelerator="gpu",
        devices=2,
        strategy=strategy,
        precision=16,
        max_epochs=1,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        limit_predict_batches=2,
        callbacks=[ck],
    )
    _run_multiple_stages(trainer, model)


@RunIf(min_cuda_gpus=1, skip_windows=True, standalone=True, min_torch="1.12")
def test_invalid_parameters_in_optimizer(tmpdir):
    trainer = Trainer(strategy="fsdp_native", accelerator="cuda", devices=1)

    class EmptyParametersModel(BoringModel):
        def configure_optimizers(self):
            return torch.optim.Adam(self.parameters(), lr=1e-2)

    model = EmptyParametersModel()
    with pytest.raises(ValueError, match="The optimizer does not seem to reference any FSDP parameters"):
        trainer.fit(model)

    class NoFlatParametersModel(BoringModel):
        def configure_optimizers(self):
            layer = torch.nn.Linear(4, 5)
            return torch.optim.Adam(layer.parameters(), lr=1e-2)

    model = NoFlatParametersModel()
    with pytest.raises(ValueError, match="The optimizer does not seem to reference any FSDP parameters"):
        trainer.fit(model)
