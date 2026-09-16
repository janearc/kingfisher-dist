# CONFORMANCE: every RPC response the server emits must parse through the
# GENERATED response type. json_format.Parse rejects unknown fields and
# illegal enum values, so this is the wire-matches-the-proto check the
# field-picking tests cannot give: a response that grew a field the
# contract does not know, or dropped one a client relies on, fails HERE.
#
# This is the gen-code-as-enforcement half of the language ruling: the
# runtime serves hand-written protojson, and these tests are what makes
# hand-written unable to drift.

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gen"))

from google.protobuf import json_format  # noqa: E402
from kingfisher.discovery.v1 import discovery_pb2  # noqa: E402
from kingfisher.routing.v1 import routing_pb2  # noqa: E402

import providers  # noqa: E402
import serve  # noqa: E402


class Flags:
    def check(self, key):
        return True


SF = {"lat": 37.7749, "lng": -122.4194}
OAK = {"lat": 37.8044, "lng": -122.2712}


def _post(get, path, body):
    return get(path, method="POST", data=json.dumps(body).encode(),
               headers={"Content-Type": "application/json"})


def conform(resp, msg_type):
    assert resp.status == 200, resp.text
    # Parse is STRICT: unknown fields and bad enums raise ParseError
    return json_format.Parse(resp.body, msg_type())


def test_routing_responses_conform(get, monkeypatch):
    monkeypatch.delenv("KINGFISHER_VALHALLA_URL", raising=False)
    base = "/kingfisher.routing.v1.RoutingService/"
    m = conform(_post(get, base + "GetCapability", {}),
                routing_pb2.GetCapabilityResponse)
    assert routing_pb2.MODE_AUTO in m.capability.modes
    r = conform(_post(get, base + "Route",
                      {"mode": "MODE_AUTO", "origin": SF, "destination": OAK}),
                routing_pb2.RouteResponse)
    assert r.method != routing_pb2.METHOD_UNSPECIFIED  # the ruled zero never ships
    conform(_post(get, base + "Estimate",
                  {"mode": "MODE_BICYCLE", "origin": SF, "destination": OAK}),
            routing_pb2.EstimateResponse)
    conform(_post(get, base + "Matrix",
                  {"mode": "MODE_AUTO", "sources": [SF], "targets": [OAK]}),
            routing_pb2.MatrixResponse)


def test_discovery_responses_conform(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags())
    monkeypatch.setattr(providers, "_get_json", lambda s_, u, timeout=30: {
        "items": [{"sourceId": "X1", "title": "t", "sizeInBytes": 5,
                   "downloadURL": "http://x/a", "publicationDate": "2024-01-01"}]})
    base = "/kingfisher.discovery.v1.DiscoveryService/"
    s_ = conform(_post(get, base + "ListSources", {}),
                 discovery_pb2.ListSourcesResponse)
    assert len(s_.sources) == len(providers.SOURCES)
    conform(_post(get, base + "RefreshIndex", {"sourceId": "usgs"}),
            discovery_pb2.RefreshIndexResponse)
    d = conform(_post(get, base + "ListDatasets", {}),
                discovery_pb2.ListDatasetsResponse)
    for rec in d.datasets:
        # the zero-value rule holds on the wire: state is never left implicit
        assert rec.state != discovery_pb2.FETCH_STATE_UNSPECIFIED
