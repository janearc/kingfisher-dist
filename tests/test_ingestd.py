# the stomach's rules: kind routes to pipeline, no-pipeline WAITS, existing
# dataset means done, flag pause is loud, asset placement is atomic.

import http.server
import json
import os
import threading
import urllib.error
import urllib.request

import ingestd


def _serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ingestd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Flags:
    def __init__(self, ok=True):
        self.ok = ok
    def check(self, key):
        return self.ok


def _spool(tmp_path, monkeypatch, meta, payload=b"JPEGBYTES"):
    spool = tmp_path / "spool"; out = tmp_path / "out"
    spool.mkdir(); out.mkdir()
    (spool / meta["id"]).write_bytes(payload)
    (spool / (meta["id"] + ".meta.json")).write_text(json.dumps(meta))
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "OUT", str(out))
    return spool, out


def test_asset_ingests_atomically(tmp_path, monkeypatch):
    meta = {"id": "gibs-x", "kind": "LAYER_KIND_ASSET", "title": "t",
            "description": "what it is, in prose",
            "vintage_id": "2026-08-27", "source_id": "nasa_gibs",
            "download_url": "http://x?FORMAT=image/jpeg"}
    spool, out = _spool(tmp_path, monkeypatch, meta)
    ingestd.scan_once(flags=Flags())
    d = out / "gibs-x"
    assert (d / "content.jpg").read_bytes() == b"JPEGBYTES"
    m = json.loads((d / "manifest.json").read_text())
    assert m["vintage_id"] == "2026-08-27"
    assert m["description"] == "what it is, in prose"
    assert m["content"] == "content.jpg"
    assert m["content_type"] == "image/jpeg"
    assert not (spool / "gibs-x.meta.json").exists()  # consumed
    assert ingestd.STATE["ingested"] >= 1


def test_no_pipeline_waits(tmp_path, monkeypatch):
    # LAYER_KIND_INTENSIVE used to be this test's example of "no pipeline
    # yet". It has one now (pointcloud.py), so the invariant needs a kind that
    # genuinely has none -- PAIRS, which DESIGN.md still lists as unwritten.
    # The RULE is unchanged and is what matters: a spooled file whose kind has
    # no pipeline is left alone, never consumed blind.
    meta = {"id": "pairs-1", "kind": "LAYER_KIND_PAIRS"}
    spool, out = _spool(tmp_path, monkeypatch, meta)
    before = ingestd.STATE["skipped_no_pipeline"]
    ingestd.scan_once(flags=Flags())
    # the file WAITS for its pipeline -- meta intact, nothing consumed
    assert (spool / "pairs-1.meta.json").exists()
    assert not (out / "pairs-1").exists()
    assert ingestd.STATE["skipped_no_pipeline"] == before + 1


def test_a_point_cloud_that_is_not_a_point_cloud_fails_loudly(tmp_path, monkeypatch):
    # the INTENSIVE pipeline exists now, so a payload that claims to be lidar
    # and is not must raise rather than shelve something unreadable. The meta
    # stays in the spool: a failed ingest is retryable, not consumed.
    meta = {"id": "lidar-1", "kind": "LAYER_KIND_INTENSIVE"}
    spool, out = _spool(tmp_path, monkeypatch, meta)
    before = ingestd.STATE["errors"]
    ingestd.scan_once(flags=Flags())
    assert not (out / "lidar-1").exists()
    assert (spool / "lidar-1.meta.json").exists()
    assert ingestd.STATE["errors"] == before + 1


def test_existing_dataset_is_done(tmp_path, monkeypatch):
    meta = {"id": "gibs-y", "kind": "LAYER_KIND_ASSET"}
    spool, out = _spool(tmp_path, monkeypatch, meta)
    (out / "gibs-y").mkdir()
    ingestd.scan_once(flags=Flags())
    assert not (spool / "gibs-y.meta.json").exists()  # acknowledged, not redone


def test_flag_off_pauses_loudly(tmp_path, monkeypatch):
    meta = {"id": "gibs-z", "kind": "LAYER_KIND_ASSET"}
    spool, out = _spool(tmp_path, monkeypatch, meta)
    before = ingestd.STATE["paused_flag"]
    ingestd.scan_once(flags=Flags(ok=False))
    assert ingestd.STATE["paused_flag"] == before + 1
    assert (spool / "gibs-z.meta.json").exists()  # untouched


def test_api_serves_the_daemon_descriptor():
    # ingestd has no RPC of its own -- /api is what it consumes, not what
    # it serves (mitigation-1's ruling: nobody gets to have no /api).
    srv = _serve()
    try:
        port = srv.server_address[1]
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=5)
        assert r.status == 200
        assert "application/x-protobuf" in r.headers.get("content-type", "")
        body = r.read()
        assert body == ingestd.descriptor_bytes()
        assert b"flipr/v1/flipr.proto" in body
    finally:
        srv.shutdown()


def test_unknown_path_names_the_three_real_routes():
    srv = _serve()
    try:
        port = srv.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            assert False, "expected a 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert b"health, metrics, or api" in e.read()
    finally:
        srv.shutdown()
