# Copyright The Lightning AI team.
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

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from time import time
from typing import Any, Dict, List, Optional, Union

import numpy as np
from torch.utils.data import IterableDataset

from lightning.data.streaming import Cache
from lightning.data.streaming.constants import _DEFAULT_CACHE_DIR, _INDEX_FILENAME, _LIGHTNING_CLOUD_LATEST
from lightning.data.streaming.item_loader import BaseItemLoader
from lightning.data.streaming.sampler import ChunkedIndex
from lightning.data.streaming.serializers import Serializer
from lightning.data.streaming.shuffle import FullShuffle, NoShuffle, Shuffle
from lightning.data.utilities.env import Environment, _DistributedEnv, _WorkerEnv
from lightning.fabric.utilities.types import _DictKey

if _LIGHTNING_CLOUD_LATEST:
    from lightning_cloud.resolver import Dir, _resolve_dir


class StreamingDataset(IterableDataset):
    """The streaming dataset can be used once your data have been optimised using the DatasetOptimiser class."""

    def __init__(
        self,
        input_dir: Union[str, "RemoteDir"],
        item_loader: Optional[BaseItemLoader] = None,
        shuffle: bool = False,
        drop_last: bool = False,
        seed: int = 42,
        serializers: Optional[Dict[str, Serializer]] = None,
        checkpoint_interval=None,
    ) -> None:
        """The streaming dataset can be used once your data have been optimised using the DatasetOptimiser class.

        Arguments:
            input_dir: Path to the folder where the input data is stored.
            item_loader: The logic to load an item from a chunk.
            shuffle: Whether to shuffle the data.
            drop_last: If `True`, drops the last items to ensure that
                all processes/workers return the same amount of data.
            seed: Random seed for shuffling.
            serializers: The serializers used to serialize and deserialize the chunks.
            checkpoint_interval: Interval in seconds at which the workers are going to store their own progress.

        """
        super().__init__()
        if not isinstance(shuffle, bool):
            raise ValueError(f"Shuffle should be a boolean. Found {shuffle}")

        if isinstance(input_dir, RemoteDir):
            input_dir = Dir(path=input_dir.cache_dir, url=input_dir.remote)

        input_dir = _resolve_dir(input_dir)

        self.input_dir = input_dir
        self.item_loader = item_loader
        self.shuffle: bool = shuffle
        self.drop_last = drop_last
        self.seed = seed

        self.cache: Optional[Cache] = None
        self.distributed_env = _DistributedEnv.detect()
        self.worker_env: Optional[_WorkerEnv] = None
        self.worker_chunks: List[int] = []
        self.worker_intervals: List[List[int]] = []
        self.current_indexes: List[int] = []
        self.chunk_index = 0
        self.global_index = 0
        self.index = 0
        self.has_triggered_download = False
        self.min_items_per_replica: Optional[int] = None
        self.current_epoch = 0
        self.random_state = None
        self.shuffler: Optional[Shuffle] = None
        self.serializers = serializers
        self.resume_id = uuid.uuid4()
        self.checkpoint_interval = checkpoint_interval or 60 * 5
        self._state_dict: Optional[Dict] = None

    def _create_cache(self, worker_env: _WorkerEnv) -> Cache:
        env = Environment(dist_env=self.distributed_env, worker_env=worker_env)

        if self.input_dir.path is None:
            cache_path = _try_create_cache_dir(
                input_dir=self.input_dir.path if self.input_dir.path else self.input_dir.url, shard_rank=env.shard_rank
            )
            if cache_path is not None:
                self.input_dir.path = cache_path

        cache = Cache(
            input_dir=self.input_dir, item_loader=self.item_loader, chunk_bytes=1, serializers=self.serializers
        )
        cache._reader._try_load_config()

        if not cache.filled:
            raise ValueError(
                f"The provided dataset `{self.input_dir}` doesn't contain any {_INDEX_FILENAME} file."
                " HINT: Did you successfully optimize a dataset to the provided `input_dir`?"
            )

        return cache

    def _create_shuffler(self, cache: Cache) -> Shuffle:
        seed = self.seed
        if self._state_dict:
            seed = self._state_dict[str(self.cache.rank)]["seed"]
        return FullShuffle(cache, seed, self.drop_last) if self.shuffle else NoShuffle(cache, seed, self.drop_last)

    def __len__(self) -> int:
        if self.shuffler is None:
            cache = self._create_cache(worker_env=_WorkerEnv.detect())
            self.shuffler = self._create_shuffler(cache)
        return self.shuffler.get_len(self.distributed_env, self.current_epoch)

    def __iter__(self) -> "StreamingDataset":
        self.worker_env = _WorkerEnv.detect()
        self.cache = self._create_cache(worker_env=self.worker_env)
        self.shuffler = self._create_shuffler(self.cache)

        chunks_per_replica, intervals_per_replica = self.shuffler.get_chunks_and_intervals_per_ranks(
            self.distributed_env, self.current_epoch
        )
        current_chunks = chunks_per_replica[self.distributed_env.global_rank % self.distributed_env.world_size]
        current_intervals = intervals_per_replica[self.distributed_env.global_rank % self.distributed_env.world_size]

        self.worker_chunks = []
        self.worker_intervals = []

        for i, (chunk_index, chunk_interval) in enumerate(zip(current_chunks, current_intervals)):
            if i % self.worker_env.world_size != self.worker_env.rank:
                continue
            self.worker_chunks.append(chunk_index)
            self.worker_intervals.append(chunk_interval)

        # Handle restart
        if self._state_dict:
            state = self._state_dict[str(self.cache.rank)]
            self.chunk_index = state["chunk_index"]
            self.global_index = state["global_index"]
            self.index = state["index"]
            interval = self.worker_intervals[self.chunk_index]
            current_indexes = np.arange(interval[0], interval[1])
            current_indexes = self.shuffler(current_indexes)
            self.current_indexes = current_indexes[state["index"] :]
            self.has_triggered_download = False
            self.last_time = time()
            self.chunk_index += 1
        else:
            self.current_indexes = []
            self.chunk_index = 0
            self.global_index = 0
            self.index = 0
            self.has_triggered_download = False
            self.last_time = time()

        return self

    def __getitem__(self, index: Union[ChunkedIndex, int]) -> Any:
        if self.cache is None:
            self.worker_env = _WorkerEnv.detect()
            self.cache = self._create_cache(worker_env=self.worker_env)
            self.shuffler = self._create_shuffler(self.cache)
        if isinstance(index, int):
            index = ChunkedIndex(index, self.cache._get_chunk_index_from_index(index))
        return self.cache[index]

    def __next__(self) -> Any:
        # Prevent to create more batch on a given process
        if self.global_index >= len(self):
            self.current_epoch += 1
            raise StopIteration

        # Lazily re-populate the interval to reduce memory usage.
        if len(self.current_indexes) == 0:
            if self.chunk_index == len(self.worker_intervals):
                self.current_epoch += 1
                raise StopIteration

            # reset index
            self.index = 0

            # Checkpoint when reaching a new chunk
            self.checkpoint()

            interval = self.worker_intervals[self.chunk_index]
            current_indexes = np.arange(interval[0], interval[1])

            assert self.shuffler is not None
            self.current_indexes = self.shuffler(current_indexes)
            self.chunk_index += 1

        last_index = self.chunk_index == len(self.worker_intervals) and len(self.current_indexes) == 1

        # Get the first index
        index = self.current_indexes.pop(0)

        # Call the `__getitem__` method.
        data = self.__getitem__(
            ChunkedIndex(
                index=index,
                chunk_index=self.worker_chunks[self.chunk_index - 1],
                # We provide the chunks indexes only one the first
                chunk_indexes=None if self.has_triggered_download else self.worker_chunks,
                last_index=last_index,
            )
        )

        self.has_triggered_download = True
        self.global_index += 1
        self.index += 1

        # Checkpoint based on time
        if (self.last_time - time()) > self.checkpoint_interval:
            self.checkpoint()

        return data

    def checkpoint(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w+") as tmp:
            json.dump(
                {
                    "rank": self.cache._reader.rank,
                    "current_epoch": self.current_epoch,
                    "input_dir_path": self.input_dir.path,
                    "input_dir_url": self.input_dir.url,
                    "item_loader": self.item_loader.state_dict(),
                    "drop_last": self.drop_last,
                    "seed": self.seed,
                    "checkpoint_interval": self.checkpoint_interval,
                    "chunk_index": self.chunk_index,
                    "global_index": self.global_index,
                    "index": self.index,
                },
                tmp,
            )

            tmp.flush()

            now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%fZ")
            checkpoint_path = os.path.join(self.cache.resume_folder, f"checkpoint-{now}.json")
            shutil.copyfile(tmp.name, checkpoint_path)

        self.last_time = time()

    def state_dict(self) -> Dict[_DictKey, Any]:
        if self.cache is None:
            self.worker_env = _WorkerEnv.detect()
            self.cache = self._create_cache(worker_env=self.worker_env)

        state_dict = {}
        worker_env = _WorkerEnv.detect()
        if worker_env.world_size == 1:
            checkpoint_dir = os.path.join(self.cache._cache_dir, "checkpoints")
            if not os.path.exists(checkpoint_dir):
                return state_dict
            for worker_idx in os.listdir(checkpoint_dir):
                checkpoints = os.listdir(os.path.join(checkpoint_dir, str(worker_idx)))
                checkpoints = sorted(
                    checkpoints,
                    key=lambda item: datetime.strptime(
                        item.split("checkpoint-")[1].split(".json")[0], "%Y-%m-%d_%H-%M-%S.%fZ"
                    ),
                )
                checkpoint_path = os.path.join(checkpoint_dir, str(worker_idx), checkpoints[-1])
                with open(checkpoint_path) as f:
                    state_dict[worker_idx] = json.load(f)
        else:
            raise NotImplementedError("The `state_dict` should be called on the main thread.")
        return state_dict

    def load_state_dict(self, state_dict: Dict[_DictKey, Any]) -> None:
        if state_dict:
            self._state_dict = state_dict


def _try_create_cache_dir(input_dir: str, shard_rank: int = 0) -> Optional[str]:
    hash_object = hashlib.md5(input_dir.encode())
    if "LIGHTNING_CLUSTER_ID" not in os.environ or "LIGHTNING_CLOUD_PROJECT_ID" not in os.environ:
        cache_dir = os.path.join(_DEFAULT_CACHE_DIR, hash_object.hexdigest(), str(shard_rank))
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir
    cache_dir = os.path.join("/cache", "chunks", hash_object.hexdigest(), str(shard_rank))
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


@dataclass
class RemoteDir:
    """Holds a remote URL to a directory and a cache directory where the data will be downloaded."""

    cache_dir: str
    remote: str
