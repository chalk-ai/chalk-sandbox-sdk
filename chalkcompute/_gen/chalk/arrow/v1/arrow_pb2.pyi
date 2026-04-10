from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import (
    ClassVar as _ClassVar,
    Iterable as _Iterable,
    Mapping as _Mapping,
    Optional as _Optional,
    Union as _Union,
)

DESCRIPTOR: _descriptor.FileDescriptor

class TimeUnit(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TIME_UNIT_UNSPECIFIED: _ClassVar[TimeUnit]
    TIME_UNIT_SECOND: _ClassVar[TimeUnit]
    TIME_UNIT_MILLISECOND: _ClassVar[TimeUnit]
    TIME_UNIT_MICROSECOND: _ClassVar[TimeUnit]
    TIME_UNIT_NANOSECOND: _ClassVar[TimeUnit]

class FeatherCompression(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FEATHER_COMPRESSION_UNSPECIFIED: _ClassVar[FeatherCompression]
    FEATHER_COMPRESSION_UNCOMPRESSED: _ClassVar[FeatherCompression]
    FEATHER_COMPRESSION_LZ4: _ClassVar[FeatherCompression]
    FEATHER_COMPRESSION_ZSTD: _ClassVar[FeatherCompression]

TIME_UNIT_UNSPECIFIED: TimeUnit
TIME_UNIT_SECOND: TimeUnit
TIME_UNIT_MILLISECOND: TimeUnit
TIME_UNIT_MICROSECOND: TimeUnit
TIME_UNIT_NANOSECOND: TimeUnit
FEATHER_COMPRESSION_UNSPECIFIED: FeatherCompression
FEATHER_COMPRESSION_UNCOMPRESSED: FeatherCompression
FEATHER_COMPRESSION_LZ4: FeatherCompression
FEATHER_COMPRESSION_ZSTD: FeatherCompression

class Schema(_message.Message):
    __slots__ = ("columns", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    COLUMNS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    columns: _containers.RepeatedCompositeFieldContainer[Field]
    metadata: _containers.ScalarMap[str, str]
    def __init__(
        self,
        columns: _Optional[_Iterable[_Union[Field, _Mapping]]] = ...,
        metadata: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class Field(_message.Message):
    __slots__ = ("name", "arrow_type", "nullable", "children", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

    NAME_FIELD_NUMBER: _ClassVar[int]
    ARROW_TYPE_FIELD_NUMBER: _ClassVar[int]
    NULLABLE_FIELD_NUMBER: _ClassVar[int]
    CHILDREN_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    name: str
    arrow_type: ArrowType
    nullable: bool
    children: _containers.RepeatedCompositeFieldContainer[Field]
    metadata: _containers.ScalarMap[str, str]
    def __init__(
        self,
        name: _Optional[str] = ...,
        arrow_type: _Optional[_Union[ArrowType, _Mapping]] = ...,
        nullable: bool = ...,
        children: _Optional[_Iterable[_Union[Field, _Mapping]]] = ...,
        metadata: _Optional[_Mapping[str, str]] = ...,
    ) -> None: ...

class FixedSizeBinary(_message.Message):
    __slots__ = ("length",)
    LENGTH_FIELD_NUMBER: _ClassVar[int]
    length: int
    def __init__(self, length: _Optional[int] = ...) -> None: ...

class Timestamp(_message.Message):
    __slots__ = ("time_unit", "timezone")
    TIME_UNIT_FIELD_NUMBER: _ClassVar[int]
    TIMEZONE_FIELD_NUMBER: _ClassVar[int]
    time_unit: TimeUnit
    timezone: str
    def __init__(self, time_unit: _Optional[_Union[TimeUnit, str]] = ..., timezone: _Optional[str] = ...) -> None: ...

class Decimal(_message.Message):
    __slots__ = ("precision", "scale")
    PRECISION_FIELD_NUMBER: _ClassVar[int]
    SCALE_FIELD_NUMBER: _ClassVar[int]
    precision: int
    scale: int
    def __init__(self, precision: _Optional[int] = ..., scale: _Optional[int] = ...) -> None: ...

class List(_message.Message):
    __slots__ = ("field_type",)
    FIELD_TYPE_FIELD_NUMBER: _ClassVar[int]
    field_type: Field
    def __init__(self, field_type: _Optional[_Union[Field, _Mapping]] = ...) -> None: ...

class FixedSizeList(_message.Message):
    __slots__ = ("field_type", "list_size")
    FIELD_TYPE_FIELD_NUMBER: _ClassVar[int]
    LIST_SIZE_FIELD_NUMBER: _ClassVar[int]
    field_type: Field
    list_size: int
    def __init__(
        self, field_type: _Optional[_Union[Field, _Mapping]] = ..., list_size: _Optional[int] = ...
    ) -> None: ...

class Struct(_message.Message):
    __slots__ = ("sub_field_types",)
    SUB_FIELD_TYPES_FIELD_NUMBER: _ClassVar[int]
    sub_field_types: _containers.RepeatedCompositeFieldContainer[Field]
    def __init__(self, sub_field_types: _Optional[_Iterable[_Union[Field, _Mapping]]] = ...) -> None: ...

class Map(_message.Message):
    __slots__ = ("key_field", "item_field", "keys_sorted")
    KEY_FIELD_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_FIELD_NUMBER: _ClassVar[int]
    KEYS_SORTED_FIELD_NUMBER: _ClassVar[int]
    key_field: Field
    item_field: Field
    keys_sorted: bool
    def __init__(
        self,
        key_field: _Optional[_Union[Field, _Mapping]] = ...,
        item_field: _Optional[_Union[Field, _Mapping]] = ...,
        keys_sorted: bool = ...,
    ) -> None: ...

class ScalarListValue(_message.Message):
    __slots__ = ("arrow_data", "schema")
    ARROW_DATA_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_FIELD_NUMBER: _ClassVar[int]
    arrow_data: bytes
    schema: Schema
    def __init__(
        self, arrow_data: _Optional[bytes] = ..., schema: _Optional[_Union[Schema, _Mapping]] = ...
    ) -> None: ...

class ScalarTime32Value(_message.Message):
    __slots__ = ("time32_second_value", "time32_millisecond_value")
    TIME32_SECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME32_MILLISECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    time32_second_value: int
    time32_millisecond_value: int
    def __init__(
        self, time32_second_value: _Optional[int] = ..., time32_millisecond_value: _Optional[int] = ...
    ) -> None: ...

class ScalarTime64Value(_message.Message):
    __slots__ = ("time64_microsecond_value", "time64_nanosecond_value")
    TIME64_MICROSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME64_NANOSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    time64_microsecond_value: int
    time64_nanosecond_value: int
    def __init__(
        self, time64_microsecond_value: _Optional[int] = ..., time64_nanosecond_value: _Optional[int] = ...
    ) -> None: ...

class ScalarTimestampValue(_message.Message):
    __slots__ = (
        "time_microsecond_value",
        "time_nanosecond_value",
        "time_second_value",
        "time_millisecond_value",
        "timezone",
    )
    TIME_MICROSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME_NANOSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME_SECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME_MILLISECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIMEZONE_FIELD_NUMBER: _ClassVar[int]
    time_microsecond_value: int
    time_nanosecond_value: int
    time_second_value: int
    time_millisecond_value: int
    timezone: str
    def __init__(
        self,
        time_microsecond_value: _Optional[int] = ...,
        time_nanosecond_value: _Optional[int] = ...,
        time_second_value: _Optional[int] = ...,
        time_millisecond_value: _Optional[int] = ...,
        timezone: _Optional[str] = ...,
    ) -> None: ...

class StructValue(_message.Message):
    __slots__ = ("field_values", "fields")
    FIELD_VALUES_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    field_values: _containers.RepeatedCompositeFieldContainer[ScalarValue]
    fields: _containers.RepeatedCompositeFieldContainer[Field]
    def __init__(
        self,
        field_values: _Optional[_Iterable[_Union[ScalarValue, _Mapping]]] = ...,
        fields: _Optional[_Iterable[_Union[Field, _Mapping]]] = ...,
    ) -> None: ...

class ScalarFixedSizeBinary(_message.Message):
    __slots__ = ("values", "length")
    VALUES_FIELD_NUMBER: _ClassVar[int]
    LENGTH_FIELD_NUMBER: _ClassVar[int]
    values: bytes
    length: int
    def __init__(self, values: _Optional[bytes] = ..., length: _Optional[int] = ...) -> None: ...

class DecimalValue(_message.Message):
    __slots__ = ("value", "precision", "scale")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    PRECISION_FIELD_NUMBER: _ClassVar[int]
    SCALE_FIELD_NUMBER: _ClassVar[int]
    value: bytes
    precision: int
    scale: int
    def __init__(
        self, value: _Optional[bytes] = ..., precision: _Optional[int] = ..., scale: _Optional[int] = ...
    ) -> None: ...

class Extension(_message.Message):
    __slots__ = ("name", "storage_type")
    NAME_FIELD_NUMBER: _ClassVar[int]
    STORAGE_TYPE_FIELD_NUMBER: _ClassVar[int]
    name: str
    storage_type: ArrowType
    def __init__(
        self, name: _Optional[str] = ..., storage_type: _Optional[_Union[ArrowType, _Mapping]] = ...
    ) -> None: ...

class ExtensionValue(_message.Message):
    __slots__ = ("extension_type", "storage_value")
    EXTENSION_TYPE_FIELD_NUMBER: _ClassVar[int]
    STORAGE_VALUE_FIELD_NUMBER: _ClassVar[int]
    extension_type: Extension
    storage_value: ScalarValue
    def __init__(
        self,
        extension_type: _Optional[_Union[Extension, _Mapping]] = ...,
        storage_value: _Optional[_Union[ScalarValue, _Mapping]] = ...,
    ) -> None: ...

class ScalarValue(_message.Message):
    __slots__ = (
        "null_value",
        "bool_value",
        "float64_value",
        "int64_value",
        "large_utf8_value",
        "timestamp_value",
        "date_64_value",
        "struct_value",
        "large_list_value",
        "time64_value",
        "duration_second_value",
        "duration_millisecond_value",
        "duration_microsecond_value",
        "duration_nanosecond_value",
        "utf8_value",
        "int8_value",
        "int16_value",
        "int32_value",
        "uint8_value",
        "uint16_value",
        "uint32_value",
        "uint64_value",
        "float16_value",
        "float32_value",
        "date_32_value",
        "time32_value",
        "list_value",
        "fixed_size_list_value",
        "map_value",
        "binary_value",
        "large_binary_value",
        "fixed_size_binary_value",
        "decimal128_value",
        "decimal256_value",
        "extension_value",
    )
    NULL_VALUE_FIELD_NUMBER: _ClassVar[int]
    BOOL_VALUE_FIELD_NUMBER: _ClassVar[int]
    FLOAT64_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT64_VALUE_FIELD_NUMBER: _ClassVar[int]
    LARGE_UTF8_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_VALUE_FIELD_NUMBER: _ClassVar[int]
    DATE_64_VALUE_FIELD_NUMBER: _ClassVar[int]
    STRUCT_VALUE_FIELD_NUMBER: _ClassVar[int]
    LARGE_LIST_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME64_VALUE_FIELD_NUMBER: _ClassVar[int]
    DURATION_SECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    DURATION_MILLISECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    DURATION_MICROSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    DURATION_NANOSECOND_VALUE_FIELD_NUMBER: _ClassVar[int]
    UTF8_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT8_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT16_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT32_VALUE_FIELD_NUMBER: _ClassVar[int]
    UINT8_VALUE_FIELD_NUMBER: _ClassVar[int]
    UINT16_VALUE_FIELD_NUMBER: _ClassVar[int]
    UINT32_VALUE_FIELD_NUMBER: _ClassVar[int]
    UINT64_VALUE_FIELD_NUMBER: _ClassVar[int]
    FLOAT16_VALUE_FIELD_NUMBER: _ClassVar[int]
    FLOAT32_VALUE_FIELD_NUMBER: _ClassVar[int]
    DATE_32_VALUE_FIELD_NUMBER: _ClassVar[int]
    TIME32_VALUE_FIELD_NUMBER: _ClassVar[int]
    LIST_VALUE_FIELD_NUMBER: _ClassVar[int]
    FIXED_SIZE_LIST_VALUE_FIELD_NUMBER: _ClassVar[int]
    MAP_VALUE_FIELD_NUMBER: _ClassVar[int]
    BINARY_VALUE_FIELD_NUMBER: _ClassVar[int]
    LARGE_BINARY_VALUE_FIELD_NUMBER: _ClassVar[int]
    FIXED_SIZE_BINARY_VALUE_FIELD_NUMBER: _ClassVar[int]
    DECIMAL128_VALUE_FIELD_NUMBER: _ClassVar[int]
    DECIMAL256_VALUE_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_VALUE_FIELD_NUMBER: _ClassVar[int]
    null_value: ArrowType
    bool_value: bool
    float64_value: float
    int64_value: int
    large_utf8_value: str
    timestamp_value: ScalarTimestampValue
    date_64_value: int
    struct_value: StructValue
    large_list_value: ScalarListValue
    time64_value: ScalarTime64Value
    duration_second_value: int
    duration_millisecond_value: int
    duration_microsecond_value: int
    duration_nanosecond_value: int
    utf8_value: str
    int8_value: int
    int16_value: int
    int32_value: int
    uint8_value: int
    uint16_value: int
    uint32_value: int
    uint64_value: int
    float16_value: float
    float32_value: float
    date_32_value: int
    time32_value: ScalarTime32Value
    list_value: ScalarListValue
    fixed_size_list_value: ScalarListValue
    map_value: ScalarListValue
    binary_value: bytes
    large_binary_value: bytes
    fixed_size_binary_value: ScalarFixedSizeBinary
    decimal128_value: DecimalValue
    decimal256_value: DecimalValue
    extension_value: ExtensionValue
    def __init__(
        self,
        null_value: _Optional[_Union[ArrowType, _Mapping]] = ...,
        bool_value: bool = ...,
        float64_value: _Optional[float] = ...,
        int64_value: _Optional[int] = ...,
        large_utf8_value: _Optional[str] = ...,
        timestamp_value: _Optional[_Union[ScalarTimestampValue, _Mapping]] = ...,
        date_64_value: _Optional[int] = ...,
        struct_value: _Optional[_Union[StructValue, _Mapping]] = ...,
        large_list_value: _Optional[_Union[ScalarListValue, _Mapping]] = ...,
        time64_value: _Optional[_Union[ScalarTime64Value, _Mapping]] = ...,
        duration_second_value: _Optional[int] = ...,
        duration_millisecond_value: _Optional[int] = ...,
        duration_microsecond_value: _Optional[int] = ...,
        duration_nanosecond_value: _Optional[int] = ...,
        utf8_value: _Optional[str] = ...,
        int8_value: _Optional[int] = ...,
        int16_value: _Optional[int] = ...,
        int32_value: _Optional[int] = ...,
        uint8_value: _Optional[int] = ...,
        uint16_value: _Optional[int] = ...,
        uint32_value: _Optional[int] = ...,
        uint64_value: _Optional[int] = ...,
        float16_value: _Optional[float] = ...,
        float32_value: _Optional[float] = ...,
        date_32_value: _Optional[int] = ...,
        time32_value: _Optional[_Union[ScalarTime32Value, _Mapping]] = ...,
        list_value: _Optional[_Union[ScalarListValue, _Mapping]] = ...,
        fixed_size_list_value: _Optional[_Union[ScalarListValue, _Mapping]] = ...,
        map_value: _Optional[_Union[ScalarListValue, _Mapping]] = ...,
        binary_value: _Optional[bytes] = ...,
        large_binary_value: _Optional[bytes] = ...,
        fixed_size_binary_value: _Optional[_Union[ScalarFixedSizeBinary, _Mapping]] = ...,
        decimal128_value: _Optional[_Union[DecimalValue, _Mapping]] = ...,
        decimal256_value: _Optional[_Union[DecimalValue, _Mapping]] = ...,
        extension_value: _Optional[_Union[ExtensionValue, _Mapping]] = ...,
    ) -> None: ...

class ArrowType(_message.Message):
    __slots__ = (
        "none",
        "bool",
        "float64",
        "int64",
        "large_utf8",
        "timestamp",
        "date64",
        "struct",
        "large_list",
        "time64",
        "duration",
        "utf8",
        "int8",
        "int16",
        "int32",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float16",
        "float32",
        "date32",
        "time32",
        "list",
        "fixed_size_list",
        "binary",
        "large_binary",
        "fixed_size_binary",
        "decimal_128",
        "decimal_256",
        "map",
        "extension",
    )
    NONE_FIELD_NUMBER: _ClassVar[int]
    BOOL_FIELD_NUMBER: _ClassVar[int]
    FLOAT64_FIELD_NUMBER: _ClassVar[int]
    INT64_FIELD_NUMBER: _ClassVar[int]
    LARGE_UTF8_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    DATE64_FIELD_NUMBER: _ClassVar[int]
    STRUCT_FIELD_NUMBER: _ClassVar[int]
    LARGE_LIST_FIELD_NUMBER: _ClassVar[int]
    TIME64_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    UTF8_FIELD_NUMBER: _ClassVar[int]
    INT8_FIELD_NUMBER: _ClassVar[int]
    INT16_FIELD_NUMBER: _ClassVar[int]
    INT32_FIELD_NUMBER: _ClassVar[int]
    UINT8_FIELD_NUMBER: _ClassVar[int]
    UINT16_FIELD_NUMBER: _ClassVar[int]
    UINT32_FIELD_NUMBER: _ClassVar[int]
    UINT64_FIELD_NUMBER: _ClassVar[int]
    FLOAT16_FIELD_NUMBER: _ClassVar[int]
    FLOAT32_FIELD_NUMBER: _ClassVar[int]
    DATE32_FIELD_NUMBER: _ClassVar[int]
    TIME32_FIELD_NUMBER: _ClassVar[int]
    LIST_FIELD_NUMBER: _ClassVar[int]
    FIXED_SIZE_LIST_FIELD_NUMBER: _ClassVar[int]
    BINARY_FIELD_NUMBER: _ClassVar[int]
    LARGE_BINARY_FIELD_NUMBER: _ClassVar[int]
    FIXED_SIZE_BINARY_FIELD_NUMBER: _ClassVar[int]
    DECIMAL_128_FIELD_NUMBER: _ClassVar[int]
    DECIMAL_256_FIELD_NUMBER: _ClassVar[int]
    MAP_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_FIELD_NUMBER: _ClassVar[int]
    none: EmptyMessage
    bool: EmptyMessage
    float64: EmptyMessage
    int64: EmptyMessage
    large_utf8: EmptyMessage
    timestamp: Timestamp
    date64: EmptyMessage
    struct: Struct
    large_list: List
    time64: TimeUnit
    duration: TimeUnit
    utf8: EmptyMessage
    int8: EmptyMessage
    int16: EmptyMessage
    int32: EmptyMessage
    uint8: EmptyMessage
    uint16: EmptyMessage
    uint32: EmptyMessage
    uint64: EmptyMessage
    float16: EmptyMessage
    float32: EmptyMessage
    date32: EmptyMessage
    time32: TimeUnit
    list: List
    fixed_size_list: FixedSizeList
    binary: EmptyMessage
    large_binary: EmptyMessage
    fixed_size_binary: int
    decimal_128: Decimal
    decimal_256: Decimal
    map: Map
    extension: Extension
    def __init__(
        self,
        none: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        bool: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        float64: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        int64: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        large_utf8: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        timestamp: _Optional[_Union[Timestamp, _Mapping]] = ...,
        date64: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        struct: _Optional[_Union[Struct, _Mapping]] = ...,
        large_list: _Optional[_Union[List, _Mapping]] = ...,
        time64: _Optional[_Union[TimeUnit, str]] = ...,
        duration: _Optional[_Union[TimeUnit, str]] = ...,
        utf8: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        int8: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        int16: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        int32: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        uint8: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        uint16: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        uint32: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        uint64: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        float16: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        float32: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        date32: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        time32: _Optional[_Union[TimeUnit, str]] = ...,
        list: _Optional[_Union[List, _Mapping]] = ...,
        fixed_size_list: _Optional[_Union[FixedSizeList, _Mapping]] = ...,
        binary: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        large_binary: _Optional[_Union[EmptyMessage, _Mapping]] = ...,
        fixed_size_binary: _Optional[int] = ...,
        decimal_128: _Optional[_Union[Decimal, _Mapping]] = ...,
        decimal_256: _Optional[_Union[Decimal, _Mapping]] = ...,
        map: _Optional[_Union[Map, _Mapping]] = ...,
        extension: _Optional[_Union[Extension, _Mapping]] = ...,
    ) -> None: ...

class EmptyMessage(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TableParquetBytes(_message.Message):
    __slots__ = ("encoded_parquet_bytes",)
    ENCODED_PARQUET_BYTES_FIELD_NUMBER: _ClassVar[int]
    encoded_parquet_bytes: bytes
    def __init__(self, encoded_parquet_bytes: _Optional[bytes] = ...) -> None: ...
