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
import os
import time
from copy import deepcopy
from typing import Callable

import pytest
import torch
import torch.distributed
import torch.nn.functional
from tests_fabric.helpers.runif import RunIf
from unittest import mock

from lightning.fabric.fabric import Fabric
from lightning.fabric.utilities.cloud_io import _atomic_save

from tests_fabric.parity.utils import precision_context, is_state_dict_equal, make_deterministic
from tests_fabric.parity.models import ConvNet

NUM_STEPS_DEFAULT = 2000


def train_torch(
    move_to_device: Callable,
    precision_context,
    num_steps=NUM_STEPS_DEFAULT,
    batch_size=4,
    checkpoint_dir=".",
):
    make_deterministic()
    model = ConvNet()
    model = move_to_device(model)
    dataloader = model.get_dataloader(dataset_size=(num_steps * batch_size), batch_size=batch_size)
    optimizer = model.get_optimizer()
    loss_fn = model.get_loss_function()

    iteration_timings = []

    model.train()
    iterator = iter(dataloader)
    for _ in range(num_steps):
        t0 = time.perf_counter()

        inputs, labels = next(iterator)
        inputs, labels = move_to_device(inputs), move_to_device(labels)
        optimizer.zero_grad()
        with precision_context():
            outputs = model(inputs)
        loss = loss_fn(outputs, labels)
        loss.backward()
        optimizer.step()

        t1 = time.perf_counter()
        iteration_timings.append(t1 - t0)

    state = dict(state_dict=model.state_dict(), iteration_timings=torch.tensor(iteration_timings))
    _atomic_save(state, os.path.join(checkpoint_dir, "torch_model.pt"))


class FabricRunner(Fabric):
    def run(self, num_steps=NUM_STEPS_DEFAULT, batch_size=4, checkpoint_dir="."):
        make_deterministic()

        model = ConvNet()
        initial_state_dict = deepcopy(model.state_dict())

        optimizer = model.get_optimizer()
        model, optimizer = self.setup(model, optimizer)

        dataloader = model.get_dataloader(dataset_size=(num_steps * batch_size), batch_size=batch_size)
        dataloader = self.setup_dataloaders(dataloader)
        loss_fn = model.get_loss_function()

        iteration_timings = []

        model.train()
        iterator = iter(dataloader)
        for _ in range(num_steps):
            t0 = time.perf_counter()

            inputs, labels = next(iterator)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = loss_fn(outputs, labels)
            self.backward(loss)
            optimizer.step()

        t1 = time.perf_counter()
        iteration_timings.append(t1 - t0)

        # check that the model has changed
        assert not is_state_dict_equal(initial_state_dict, model.state_dict())

        if self.global_rank == 0:
            state = dict(state_dict=model.state_dict(), iteration_timings=torch.tensor(iteration_timings))
            _atomic_save(state, os.path.join(checkpoint_dir, "fabric_model.pt"))


@pytest.mark.parametrize(
    "precision, accelerator",
    [
        (32, "cpu"),
        pytest.param(32, "gpu", marks=RunIf(min_cuda_gpus=1)),
        # pytest.param(16, "gpu", marks=RunIf(min_cuda_gpus=1)),  # TODO: requires GradScaler
        pytest.param("bf16", "gpu", marks=RunIf(min_cuda_gpus=1, bf16_cuda=True)),
        pytest.param(32, "mps", marks=RunIf(mps=True)),
    ],
)
def test_parity_single_device(precision, accelerator, tmpdir):
    fabric = FabricRunner(precision=precision, accelerator=accelerator, devices=1)
    fabric.run(checkpoint_dir=tmpdir)

    train_torch(fabric.to_device, precision_context=fabric.autocast, checkpoint_dir=tmpdir)

    fabric_results = torch.load(os.path.join(tmpdir, "fabric_model.pt"))
    torch_results = torch.load(os.path.join(tmpdir, "torch_model.pt"))
    assert is_state_dict_equal(fabric_results["state_dict"], torch_results["state_dict"])

    timings_fabric = fabric_results["iteration_timings"]
    timings_torch = torch_results["iteration_timings"]
    # The median is more robust to outliers than the mean
    assert torch.isclose(torch.median(timings_torch), torch.median(timings_fabric), rtol=1e-4, atol=1e-4)
