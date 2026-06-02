"""Private protobuf descriptor pool for generated Chalk protos.

The generated ``chalkcompute._gen`` modules use this pool instead of
``descriptor_pool.Default()`` so they can coexist with chalkpy-generated protos
from a different Chalk proto revision in the same Python process.
"""

from __future__ import annotations

from typing import cast

from google.protobuf import descriptor_pb2
from google.protobuf import descriptor_pool
from google.protobuf import duration_pb2
from google.protobuf import field_mask_pb2
from google.protobuf import struct_pb2
from google.protobuf import symbol_database
from google.protobuf import timestamp_pb2

DESCRIPTOR_POOL = descriptor_pool.DescriptorPool()  # ty: ignore[possibly-missing-implicit-call]
SYMBOL_DATABASE = symbol_database.SymbolDatabase(pool=DESCRIPTOR_POOL)


def _add(serialized_file: bytes) -> None:
    DESCRIPTOR_POOL.AddSerializedFile(serialized_file)


for _module in (
    descriptor_pb2,
    duration_pb2,
    field_mask_pb2,
    struct_pb2,
    timestamp_pb2,
):
    _add(cast(bytes, _module.DESCRIPTOR.serialized_pb))

del _module
