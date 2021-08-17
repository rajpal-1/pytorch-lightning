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

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from copy import deepcopy
from functools import partial
from typing import Any, Callable, Generator, List, Optional, Tuple

import torch
from torch.utils.data.dataloader import DataLoader

import pytorch_lightning as pl
from pytorch_lightning.trainer.supporters import CombinedLoader, CycleIterator
from pytorch_lightning.utilities.apply_func import apply_to_collection, apply_to_collections
from pytorch_lightning.utilities.auto_restart import (
    _add_capture_metadata_collate,
    IteratorState,
    MergedIteratorState,
    patch_dataloader_iterator,
)
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _FAULT_TOLERANT_ENABLED


class AbstractDataFetcher(ABC):

    """
    This class is used to control batch fetching flow.
    """

    @abstractmethod
    def fetching_function(self) -> Generator:
        pass

    def __init__(
        self,
        prefetch_batches: int = 0,
    ) -> None:
        if not isinstance(prefetch_batches, int) or (isinstance(prefetch_batches, int) and prefetch_batches < 0):
            raise MisconfigurationException("`prefetch_batches` should at least be 0.")

        self.prefetch_batches = prefetch_batches + 1

        self.dataloader: Optional[Iterable] = None
        self.dataloader_iter: Optional[Iterator] = None

        self.batch_to_device: Optional[Callable]
        self.profiler: "Optional[pl.profiler.base.BaseProfiler]"

        self.batches: List
        self.fetched: int
        self.done: bool

        self.reset()

    def setup(
        self,
        dataloader: Iterable,
        batch_to_device: Optional[Callable] = None,
        profiler: "Optional[pl.profiler.base.BaseProfiler]" = None,
    ) -> None:
        self._add_capture_metadata_collate(dataloader)

        self.dataloader = dataloader
        self.batch_to_device = batch_to_device
        self.profiler = profiler

        if isinstance(dataloader, DataLoader) and not isinstance(dataloader.collate_fn, partial):
            _add_capture_metadata_collate(dataloader)

    @staticmethod
    def _add_capture_metadata_collate(dataloader: Iterable) -> None:
        if not isinstance(dataloader, (DataLoader, CombinedLoader)):
            return

        if isinstance(dataloader, CombinedLoader):
            dataloader = dataloader.loaders

        apply_to_collection(dataloader, DataLoader, _add_capture_metadata_collate)

    def append_batch(self, batch) -> None:
        self.batches.append(batch)

    def pop_batch(self) -> Any:
        return self.batches.pop(0)

    def _apply_patch(self):
        def _apply_patch_fn(loader: DataLoader, iterator: Iterator):
            if isinstance(loader, CycleIterator):
                loader = loader.loader
                # cycle_iterator = iterator
                iterator = iterator._loader_iter

            if isinstance(loader, DataLoader) and _FAULT_TOLERANT_ENABLED:
                loader._lightning_fetcher = self
                patch_dataloader_iterator(loader, iterator, self)

        apply_to_collections(self.loaders, self.loader_iters, (Iterator, DataLoader), _apply_patch_fn)

    def _store_dataloader_iter_state(
        self, dataloader_iter: Iterator, dataloader_iter_states: List[IteratorState]
    ) -> None:
        if getattr(dataloader_iter, "cache_states", None) is None:
            dataloader_iter.cache_states = {}

        if getattr(dataloader_iter, "state", None) is None:
            dataloader_iter.state = MergedIteratorState()

        for iter_state in dataloader_iter_states:
            iter_name = iter_state.name
            if iter_name not in dataloader_iter.cache_states:
                dataloader_iter.cache_states[iter_name] = []
            dataloader_iter.cache_states[iter_name].append(iter_state)

        if self.fetched >= self.prefetch_batches:
            for iter_state in dataloader_iter_states:
                if len(dataloader_iter.state):
                    dataloader_iter.previous_state = deepcopy(dataloader_iter.state)
                iter_name = iter_state.name
                state = dataloader_iter.cache_states[iter_name].pop(0)
                dataloader_iter.state.update(iter_name, state)

    @property
    def loaders(self) -> List[DataLoader]:
        if self.dataloader is None:
            raise MisconfigurationException(
                "The `DataFetcher` should be setup with an instance of a PyTorch ``DataLoader``."
            )
        if isinstance(self.dataloader, CombinedLoader):
            loaders = self.dataloader.loaders
        else:
            loaders = [self.dataloader]
        return loaders

    @property
    def loader_iters(self) -> List[Iterator]:
        if self.dataloader is None:
            raise MisconfigurationException(
                "The `DataFetcher` should be setup with an instance of a PyTorch ``DataLoader``."
            )

        if self.dataloader_iter is None:
            raise MisconfigurationException("The `dataloader_iter` isn't available outside the __iter__ context.")

        if isinstance(self.dataloader, CombinedLoader):
            loader_iters = self.dataloader_iter.loader_iters
        else:
            loader_iters = [self.dataloader_iter]
        return loader_iters

    @property
    def state(self) -> Any:
        def collect_state(iterator: Iterator):
            return iterator.state

        return apply_to_collection(self.loader_iters, Iterator, collect_state)

    def __iter__(self) -> Generator[Tuple[Any, bool], None, None]:
        if self.dataloader is None:
            raise MisconfigurationException("The iterate hasn't been provided. HINT: Did you call setup function ?.")
        self.reset()
        self.dataloader_iter = iter(self.dataloader)
        self._apply_patch()
        return self.fetching_function()

    def reset(self) -> None:
        self.batches: List = []
        self.dataloader: Optional[Iterable]
        self.fetched: int = 0
        self.done: bool = False


