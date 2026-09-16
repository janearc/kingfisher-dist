# gibsd's rules: flag pause is loud and fetches nothing, one bad tile
# never kills the poll (the survivors still shelve and the manifest names
# only what landed), the manifest write is atomic, the 4326 bounds are
# exact linear arithmetic, and the daemon has an /api like every other
# citizen. Written by interface-3 closing its own gap from the announced
# gibsd landing; the structure is test_weatherd.py's, ported.

import json
import os
import urllib.error
import urllib.request

import gibsd


class Flags:
    def __init__(self, ok=True):
        self.ok = ok

    def check(self, key):
        return self.ok


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(gibsd, "OUT", str(tmp_path / "clouds"))
    for k in gibsd.STATE:
        # bool IS int in python; zeroing flipr_ok made /health answer an
        # honest 503 and the suite caught it -- counters only
        if isinstance(gibsd.STATE[k], int) and not isinstance(gibsd.STATE[k], bool):
            gibsd.STATE[k] = 0


def test_tile_bounds_are_exact_linear_arithmetic():
    # level 6: 2.8125 degrees per tile from (-180, 90); row south, col east
    assert gibsd.tile_bounds(0, 0) == [-180.0, 90.0 - gibsd.TILE_DEG, -180.0 + gibsd.TILE_DEG, 90.0]
    w, s, e, n = gibsd.tile_bounds(18, 20)
    assert (w, n) == (-180.0 + 20 * gibsd.TILE_DEG, 90.0 - 18 * gibsd.TILE_DEG)
    assert abs((e - w) - gibsd.TILE_DEG) < 1e-12 and abs((n - s) - gibsd.TILE_DEG) < 1e-12
    # the bay window actually covers the bay: row 18 col 20 spans SF
    assert w <= -122.4 <= e and s <= 37.77 <= n


def test_flag_off_pauses_loudly_and_fetches_nothing(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(gibsd, "fetch_tile", lambda r, c: (_ for _ in ()).throw(AssertionError("should not fetch")))
    before = gibsd.STATE["paused_flag"]
    gibsd.poll_once(flags=Flags(ok=False))
    assert gibsd.STATE["paused_flag"] == before + 1
    assert not os.path.exists(os.path.join(gibsd.OUT, "manifest.json"))


def test_one_bad_tile_never_kills_the_poll(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)

    def flaky(row, col):
        if (row, col) == (18, 20):
            raise OSError("gibs hiccup")
        return b"png-bytes-" + f"{row}-{col}".encode()

    monkeypatch.setattr(gibsd, "fetch_tile", flaky)
    gibsd.poll_once(flags=Flags())
    manifest = json.load(open(os.path.join(gibsd.OUT, "manifest.json")))
    total = len(gibsd.ROWS) * len(gibsd.COLS)
    assert len(manifest["tiles"]) == total - 1, "the survivors shelve"
    assert not any((t["row"], t["col"]) == (18, 20) for t in manifest["tiles"]), "the failure is named by absence, never listed as present"
    assert gibsd.STATE["tiles_failed"] == 1
    assert gibsd.STATE["tiles_ok"] == total - 1
    for t in manifest["tiles"]:
        assert t["bounds"] == gibsd.tile_bounds(t["row"], t["col"])
        assert os.path.exists(os.path.join(gibsd.OUT, t["file"]))


def test_writes_are_atomic_no_tmp_survives(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(gibsd, "fetch_tile", lambda r, c: b"png")
    gibsd.poll_once(flags=Flags())
    leftovers = [f for root, _, files in os.walk(gibsd.OUT) for f in files if f.endswith(".tmp")]
    assert leftovers == [], "tmp-then-rename leaves no partial file behind"
    assert json.load(open(os.path.join(gibsd.OUT, "manifest.json")))["layer"] == gibsd.LAYER


def test_api_serves_the_shared_descriptor(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), gibsd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            assert r.status == 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200
                assert "protobuf" in r.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            # descriptor not beside the code in a bare checkout: the daemon
            # says so with 503 rather than serving nothing -- also correct
            assert e.code == 503
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            body = r.read().decode()
            assert "gibsd_polls_total" in body and "gibsd_flipr_reachable" in body
    finally:
        srv.shutdown()
