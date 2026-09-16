from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Mode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MODE_UNSPECIFIED: _ClassVar[Mode]
    MODE_AUTO: _ClassVar[Mode]
    MODE_BICYCLE: _ClassVar[Mode]
    MODE_PEDESTRIAN: _ClassVar[Mode]

class Method(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    METHOD_UNSPECIFIED: _ClassVar[Method]
    METHOD_REDUCED_MATRIX: _ClassVar[Method]
    METHOD_VALHALLA: _ClassVar[Method]
    METHOD_HAVERSINE: _ClassVar[Method]
MODE_UNSPECIFIED: Mode
MODE_AUTO: Mode
MODE_BICYCLE: Mode
MODE_PEDESTRIAN: Mode
METHOD_UNSPECIFIED: Method
METHOD_REDUCED_MATRIX: Method
METHOD_VALHALLA: Method
METHOD_HAVERSINE: Method

class RoutingCapability(_message.Message):
    __slots__ = ("modes", "extent", "tile_vintage")
    MODES_FIELD_NUMBER: _ClassVar[int]
    EXTENT_FIELD_NUMBER: _ClassVar[int]
    TILE_VINTAGE_FIELD_NUMBER: _ClassVar[int]
    modes: _containers.RepeatedScalarFieldContainer[Mode]
    extent: Extent
    tile_vintage: str
    def __init__(self, modes: _Optional[_Iterable[_Union[Mode, str]]] = ..., extent: _Optional[_Union[Extent, _Mapping]] = ..., tile_vintage: _Optional[str] = ...) -> None: ...

class Extent(_message.Message):
    __slots__ = ("w", "s", "e", "n")
    W_FIELD_NUMBER: _ClassVar[int]
    S_FIELD_NUMBER: _ClassVar[int]
    E_FIELD_NUMBER: _ClassVar[int]
    N_FIELD_NUMBER: _ClassVar[int]
    w: float
    s: float
    e: float
    n: float
    def __init__(self, w: _Optional[float] = ..., s: _Optional[float] = ..., e: _Optional[float] = ..., n: _Optional[float] = ...) -> None: ...

class Point(_message.Message):
    __slots__ = ("lat", "lng")
    LAT_FIELD_NUMBER: _ClassVar[int]
    LNG_FIELD_NUMBER: _ClassVar[int]
    lat: float
    lng: float
    def __init__(self, lat: _Optional[float] = ..., lng: _Optional[float] = ...) -> None: ...

class GetCapabilityRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetCapabilityResponse(_message.Message):
    __slots__ = ("capability",)
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    capability: RoutingCapability
    def __init__(self, capability: _Optional[_Union[RoutingCapability, _Mapping]] = ...) -> None: ...

class MatrixRequest(_message.Message):
    __slots__ = ("mode", "sources", "targets")
    MODE_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    mode: Mode
    sources: _containers.RepeatedCompositeFieldContainer[Point]
    targets: _containers.RepeatedCompositeFieldContainer[Point]
    def __init__(self, mode: _Optional[_Union[Mode, str]] = ..., sources: _Optional[_Iterable[_Union[Point, _Mapping]]] = ..., targets: _Optional[_Iterable[_Union[Point, _Mapping]]] = ...) -> None: ...

class MatrixResponse(_message.Message):
    __slots__ = ("method", "seconds", "tile_vintage")
    METHOD_FIELD_NUMBER: _ClassVar[int]
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    TILE_VINTAGE_FIELD_NUMBER: _ClassVar[int]
    method: Method
    seconds: _containers.RepeatedScalarFieldContainer[float]
    tile_vintage: str
    def __init__(self, method: _Optional[_Union[Method, str]] = ..., seconds: _Optional[_Iterable[float]] = ..., tile_vintage: _Optional[str] = ...) -> None: ...

class RouteRequest(_message.Message):
    __slots__ = ("mode", "origin", "destination")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    mode: Mode
    origin: Point
    destination: Point
    def __init__(self, mode: _Optional[_Union[Mode, str]] = ..., origin: _Optional[_Union[Point, _Mapping]] = ..., destination: _Optional[_Union[Point, _Mapping]] = ...) -> None: ...

class RouteResponse(_message.Message):
    __slots__ = ("method", "path", "seconds", "meters", "tile_vintage")
    METHOD_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    METERS_FIELD_NUMBER: _ClassVar[int]
    TILE_VINTAGE_FIELD_NUMBER: _ClassVar[int]
    method: Method
    path: _containers.RepeatedCompositeFieldContainer[Point]
    seconds: float
    meters: float
    tile_vintage: str
    def __init__(self, method: _Optional[_Union[Method, str]] = ..., path: _Optional[_Iterable[_Union[Point, _Mapping]]] = ..., seconds: _Optional[float] = ..., meters: _Optional[float] = ..., tile_vintage: _Optional[str] = ...) -> None: ...

class EstimateRequest(_message.Message):
    __slots__ = ("mode", "origin", "destination")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    mode: Mode
    origin: Point
    destination: Point
    def __init__(self, mode: _Optional[_Union[Mode, str]] = ..., origin: _Optional[_Union[Point, _Mapping]] = ..., destination: _Optional[_Union[Point, _Mapping]] = ...) -> None: ...

class EstimateResponse(_message.Message):
    __slots__ = ("method", "seconds", "meters")
    METHOD_FIELD_NUMBER: _ClassVar[int]
    SECONDS_FIELD_NUMBER: _ClassVar[int]
    METERS_FIELD_NUMBER: _ClassVar[int]
    method: Method
    seconds: float
    meters: float
    def __init__(self, method: _Optional[_Union[Method, str]] = ..., seconds: _Optional[float] = ..., meters: _Optional[float] = ...) -> None: ...
