# Copyright The Lightning AI team.
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
from typing import Any

import pytest
import torch
import torch.nn as nn

from lightning.pytorch import LightningModule, Trainer
from lightning.pytorch.demos.boring_classes import BoringModel
from lightning.pytorch.utilities.model_summary.model_summary import ModelSummary, summarize, UNKNOWN_SIZE
from lightning.pytorch.utilities.testing import _RunIf as RunIf
from tests_pytorch.helpers.advanced_models import ParityModuleRNN


class EmptyModule(LightningModule):
    """A module that has no layers."""

    def __init__(self):
        super().__init__()
        self.parameter = torch.rand(3, 3, requires_grad=True)
        self.example_input_array = torch.zeros(1, 2, 3, 4, 5)

    def forward(self, *args, **kwargs):
        return {"loss": self.parameter.sum()}


class PreCalculatedModel(BoringModel):
    """A model with precalculated total params size in MB for FP16 and FP32."""

    def __init__(self, precision: int = 32):
        super().__init__()
        # 32K params
        self.layer = nn.Linear(32, 1000, bias=False)
        # 218K params
        self.layer1 = nn.Linear(1000, 218, bias=False)
        # calculate model size based on precision.
        self.pre_calculated_model_size = 1.0 / (32 / precision)

    def forward(self, x):
        x = self.layer(x)
        return self.layer1(x)


class UnorderedModel(LightningModule):
    """A model in which the layers not defined in order of execution."""

    def __init__(self):
        super().__init__()
        # note: the definition order is intentionally scrambled for this test
        self.layer2 = nn.Linear(10, 2)
        self.combine = nn.Linear(7, 9)
        self.layer1 = nn.Linear(3, 5)
        self.relu = nn.ReLU()
        # this layer is unused, therefore input-/output shapes are unknown
        self.unused = nn.Conv2d(1, 1, 1)

        self.example_input_array = (torch.rand(2, 3), torch.rand(2, 10))

    def forward(self, x, y):
        out1 = self.layer1(x)
        out2 = self.layer2(y)
        out = self.relu(torch.cat((out1, out2), 1))
        out = self.combine(out)
        return out


class MixedDtypeModel(LightningModule):
    """The parameters and inputs of this model have different dtypes."""

    def __init__(self):
        super().__init__()
        self.embed = nn.Embedding(10, 20)  # expects dtype long as input
        self.reduce = nn.Linear(20, 1)  # dtype: float
        self.example_input_array = torch.tensor([[0, 2, 1], [3, 5, 3]])  # dtype: long

    def forward(self, x):
        return self.reduce(self.embed(x))


class PartialScriptModel(LightningModule):
    """A model which contains scripted layers."""

    def __init__(self):
        super().__init__()
        self.layer1 = torch.jit.script(nn.Linear(5, 3))
        self.layer2 = nn.Linear(3, 2)
        self.example_input_array = torch.rand(2, 5)

    def forward(self, x):
        return self.layer2(self.layer1(x))


class LazyModel(LightningModule):
    """A model which contains lazy layers with uninitialized parameters."""

    def __init__(self):
        super().__init__()
        self.layer1 = nn.LazyLinear(5)
        self.layer2 = nn.LazyLinear(2)

    def forward(self, inp):
        return self.layer2(self.layer1(inp))


class DeepNestedModel(LightningModule):
    """A model with deep nested layers."""

    def __init__(self):
        super().__init__()
        self.branch1 = nn.Sequential(
            nn.Linear(5, 5),
            nn.Sequential(
                nn.Linear(5, 5),
                nn.Sequential(
                    nn.Linear(5, 5),
                    nn.Sequential(nn.Linear(5, 5), nn.Sequential(nn.Linear(5, 5), nn.Sequential(nn.Linear(5, 3)))),
                ),
            ),
        )
        self.branch2 = nn.Linear(5, 10)
        self.head = UnorderedModel()
        self.example_input_array = torch.rand(2, 5)

    def forward(self, inp):
        return self.head(self.branch1(inp), self.branch2(inp))


