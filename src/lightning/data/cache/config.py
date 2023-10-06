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

import json
import os
from typing import Optional, Tuple

from lightning.data.cache.constants import INDEX_FILENAME
from lightning.data.cache.downloader import get_downloader_cls
from lightning.data.cache.pytree import treespec_loads
from lightning.data.cache.sampler import ChunkedIndex


class ChunksConfig:
    def __init__(self, cache_dir: str, remote_dir: Optional[str]):
        """The ChunksConfig reads the index files associated a chunked dataset and enables to map an index to its
        chunk.

        Arguments:
            cache_dir: The path to cache folder.
            remote_dir: The remote folder where the data are stored.

        """
        self._cache_dir = cache_dir
        self._intervals = []
        self._config = None
        self._chunks = []
        self._remote_dir = remote_dir

        with open(os.path.join(self._cache_dir, INDEX_FILENAME)) as f:
            data = json.load(f)

            self._config = data["config"]

            self._chunks.extend(data["chunks"])

        self._config["data_spec"] = treespec_loads(self._config["data_spec"])

        for chunk in self._chunks:
            start, end = chunk["interval"]
            if (end - start) != chunk["chunk_size"]:
                raise Exception(
                    "The config intervals doesn't match the number of samples. This shouldn't have happened."
                )
            self._intervals.append(chunk["interval"])

        self._length = sum([chunk["chunk_size"] for chunk in self._chunks])

        self._downloader = None
        if remote_dir:
            self._downloader = get_downloader_cls(remote_dir)(remote_dir, cache_dir, self._chunks)

    def download_chunk_from_index(self, chunk_index: int) -> None:
        chunk_filename = self._chunks[chunk_index]["filename"]

        local_chunkpath = os.path.join(self._cache_dir, chunk_filename)

        if os.path.exists(local_chunkpath):
            return None

        return self._downloader.download_chunk_from_index(chunk_index)

    @property
    def intervals(self):
        return self._intervals

    @property
    def data_format(self):
        return self._config["data_format"]

    @property
    def config(self):
        return self._config

    def _get_chunk_index_from_index(self, index: int) -> int:
        for chunk_index, internal in enumerate(self._intervals):
            if internal[0] <= index < internal[1]:
                return chunk_index
        raise ValueError(
            f"The provided index {index} didn't find a match within the chunk intervals {self._intervals}."
        )

    def __getitem__(self, index: ChunkedIndex) -> Tuple[str, int, int]:
        """Find the associated chunk metadata."""
        chunk = self._chunks[index.chunk_index]
        return os.path.join(self._cache_dir, chunk["filename"]), *self._intervals[index.chunk_index]

    @classmethod
    def load(cls, cache_dir: str, remote_dir: Optional[str] = None) -> Optional["ChunksConfig"]:
        cache_index_filepath = os.path.join(cache_dir, INDEX_FILENAME)

        if isinstance(remote_dir, str):
            downloader = get_downloader_cls(remote_dir)(remote_dir, cache_dir, [])
            downloader.download_file(os.path.join(remote_dir, INDEX_FILENAME), cache_index_filepath)

        if not os.path.exists(cache_index_filepath):
            return None

        return ChunksConfig(cache_dir, remote_dir)

    def __len__(self) -> int:
        return self._length
