from kingfisher.map.v1 import map_pb2 as _map_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FetchState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FETCH_STATE_UNSPECIFIED: _ClassVar[FetchState]
    FETCH_STATE_INDEXED: _ClassVar[FetchState]
    FETCH_STATE_FETCHING: _ClassVar[FetchState]
    FETCH_STATE_HELD: _ClassVar[FetchState]
    FETCH_STATE_FAILED: _ClassVar[FetchState]
    FETCH_STATE_QUEUED: _ClassVar[FetchState]
    FETCH_STATE_TOMBSTONED: _ClassVar[FetchState]
FETCH_STATE_UNSPECIFIED: FetchState
FETCH_STATE_INDEXED: FetchState
FETCH_STATE_FETCHING: FetchState
FETCH_STATE_HELD: FetchState
FETCH_STATE_FAILED: FetchState
FETCH_STATE_QUEUED: FetchState
FETCH_STATE_TOMBSTONED: FetchState

class Source(_message.Message):
    __slots__ = ("id", "title", "index_url", "expensive", "paid", "country")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    INDEX_URL_FIELD_NUMBER: _ClassVar[int]
    EXPENSIVE_FIELD_NUMBER: _ClassVar[int]
    PAID_FIELD_NUMBER: _ClassVar[int]
    COUNTRY_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    index_url: str
    expensive: bool
    paid: bool
    country: str
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., index_url: _Optional[str] = ..., expensive: _Optional[bool] = ..., paid: _Optional[bool] = ..., country: _Optional[str] = ...) -> None: ...

class DatasetRecord(_message.Message):
    __slots__ = ("id", "source_id", "title", "kind", "domain", "license", "bytes_estimate", "state", "vintage_id", "download_url", "bytes_downloaded", "last_error", "tombstone_reason", "tombstoned_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    BYTES_ESTIMATE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    VINTAGE_ID_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_URL_FIELD_NUMBER: _ClassVar[int]
    BYTES_DOWNLOADED_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    TOMBSTONE_REASON_FIELD_NUMBER: _ClassVar[int]
    TOMBSTONED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    source_id: str
    title: str
    kind: _map_pb2.LayerKind
    domain: _map_pb2.Domain
    license: str
    bytes_estimate: int
    state: FetchState
    vintage_id: str
    download_url: str
    bytes_downloaded: int
    last_error: str
    tombstone_reason: str
    tombstoned_at: str
    def __init__(self, id: _Optional[str] = ..., source_id: _Optional[str] = ..., title: _Optional[str] = ..., kind: _Optional[_Union[_map_pb2.LayerKind, str]] = ..., domain: _Optional[_Union[_map_pb2.Domain, _Mapping]] = ..., license: _Optional[str] = ..., bytes_estimate: _Optional[int] = ..., state: _Optional[_Union[FetchState, str]] = ..., vintage_id: _Optional[str] = ..., download_url: _Optional[str] = ..., bytes_downloaded: _Optional[int] = ..., last_error: _Optional[str] = ..., tombstone_reason: _Optional[str] = ..., tombstoned_at: _Optional[str] = ...) -> None: ...

class ListSourcesRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListSourcesResponse(_message.Message):
    __slots__ = ("sources",)
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    sources: _containers.RepeatedCompositeFieldContainer[Source]
    def __init__(self, sources: _Optional[_Iterable[_Union[Source, _Mapping]]] = ...) -> None: ...

class ListDatasetsRequest(_message.Message):
    __slots__ = ("source_id",)
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    def __init__(self, source_id: _Optional[str] = ...) -> None: ...

class ListDatasetsResponse(_message.Message):
    __slots__ = ("datasets",)
    DATASETS_FIELD_NUMBER: _ClassVar[int]
    datasets: _containers.RepeatedCompositeFieldContainer[DatasetRecord]
    def __init__(self, datasets: _Optional[_Iterable[_Union[DatasetRecord, _Mapping]]] = ...) -> None: ...

class RefreshIndexRequest(_message.Message):
    __slots__ = ("source_id", "bbox", "limit", "query")
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    bbox: _containers.RepeatedScalarFieldContainer[float]
    limit: int
    query: str
    def __init__(self, source_id: _Optional[str] = ..., bbox: _Optional[_Iterable[float]] = ..., limit: _Optional[int] = ..., query: _Optional[str] = ...) -> None: ...

class RefreshIndexResponse(_message.Message):
    __slots__ = ("indexed",)
    INDEXED_FIELD_NUMBER: _ClassVar[int]
    indexed: int
    def __init__(self, indexed: _Optional[int] = ...) -> None: ...

class FetchRequest(_message.Message):
    __slots__ = ("dataset_id",)
    DATASET_ID_FIELD_NUMBER: _ClassVar[int]
    dataset_id: str
    def __init__(self, dataset_id: _Optional[str] = ...) -> None: ...

class FetchResponse(_message.Message):
    __slots__ = ("ticket",)
    TICKET_FIELD_NUMBER: _ClassVar[int]
    ticket: str
    def __init__(self, ticket: _Optional[str] = ...) -> None: ...

class GetFetchStatusRequest(_message.Message):
    __slots__ = ("ticket",)
    TICKET_FIELD_NUMBER: _ClassVar[int]
    ticket: str
    def __init__(self, ticket: _Optional[str] = ...) -> None: ...

class GetFetchStatusResponse(_message.Message):
    __slots__ = ("record",)
    RECORD_FIELD_NUMBER: _ClassVar[int]
    record: DatasetRecord
    def __init__(self, record: _Optional[_Union[DatasetRecord, _Mapping]] = ...) -> None: ...

class TombstoneRequest(_message.Message):
    __slots__ = ("dataset_id", "reason", "revive")
    DATASET_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    REVIVE_FIELD_NUMBER: _ClassVar[int]
    dataset_id: str
    reason: str
    revive: bool
    def __init__(self, dataset_id: _Optional[str] = ..., reason: _Optional[str] = ..., revive: _Optional[bool] = ...) -> None: ...

class TombstoneResponse(_message.Message):
    __slots__ = ("record",)
    RECORD_FIELD_NUMBER: _ClassVar[int]
    record: DatasetRecord
    def __init__(self, record: _Optional[_Union[DatasetRecord, _Mapping]] = ...) -> None: ...