def test_invalid_max_depth():
    """Test that invalid value for max_depth raises an error."""
    model = LightningModule()

    with pytest.raises(ValueError, match="max_depth` can be .* got temp"):
        ModelSummary(model, max_depth="temp")


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_empty_model_summary_shapes(max_depth):
    """Test that the summary works for models that have no submodules."""
    model = EmptyModule()
    summary = summarize(model, max_depth=max_depth)
    assert summary.in_sizes == []
    assert summary.out_sizes == []
    assert summary.param_nums == []


@pytest.mark.parametrize("max_depth", [-1, 1])
@pytest.mark.parametrize(
    "device_str",
    [
        "cpu",
        pytest.param("cuda:0", marks=_RunIf(min_cuda_gpus=1)),
        pytest.param("mps:0", marks=_RunIf(mps=True)),
    ],
)
def test_linear_model_summary_shapes(device_str, max_depth):
    """Test that the model summary correctly computes the input- and output shapes."""
    device = torch.device(device_str)
    model = UnorderedModel().to(device)
    model.train()
    summary = summarize(model, max_depth=max_depth)
    assert summary.in_sizes == [[2, 10], [2, 7], [2, 3], [2, 7], UNKNOWN_SIZE]  # layer 2  # combine  # layer 1  # relu
    assert summary.out_sizes == [[2, 2], [2, 9], [2, 5], [2, 7], UNKNOWN_SIZE]  # layer 2  # combine  # layer 1  # relu
    assert model.training
    assert model.device == device


def test_mixed_dtype_model_summary():
    """Test that the model summary works with models that have mixed input- and parameter dtypes."""
    model = MixedDtypeModel()
    summary = summarize(model)
    assert summary.in_sizes == [[2, 3], [2, 3, 20]]  # embed  # reduce
    assert summary.out_sizes == [[2, 3, 20], [2, 3, 1]]  # embed  # reduce


@pytest.mark.parametrize("max_depth", [-1, 0])
def test_hooks_removed_after_summarize(max_depth):
    """Test that all hooks were properly removed after summary, even ones that were not run."""
    model = UnorderedModel()
    summary = ModelSummary(model, max_depth=max_depth)
    # hooks should be removed
    for _, layer in summary.summarize().items():
        handle = layer._hook_handle
        assert handle.id not in handle.hooks_dict_ref()


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_rnn_summary_shapes(max_depth):
    """Test that the model summary works for RNNs."""
    model = ParityModuleRNN()

    b = 3
    t = 5
    i = model.rnn.input_size
    h = model.rnn.hidden_size
    o = model.linear_out.out_features

    model.example_input_array = torch.zeros(b, t, 10)

    summary = summarize(model, max_depth=max_depth)
    assert summary.in_sizes == [[b, t, i], [b, t, h]]  # rnn  # linear
    assert summary.out_sizes == [[[b, t, h], [[1, b, h], [1, b, h]]], [b, t, o]]  # rnn  # linear


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_summary_parameter_count(max_depth):
    """Test that the summary counts the number of parameters in every submodule."""
    model = UnorderedModel()
    summary = summarize(model, max_depth=max_depth)
    assert summary.param_nums == [
        model.layer2.weight.numel() + model.layer2.bias.numel(),
        model.combine.weight.numel() + model.combine.bias.numel(),
        model.layer1.weight.numel() + model.layer1.bias.numel(),
        0,  # ReLU
        model.unused.weight.numel() + model.unused.bias.numel(),
    ]


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_summary_layer_types(max_depth):
    """Test that the summary displays the layer names correctly."""
    model = UnorderedModel()
    summary = summarize(model, max_depth=max_depth)
    assert summary.layer_types == ["Linear", "Linear", "Linear", "ReLU", "Conv2d"]


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_summary_with_scripted_modules(max_depth):
    model = PartialScriptModel()
    summary = summarize(model, max_depth=max_depth)
    assert summary.layer_types == ["RecursiveScriptModule", "Linear"]
    assert summary.in_sizes == [UNKNOWN_SIZE, [2, 3]]
    assert summary.out_sizes == [UNKNOWN_SIZE, [2, 2]]


