from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NoDataReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NO_DATA_REASON_UNSPECIFIED: _ClassVar[NoDataReason]
    NO_DATA_REASON_SUPPRESSED: _ClassVar[NoDataReason]
    NO_DATA_REASON_STATE_UNAVAILABLE: _ClassVar[NoDataReason]
    NO_DATA_REASON_NOT_PUBLISHED: _ClassVar[NoDataReason]

class LayerKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LAYER_KIND_UNSPECIFIED: _ClassVar[LayerKind]
    LAYER_KIND_INTENSIVE: _ClassVar[LayerKind]
    LAYER_KIND_EXTENSIVE: _ClassVar[LayerKind]
    LAYER_KIND_ASSET: _ClassVar[LayerKind]
    LAYER_KIND_PAIRS: _ClassVar[LayerKind]
NO_DATA_REASON_UNSPECIFIED: NoDataReason
NO_DATA_REASON_SUPPRESSED: NoDataReason
NO_DATA_REASON_STATE_UNAVAILABLE: NoDataReason
NO_DATA_REASON_NOT_PUBLISHED: NoDataReason
LAYER_KIND_UNSPECIFIED: LayerKind
LAYER_KIND_INTENSIVE: LayerKind
LAYER_KIND_EXTENSIVE: LayerKind
LAYER_KIND_ASSET: LayerKind
LAYER_KIND_PAIRS: LayerKind

class Catalogue(_message.Message):
    __slots__ = ("layers", "resolutions", "footprints", "generated_at")
    LAYERS_FIELD_NUMBER: _ClassVar[int]
    RESOLUTIONS_FIELD_NUMBER: _ClassVar[int]
    FOOTPRINTS_FIELD_NUMBER: _ClassVar[int]
    GENERATED_AT_FIELD_NUMBER: _ClassVar[int]
    layers: _containers.RepeatedCompositeFieldContainer[LayerDescriptor]
    resolutions: _containers.RepeatedCompositeFieldContainer[ResolutionDescriptor]
    footprints: _containers.RepeatedCompositeFieldContainer[Footprint]
    generated_at: str
    def __init__(self, layers: _Optional[_Iterable[_Union[LayerDescriptor, _Mapping]]] = ..., resolutions: _Optional[_Iterable[_Union[ResolutionDescriptor, _Mapping]]] = ..., footprints: _Optional[_Iterable[_Union[Footprint, _Mapping]]] = ..., generated_at: _Optional[str] = ...) -> None: ...

class LayerDescriptor(_message.Message):
    __slots__ = ("id", "title", "kind", "unit", "vintage", "domain", "reliability", "footprint_id", "resolutions", "license", "source")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    VINTAGE_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    RELIABILITY_FIELD_NUMBER: _ClassVar[int]
    FOOTPRINT_ID_FIELD_NUMBER: _ClassVar[int]
    RESOLUTIONS_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    kind: LayerKind
    unit: str
    vintage: Vintage
    domain: Domain
    reliability: Reliability
    footprint_id: str
    resolutions: _containers.RepeatedScalarFieldContainer[int]
    license: str
    source: str
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., kind: _Optional[_Union[LayerKind, str]] = ..., unit: _Optional[str] = ..., vintage: _Optional[_Union[Vintage, _Mapping]] = ..., domain: _Optional[_Union[Domain, _Mapping]] = ..., reliability: _Optional[_Union[Reliability, _Mapping]] = ..., footprint_id: _Optional[str] = ..., resolutions: _Optional[_Iterable[int]] = ..., license: _Optional[str] = ..., source: _Optional[str] = ...) -> None: ...

class Vintage(_message.Message):
    __slots__ = ("id", "year", "period", "state_overrides")
    class StateOverridesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    YEAR_FIELD_NUMBER: _ClassVar[int]
    PERIOD_FIELD_NUMBER: _ClassVar[int]
    STATE_OVERRIDES_FIELD_NUMBER: _ClassVar[int]
    id: str
    year: int
    period: str
    state_overrides: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., year: _Optional[int] = ..., period: _Optional[str] = ..., state_overrides: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Domain(_message.Message):
    __slots__ = ("min", "max", "p05", "p50", "p95", "n")
    MIN_FIELD_NUMBER: _ClassVar[int]
    MAX_FIELD_NUMBER: _ClassVar[int]
    P05_FIELD_NUMBER: _ClassVar[int]
    P50_FIELD_NUMBER: _ClassVar[int]
    P95_FIELD_NUMBER: _ClassVar[int]
    N_FIELD_NUMBER: _ClassVar[int]
    min: float
    max: float
    p05: float
    p50: float
    p95: float
    n: int
    def __init__(self, min: _Optional[float] = ..., max: _Optional[float] = ..., p05: _Optional[float] = ..., p50: _Optional[float] = ..., p95: _Optional[float] = ..., n: _Optional[int] = ...) -> None: ...

class Reliability(_message.Message):
    __slots__ = ("n", "p50", "p90", "mute_above", "share_above_caution", "share_above_standard")
    N_FIELD_NUMBER: _ClassVar[int]
    P50_FIELD_NUMBER: _ClassVar[int]
    P90_FIELD_NUMBER: _ClassVar[int]
    MUTE_ABOVE_FIELD_NUMBER: _ClassVar[int]
    SHARE_ABOVE_CAUTION_FIELD_NUMBER: _ClassVar[int]
    SHARE_ABOVE_STANDARD_FIELD_NUMBER: _ClassVar[int]
    n: int
    p50: float
    p90: float
    mute_above: float
    share_above_caution: float
    share_above_standard: float
    def __init__(self, n: _Optional[int] = ..., p50: _Optional[float] = ..., p90: _Optional[float] = ..., mute_above: _Optional[float] = ..., share_above_caution: _Optional[float] = ..., share_above_standard: _Optional[float] = ...) -> None: ...

