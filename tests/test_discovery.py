# the librarian surface: what kingfisher knows about, and the flipr-gated
# acts that grow it. No live network anywhere in here -- providers' outbound
# door is monkeypatched, and the flag plane is a stub.

import io
import json

import flipr_client
import net
import providers
import serve


class Flags:
    # a stub flag plane. ok=True answers every check yes; down=True raises
    # FliprDown the way the real client does when flipr is unreachable.
    def __init__(self, ok=True, down=False):
        self.ok, self.down = ok, down

    def check(self, key):
        if self.down:
            raise flipr_client.FliprDown("stubbed: flipr is down")
        return self.ok


def _post(get, rpc, body=b"{}"):
    return get(f"/kingfisher.discovery.v1.DiscoveryService/{rpc}",
               method="POST", data=body,
               headers={"Content-Type": "application/json"})


def setup_function(_):
    with providers._INDEX_LOCK:
        providers._INDEX.clear()
        providers._TICKETS.clear()
    serve._FLIPR = None


def test_discovery_blob_names_every_source(get):
    resp = get("/discovery")
    assert resp.status == 200
    blob = resp.json()
    ids = {s["id"] for s in blob["sources"]}
    assert {"usgs", "noaa", "overture", "nasa", "carto"} <= ids
    assert len(ids) == len(providers.SOURCES)
    assert all(s["expensive"] for s in blob["sources"])
    # paid is the MONEY pill and exactly one source wears it
    assert [s["id"] for s in blob["sources"] if s.get("paid")] == ["carto"]
    # the national programs carry their country; the organizing key
    assert sum(1 for s in blob["sources"] if s.get("country")) >= 55
    assert blob["datasets"] == []


def test_list_sources_rpc_speaks_protojson(get):
    resp = _post(get, "ListSources")
    assert resp.status == 200
    out = resp.json()
    assert len(out["sources"]) == len(providers.SOURCES)


def test_rpcs_are_post_only(get):
    resp = get("/kingfisher.discovery.v1.DiscoveryService/ListSources")
    assert resp.status == 405


def test_fetch_refuses_when_flags_off(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=False))
    with providers._INDEX_LOCK:
        providers._INDEX["d1"] = {"id": "d1", "source_id": "usgs", "title": "t",
                                  "state": "FETCH_STATE_INDEXED",
                                  "download_url": "http://x/y"}
    resp = _post(get, "Fetch", json.dumps({"datasetId": "d1"}).encode())
    assert resp.status == 403
    assert "fetch.enabled" in resp.text


def test_fetch_surface_fails_closed_when_flipr_down(get, monkeypatch):
    # THE ruled semantic: flipr unreachable means the expensive surface
    # refuses, loudly, naming flipr. Never a quiet fallback.
    monkeypatch.setattr(serve, "_FLIPR", Flags(down=True))
    with providers._INDEX_LOCK:
        providers._INDEX["d1"] = {"id": "d1", "source_id": "usgs", "title": "t",
                                  "state": "FETCH_STATE_INDEXED",
                                  "download_url": "http://x/y"}
    resp = _post(get, "Fetch", json.dumps({"datasetId": "d1"}).encode())
    assert resp.status == 503
    assert "flipr" in resp.text


