# the routing ladder: valhalla when present and permitted, the haversine
# floor otherwise, method labelling every answer. The property that matters
# most is NEGATIVE: no combination of absent valhalla, absent flags, or a
# dead flipr stops an answer -- the ladder only ever steps DOWN.

import io
import json

import flipr_client
import net
import routing
import serve


class Flags:
    def __init__(self, ok=True, down=False):
        self.ok, self.down = ok, down

    def check(self, key):
        if self.down:
            raise flipr_client.FliprDown("stubbed: down")
        return self.ok


def _post(get, rpc, body):
    return get(f"/kingfisher.routing.v1.RoutingService/{rpc}",
               method="POST", data=json.dumps(body).encode(),
               headers={"Content-Type": "application/json"})


SF = {"lat": 37.7749, "lng": -122.4194}
OAK = {"lat": 37.8044, "lng": -122.2712}
ROUTE_BODY = {"mode": "MODE_AUTO", "origin": SF, "destination": OAK}


def setup_function(_):
    serve._FLIPR = None


def test_haversine_is_roughly_right():
    # SF to Oakland is about 13.4 km crow-flies; assert the ballpark, not
    # the decimals -- this is the floor, not a survey instrument
    m = routing.haversine_m(SF["lat"], SF["lng"], OAK["lat"], OAK["lng"])
    assert 12000 < m < 15000


def test_no_valhalla_degrades_to_labelled_floor(get, monkeypatch):
    monkeypatch.delenv("KINGFISHER_VALHALLA_URL", raising=False)
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"
    assert out["meters"] > 12000
    assert out["seconds"] > 0


def test_flipr_down_degrades_instead_of_failing(get, monkeypatch):
    # THE ruled distinction: on the fetch surface flipr-down refuses; on
    # routing it steps down the ladder. An answer always flows.
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setattr(serve, "_FLIPR", Flags(down=True))
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"


def test_flags_off_means_floor_not_valhalla(get, monkeypatch):
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=False))
    called = []
    monkeypatch.setattr(net, "_urlopen",
                        lambda *a, **k: called.append(1))
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"
    assert called == []


def test_valhalla_answers_when_permitted(get, monkeypatch):
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setenv("KINGFISHER_TILE_VINTAGE", "osm-2026-07")
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))

    class FakeResp(io.BytesIO):
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    trip = {"trip": {"legs": [{"summary": {"time": 1042, "length": 17.5},
                               "shape": "xyz"}]}}
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=10: FakeResp(json.dumps(trip).encode()))
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_VALHALLA"
    assert out["seconds"] == 1042
    assert out["meters"] == 17500
    assert out["tileVintage"] == "osm-2026-07"


def test_valhalla_error_steps_down_labelled(get, monkeypatch):
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))

    def boom(req, timeout=10):
        raise OSError("connection refused")
    monkeypatch.setattr(net, "_urlopen", boom)
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"


def test_matrix_floor_and_cap(get, monkeypatch):
    monkeypatch.delenv("KINGFISHER_VALHALLA_URL", raising=False)
    body = {"mode": "MODE_PEDESTRIAN", "sources": [SF], "targets": [OAK, SF]}
    out = _post(get, "Matrix", body).json()
    assert out["method"] == "METHOD_HAVERSINE"
    assert len(out["seconds"]) == 2
    monkeypatch.setenv("KINGFISHER_MATRIX_MAX", "1")
    resp = _post(get, "Matrix", body)
    assert resp.status == 413
    assert "cap" in resp.text


def test_matrix_valhalla_marks_unreachable(get, monkeypatch):
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))

    class FakeResp(io.BytesIO):
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    rows = {"sources_to_targets": [[{"time": 300}, {"time": None}]]}
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=30: FakeResp(json.dumps(rows).encode()))
    out = _post(get, "Matrix",
                {"mode": "MODE_AUTO", "sources": [SF], "targets": [OAK, SF]}).json()
    assert out["method"] == "METHOD_VALHALLA"
    assert out["seconds"] == [300.0, -1.0]


def test_capability_names_modes(get):
    out = _post(get, "GetCapability", {}).json()
    assert set(out["capability"]["modes"]) == \
        {"MODE_AUTO", "MODE_BICYCLE", "MODE_PEDESTRIAN"}


def test_estimate_is_always_the_floor(get, monkeypatch):
    # Estimate is the degraded path BY NAME; it never consults valhalla
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    out = _post(get, "Estimate", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"


def test_network_master_floors_routing(get, monkeypatch):
    # network.enabled off does not break routing -- it collapses the ladder
    # to arithmetic, labelled, same as every other absence
    monkeypatch.setenv("KINGFISHER_VALHALLA_URL", "http://valhalla.test")

    class PerKey:
        def check(self, key):
            return key != "network.enabled"
    monkeypatch.setattr(serve, "_FLIPR", PerKey())
    out = _post(get, "Route", ROUTE_BODY).json()
    assert out["method"] == "METHOD_HAVERSINE"
