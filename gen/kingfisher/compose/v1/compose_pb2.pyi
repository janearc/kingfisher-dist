from kingfisher.map.v1 import map_pb2 as _map_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Combiner(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMBINER_UNSPECIFIED: _ClassVar[Combiner]
    COMBINER_MEAN: _ClassVar[Combiner]
    COMBINER_MIN: _ClassVar[Combiner]
    COMBINER_MAX: _ClassVar[Combiner]
    COMBINER_EXPRESSION: _ClassVar[Combiner]
COMBINER_UNSPECIFIED: Combiner
COMBINER_MEAN: Combiner
COMBINER_MIN: Combiner
COMBINER_MAX: Combiner
COMBINER_EXPRESSION: Combiner

class LayerDefinition(_message.Message):
    __slots__ = ("id", "label", "inputs", "combiner", "expression", "min_coverage", "band_lo", "band_hi")
    ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    COMBINER_FIELD_NUMBER: _ClassVar[int]
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    MIN_COVERAGE_FIELD_NUMBER: _ClassVar[int]
    BAND_LO_FIELD_NUMBER: _ClassVar[int]
    BAND_HI_FIELD_NUMBER: _ClassVar[int]
    id: str
    label: str
    inputs: _containers.RepeatedCompositeFieldContainer[Input]
    combiner: Combiner
    expression: str
    min_coverage: int
    band_lo: float
    band_hi: float
    def __init__(self, id: _Optional[str] = ..., label: _Optional[str] = ..., inputs: _Optional[_Iterable[_Union[Input, _Mapping]]] = ..., combiner: _Optional[_Union[Combiner, str]] = ..., expression: _Optional[str] = ..., min_coverage: _Optional[int] = ..., band_lo: _Optional[float] = ..., band_hi: _Optional[float] = ...) -> None: ...

class Input(_message.Message):
    __slots__ = ("layer_id", "weight", "invert", "vintage_id")
    LAYER_ID_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    INVERT_FIELD_NUMBER: _ClassVar[int]
    VINTAGE_ID_FIELD_NUMBER: _ClassVar[int]
    layer_id: str
    weight: float
    invert: bool
    vintage_id: str
    def __init__(self, layer_id: _Optional[str] = ..., weight: _Optional[float] = ..., invert: _Optional[bool] = ..., vintage_id: _Optional[str] = ...) -> None: ...

class ListDefinitionsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListDefinitionsResponse(_message.Message):
    __slots__ = ("definitions",)
    DEFINITIONS_FIELD_NUMBER: _ClassVar[int]
    definitions: _containers.RepeatedCompositeFieldContainer[LayerDefinition]
    def __init__(self, definitions: _Optional[_Iterable[_Union[LayerDefinition, _Mapping]]] = ...) -> None: ...

class PutDefinitionRequest(_message.Message):
    __slots__ = ("definition",)
    DEFINITION_FIELD_NUMBER: _ClassVar[int]
    definition: LayerDefinition
    def __init__(self, definition: _Optional[_Union[LayerDefinition, _Mapping]] = ...) -> None: ...

class PutDefinitionResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ComposeRequest(_message.Message):
    __slots__ = ("definition_id", "extent")
    DEFINITION_ID_FIELD_NUMBER: _ClassVar[int]
    EXTENT_FIELD_NUMBER: _ClassVar[int]
    definition_id: str
    extent: _map_pb2.ChunkRef
    def __init__(self, definition_id: _Optional[str] = ..., extent: _Optional[_Union[_map_pb2.ChunkRef, _Mapping]] = ...) -> None: ...

class ComposeResponse(_message.Message):
    __slots__ = ("res", "h", "hot", "inputs_combined", "input_vintages")
    class InputVintagesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    RES_FIELD_NUMBER: _ClassVar[int]
    H_FIELD_NUMBER: _ClassVar[int]
    HOT_FIELD_NUMBER: _ClassVar[int]
    INPUTS_COMBINED_FIELD_NUMBER: _ClassVar[int]
    INPUT_VINTAGES_FIELD_NUMBER: _ClassVar[int]
    res: int
    h: _containers.RepeatedScalarFieldContainer[str]
    hot: _containers.RepeatedScalarFieldContainer[float]
    inputs_combined: int
    input_vintages: _containers.ScalarMap[str, str]
    def __init__(self, res: _Optional[int] = ..., h: _Optional[_Iterable[str]] = ..., hot: _Optional[_Iterable[float]] = ..., inputs_combined: _Optional[int] = ..., input_vintages: _Optional[_Mapping[str, str]] = ...) -> None: ...
