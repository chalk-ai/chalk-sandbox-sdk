from chalkcompute._gen.buf.validate import validate_pb2 as _validate_pb2
from chalkcompute._gen.chalk.auth.v1 import permissions_pb2 as _permissions_pb2
from chalkcompute._gen.chalk.flags.v1 import flags_pb2 as _flags_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
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

class VolumeAccessMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VOLUME_ACCESS_MODE_UNSPECIFIED: _ClassVar[VolumeAccessMode]
    VOLUME_ACCESS_MODE_READ_ONLY: _ClassVar[VolumeAccessMode]
    VOLUME_ACCESS_MODE_READ_WRITE: _ClassVar[VolumeAccessMode]

class FileKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILE_KIND_UNSPECIFIED: _ClassVar[FileKind]
    FILE_KIND_FILE: _ClassVar[FileKind]
    FILE_KIND_DIRECTORY: _ClassVar[FileKind]
    FILE_KIND_SYMLINK: _ClassVar[FileKind]

class PathWriteMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PATH_WRITE_MODE_UNSPECIFIED: _ClassVar[PathWriteMode]
    PATH_WRITE_MODE_UPSERT: _ClassVar[PathWriteMode]
    PATH_WRITE_MODE_CREATE: _ClassVar[PathWriteMode]
    PATH_WRITE_MODE_REPLACE: _ClassVar[PathWriteMode]

