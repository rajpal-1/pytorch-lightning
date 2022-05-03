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
import itertools
from operator import itemgetter
from typing import Any, cast, Iterable, Iterator, List, Optional, Sized, Union

import torch
from torch import Tensor
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import BatchSampler, Dataset, DistributedSampler, Sampler

from pytorch_lightning.overrides.base import _LightningModuleWrapperBase
from pytorch_lightning.utilities import rank_zero_deprecation


class LightningDistributedModule(_LightningModuleWrapperBase):
    ...


def _find_tensors(
    obj: Union[Tensor, list, tuple, dict, Any]
) -> Union[List[Tensor], itertools.chain]:  # pragma: no-cover
    """Recursively find all tensors contained in the specified object."""
    if isinstance(obj, Tensor):
        return [obj]
    if isinstance(obj, (list, tuple)):
        return itertools.chain(*map(_find_tensors, obj))
    if isinstance(obj, dict):
        return itertools.chain(*map(_find_tensors, obj.values()))
    return []


# In manual_optimization, we need to call reducer prepare_for_backward.
# Note: Keep track of Pytorch DDP and update if there is a change
# https://github.com/pytorch/pytorch/blob/v1.7.1/torch/nn/parallel/distributed.py#L626-L638
def prepare_for_backward(model: DistributedDataParallel, output: Any) -> None:
    # `prepare_for_backward` is `DistributedDataParallel` specific.
    if not isinstance(model, DistributedDataParallel):
        return
    if torch.is_grad_enabled() and model.require_backward_grad_sync:
        model.require_forward_param_sync = True  # type: ignore[assignment]
        # We'll return the output object verbatim since it is a freeform
        # object. We need to find any tensors in this object, though,
        # because we need to figure out which parameters were used during
        # this forward pass, to ensure we short circuit reduction for any
        # unused parameters. Only if `find_unused_parameters` is set.
        args = list(_find_tensors(output)) if model.find_unused_parameters else []
        reducer = cast(torch._C._distributed_c10d.Reducer, model.reducer)
        reducer._rebuild_buckets()  # avoids "INTERNAL ASSERT FAILED" with `find_unused_parameters=False`
        reducer.prepare_for_backward(args)
    else:
        model.require_forward_param_sync = False  # type: ignore[assignment]


