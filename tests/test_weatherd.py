# weatherd's rules: flag pause is loud, Open-Meteo failure falls back to
# NWS, both failing leaves the point unanswered (not a fabricated zero),
# the snapshot write is atomic, and the daemon has an /api like every
# other citizen.

import http.server
import json
import threading
import urllib.request

import weatherd


class Flags:
    def __init__(self, ok=True):
        self.ok = ok
    def check(self, key):
        return self.ok


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), weatherd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _reset(tmp_path, monkeypatch):
    weatherd.READINGS.clear()
    weatherd.NWS_STATION.clear()
    monkeypatch.setattr(weatherd, "OUT", str(tmp_path / "weather"))


ONE_POINT = [{"name": "test-point", "lat": 1.0, "lon": 2.0}]


def test_flag_off_pauses_loudly(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(weatherd, "fetch_open_meteo", lambda lat, lon: (_ for _ in ()).throw(AssertionError("should not fetch")))
    before = weatherd.STATE["paused_flag"]
    weatherd.poll_once(flags=Flags(ok=False), points=ONE_POINT)
    assert weatherd.STATE["paused_flag"] == before + 1
    assert not weatherd.READINGS


def test_open_meteo_success_never_touches_nws(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(weatherd, "fetch_open_meteo", lambda lat, lon: {
        "source": "open-meteo", "observed_at": "2026-08-28T21:00:00Z", "temperature_c": 20.0,
        "wind_speed_kmh": 5.0, "wind_direction_deg": 180, "precipitation_mm": 0.0,
        "cloud_cover_pct": 10, "pressure_hpa": 1013.0, "humidity_pct": 55})
    monkeypatch.setattr(weatherd, "fetch_nws", lambda name, lat, lon: (_ for _ in ()).throw(AssertionError("should not fall back")))
    weatherd.poll_once(flags=Flags(), points=ONE_POINT)
    assert weatherd.READINGS["test-point"]["source"] == "open-meteo"
    assert weatherd.STATE["open_meteo_ok"] >= 1


def test_open_meteo_failure_falls_back_to_nws(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    def boom(lat, lon):
        raise OSError("open-meteo unreachable")
    monkeypatch.setattr(weatherd, "fetch_open_meteo", boom)
    monkeypatch.setattr(weatherd, "fetch_nws", lambda name, lat, lon: {
        "source": "nws", "station": "TESTS1", "observed_at": "2026-08-28T21:00:00Z",
        "temperature_c": 18.0, "wind_speed_kmh": None, "wind_direction_deg": None,
        "precipitation_mm": None, "cloud_cover_pct": None, "pressure_hpa": None, "humidity_pct": 60})
    weatherd.poll_once(flags=Flags(), points=ONE_POINT)
    assert weatherd.READINGS["test-point"]["source"] == "nws"
    assert weatherd.STATE["open_meteo_failed"] >= 1
    assert weatherd.STATE["nws_ok"] >= 1


def test_both_providers_failing_leaves_the_point_unanswered_not_zeroed(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(weatherd, "fetch_open_meteo", lambda lat, lon: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(weatherd, "fetch_nws", lambda name, lat, lon: (_ for _ in ()).throw(OSError("also down")))
    weatherd.poll_once(flags=Flags(), points=ONE_POINT)
    assert "test-point" not in weatherd.READINGS


def test_snapshot_writes_atomically_and_carries_the_reading(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(weatherd, "fetch_open_meteo", lambda lat, lon: {
        "source": "open-meteo", "observed_at": "2026-08-28T21:00:00Z", "temperature_c": 20.0,
        "wind_speed_kmh": 5.0, "wind_direction_deg": 180, "precipitation_mm": 0.0,
        "cloud_cover_pct": 10, "pressure_hpa": 1013.0, "humidity_pct": 55})
    weatherd.poll_once(flags=Flags(), points=ONE_POINT)
    snap = json.loads((tmp_path / "weather" / "current.json").read_text())
    assert len(snap["points"]) == 1
    assert snap["points"][0]["name"] == "test-point"
    assert snap["points"][0]["temperature_c"] == 20.0
    assert not (tmp_path / "weather" / "current.json.tmp").exists()


def test_nws_station_resolved_once_and_cached(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    calls = {"n": 0}
    def fake_get_json(url):
        calls["n"] += 1
        if "/points/" in url:
            return {"properties": {"observationStations": "https://x/stations"}}
        return {"features": [{"properties": {"stationIdentifier": "TESTS1"}}]}
    monkeypatch.setattr(weatherd, "_get_json", fake_get_json)
    s1 = weatherd._nws_station("test-point", 1.0, 2.0)
    s2 = weatherd._nws_station("test-point", 1.0, 2.0)
    assert s1 == s2 == "TESTS1"
    assert calls["n"] == 2  # points + stations, once -- the second call is pure cache


def test_api_serves_the_daemon_descriptor():
    srv = _serve()
    try:
        port = srv.server_address[1]
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=5)
        assert r.status == 200
        assert "application/x-protobuf" in r.headers.get("content-type", "")
        assert r.read() == weatherd.descriptor_bytes()
    finally:
        srv.shutdown()
