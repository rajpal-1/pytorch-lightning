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
import math
import os
from typing import Sequence
from unittest import mock

import pytest
import torch
from torch import Tensor
from torch.utils._pytree import tree_flatten
from torch.utils.data import DataLoader, TensorDataset
from torch.utils.data.dataset import Dataset, IterableDataset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler, SequentialSampler

from lightning.pytorch import Trainer
from lightning.pytorch.demos.boring_classes import BoringModel, RandomDataset
from lightning.pytorch.trainer.supporters import (
    _CombinedDataset,
    _MaxSizeCycle,
    _MinSize,
    _Sequential,
    _supported_modes,
    CombinedLoader,
)
from tests_pytorch.helpers.runif import RunIf


@pytest.mark.parametrize(
    ["dataset_1", "dataset_2"],
    [
        ([list(range(10)), list(range(20))]),
        ([range(10), range(20)]),
        ([torch.randn(10, 3, 2), torch.randn(20, 5, 6)]),
        ([TensorDataset(torch.randn(10, 3, 2)), TensorDataset(torch.randn(20, 5, 6))]),
    ],
)
def test_combined_dataset(dataset_1, dataset_2):
    """Verify the length of the CombinedDataset."""
    datasets = [dataset_1, dataset_2]
    combined_dataset = _CombinedDataset(datasets, "max_size_cycle")
    assert len(combined_dataset) == 20

    combined_dataset = _CombinedDataset(datasets, "min_size")
    assert len(combined_dataset) == 10


def test_combined_dataset_length_mode_error():
    with pytest.raises(ValueError, match="Unsupported mode 'test'"):
        _CombinedDataset([], mode="test")


def test_combined_dataset_no_length():
    class Foo:
        # map-style
        def __len__(self):
            return 5

    class Bar:
        # iterable style
        ...

    class Baz:
        # None length
        def __len__(self):
            pass

    cd = _CombinedDataset([Foo(), Bar(), Baz()])
    assert len(cd) == 5

    cd = _CombinedDataset(Bar)
    with pytest.raises(NotImplementedError, match="All datasets are iterable-style"):
        len(cd)


def test_combined_loader_modes():
    """Test `CombinedLoaderIterator` given mapping iterables."""
    iterables = {
        "a": torch.utils.data.DataLoader(range(10), batch_size=4),
        "b": torch.utils.data.DataLoader(range(20), batch_size=5),
    }
    lengths = [len(v) for v in iterables.values()]

    # min_size with dict
    min_len = min(lengths)
    combined_loader = CombinedLoader(iterables, "min_size")
    assert combined_loader._iterator is None
    assert len(combined_loader) == min_len
    for idx, item in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _MinSize)
        assert isinstance(item, dict)
        assert list(item) == ["a", "b"]
    assert idx == min_len - 1
    assert idx == len(combined_loader) - 1

    # max_size_cycle with dict
    max_len = max(lengths)
    combined_loader = CombinedLoader(iterables, "max_size_cycle")
    assert combined_loader._iterator is None
    assert len(combined_loader) == max_len
    for idx, item in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _MaxSizeCycle)
        assert isinstance(item, dict)
        assert list(item) == ["a", "b"]
    assert idx == max_len - 1
    assert idx == len(combined_loader) - 1

    # sequential with dict
    sum_len = sum(lengths)
    combined_loader = CombinedLoader(iterables, "sequential")
    assert combined_loader._iterator is None
    assert len(combined_loader) == sum_len
    for total_idx, (idx, item) in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _Sequential)
        assert isinstance(idx, int)
        assert isinstance(item, Tensor)
    assert idx == lengths[-1] - 1
    assert total_idx == sum_len - 1
    assert total_idx == len(combined_loader) - 1

    iterables = list(iterables.values())

    # min_size with list
    combined_loader = CombinedLoader(iterables, "min_size")
    assert len(combined_loader) == min_len
    for idx, item in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _MinSize)
        assert isinstance(item, list)
        assert len(item) == 2
    assert idx == min_len - 1
    assert idx == len(combined_loader) - 1

    # max_size_cycle with list
    combined_loader = CombinedLoader(iterables, "max_size_cycle")
    assert len(combined_loader) == max_len
    for idx, item in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _MaxSizeCycle)
        assert isinstance(item, list)
        assert len(item) == 2
    assert idx == max_len - 1
    assert idx == len(combined_loader) - 1

    # sequential with list
    combined_loader = CombinedLoader(iterables, "sequential")
    assert combined_loader._iterator is None
    assert len(combined_loader) == sum_len
    for total_idx, (idx, item) in enumerate(combined_loader):
        assert isinstance(combined_loader._iterator, _Sequential)
        assert isinstance(idx, int)
        assert isinstance(item, Tensor)
    assert idx == lengths[-1] - 1
    assert total_idx == sum_len - 1
    assert total_idx == len(combined_loader) - 1


