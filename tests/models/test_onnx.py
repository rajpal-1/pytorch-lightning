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

import pytest
import torch

import tests.helpers.pipelines as tpipes
import tests.helpers.utils as tutils
from pytorch_lightning import Trainer
from tests.helpers import BoringModel
from tests.helpers.runif import RunIf


def test_model_saves_with_input_sample(tmpdir):
    """Test that ONNX model saves with input sample and size is greater than 3 MB"""
    model = BoringModel()
    trainer = Trainer(fast_dev_run=True)
    trainer.fit(model)

    file_path = os.path.join(tmpdir, "model.onnx")
    input_sample = torch.randn((1, 32))
    model.to_onnx(file_path, input_sample)
    assert os.path.isfile(file_path)
    assert os.path.getsize(file_path) > 4e2


@RunIf(min_gpus=1)
def test_model_saves_on_gpu(tmpdir):
    """Test that model saves on gpu"""
    model = BoringModel()
    trainer = Trainer(gpus=1, fast_dev_run=True)
    trainer.fit(model)

    file_path = os.path.join(tmpdir, "model.onnx")
    input_sample = torch.randn((1, 32))
    model.to_onnx(file_path, input_sample)
    assert os.path.isfile(file_path)
    assert os.path.getsize(file_path) > 4e2


def test_model_saves_with_example_output(tmpdir):
    """Test that ONNX model saves when provided with example output"""
    model = BoringModel()
    trainer = Trainer(fast_dev_run=True)
    trainer.fit(model)

    file_path = os.path.join(tmpdir, "model.onnx")
    input_sample = torch.randn((1, 32))
    model.eval()
    example_outputs = model.forward(input_sample)
    model.to_onnx(file_path, input_sample, example_outputs=example_outputs)
    assert os.path.exists(file_path) is True


def test_model_saves_with_example_input_array(tmpdir):
    """Test that ONNX model saves with_example_input_array and size is greater than 3 MB"""
    model = BoringModel()
    model.example_input_array = torch.randn(5, 32)

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path)
    assert os.path.exists(file_path) is True
    assert os.path.getsize(file_path) > 4e2


@RunIf(min_gpus=2)
def test_model_saves_on_multi_gpu(tmpdir):
    """Test that ONNX model saves on a distributed backend"""
    tutils.set_random_master_port()

    trainer_options = dict(
        default_root_dir=tmpdir,
        max_epochs=1,
        limit_train_batches=10,
        limit_val_batches=10,
        gpus=[0, 1],
        accelerator='ddp_spawn',
        progress_bar_refresh_rate=0,
    )

    model = BoringModel()
    model.example_input_array = torch.randn(5, 32)

    tpipes.run_model_test(trainer_options, model, min_acc=0.08)

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path)
    assert os.path.exists(file_path) is True


def test_verbose_param(tmpdir, capsys):
    """Test that output is present when verbose parameter is set"""
    model = BoringModel()
    model.example_input_array = torch.randn(5, 32)

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path, verbose=True)
    captured = capsys.readouterr()
    assert "graph(%" in captured.out


def test_error_if_no_input(tmpdir):
    """Test that an error is thrown when there is no input tensor"""
    model = BoringModel()
    model.example_input_array = None
    file_path = os.path.join(tmpdir, "model.onnx")
    with pytest.raises(
        ValueError,
        match=r'Could not export to ONNX since neither `input_sample` nor'
        r' `model.example_input_array` attribute is set.'
    ):
        model.to_onnx(file_path)


def test_single_input_single_output_model_inference_is_valid(tmpdir):
    model = BoringModel()
    model.example_input_array = torch.randn(5, 32)

    trainer = Trainer(fast_dev_run=True)
    trainer.fit(model)

    model.eval()

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path, model.example_input_array, export_params=True)


def test_single_input_multi_output_model_inference_is_valid(tmpdir):

    class BoringModelSIMO(BoringModel):

        def __init__(self):
            super().__init__()
            self.layer1 = torch.nn.Linear(32, 1)
            self.layer2 = torch.nn.Linear(32, 1)

        def forward(self, x):
            y1 = self.layer1(x)
            y2 = self.layer2(x)
            return y1, y2

    model = BoringModelSIMO()
    model.example_input_array = torch.randn(5, 32)

    model.eval()

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path, model.example_input_array, export_params=True)


def test_multi_input_single_output_model_inference_is_valid(tmpdir):

    class BoringModelMISO(BoringModel):

        def __init__(self):
            super().__init__()
            self.layer1 = torch.nn.Linear(32, 1)
            self.layer2 = torch.nn.Linear(32, 1)

        def forward(self, x1, x2):
            y1 = self.layer1(x1)
            y2 = self.layer2(x2)
            return y1 + y2

    model = BoringModelMISO()
    model.example_input_array = (torch.randn(5, 32), torch.randn(5, 32))

    model.eval()

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path, model.example_input_array, export_params=True)


def test_multi_input_multi_output_model_inference_is_valid(tmpdir):

    class BoringModelMIMO(BoringModel):

        def __init__(self):
            super().__init__()
            self.layer1 = torch.nn.Linear(32, 1)
            self.layer2 = torch.nn.Linear(32, 1)

        def forward(self, x1, x2):
            y1 = self.layer1(x1)
            y2 = self.layer2(x2)
            return y1, y2

    model = BoringModelMIMO()
    model.example_input_array = (torch.randn(5, 32), torch.randn(5, 32))

    model.eval()

    file_path = os.path.join(tmpdir, "model.onnx")
    model.to_onnx(file_path, model.example_input_array, export_params=True)