class UnrepeatedDistributedSampler(DistributedSampler):
    """A fork of the PyTorch DistributedSampler that doesn't repeat data, instead allowing the number of batches
    per process to be off-by-one from each other. This makes this sampler usable for predictions (it's
    deterministic and doesn't require shuffling). It is potentially unsafe to use this sampler for training,
    because during training the DistributedDataParallel syncs buffers on each forward pass, so it could freeze if
    one of the processes runs one fewer batch. During prediction, buffers are only synced on the first batch, so
    this is safe to use as long as each process runs at least one batch. We verify this in an assert.

    Taken from https://github.com/jpuigcerver/PyLaia/blob/v1.0.0/laia/data/unpadded_distributed_sampler.py
    and https://github.com/pytorch/pytorch/issues/25162#issuecomment-634146002
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not isinstance(self.dataset, Sized):
            raise TypeError("The given dataset must implement the `__len__` method.")
        self.num_samples = len(range(self.rank, len(self.dataset), self.num_replicas))
        self.total_size = len(self.dataset)
        # If any process has at least one batch, every other process needs to
        # have at least one batch, or the DistributedDataParallel could lock up.
        assert self.num_samples >= 1 or self.total_size == 0

    def __iter__(self) -> Iterator[List[int]]:
        if not isinstance(self.dataset, Sized):
            raise TypeError("The given dataset must implement the `__len__` method.")
        if self.shuffle:
            # deterministically shuffle based on epoch
            g = torch.Generator()
            g.manual_seed(self.epoch)
            indices = torch.randperm(len(self.dataset), generator=g).tolist()
        else:
            indices = list(range(len(self.dataset)))

        assert len(indices) == self.total_size

        # subsample
        indices = indices[self.rank : self.total_size : self.num_replicas]
        assert len(indices) == self.num_samples

        return iter(indices)


class _DatasetFromSampler(Dataset):
    """Dataset to create indexes from `Sampler`.

    Taken from https://github.com/catalyst-team/catalyst/blob/v22.04/catalyst/data/dataset.py
    Args:
        sampler: PyTorch sampler
    """

    def __init__(self, sampler: Union[Sampler, Iterable]):
        """Initialisation for DatasetFromSampler."""
        if not isinstance(sampler, Sized):
            raise TypeError("The given sampler must implement the `__len__` method.")
        if len(sampler) == float("inf"):
            raise TypeError("The given sampler must be bounded.")
        self.sampler = sampler
        self.sampler_list: Optional[List[Any]] = None

    def __getitem__(self, index: int) -> Any:
        """Gets element of the dataset.

        Args:
            index: index of the element in the dataset
        Returns:
            Single element by index
        """
        if self.sampler_list is None:
            self.sampler_list = list(self.sampler)
        return self.sampler_list[index]

    def __len__(self) -> int:
        """
        Returns:
            int: length of the dataset
        """
        return len(self.sampler)


class DistributedSamplerWrapper(DistributedSampler):
    """Wrapper over `Sampler` for distributed training. Allows you to use any sampler in distributed mode. It is
    especially useful in conjunction with `torch.nn.parallel.DistributedDataParallel`. In such case, each process
    can pass a DistributedSamplerWrapper instance as a DataLoader sampler, and load a subset of subsampled data of
    the original dataset that is exclusive to it.

    .. note::
        Sampler is assumed to be of constant size.

    Taken from https://github.com/catalyst-team/catalyst/blob/v22.04/catalyst/data/sampler.py
    """

    def __init__(
        self,
        sampler: Union[Sampler, Iterable],
        num_replicas: Optional[int] = None,
        rank: Optional[int] = None,
        shuffle: bool = True,
        seed: int = 0,
        drop_last: bool = False,
    ) -> None:
        super().__init__(_DatasetFromSampler(sampler), num_replicas, rank, shuffle, seed, drop_last)
        self.sampler = sampler

    def __iter__(self) -> Iterator:
        self.dataset = _DatasetFromSampler(self.sampler)
        indexes_of_indexes = list(super().__iter__())
        subsampler_indexes = self.dataset
        num_indices = len(indexes_of_indexes)

        if num_indices >= 2:
            return iter(itemgetter(*indexes_of_indexes)(subsampler_indexes))
        if num_indices == 1:
            return iter([itemgetter(*indexes_of_indexes)(subsampler_indexes)])
        return iter([])


class UnrepeatedDistributedSamplerWrapper(UnrepeatedDistributedSampler):
    """Wrapper over `Sampler` for distributed training. Allows you to use any sampler in distributed mode. It is
    especially useful in conjunction with `torch.nn.parallel.DistributedDataParallel`. In such case, each process
    can pass a DistributedSamplerWrapper instance as a DataLoader sampler, and load a subset of subsampled data of
    the original dataset that is exclusive to it.

    .. note::
        Sampler is assumed to be of constant size.

    Taken from https://github.com/catalyst-team/catalyst/blob/v22.04/catalyst/data/sampler.py
    """

    def __init__(
        self,
        sampler: Union[Sampler, Iterable],
        num_replicas: Optional[int] = None,
        rank: Optional[int] = None,
        shuffle: bool = True,
        seed: int = 0,
        drop_last: bool = False,
    ) -> None:
        super().__init__(_DatasetFromSampler(sampler), num_replicas, rank, shuffle, seed, drop_last)
        self.sampler = sampler

    def __iter__(self) -> Iterator:
        self.dataset = _DatasetFromSampler(self.sampler)
        indexes_of_indexes = list(super().__iter__())
        subsampler_indexes = self.dataset
        num_indices = len(indexes_of_indexes)

        if num_indices >= 2:
            return iter(itemgetter(*indexes_of_indexes)(subsampler_indexes))
        if num_indices == 1:
            return iter([itemgetter(*indexes_of_indexes)(subsampler_indexes)])
        return iter([])


class IndexBatchSamplerWrapper:
    """This class is used to wrap a :class:`torch.utils.data.BatchSampler` and capture its indices."""

    def __init__(self, sampler: BatchSampler) -> None:
        self.seen_batch_indices: List[List[int]] = []
        self._sampler = sampler
        self._batch_indices: List[int] = []

    @property
    def batch_indices(self) -> List[int]:
        rank_zero_deprecation(
            "The attribute `IndexBatchSamplerWrapper.batch_indices` was deprecated in v1.5 and will be removed in"
            " v1.7. Access the full list `seen_batch_indices` instead."
        )
        return self._batch_indices

    @batch_indices.setter
    def batch_indices(self, indices: List[int]) -> None:
        rank_zero_deprecation(
            "The attribute `IndexBatchSamplerWrapper.batch_indices` was deprecated in v1.5 and will be removed in"
            " v1.7. Access the full list `seen_batch_indices` instead."
        )
        self._batch_indices = indices

    def __iter__(self) -> Iterator[List[int]]:
        self.seen_batch_indices = []
        for batch in self._sampler:
            self._batch_indices = batch
            self.seen_batch_indices.append(batch)
            yield batch

    def __len__(self) -> int:
        return len(self._sampler)

    @property
    def drop_last(self) -> bool:
        return self._sampler.drop_last

    @property
    def batch_size(self) -> int:
        return self._sampler.batch_size

    @property
    def sampler(self) -> Union[Sampler, Iterable]:
        return self._sampler.sampler