def test_combined_loader_raises():
    with pytest.raises(ValueError, match="Unsupported mode 'testtt'"):
        CombinedLoader([range(10)], "testtt")

    combined_loader = CombinedLoader(None, "max_size_cycle")
    with pytest.raises(NotImplementedError, match="NoneType` does not define `__len__"):
        len(combined_loader)


class TestIterableDataset(IterableDataset):
    def __init__(self, size: int = 10):
        self.size = size

    def __iter__(self):
        self.sampler = SequentialSampler(range(self.size))
        self.sampler_iter = iter(self.sampler)
        return self

    def __next__(self):
        return next(self.sampler_iter)


@pytest.mark.parametrize("mode", ["min_size", "max_size_cycle", "sequential"])
@pytest.mark.parametrize("use_multiple_dataloaders", [False, True])
def test_combined_loader_sequence_iterable_dataset(mode, use_multiple_dataloaders):
    """Test `CombinedLoader` of mode 'min_size' given sequence iterables."""
    if use_multiple_dataloaders:
        loaders = [
            torch.utils.data.DataLoader(TestIterableDataset(10), batch_size=2),
            torch.utils.data.DataLoader(TestIterableDataset(20), batch_size=2),
        ]
    else:
        loaders = [
            torch.utils.data.DataLoader(TestIterableDataset(10), batch_size=2),
        ]
    combined_loader = CombinedLoader(loaders, mode)

    has_break = False
    for idx, item in enumerate(combined_loader):
        assert isinstance(item, Sequence)
        assert len(item) == 2 if use_multiple_dataloaders else 1
        if not use_multiple_dataloaders and idx == 4:
            has_break = True
            break

    if mode == "max_size_cycle":
        assert all(combined_loader._iterator._consumed) == (not has_break)
    expected = 5
    if use_multiple_dataloaders:
        if mode == "max_size_cycle":
            expected = 10
        elif mode == "sequential":
            expected = 15
    assert idx == expected - 1


@pytest.mark.parametrize("lengths", [[4, 6], [5, 5], [6, 4]])
def test_combined_loader_sequence_with_map_and_iterable(lengths):
    class MyIterableDataset(IterableDataset):
        def __init__(self, size: int = 10):
            self.size = size

        def __iter__(self):
            self.sampler = SequentialSampler(range(self.size))
            self.iter_sampler = iter(self.sampler)
            return self

        def __next__(self):
            return next(self.iter_sampler)

    class MyMapDataset(Dataset):
        def __init__(self, size: int = 10):
            self.size = size

        def __getitem__(self, index):
            return index

        def __len__(self):
            return self.size

    x, y = lengths
    loaders = [DataLoader(MyIterableDataset(x)), DataLoader(MyMapDataset(y))]
    dataloader = CombinedLoader(loaders, mode="max_size_cycle")
    seen = sum(1 for _ in dataloader)
    assert seen == max(x, y)


@mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "0,1"})
@pytest.mark.parametrize("replace_sampler_ddp", [False, True])
def test_combined_data_loader_validation_test(mps_count_0, cuda_count_2, replace_sampler_ddp):
    """This test makes sure distributed sampler has been properly injected in dataloaders when using
    CombinedLoader."""

    class CustomDataset(Dataset):
        def __init__(self, data):
            self.data = data

        def __len__(self):
            return len(self.data)

        def __getitem__(self, index):
            return self.data[index]

    class CustomSampler(RandomSampler):
        def __init__(self, data_source, name) -> None:
            super().__init__(data_source)
            self.name = name

    dataset = CustomDataset(range(10))
    dataloader = CombinedLoader(
        {
            "a": DataLoader(CustomDataset(range(10))),
            "b": DataLoader(dataset, sampler=CustomSampler(dataset, "custom_sampler")),
            "c": {"c": DataLoader(CustomDataset(range(10))), "d": DataLoader(CustomDataset(range(10)))},
            "d": [DataLoader(CustomDataset(range(10))), DataLoader(CustomDataset(range(10)))],
        }
    )

    trainer = Trainer(replace_sampler_ddp=replace_sampler_ddp, strategy="ddp", accelerator="gpu", devices=2)
    dataloader = trainer._data_connector._prepare_dataloader(dataloader, shuffle=True)

    samplers_flattened = tree_flatten(dataloader.sampler)[0]
    assert len(samplers_flattened) == 6
    if replace_sampler_ddp:
        assert all(isinstance(s, DistributedSampler) for s in samplers_flattened)
    else:
        assert all(isinstance(s, (SequentialSampler, CustomSampler)) for s in samplers_flattened)

    datasets_flattened = tree_flatten(dataloader.dataset.datasets)[0]
    assert len(datasets_flattened) == 6
    assert all(isinstance(ds, CustomDataset) for ds in datasets_flattened)


