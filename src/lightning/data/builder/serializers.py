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

import numpy as np
from lightning_utilities.core.imports import RequirementCache

from lightning.data.builder.base import Serializer

_PIL_AVAILABLE = RequirementCache("PIL")

if _PIL_AVAILABLE:
    from PIL import Image
    from PIL.JpegImagePlugin import JpegImageFile
else:
    Image = Any
    JpegImageFile = None


class PILSerializer(Serializer):
    def serialize(self, item: any) -> bytes:
        mode = item.mode.encode("utf-8")
        width, height = item.size
        raw = item.tobytes()
        ints = np.array([width, height, len(mode)], np.uint32)
        return ints.tobytes() + mode + raw

    def deserialize(self, data: bytes) -> any:
        idx = 3 * 4
        width, height, mode_size = np.frombuffer(data[:idx], np.uint32)
        idx2 = idx + mode_size
        mode = data[idx:idx2].decode("utf-8")
        size = width, height
        raw = data[idx2:]
        return Image.frombytes(mode, size, raw)  # pyright: ignore


class IntSerializer(Serializer):
    def serialize(self, item: int) -> bytes:
        return str(item).encode("utf-8")

    def deserialize(self, data: bytes) -> int:
        return int(data.decode("utf-8"))


class JPEGSerializer(Serializer):
    def serialize(self, obj: Image) -> bytes:
        if isinstance(obj, JpegImageFile) and hasattr(obj, "filename"):
            with open(obj.filename, "rb") as f:
                return f.read()
        else:
            out = BytesIO()
            obj.save(out, format="JPEG")
            return out.getvalue()

    def deserialize(self, data: bytes) -> Image:
        inp = BytesIO(data)
        return Image.open(inp)


_SERIALIZERS = {
    "pil": PILSerializer(),
    "int": IntSerializer(),
    "jpeg": JPEGSerializer(),
}