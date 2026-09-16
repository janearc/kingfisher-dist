from kingfisher.routing.v1 import routing_pb2 as _routing_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CellCover(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CELL_COVER_UNSPECIFIED: _ClassVar[CellCover]
    CELL_COVER_LAND: _ClassVar[CellCover]
    CELL_COVER_WATER: _ClassVar[CellCover]
    CELL_COVER_MIXED: _ClassVar[CellCover]

class BakeState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BAKE_STATE_UNSPECIFIED: _ClassVar[BakeState]
    BAKE_STATE_RUNNING: _ClassVar[BakeState]
    BAKE_STATE_DONE: _ClassVar[BakeState]
    BAKE_STATE_FAILED: _ClassVar[BakeState]
CELL_COVER_UNSPECIFIED: CellCover
CELL_COVER_LAND: CellCover
CELL_COVER_WATER: CellCover
CELL_COVER_MIXED: CellCover
BAKE_STATE_UNSPECIFIED: BakeState
BAKE_STATE_RUNNING: BakeState
BAKE_STATE_DONE: BakeState
BAKE_STATE_FAILED: BakeState

class Geofence(_message.Message):
    __slots__ = ("id", "name", "rings", "owner", "updated_at", "tags")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    RINGS_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    rings: _containers.RepeatedCompositeFieldContainer[Ring]
    owner: str
    updated_at: str
    tags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., rings: _Optional[_Iterable[_Union[Ring, _Mapping]]] = ..., owner: _Optional[str] = ..., updated_at: _Optional[str] = ..., tags: _Optional[_Iterable[str]] = ...) -> None: ...

class Ring(_message.Message):
    __slots__ = ("points",)
    POINTS_FIELD_NUMBER: _ClassVar[int]
    points: _containers.RepeatedCompositeFieldContainer[_routing_pb2.Point]
    def __init__(self, points: _Optional[_Iterable[_Union[_routing_pb2.Point, _Mapping]]] = ...) -> None: ...

class ListGeofencesRequest(_message.Message):
    __slots__ = ("tags",)
    TAGS_FIELD_NUMBER: _ClassVar[int]
    tags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tags: _Optional[_Iterable[str]] = ...) -> None: ...

class ListGeofencesResponse(_message.Message):
    __slots__ = ("geofences",)
    GEOFENCES_FIELD_NUMBER: _ClassVar[int]
    geofences: _containers.RepeatedCompositeFieldContainer[Geofence]
    def __init__(self, geofences: _Optional[_Iterable[_Union[Geofence, _Mapping]]] = ...) -> None: ...

class GetGeofenceRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetGeofenceResponse(_message.Message):
    __slots__ = ("geofence",)
    GEOFENCE_FIELD_NUMBER: _ClassVar[int]
    geofence: Geofence
    def __init__(self, geofence: _Optional[_Union[Geofence, _Mapping]] = ...) -> None: ...

class PutGeofenceRequest(_message.Message):
    __slots__ = ("geofence",)
    GEOFENCE_FIELD_NUMBER: _ClassVar[int]
    geofence: Geofence
    def __init__(self, geofence: _Optional[_Union[Geofence, _Mapping]] = ...) -> None: ...

class PutGeofenceResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class HexAreaRequest(_message.Message):
    __slots__ = ("geofence_id", "rings", "res")
    GEOFENCE_ID_FIELD_NUMBER: _ClassVar[int]
    RINGS_FIELD_NUMBER: _ClassVar[int]
    RES_FIELD_NUMBER: _ClassVar[int]
    geofence_id: str
    rings: _containers.RepeatedCompositeFieldContainer[Ring]
    res: int
    def __init__(self, geofence_id: _Optional[str] = ..., rings: _Optional[_Iterable[_Union[Ring, _Mapping]]] = ..., res: _Optional[int] = ...) -> None: ...

class HexAreaResponse(_message.Message):
    __slots__ = ("res", "h", "cover")
    RES_FIELD_NUMBER: _ClassVar[int]
    H_FIELD_NUMBER: _ClassVar[int]
    COVER_FIELD_NUMBER: _ClassVar[int]
    res: int
    h: _containers.RepeatedScalarFieldContainer[str]
    cover: _containers.RepeatedScalarFieldContainer[CellCover]
    def __init__(self, res: _Optional[int] = ..., h: _Optional[_Iterable[str]] = ..., cover: _Optional[_Iterable[_Union[CellCover, str]]] = ...) -> None: ...

class BakeTimesRequest(_message.Message):
    __slots__ = ("geofence_id", "mode", "res")
    GEOFENCE_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    RES_FIELD_NUMBER: _ClassVar[int]
    geofence_id: str
    mode: _routing_pb2.Mode
    res: int
    def __init__(self, geofence_id: _Optional[str] = ..., mode: _Optional[_Union[_routing_pb2.Mode, str]] = ..., res: _Optional[int] = ...) -> None: ...

class BakeTimesResponse(_message.Message):
    __slots__ = ("ticket",)
    TICKET_FIELD_NUMBER: _ClassVar[int]
    ticket: str
    def __init__(self, ticket: _Optional[str] = ...) -> None: ...

class GetBakeStatusRequest(_message.Message):
    __slots__ = ("ticket",)
    TICKET_FIELD_NUMBER: _ClassVar[int]
    ticket: str
    def __init__(self, ticket: _Optional[str] = ...) -> None: ...

class GetBakeStatusResponse(_message.Message):
    __slots__ = ("state", "method", "cells", "tile_vintage")
    STATE_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    CELLS_FIELD_NUMBER: _ClassVar[int]
    TILE_VINTAGE_FIELD_NUMBER: _ClassVar[int]
    state: BakeState
    method: _routing_pb2.Method
    cells: int
    tile_vintage: str
    def __init__(self, state: _Optional[_Union[BakeState, str]] = ..., method: _Optional[_Union[_routing_pb2.Method, str]] = ..., cells: _Optional[int] = ..., tile_vintage: _Optional[str] = ...) -> None: ...