class Footprint(_message.Message):
    __slots__ = ("id", "states", "missing", "degraded", "note")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATES_FIELD_NUMBER: _ClassVar[int]
    MISSING_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    NOTE_FIELD_NUMBER: _ClassVar[int]
    id: str
    states: _containers.RepeatedScalarFieldContainer[str]
    missing: _containers.RepeatedScalarFieldContainer[str]
    degraded: _containers.RepeatedCompositeFieldContainer[DegradedRegion]
    note: str
    def __init__(self, id: _Optional[str] = ..., states: _Optional[_Iterable[str]] = ..., missing: _Optional[_Iterable[str]] = ..., degraded: _Optional[_Iterable[_Union[DegradedRegion, _Mapping]]] = ..., note: _Optional[str] = ...) -> None: ...

class DegradedRegion(_message.Message):
    __slots__ = ("fips", "usps", "reason")
    FIPS_FIELD_NUMBER: _ClassVar[int]
    USPS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    fips: str
    usps: str
    reason: str
    def __init__(self, fips: _Optional[str] = ..., usps: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class ResolutionDescriptor(_message.Message):
    __slots__ = ("res", "cells", "chunk_parent_res", "chunk_count", "total_bytes")
    RES_FIELD_NUMBER: _ClassVar[int]
    CELLS_FIELD_NUMBER: _ClassVar[int]
    CHUNK_PARENT_RES_FIELD_NUMBER: _ClassVar[int]
    CHUNK_COUNT_FIELD_NUMBER: _ClassVar[int]
    TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    res: int
    cells: int
    chunk_parent_res: int
    chunk_count: int
    total_bytes: int
    def __init__(self, res: _Optional[int] = ..., cells: _Optional[int] = ..., chunk_parent_res: _Optional[int] = ..., chunk_count: _Optional[int] = ..., total_bytes: _Optional[int] = ...) -> None: ...

class ChunkRef(_message.Message):
    __slots__ = ("layer_id", "res", "ancestor_cell", "vintage_id", "path", "cells", "bytes", "content_hash")
    LAYER_ID_FIELD_NUMBER: _ClassVar[int]
    RES_FIELD_NUMBER: _ClassVar[int]
    ANCESTOR_CELL_FIELD_NUMBER: _ClassVar[int]
    VINTAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    CELLS_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    layer_id: str
    res: int
    ancestor_cell: str
    vintage_id: str
    path: str
    cells: int
    bytes: int
    content_hash: str
    def __init__(self, layer_id: _Optional[str] = ..., res: _Optional[int] = ..., ancestor_cell: _Optional[str] = ..., vintage_id: _Optional[str] = ..., path: _Optional[str] = ..., cells: _Optional[int] = ..., bytes: _Optional[int] = ..., content_hash: _Optional[str] = ...) -> None: ...

class Chunk(_message.Message):
    __slots__ = ("res", "parent", "h", "values", "metro", "metro_share")
    RES_FIELD_NUMBER: _ClassVar[int]
    PARENT_FIELD_NUMBER: _ClassVar[int]
    H_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    METRO_FIELD_NUMBER: _ClassVar[int]
    METRO_SHARE_FIELD_NUMBER: _ClassVar[int]
    res: int
    parent: str
    h: _containers.RepeatedScalarFieldContainer[str]
    values: _containers.RepeatedCompositeFieldContainer[LayerValues]
    metro: _containers.RepeatedScalarFieldContainer[str]
    metro_share: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, res: _Optional[int] = ..., parent: _Optional[str] = ..., h: _Optional[_Iterable[str]] = ..., values: _Optional[_Iterable[_Union[LayerValues, _Mapping]]] = ..., metro: _Optional[_Iterable[str]] = ..., metro_share: _Optional[_Iterable[float]] = ...) -> None: ...

class LayerValues(_message.Message):
    __slots__ = ("layer_id", "v", "cv", "nd")
    LAYER_ID_FIELD_NUMBER: _ClassVar[int]
    V_FIELD_NUMBER: _ClassVar[int]
    CV_FIELD_NUMBER: _ClassVar[int]
    ND_FIELD_NUMBER: _ClassVar[int]
    layer_id: str
    v: _containers.RepeatedScalarFieldContainer[float]
    cv: _containers.RepeatedScalarFieldContainer[float]
    nd: _containers.RepeatedCompositeFieldContainer[NoData]
    def __init__(self, layer_id: _Optional[str] = ..., v: _Optional[_Iterable[float]] = ..., cv: _Optional[_Iterable[float]] = ..., nd: _Optional[_Iterable[_Union[NoData, _Mapping]]] = ...) -> None: ...

class NoData(_message.Message):
    __slots__ = ("index", "reason")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    index: int
    reason: NoDataReason
    def __init__(self, index: _Optional[int] = ..., reason: _Optional[_Union[NoDataReason, str]] = ...) -> None: ...

class GetCatalogueRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetCatalogueResponse(_message.Message):
    __slots__ = ("catalogue",)
    CATALOGUE_FIELD_NUMBER: _ClassVar[int]
    catalogue: Catalogue
    def __init__(self, catalogue: _Optional[_Union[Catalogue, _Mapping]] = ...) -> None: ...

class GetChunkRequest(_message.Message):
    __slots__ = ("ref",)
    REF_FIELD_NUMBER: _ClassVar[int]
    ref: ChunkRef
    def __init__(self, ref: _Optional[_Union[ChunkRef, _Mapping]] = ...) -> None: ...

class GetChunkResponse(_message.Message):
    __slots__ = ("chunk",)
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    chunk: Chunk
    def __init__(self, chunk: _Optional[_Union[Chunk, _Mapping]] = ...) -> None: ...