@pytest.mark.parametrize("accelerator", ["cpu", pytest.param("gpu", marks=RunIf(min_cuda_gpus=2))])
@pytest.mark.parametrize("replace_sampler_ddp", [False, True])
def test_combined_data_loader_with_max_size_cycle_and_ddp(accelerator, replace_sampler_ddp):
    """This test makes sure distributed sampler has been properly injected in dataloaders when using CombinedLoader
    with ddp and `max_size_cycle` mode."""
    trainer = Trainer(strategy="ddp", accelerator=accelerator, devices=2, replace_sampler_ddp=replace_sampler_ddp)

    dataloader = CombinedLoader(
        {"a": DataLoader(RandomDataset(32, 8), batch_size=1), "b": DataLoader(RandomDataset(32, 8), batch_size=1)},
    )
    dataloader = trainer._data_connector._prepare_dataloader(dataloader, shuffle=False)
    assert len(dataloader) == 4 if replace_sampler_ddp else 8

    for a_length in [6, 8, 10]:
        dataloader = CombinedLoader(
            {
                "a": DataLoader(range(a_length), batch_size=1),
                "b": DataLoader(range(8), batch_size=1),
            },
            mode="max_size_cycle",
        )

        length = max(a_length, 8)
        assert len(dataloader) == length
        dataloader = trainer._data_connector._prepare_dataloader(dataloader, shuffle=False)
        assert len(dataloader) == length // 2 if replace_sampler_ddp else length
        if replace_sampler_ddp:
            last_batch = list(dataloader)[-1]
            if a_length == 6:
                assert last_batch == {"a": torch.tensor([0]), "b": torch.tensor([6])}
            elif a_length == 8:
                assert last_batch == {"a": torch.tensor([6]), "b": torch.tensor([6])}
            elif a_length == 10:
                assert last_batch == {"a": torch.tensor([8]), "b": torch.tensor([0])}

    class InfiniteDataset(IterableDataset):
        def __iter__(self):
            while True:
                yield 1

    dataloader = CombinedLoader(
        {
            "a": DataLoader(InfiniteDataset(), batch_size=1),
            "b": DataLoader(range(8), batch_size=1),
        },
        mode="max_size_cycle",
    )
    with pytest.raises(NotImplementedError, match="DataLoader` does not define `__len__"):
        len(dataloader)
    assert len(dataloader.iterables["b"]) == 8
    dataloader = trainer._data_connector._prepare_dataloader(dataloader, shuffle=False)
    assert len(dataloader.iterables["b"]) == 4 if replace_sampler_ddp else 8
    with pytest.raises(NotImplementedError, match="DataLoader` does not define `__len__"):
        len(dataloader)


@pytest.mark.parametrize("replace_sampler_ddp", [False, True])
@pytest.mark.parametrize("mode", ("min_size", "max_size_cycle", "sequential"))
@pytest.mark.parametrize("use_combined_loader", [False, True])
def test_combined_dataloader_for_training_with_ddp(replace_sampler_ddp, mode, use_combined_loader, mps_count_0):
    """When providing a CombinedLoader as the training data, it should be correctly receive the distributed
    samplers."""
    dim = 3
    n1 = 8
    n2 = 6
    dataloader = {
        "a": DataLoader(RandomDataset(dim, n1), batch_size=1),
        "b": DataLoader(RandomDataset(dim, n2), batch_size=1),
    }
    if use_combined_loader:
        dataloader = CombinedLoader(dataloader, mode=mode)
    model = BoringModel()
    trainer = Trainer(
        strategy="ddp",
        accelerator="auto",
        devices="auto",
        replace_sampler_ddp=replace_sampler_ddp,
        multiple_trainloader_mode=mode,
    )
    trainer._data_connector.attach_data(
        model=model, train_dataloaders=dataloader, val_dataloaders=None, datamodule=None
    )
    fn = _supported_modes[mode]["fn"]
    expected_length_before_ddp = fn([n1, n2])
    expected_length_after_ddp = (
        math.ceil(expected_length_before_ddp / trainer.num_devices)
        if replace_sampler_ddp
        else expected_length_before_ddp
    )
    trainer.reset_train_dataloader(model=model)
    assert trainer.train_dataloader is not None
    assert isinstance(trainer.train_dataloader, CombinedLoader)
    assert trainer.train_dataloader._mode == mode
    assert trainer.num_training_batches == expected_length_after_ddp
