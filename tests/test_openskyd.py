# openskyd's rules: flag pause is loud, a state vector with no fix is
# dropped, a trail accumulates and caps, a cold aircraft is evicted, the
# snapshot write is atomic.

import http.server
import json
import threading
import urllib.request

import openskyd


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), openskyd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Flags:
    def __init__(self, ok=True):
        self.ok = ok
    def check(self, key):
        return self.ok


def _reset(tmp_path, monkeypatch):
    openskyd.AIRCRAFT.clear()
    monkeypatch.setattr(openskyd, "OUT", str(tmp_path / "flights"))


def _sv(icao24="a4d8fe", callsign="N411MV  ", lon=-122.09, lat=37.40,
        baro=868.0, on_ground=False, velocity=105.0, track=124.0,
        vrate=0.0, sensors=None, geo=899.0, last_contact=1000):
    return [icao24, callsign, "United States", last_contact, last_contact,
            lon, lat, baro, on_ground, velocity, track, vrate, sensors,
            geo, "1773", False, 0]


def test_flag_off_pauses_loudly(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    before = openskyd.STATE["paused_flag"]
    openskyd.poll_once(flags=Flags(ok=False), fetch=lambda: {"states": [_sv()]})
    assert openskyd.STATE["paused_flag"] == before + 1
    assert not openskyd.AIRCRAFT


def test_a_state_vector_with_no_fix_is_dropped(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    sv = _sv()
    sv[5] = None  # no longitude fix
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [sv]})
    assert not openskyd.AIRCRAFT


def test_position_lands_and_snapshot_writes_atomically(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv()]})
    assert "a4d8fe" in openskyd.AIRCRAFT
    row = openskyd.AIRCRAFT["a4d8fe"]
    assert row["lat"] == 37.40 and row["lng"] == -122.09
    assert row["callsign"] == "N411MV"  # padding stripped
    assert row["altitude_m"] == 899.0  # geo_altitude preferred over baro
    snap = json.loads((tmp_path / "flights" / "positions.json").read_text())
    assert snap["count"] == 1
    assert snap["aircraft"][0]["icao24"] == "a4d8fe"
    assert not (tmp_path / "flights" / "positions.json.tmp").exists()


def test_trail_accumulates_and_caps(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(openskyd, "TRAIL_LEN", 3)
    for t in range(1000, 1006):
        openskyd.poll_once(flags=Flags(), fetch=lambda t=t: {"states": [_sv(lat=37.0 + t / 100000, last_contact=t)]})
    trail = openskyd.AIRCRAFT["a4d8fe"]["trail"]
    assert len(trail) == 3
    assert trail[-1][2] == 1005  # newest point kept


def test_repeated_last_contact_does_not_duplicate_trail_point(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv(last_contact=1000)]})
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv(last_contact=1000)]})
    assert len(openskyd.AIRCRAFT["a4d8fe"]["trail"]) == 1


def test_a_missing_aircraft_goes_cold_after_the_threshold(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(openskyd, "COLD_AFTER_POLLS", 2)
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv()]})
    assert "a4d8fe" in openskyd.AIRCRAFT
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": []})
    assert "a4d8fe" in openskyd.AIRCRAFT  # one miss, still warm
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": []})
    assert "a4d8fe" not in openskyd.AIRCRAFT  # two misses, cold


def test_a_reappearing_aircraft_resets_its_miss_count(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(openskyd, "COLD_AFTER_POLLS", 2)
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv()]})
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": []})
    openskyd.poll_once(flags=Flags(), fetch=lambda: {"states": [_sv()]})
    assert openskyd.AIRCRAFT["a4d8fe"]["misses"] == 0


def test_fetch_failure_is_counted_and_does_not_crash(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    before = openskyd.STATE["errors"]
    def boom():
        raise OSError("network unreachable")
    openskyd.poll_once(flags=Flags(), fetch=boom)
    assert openskyd.STATE["errors"] == before + 1
    assert "network unreachable" in openskyd.STATE["last_error"]


def test_api_serves_the_daemon_descriptor():
    # openskyd has no RPC of its own -- /api is what it consumes, not what
    # it serves (mitigation-1's ruling: nobody gets to have no /api).
    srv = _serve()
    try:
        port = srv.server_address[1]
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=5)
        assert r.status == 200
        assert "application/x-protobuf" in r.headers.get("content-type", "")
        body = r.read()
        assert body == openskyd.descriptor_bytes()
        assert b"flipr/v1/flipr.proto" in body
    finally:
        srv.shutdown()