class CommitResult(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMMIT_RESULT_UNSPECIFIED: _ClassVar[CommitResult]
    COMMIT_RESULT_PENDING: _ClassVar[CommitResult]
    COMMIT_RESULT_COMMITTED: _ClassVar[CommitResult]
    COMMIT_RESULT_REBASE_REQUIRED: _ClassVar[CommitResult]
    COMMIT_RESULT_ABORTED: _ClassVar[CommitResult]

class UploadedObjectKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    UPLOADED_OBJECT_KIND_UNSPECIFIED: _ClassVar[UploadedObjectKind]
    UPLOADED_OBJECT_KIND_CHUNK: _ClassVar[UploadedObjectKind]
    UPLOADED_OBJECT_KIND_PACK: _ClassVar[UploadedObjectKind]
    UPLOADED_OBJECT_KIND_INTENT: _ClassVar[UploadedObjectKind]

class VolumeKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VOLUME_KIND_UNSPECIFIED: _ClassVar[VolumeKind]
    VOLUME_KIND_SOURCE_CODE: _ClassVar[VolumeKind]

VOLUME_ACCESS_MODE_UNSPECIFIED: VolumeAccessMode
VOLUME_ACCESS_MODE_READ_ONLY: VolumeAccessMode
VOLUME_ACCESS_MODE_READ_WRITE: VolumeAccessMode
FILE_KIND_UNSPECIFIED: FileKind
FILE_KIND_FILE: FileKind
FILE_KIND_DIRECTORY: FileKind
FILE_KIND_SYMLINK: FileKind
PATH_WRITE_MODE_UNSPECIFIED: PathWriteMode
PATH_WRITE_MODE_UPSERT: PathWriteMode
PATH_WRITE_MODE_CREATE: PathWriteMode
PATH_WRITE_MODE_REPLACE: PathWriteMode
COMMIT_RESULT_UNSPECIFIED: CommitResult
COMMIT_RESULT_PENDING: CommitResult
COMMIT_RESULT_COMMITTED: CommitResult
COMMIT_RESULT_REBASE_REQUIRED: CommitResult
COMMIT_RESULT_ABORTED: CommitResult
UPLOADED_OBJECT_KIND_UNSPECIFIED: UploadedObjectKind
UPLOADED_OBJECT_KIND_CHUNK: UploadedObjectKind
UPLOADED_OBJECT_KIND_PACK: UploadedObjectKind
UPLOADED_OBJECT_KIND_INTENT: UploadedObjectKind
VOLUME_KIND_UNSPECIFIED: VolumeKind
VOLUME_KIND_SOURCE_CODE: VolumeKind

class VolumeInfo(_message.Message):
    __slots__ = (
        "volume_id",
        "name",
        "created_at",
        "access_mode",
        "ref",
        "object_store_uri",
    )
    VOLUME_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    ACCESS_MODE_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    OBJECT_STORE_URI_FIELD_NUMBER: _ClassVar[int]
    volume_id: str
    name: str
    created_at: _timestamp_pb2.Timestamp
    access_mode: VolumeAccessMode
    ref: str
    object_store_uri: str
    def __init__(
        self,
        volume_id: _Optional[str] = ...,
        name: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        access_mode: _Optional[_Union[VolumeAccessMode, str]] = ...,
        ref: _Optional[str] = ...,
        object_store_uri: _Optional[str] = ...,
    ) -> None: ...

class VolumeRef(_message.Message):
    __slots__ = ("volume_id", "name")
    VOLUME_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    volume_id: str
    name: str
    def __init__(
        self, volume_id: _Optional[str] = ..., name: _Optional[str] = ...
    ) -> None: ...

class VersionInfo(_message.Message):
    __slots__ = (
        "version_id",
        "sequence_number",
        "parent_id",
        "created_at",
        "ref",
        "primary_pack_id",
        "commit_id",
    )
    VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    PARENT_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_PACK_ID_FIELD_NUMBER: _ClassVar[int]
    COMMIT_ID_FIELD_NUMBER: _ClassVar[int]
    version_id: int
    sequence_number: int
    parent_id: int
    created_at: _timestamp_pb2.Timestamp
    ref: str
    primary_pack_id: str
    commit_id: str
    def __init__(
        self,
        version_id: _Optional[int] = ...,
        sequence_number: _Optional[int] = ...,
        parent_id: _Optional[int] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        ref: _Optional[str] = ...,
        primary_pack_id: _Optional[str] = ...,
        commit_id: _Optional[str] = ...,
    ) -> None: ...

class VersionSelector(_message.Message):
    __slots__ = ("ref", "version_id")
    REF_FIELD_NUMBER: _ClassVar[int]
    VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    ref: str
    version_id: int
    def __init__(
        self, ref: _Optional[str] = ..., version_id: _Optional[int] = ...
    ) -> None: ...

class FileMetadata(_message.Message):
    __slots__ = ("mode", "updated_at", "uid", "gid")
    MODE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    GID_FIELD_NUMBER: _ClassVar[int]
    mode: int
    updated_at: _timestamp_pb2.Timestamp
    uid: int
    gid: int
    def __init__(
        self,
        mode: _Optional[int] = ...,
        updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        uid: _Optional[int] = ...,
        gid: _Optional[int] = ...,
    ) -> None: ...

class FileInfo(_message.Message):
    __slots__ = (
        "path",
        "size",
        "mode",
        "updated_at",
        "kind",
        "hash",
        "uid",
        "gid",
        "nlink",
    )
    PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    GID_FIELD_NUMBER: _ClassVar[int]
    NLINK_FIELD_NUMBER: _ClassVar[int]
    path: str
    size: int
    mode: int
    updated_at: _timestamp_pb2.Timestamp
    kind: FileKind
    hash: str
    uid: int
    gid: int
    nlink: int
    def __init__(
        self,
        path: _Optional[str] = ...,
        size: _Optional[int] = ...,
        mode: _Optional[int] = ...,
        updated_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        kind: _Optional[_Union[FileKind, str]] = ...,
        hash: _Optional[str] = ...,
        uid: _Optional[int] = ...,
        gid: _Optional[int] = ...,
        nlink: _Optional[int] = ...,
    ) -> None: ...

class EmptyFileContent(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class InlineFileContent(_message.Message):
    __slots__ = ("data", "hash", "size")
    DATA_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    hash: str
    size: int
    def __init__(
        self,
        data: _Optional[bytes] = ...,
        hash: _Optional[str] = ...,
        size: _Optional[int] = ...,
    ) -> None: ...

class ChunkRef(_message.Message):
    __slots__ = ("object_key", "hash", "size", "offset")
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    object_key: str
    hash: str
    size: int
    offset: int
    def __init__(
        self,
        object_key: _Optional[str] = ...,
        hash: _Optional[str] = ...,
        size: _Optional[int] = ...,
        offset: _Optional[int] = ...,
    ) -> None: ...

class PackEntryRef(_message.Message):
    __slots__ = ("object_key", "pack_id", "offset", "size")
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    PACK_ID_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    object_key: str
    pack_id: str
    offset: int
    size: int
    def __init__(
        self,
        object_key: _Optional[str] = ...,
        pack_id: _Optional[str] = ...,
        offset: _Optional[int] = ...,
        size: _Optional[int] = ...,
    ) -> None: ...

class ChunkedContentRef(_message.Message):
    __slots__ = ("hash", "size", "chunks")
    HASH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    CHUNKS_FIELD_NUMBER: _ClassVar[int]
    hash: str
    size: int
    chunks: _containers.RepeatedCompositeFieldContainer[ChunkRef]
    def __init__(
        self,
        hash: _Optional[str] = ...,
        size: _Optional[int] = ...,
        chunks: _Optional[_Iterable[_Union[ChunkRef, _Mapping]]] = ...,
    ) -> None: ...

class PackedContentRef(_message.Message):
    __slots__ = ("hash", "size", "pack")
    HASH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    PACK_FIELD_NUMBER: _ClassVar[int]
    hash: str
    size: int
    pack: PackEntryRef
    def __init__(
        self,
        hash: _Optional[str] = ...,
        size: _Optional[int] = ...,
        pack: _Optional[_Union[PackEntryRef, _Mapping]] = ...,
    ) -> None: ...

class ContentRef(_message.Message):
    __slots__ = ("empty", "inline", "chunked", "packed")
    EMPTY_FIELD_NUMBER: _ClassVar[int]
    INLINE_FIELD_NUMBER: _ClassVar[int]
    CHUNKED_FIELD_NUMBER: _ClassVar[int]
    PACKED_FIELD_NUMBER: _ClassVar[int]
    empty: EmptyFileContent
    inline: InlineFileContent
    chunked: ChunkedContentRef
    packed: PackedContentRef
    def __init__(
        self,
        empty: _Optional[_Union[EmptyFileContent, _Mapping]] = ...,
        inline: _Optional[_Union[InlineFileContent, _Mapping]] = ...,
        chunked: _Optional[_Union[ChunkedContentRef, _Mapping]] = ...,
        packed: _Optional[_Union[PackedContentRef, _Mapping]] = ...,
    ) -> None: ...

class RegularFileNode(_message.Message):
    __slots__ = ("content",)
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    content: ContentRef
    def __init__(
        self, content: _Optional[_Union[ContentRef, _Mapping]] = ...
    ) -> None: ...

class DirectoryNode(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SymlinkNode(_message.Message):
    __slots__ = ("target",)
    TARGET_FIELD_NUMBER: _ClassVar[int]
    target: bytes
    def __init__(self, target: _Optional[bytes] = ...) -> None: ...

class FileNode(_message.Message):
    __slots__ = ("metadata", "file", "directory", "symlink")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    SYMLINK_FIELD_NUMBER: _ClassVar[int]
    metadata: FileMetadata
    file: RegularFileNode
    directory: DirectoryNode
    symlink: SymlinkNode
    def __init__(
        self,
        metadata: _Optional[_Union[FileMetadata, _Mapping]] = ...,
        file: _Optional[_Union[RegularFileNode, _Mapping]] = ...,
        directory: _Optional[_Union[DirectoryNode, _Mapping]] = ...,
        symlink: _Optional[_Union[SymlinkNode, _Mapping]] = ...,
    ) -> None: ...

class SignedChunkRef(_message.Message):
    __slots__ = ("signed_download_uri", "offset", "size", "hash", "expires_at")
    SIGNED_DOWNLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    signed_download_uri: str
    offset: int
    size: int
    hash: str
    expires_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        signed_download_uri: _Optional[str] = ...,
        offset: _Optional[int] = ...,
        size: _Optional[int] = ...,
        hash: _Optional[str] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class SignedPackEntryRef(_message.Message):
    __slots__ = ("signed_download_uri", "offset", "size", "pack_id", "expires_at")
    SIGNED_DOWNLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    PACK_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    signed_download_uri: str
    offset: int
    size: int
    pack_id: str
    expires_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        signed_download_uri: _Optional[str] = ...,
        offset: _Optional[int] = ...,
        size: _Optional[int] = ...,
        pack_id: _Optional[str] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class ChunkedFileContent(_message.Message):
    __slots__ = ("chunks",)
    CHUNKS_FIELD_NUMBER: _ClassVar[int]
    chunks: _containers.RepeatedCompositeFieldContainer[SignedChunkRef]
    def __init__(
        self, chunks: _Optional[_Iterable[_Union[SignedChunkRef, _Mapping]]] = ...
    ) -> None: ...

class PackedFileContent(_message.Message):
    __slots__ = ("pack",)
    PACK_FIELD_NUMBER: _ClassVar[int]
    pack: SignedPackEntryRef
    def __init__(
        self, pack: _Optional[_Union[SignedPackEntryRef, _Mapping]] = ...
    ) -> None: ...

class PathFileDelta(_message.Message):
    __slots__ = ("path", "node", "mode")
    PATH_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    path: str
    node: FileNode
    mode: PathWriteMode
    def __init__(
        self,
        path: _Optional[str] = ...,
        node: _Optional[_Union[FileNode, _Mapping]] = ...,
        mode: _Optional[_Union[PathWriteMode, str]] = ...,
    ) -> None: ...

class PathRemoveDelta(_message.Message):
    __slots__ = ("path", "recursive")
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    path: str
    recursive: bool
    def __init__(self, path: _Optional[str] = ..., recursive: bool = ...) -> None: ...

class PathDeltaList(_message.Message):
    __slots__ = ("upserts", "removes")
    UPSERTS_FIELD_NUMBER: _ClassVar[int]
    REMOVES_FIELD_NUMBER: _ClassVar[int]
    upserts: _containers.RepeatedCompositeFieldContainer[PathFileDelta]
    removes: _containers.RepeatedCompositeFieldContainer[PathRemoveDelta]
    def __init__(
        self,
        upserts: _Optional[_Iterable[_Union[PathFileDelta, _Mapping]]] = ...,
        removes: _Optional[_Iterable[_Union[PathRemoveDelta, _Mapping]]] = ...,
    ) -> None: ...

class InodeEntry(_message.Message):
    __slots__ = ("ino", "node")
    INO_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    ino: int
    node: FileNode
    def __init__(
        self,
        ino: _Optional[int] = ...,
        node: _Optional[_Union[FileNode, _Mapping]] = ...,
    ) -> None: ...

class DirentIdentifier(_message.Message):
    __slots__ = ("parent_ino", "name")
    PARENT_INO_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    parent_ino: int
    name: str
    def __init__(
        self, parent_ino: _Optional[int] = ..., name: _Optional[str] = ...
    ) -> None: ...

class DirentEntry(_message.Message):
    __slots__ = ("id", "child_ino")
    ID_FIELD_NUMBER: _ClassVar[int]
    CHILD_INO_FIELD_NUMBER: _ClassVar[int]
    id: DirentIdentifier
    child_ino: int
    def __init__(
        self,
        id: _Optional[_Union[DirentIdentifier, _Mapping]] = ...,
        child_ino: _Optional[int] = ...,
    ) -> None: ...

class DirentMove(_message.Message):
    __slots__ = ("to", "child_ino")
    FROM_FIELD_NUMBER: _ClassVar[int]
    TO_FIELD_NUMBER: _ClassVar[int]
    CHILD_INO_FIELD_NUMBER: _ClassVar[int]
    to: DirentIdentifier
    child_ino: int
    def __init__(
        self,
        to: _Optional[_Union[DirentIdentifier, _Mapping]] = ...,
        child_ino: _Optional[int] = ...,
        **kwargs,
    ) -> None: ...

class InodeMetaUpdate(_message.Message):
    __slots__ = ("ino", "metadata")
    INO_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    ino: int
    metadata: FileMetadata
    def __init__(
        self,
        ino: _Optional[int] = ...,
        metadata: _Optional[_Union[FileMetadata, _Mapping]] = ...,
    ) -> None: ...

class InodeDeltaList(_message.Message):
    __slots__ = (
        "updated_inodes",
        "updated_dirents",
        "removed_inodes",
        "removed_dirents",
        "moved_dirents",
        "meta_only_updates",
    )
    UPDATED_INODES_FIELD_NUMBER: _ClassVar[int]
    UPDATED_DIRENTS_FIELD_NUMBER: _ClassVar[int]
    REMOVED_INODES_FIELD_NUMBER: _ClassVar[int]
    REMOVED_DIRENTS_FIELD_NUMBER: _ClassVar[int]
    MOVED_DIRENTS_FIELD_NUMBER: _ClassVar[int]
    META_ONLY_UPDATES_FIELD_NUMBER: _ClassVar[int]
    updated_inodes: _containers.RepeatedCompositeFieldContainer[InodeEntry]
    updated_dirents: _containers.RepeatedCompositeFieldContainer[DirentEntry]
    removed_inodes: _containers.RepeatedScalarFieldContainer[int]
    removed_dirents: _containers.RepeatedCompositeFieldContainer[DirentIdentifier]
    moved_dirents: _containers.RepeatedCompositeFieldContainer[DirentMove]
    meta_only_updates: _containers.RepeatedCompositeFieldContainer[InodeMetaUpdate]
    def __init__(
        self,
        updated_inodes: _Optional[_Iterable[_Union[InodeEntry, _Mapping]]] = ...,
        updated_dirents: _Optional[_Iterable[_Union[DirentEntry, _Mapping]]] = ...,
        removed_inodes: _Optional[_Iterable[int]] = ...,
        removed_dirents: _Optional[_Iterable[_Union[DirentIdentifier, _Mapping]]] = ...,
        moved_dirents: _Optional[_Iterable[_Union[DirentMove, _Mapping]]] = ...,
        meta_only_updates: _Optional[
            _Iterable[_Union[InodeMetaUpdate, _Mapping]]
        ] = ...,
    ) -> None: ...

class VersionDiff(_message.Message):
    __slots__ = ("added", "modified", "removed")
    ADDED_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_FIELD_NUMBER: _ClassVar[int]
    REMOVED_FIELD_NUMBER: _ClassVar[int]
    added: _containers.RepeatedScalarFieldContainer[str]
    modified: _containers.RepeatedScalarFieldContainer[str]
    removed: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        added: _Optional[_Iterable[str]] = ...,
        modified: _Optional[_Iterable[str]] = ...,
        removed: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class UploadedObjectReference(_message.Message):
    __slots__ = ("object_key", "hash", "content_size", "kind")
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_SIZE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    object_key: str
    hash: str
    content_size: int
    kind: UploadedObjectKind
    def __init__(
        self,
        object_key: _Optional[str] = ...,
        hash: _Optional[str] = ...,
        content_size: _Optional[int] = ...,
        kind: _Optional[_Union[UploadedObjectKind, str]] = ...,
    ) -> None: ...

class UploadURLItem(_message.Message):
    __slots__ = ("object_key", "already_exists", "signed_upload_uri", "expires_at")
    OBJECT_KEY_FIELD_NUMBER: _ClassVar[int]
    ALREADY_EXISTS_FIELD_NUMBER: _ClassVar[int]
    SIGNED_UPLOAD_URI_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    object_key: str
    already_exists: bool
    signed_upload_uri: str
    expires_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        object_key: _Optional[str] = ...,
        already_exists: bool = ...,
        signed_upload_uri: _Optional[str] = ...,
        expires_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class CommitDeltasObject(_message.Message):
    __slots__ = ("path_deltas", "inode_deltas")
    PATH_DELTAS_FIELD_NUMBER: _ClassVar[int]
    INODE_DELTAS_FIELD_NUMBER: _ClassVar[int]
    path_deltas: PathDeltaList
    inode_deltas: InodeDeltaList
    def __init__(
        self,
        path_deltas: _Optional[_Union[PathDeltaList, _Mapping]] = ...,
        inode_deltas: _Optional[_Union[InodeDeltaList, _Mapping]] = ...,
    ) -> None: ...

class CommitIntent(_message.Message):
    __slots__ = (
        "volume",
        "commit_id",
        "ref",
        "base_version_id",
        "base_sequence_number",
        "uploaded_object_references",
        "author",
        "path_deltas",
        "inode_deltas",
        "uploaded_deltas",
    )
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    COMMIT_ID_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    BASE_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    BASE_SEQUENCE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    UPLOADED_OBJECT_REFERENCES_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    PATH_DELTAS_FIELD_NUMBER: _ClassVar[int]
    INODE_DELTAS_FIELD_NUMBER: _ClassVar[int]
    UPLOADED_DELTAS_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    commit_id: str
    ref: str
    base_version_id: int
    base_sequence_number: int
    uploaded_object_references: _containers.RepeatedCompositeFieldContainer[
        UploadedObjectReference
    ]
    author: str
    path_deltas: PathDeltaList
    inode_deltas: InodeDeltaList
    uploaded_deltas: UploadedObjectReference
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        commit_id: _Optional[str] = ...,
        ref: _Optional[str] = ...,
        base_version_id: _Optional[int] = ...,
        base_sequence_number: _Optional[int] = ...,
        uploaded_object_references: _Optional[
            _Iterable[_Union[UploadedObjectReference, _Mapping]]
        ] = ...,
        author: _Optional[str] = ...,
        path_deltas: _Optional[_Union[PathDeltaList, _Mapping]] = ...,
        inode_deltas: _Optional[_Union[InodeDeltaList, _Mapping]] = ...,
        uploaded_deltas: _Optional[_Union[UploadedObjectReference, _Mapping]] = ...,
    ) -> None: ...

class CommitStatus(_message.Message):
    __slots__ = (
        "volume",
        "commit_id",
        "result",
        "version",
        "latest_version",
        "created_at",
        "intent",
    )
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    COMMIT_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    LATEST_VERSION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    commit_id: str
    result: CommitResult
    version: VersionInfo
    latest_version: VersionInfo
    created_at: _timestamp_pb2.Timestamp
    intent: CommitIntent
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        commit_id: _Optional[str] = ...,
        result: _Optional[_Union[CommitResult, str]] = ...,
        version: _Optional[_Union[VersionInfo, _Mapping]] = ...,
        latest_version: _Optional[_Union[VersionInfo, _Mapping]] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
        intent: _Optional[_Union[CommitIntent, _Mapping]] = ...,
    ) -> None: ...

class CreateVolumeRequest(_message.Message):
    __slots__ = ("name", "volume_type")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VOLUME_TYPE_FIELD_NUMBER: _ClassVar[int]
    name: str
    volume_type: VolumeKind
    def __init__(
        self,
        name: _Optional[str] = ...,
        volume_type: _Optional[_Union[VolumeKind, str]] = ...,
    ) -> None: ...

class CreateVolumeResponse(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeInfo
    def __init__(
        self, volume: _Optional[_Union[VolumeInfo, _Mapping]] = ...
    ) -> None: ...

class GetVolumeRequest(_message.Message):
    __slots__ = ("volume", "selector")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    SELECTOR_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    selector: VersionSelector
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        selector: _Optional[_Union[VersionSelector, _Mapping]] = ...,
    ) -> None: ...

class GetVolumeResponse(_message.Message):
    __slots__ = ("volume", "version")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeInfo
    version: VersionInfo
    def __init__(
        self,
        volume: _Optional[_Union[VolumeInfo, _Mapping]] = ...,
        version: _Optional[_Union[VersionInfo, _Mapping]] = ...,
    ) -> None: ...

class ListVolumesRequest(_message.Message):
    __slots__ = ("limit", "cursor", "name_prefix", "volume_kind")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    NAME_PREFIX_FIELD_NUMBER: _ClassVar[int]
    VOLUME_KIND_FIELD_NUMBER: _ClassVar[int]
    limit: int
    cursor: str
    name_prefix: str
    volume_kind: VolumeKind
    def __init__(
        self,
        limit: _Optional[int] = ...,
        cursor: _Optional[str] = ...,
        name_prefix: _Optional[str] = ...,
        volume_kind: _Optional[_Union[VolumeKind, str]] = ...,
    ) -> None: ...

class ListedVolume(_message.Message):
    __slots__ = ("name", "created_at")
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    name: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(
        self,
        name: _Optional[str] = ...,
        created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...,
    ) -> None: ...

class ListVolumesResponse(_message.Message):
    __slots__ = ("volumes", "next_cursor")
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    volumes: _containers.RepeatedCompositeFieldContainer[ListedVolume]
    next_cursor: str
    def __init__(
        self,
        volumes: _Optional[_Iterable[_Union[ListedVolume, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class DeleteVolumeRequest(_message.Message):
    __slots__ = ("volume",)
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    def __init__(
        self, volume: _Optional[_Union[VolumeRef, _Mapping]] = ...
    ) -> None: ...

class DeleteVolumeResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListVolumeVersionsRequest(_message.Message):
    __slots__ = ("volume", "limit", "cursor")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    limit: int
    cursor: str
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        limit: _Optional[int] = ...,
        cursor: _Optional[str] = ...,
    ) -> None: ...

class ListVolumeVersionsResponse(_message.Message):
    __slots__ = ("versions", "next_cursor")
    VERSIONS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    versions: _containers.RepeatedCompositeFieldContainer[VersionInfo]
    next_cursor: str
    def __init__(
        self,
        versions: _Optional[_Iterable[_Union[VersionInfo, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
    ) -> None: ...

class CommitVersionRequest(_message.Message):
    __slots__ = ("intent",)
    INTENT_FIELD_NUMBER: _ClassVar[int]
    intent: CommitIntent
    def __init__(
        self, intent: _Optional[_Union[CommitIntent, _Mapping]] = ...
    ) -> None: ...

class CommitVersionResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: CommitStatus
    def __init__(
        self, status: _Optional[_Union[CommitStatus, _Mapping]] = ...
    ) -> None: ...

class GetCommitStatusRequest(_message.Message):
    __slots__ = ("volume", "commit_id", "include_diff")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    COMMIT_ID_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_DIFF_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    commit_id: str
    include_diff: bool
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        commit_id: _Optional[str] = ...,
        include_diff: bool = ...,
    ) -> None: ...

class GetCommitStatusResponse(_message.Message):
    __slots__ = ("status", "diff")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DIFF_FIELD_NUMBER: _ClassVar[int]
    status: CommitStatus
    diff: VersionDiff
    def __init__(
        self,
        status: _Optional[_Union[CommitStatus, _Mapping]] = ...,
        diff: _Optional[_Union[VersionDiff, _Mapping]] = ...,
    ) -> None: ...

class AllocateInodeRangeRequest(_message.Message):
    __slots__ = ("volume", "count", "mount_id")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    MOUNT_ID_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    count: int
    mount_id: str
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        count: _Optional[int] = ...,
        mount_id: _Optional[str] = ...,
    ) -> None: ...

class AllocateInodeRangeResponse(_message.Message):
    __slots__ = ("first_ino", "count")
    FIRST_INO_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    first_ino: int
    count: int
    def __init__(
        self, first_ino: _Optional[int] = ..., count: _Optional[int] = ...
    ) -> None: ...

class RequestUploadURLsRequest(_message.Message):
    __slots__ = ("volume", "objects")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    OBJECTS_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    objects: _containers.RepeatedCompositeFieldContainer[UploadedObjectReference]
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        objects: _Optional[_Iterable[_Union[UploadedObjectReference, _Mapping]]] = ...,
    ) -> None: ...

class RequestUploadURLsResponse(_message.Message):
    __slots__ = ("urls",)
    URLS_FIELD_NUMBER: _ClassVar[int]
    urls: _containers.RepeatedCompositeFieldContainer[UploadURLItem]
    def __init__(
        self, urls: _Optional[_Iterable[_Union[UploadURLItem, _Mapping]]] = ...
    ) -> None: ...

class ListFilesRequest(_message.Message):
    __slots__ = ("volume", "path", "recursive", "limit", "cursor", "selector")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    SELECTOR_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    path: str
    recursive: bool
    limit: int
    cursor: str
    selector: VersionSelector
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        path: _Optional[str] = ...,
        recursive: bool = ...,
        limit: _Optional[int] = ...,
        cursor: _Optional[str] = ...,
        selector: _Optional[_Union[VersionSelector, _Mapping]] = ...,
    ) -> None: ...

class ListFilesResponse(_message.Message):
    __slots__ = ("files", "next_cursor", "version")
    FILES_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    files: _containers.RepeatedCompositeFieldContainer[FileInfo]
    next_cursor: str
    version: VersionInfo
    def __init__(
        self,
        files: _Optional[_Iterable[_Union[FileInfo, _Mapping]]] = ...,
        next_cursor: _Optional[str] = ...,
        version: _Optional[_Union[VersionInfo, _Mapping]] = ...,
    ) -> None: ...

class GetFileRequest(_message.Message):
    __slots__ = ("volume", "path", "selector", "if_none_match")
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    SELECTOR_FIELD_NUMBER: _ClassVar[int]
    IF_NONE_MATCH_FIELD_NUMBER: _ClassVar[int]
    volume: VolumeRef
    path: str
    selector: VersionSelector
    if_none_match: str
    def __init__(
        self,
        volume: _Optional[_Union[VolumeRef, _Mapping]] = ...,
        path: _Optional[str] = ...,
        selector: _Optional[_Union[VersionSelector, _Mapping]] = ...,
        if_none_match: _Optional[str] = ...,
    ) -> None: ...

class GetFileResponse(_message.Message):
    __slots__ = ("file", "version", "data", "packed", "chunked", "not_modified")
    FILE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    PACKED_FIELD_NUMBER: _ClassVar[int]
    CHUNKED_FIELD_NUMBER: _ClassVar[int]
    NOT_MODIFIED_FIELD_NUMBER: _ClassVar[int]
    file: FileInfo
    version: VersionInfo
    data: bytes
    packed: PackedFileContent
    chunked: ChunkedFileContent
    not_modified: bool
    def __init__(
        self,
        file: _Optional[_Union[FileInfo, _Mapping]] = ...,
        version: _Optional[_Union[VersionInfo, _Mapping]] = ...,
        data: _Optional[bytes] = ...,
        packed: _Optional[_Union[PackedFileContent, _Mapping]] = ...,
        chunked: _Optional[_Union[ChunkedFileContent, _Mapping]] = ...,
        not_modified: bool = ...,
    ) -> None: ...