def test_refresh_index_populates_and_lists(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    monkeypatch.setattr(providers, "_get_json", lambda src, url, timeout=30: {
        "items": [{"sourceId": "USGS_LPC_CA_1", "title": "Lidar CA",
                   "sizeInBytes": 12345, "downloadURL": "http://x/a.laz",
                   "publicationDate": "2024-05-01"}]})
    resp = _post(get, "RefreshIndex",
                 json.dumps({"sourceId": "usgs", "limit": 5}).encode())
    assert resp.status == 200
    assert resp.json()["indexed"] == 1
    listed = _post(get, "ListDatasets", b"{}").json()["datasets"]
    assert listed[0]["sourceId"] == "usgs"
    assert listed[0]["kind"] == "LAYER_KIND_INTENSIVE"
    assert listed[0]["state"] == "FETCH_STATE_INDEXED"
    assert listed[0]["downloadUrl"] == "http://x/a.laz"


def test_refresh_index_unimplemented_source_says_so(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    resp = _post(get, "RefreshIndex", json.dumps({"sourceId": "noaa"}).encode())
    assert resp.status == 501


def test_fetch_spools_and_ticket_reports(get, monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    monkeypatch.setenv("KINGFISHER_SPOOL", str(tmp_path))
    with providers._INDEX_LOCK:
        providers._INDEX["d2"] = {"id": "d2", "source_id": "usgs", "title": "t",
                                  "state": "FETCH_STATE_INDEXED",
                                  "download_url": "http://x/b.laz"}

    class FakeResp(io.BytesIO):
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # THE SEAM MOVED. providers used to hold its own _urlopen so the suite could
    # stub the network without monkeypatching urllib globally (which hijacks the
    # test harness's own HTTP client). That door is now net.py's, because every
    # outbound call in this service goes through it -- so there is one place to
    # stub instead of one per module, which is the same argument that put the
    # seam there in the first place.
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=300: FakeResp(b"LASF-bytes"))
    resp = _post(get, "Fetch", json.dumps({"datasetId": "d2"}).encode())
    assert resp.status == 200
    ticket = resp.json()["ticket"]
    # the download thread races the assertion; join it via the module state
    for _ in range(100):
        if providers.ticket_status(ticket)["state"] != "FETCH_STATE_FETCHING":
            break
        serve.time.sleep(0.01)
    st = _post(get, "GetFetchStatus",
               json.dumps({"ticket": ticket}).encode()).json()
    assert st["record"]["state"] == "FETCH_STATE_HELD"
    assert (tmp_path / "d2").read_bytes() == b"LASF-bytes"


def test_outbound_requests_are_counted(get, monkeypatch):
    providers._count_out("usgs", "ok", 512)
    providers._count_out("usgs", "error")
    get("/metrics")
    text = get("/metrics").text
    assert 'kingfisher_provider_requests_total{source="usgs",outcome="ok"}' in text
    assert 'kingfisher_provider_requests_total{source="usgs",outcome="error"}' in text
    assert 'kingfisher_provider_bytes_total{source="usgs"}' in text


def test_unknown_rpc_and_unknown_ticket_404(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    assert _post(get, "NoSuchRpc").status == 404
    resp = _post(get, "GetFetchStatus", json.dumps({"ticket": "tx"}).encode())
    assert resp.status == 404


def test_refresh_index_refuses_when_flags_off(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=False))
    resp = _post(get, "RefreshIndex", json.dumps({"sourceId": "usgs"}).encode())
    assert resp.status == 403


def test_fetch_unknown_dataset_404(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    resp = _post(get, "Fetch", json.dumps({"datasetId": "nope"}).encode())
    assert resp.status == 404


def test_flag_missing_is_a_wiring_error_500(get, monkeypatch):
    class Missing:
        def check(self, key):
            raise flipr_client.FlagMissing(f"undeclared: {key}")
    monkeypatch.setattr(serve, "_FLIPR", Missing())
    with providers._INDEX_LOCK:
        providers._INDEX["d3"] = {"id": "d3", "source_id": "usgs", "title": "t",
                                  "state": "FETCH_STATE_INDEXED",
                                  "download_url": "http://x/z"}
    resp = _post(get, "Fetch", json.dumps({"datasetId": "d3"}).encode())
    assert resp.status == 500
    assert "wiring" in resp.text


def test_flipr_client_built_once_with_ruled_namespace():
    serve._FLIPR = None
    c = serve.flipr_flags()
    # the namespace version is the PROTO API VERSION, never a commit hash
    assert c._service == "kingfisher"
    assert c._version == "v1"
    assert serve.flipr_flags() is c


def test_network_master_stops_fetches(get, monkeypatch):
    # the big red switch: network.enabled off refuses every fetch even
    # with the fetch flags on
    class PerKey:
        def check(self, key):
            return key != "network.enabled"
    monkeypatch.setattr(serve, "_FLIPR", PerKey())
    resp = _post(get, "RefreshIndex", json.dumps({"sourceId": "usgs"}).encode())
    assert resp.status == 403


def test_gibs_adapter_indexes_imagery_layers(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    monkeypatch.setattr(providers, "_get_json", lambda s_, u, timeout=60: {
        "layers": {
            "VIIRS_SNPP_CorrectedReflectance_TrueColor":
                {"type": "wmts", "title": "VIIRS True Color"},
            "not_imagery": {"type": "vector", "title": "skip me"},
        }})
    resp = _post(get, "RefreshIndex",
                 json.dumps({"sourceId": "nasa_gibs", "limit": 10}).encode())
    assert resp.status == 200
    assert resp.json()["indexed"] == 1
    d = _post(get, "ListDatasets",
              json.dumps({"sourceId": "nasa_gibs"}).encode()).json()["datasets"]
    assert d[0]["kind"] == "LAYER_KIND_ASSET"
    assert "wvs.earthdata.nasa.gov" in d[0]["downloadUrl"]
    assert "BBOX=37.05,-122.6" in d[0]["downloadUrl"]


def test_gzip_compressed_catalogue_decompresses(monkeypatch):
    import gzip as gz
    import io

    class R(io.BytesIO):
        status = 200
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    payload = gz.compress(json.dumps({"layers": {}}).encode())
    monkeypatch.setattr(net, "_urlopen", lambda req, timeout=30: R(payload))
    assert providers._get_json("nasa_gibs", "http://x") == {"layers": {}}


def test_upstream_failure_is_a_labelled_502(get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", Flags(ok=True))
    def boom(s_, u, timeout=60):
        raise UnicodeDecodeError("utf-8", b"\x9d", 0, 1, "invalid start byte")
    monkeypatch.setattr(providers, "_get_json", boom)
    resp = _post(get, "RefreshIndex", json.dumps({"sourceId": "usgs"}).encode())
    assert resp.status == 502
    assert "upstream" in resp.text


def test_brotli_compressed_catalogue_decompresses(monkeypatch):
    import io

    import brotli as br

    class R(io.BytesIO):
        status = 200
        headers = {"Content-Encoding": "br"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    payload = br.compress(json.dumps({"layers": {}}).encode())
    monkeypatch.setattr(net, "_urlopen", lambda req, timeout=30: R(payload))
    assert providers._get_json("nasa_gibs", "http://x") == {"layers": {}}


def test_geofences_geojson_serves_the_store(get, monkeypatch):
    monkeypatch.setattr(serve, "_fence_rows", lambda: [
        {"id": "geofence:albatross_austin", "name": "austin", "owner": "albatross",
         "tags": ["aggregate"],
         "rings": [{"points": [{"lat": 30.1, "lng": -97.9}, {"lat": 30.5, "lng": -97.9},
                               {"lat": 30.5, "lng": -97.5}]}]},
        {"id": "geofence:x", "name": "x", "owner": "operator", "tags": ["test"],
         "rings": [{"points": [{"lat": 1, "lng": 2}, {"lat": 3, "lng": 4}, {"lat": 5, "lng": 6}]}]},
    ])
    resp = get("/geofences.geojson")
    assert resp.status == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    g = resp.json()
    assert g["type"] == "FeatureCollection" and len(g["features"]) == 2
    ring = g["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]              # GeoJSON rings close explicitly
    assert ring[0] == [-97.9, 30.1]         # lng,lat order per spec
    tagged = get("/geofences.geojson?tag=aggregate").json()
    assert [f["properties"]["name"] for f in tagged["features"]] == ["austin"]


def test_geofences_store_down_is_a_labelled_503(get, monkeypatch):
    def boom():
        raise RuntimeError("the store did not answer")
    monkeypatch.setattr(serve, "_fence_rows", boom)
    resp = get("/geofences.geojson")
    assert resp.status == 503
    assert "store unreachable" in resp.text
