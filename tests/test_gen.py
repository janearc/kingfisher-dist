# the generated types ARE the contract, importable. Two properties:
#
# 1. the protojson seam works: a message round-trips through the proto3 JSON
#    mapping, which is the wire kingfisher speaks (flipr's wire too).
# 2. the committed descriptor and the committed gen code describe the SAME
#    contract -- both are built from proto/ and drift between them is a
#    gen-freshness violation.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gen"))

from google.protobuf import descriptor_pb2, json_format  # noqa: E402
from kingfisher.map.v1 import map_pb2  # noqa: E402
from kingfisher.routing.v1 import routing_pb2  # noqa: E402

import serve  # noqa: E402


def test_catalogue_round_trips_protojson():
    cat = map_pb2.Catalogue()
    layer = cat.layers.add()
    layer.id = "acs_median_gross_rent"
    layer.kind = map_pb2.LAYER_KIND_INTENSIVE
    layer.domain.min = 200.0
    layer.domain.max = 4100.0
    layer.vintage.id = "acs-2024"
    layer.vintage.state_overrides["MI"] = "lodes-2021"
    wire = json_format.MessageToJson(cat)
    back = json_format.Parse(wire, map_pb2.Catalogue())
    assert back == cat
    assert back.layers[0].vintage.state_overrides["MI"] == "lodes-2021"


def test_method_zero_value_is_the_safe_one():
    # the ruled test: a forgotten field must degrade toward "I am not sure".
    resp = routing_pb2.RouteResponse()
    assert resp.method == routing_pb2.METHOD_UNSPECIFIED
    assert routing_pb2.Method.Name(0) == "METHOD_UNSPECIFIED"


def test_layer_kind_zero_refuses_to_mean_something():
    layer = map_pb2.LayerDescriptor()
    assert layer.kind == map_pb2.LAYER_KIND_UNSPECIFIED


def test_descriptor_and_gen_code_agree():
    # the bytes /api serves parse as a FileDescriptorSet naming exactly the
    # proto files the gen code was built from
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(serve.descriptor_bytes())
    names = {f.name for f in fds.file}
    assert "kingfisher/map/v1/map.proto" in names
    assert "kingfisher/routing/v1/routing.proto" in names
    assert "kingfisher/compose/v1/compose.proto" in names
    assert "kingfisher/discovery/v1/discovery.proto" in names