class DataFetcher(AbstractDataFetcher):

    """
    This class is used to control batch fetching flow.
    """

    @contextlib.contextmanager
    def fetching_context(self):
        yield

    def on_fetch_start(self) -> None:
        pass

    def on_fetch_end(self, batch) -> None:
        if self.batch_to_device:
            batch = self.batch_to_device(batch)
        self.append_batch(batch)

    def wait(self) -> None:
        pass

    def fetching_function(self) -> Generator:
        self.done = False
        while not self.done:
            self._prefetching(self.prefetch_batches)

            while self.batches:
                try:
                    yield_batch = self.pop_batch()
                    self._fetch_next_batch()
                    # yield last and has next
                    yield from self._yield_batch(yield_batch=yield_batch)
                except StopIteration:
                    self.batches.insert(0, yield_batch)
                    break

            yield from self._consume_prefetched_batches()

    def _prefetching(self, prefetch_batches: int) -> None:
        for _ in range(prefetch_batches):
            try:
                self._fetch_next_batch()
            except StopIteration:
                break

    def _fetch_next_batch(self):
        with self.fetching_context():
            self.on_fetch_start()
            batch = next(self.dataloader_iter)
            self.fetched += 1
            self.on_fetch_end(batch)

    def _consume_prefetched_batches(self) -> Generator:
        self.done = True
        while self.batches:
            yield from self._yield_batch()

    def _yield_batch(self, yield_batch: Optional[Any] = None) -> Generator:
        self.wait()
        if yield_batch is None:
            batch = self.batches.pop(0)
            is_last = len(self.batches) == 0
            yield batch, is_last
        else:
            yield yield_batch, False


class InterBatchParallelismDataFetcher(DataFetcher):

    """
    This class is used to control batch fetching flow.
    """

    def __init__(
        self,
        prefetch_batches: int = 0,
    ) -> None:
        super().__init__(prefetch_batches=prefetch_batches)

        self.cuda_stream = torch.cuda.Stream()
        self.events = []

    @contextlib.contextmanager
    def fetching_context(self):
        with torch.cuda.stream(self.cuda_stream):
            yield

    def on_fetch_start(self) -> None:
        self.events.append(torch.cuda.Event())

    def on_fetch_end(self, batch) -> None:
        super().on_fetch_end(batch)
        if len(self.events):
            self.events[-1].record()

    def wait(self) -> None:
        event = self.events.pop(0)
        event.wait()