@pytest.mark.parametrize("max_depth", [-1, 1])
@pytest.mark.parametrize(
    ["example_input", "expected_size"],
    [
        ([], UNKNOWN_SIZE),
        ((1, 2, 3), [UNKNOWN_SIZE] * 3),
        (torch.tensor(0), UNKNOWN_SIZE),
        (dict(tensor=torch.zeros(1, 2, 3)), UNKNOWN_SIZE),
        (torch.zeros(2, 3, 4), [2, 3, 4]),
        ([torch.zeros(2, 3), torch.zeros(4, 5)], [[2, 3], [4, 5]]),
        ((torch.zeros(2, 3), torch.zeros(4, 5)), [[2, 3], [4, 5]]),
    ],
)
def test_example_input_array_types(example_input, expected_size, max_depth):
    """Test the types of example inputs supported for display in the summary."""

    class DummyModule(nn.Module):
        def forward(self, *args, **kwargs):
            return None

    class DummyLightningModule(LightningModule):
        def __init__(self):
            super().__init__()
            self.layer = DummyModule()

        # this LightningModule and submodule accept any type of input
        def forward(self, *args, **kwargs):
            return self.layer(*args, **kwargs)

    model = DummyLightningModule()
    model.example_input_array = example_input
    summary = summarize(model, max_depth=max_depth)
    assert summary.in_sizes == [expected_size]


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_model_size(max_depth):
    """Test model size is calculated correctly."""
    model = PreCalculatedModel()
    summary = summarize(model, max_depth=max_depth)
    assert model.pre_calculated_model_size == summary.model_size


@pytest.mark.parametrize("max_depth", [-1, 1])
def test_empty_model_size(max_depth):
    """Test empty model size is zero."""
    model = EmptyModule()
    summary = summarize(model, max_depth=max_depth)
    assert 0.0 == summary.model_size


@pytest.mark.parametrize(
    "accelerator",
    [
        pytest.param("gpu", marks=_RunIf(min_cuda_gpus=1)),
        pytest.param("mps", marks=_RunIf(mps=True)),
    ],
)
def test_model_size_precision(tmpdir, accelerator):
    """Test model size for half and full precision."""
    model = PreCalculatedModel()

    # fit model
    trainer = Trainer(
        default_root_dir=tmpdir, accelerator=accelerator, devices=1, max_steps=1, max_epochs=1, precision=32
    )
    trainer.fit(model)
    summary = summarize(model)
    assert model.pre_calculated_model_size == summary.model_size


def test_lazy_model_summary():
    """Test that the model summary can work with lazy layers."""
    lazy_model = LazyModel()
    summary = ModelSummary(lazy_model)

    with pytest.warns(
        UserWarning,
        match=r"A layer with UninitializedParameter was found. "
        r"Thus, the total number of parameters detected may be inaccurate.",
    ):
        assert summary.total_parameters == 0
        assert summary.trainable_parameters == 0


@pytest.mark.parametrize("max_depth", [-1, 0, 1, 3, 999])
def test_max_depth_param(max_depth):
    """Test that only the modules up to the desired depth are shown."""
    model = DeepNestedModel()
    summary = ModelSummary(model, max_depth=max_depth)
    for lname in summary.layer_names:
        if max_depth >= 0:
            assert lname.count(".") < max_depth


@pytest.mark.parametrize("max_depth", [-99, -2, "invalid"])
def test_raise_invalid_max_depth_value(max_depth):
    with pytest.raises(ValueError, match=f"`max_depth` can be -1, 0 or > 0, got {max_depth}"):
        summarize(DeepNestedModel(), max_depth=max_depth)


@pytest.mark.parametrize("example_input", [None, torch.randn(4, 32)])
def test_summary_data_output(example_input):
    """Ensure all items are converted to strings when getting summary data."""

    class TestModel(BoringModel):
        @property
        def example_input_array(self) -> Any:
            return example_input

    summary = summarize(TestModel())
    summary_data = summary._get_summary_data()
    for column_name, entries in summary_data:
        assert all(isinstance(entry, str) for entry in entries)
