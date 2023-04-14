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

from lightning_fabric.plugins import TorchCheckpointIO


class HPUCheckpointIO(TorchCheckpointIO):
    """CheckpointIO to save checkpoints for HPU training strategies."""

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "The `HPUCheckpointIO` class has been moved to an external package."
            " Install the extension package as `pip install lightning-habana`"
            " and import with `from lightning_habana import HPUCheckpointIO`."
            " Please see: https://github.com/Lightning-AI/lightning-Habana for more details."
        )
