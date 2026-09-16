# Issue 115: a successful ingest removes its payload, not only its meta.
#
# Measured 2026-09-04: 8.2G of payloads in the spool root beside ~204M of
# actual product on the shelf -- every tile ever ingested, still there, because
# scan_once removed the .meta.json after pipe() and never the bytes. The
# dataset directory is the product (DESIGN.md names dataset-dir-exists as the
# idempotency key), the spool is runtime not source (078a6f0), nothing reads a
# payload after pipe() returns, and every one is a re-downloadable file.
#
# The one-time cleanup of the existing 8.2G is deliberately NOT here. It deletes
# nine gigabytes by hand and wants its own go/no-go.

import json
import os

import ingestd


class Flags:
    def check(self, key):
        return True


def _spool(monkeypatch, tmp_path, meta, payload=b"BYTES"):
    spool = tmp_path / "spool"
    out = tmp_path / "out"
    spool.mkdir()
    out.mkdir()
    (spool / meta["id"]).write_bytes(payload)
    (spool / (meta["id"] + ".meta.json")).write_text(json.dumps(meta))
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "OUT", str(out))
    with ingestd.STATE_LOCK:
        ingestd.STATE["ingested"] = 0
        ingestd.STATE["errors"] = 0
    return spool, out


def test_a_successful_asset_ingest_leaves_nothing_in_the_spool(monkeypatch, tmp_path):
    """The asset pipeline MOVES its payload into the dataset dir, so this case
    was already clean for the bytes -- the meta is what it left behind."""
    spool, out = _spool(monkeypatch, tmp_path, {
        "id": "img", "kind": "LAYER_KIND_ASSET", "download_url": "https://x/y.jpeg"})
    ingestd.scan_once(flags=Flags())
    assert ingestd.STATE["ingested"] == 1
    assert (out / "img" / "manifest.json").exists()
    assert sorted(os.listdir(spool)) == [], os.listdir(spool)


def test_a_pipeline_that_only_reads_its_payload_still_has_it_removed(monkeypatch, tmp_path):
    """The lidar pipeline READS the LAZ and writes cells; the payload was left
    exactly where it was, forever. This is the 8.2G."""
    spool, out = _spool(monkeypatch, tmp_path, {"id": "cloud", "kind": "LAYER_KIND_READONLY"})

    def read_only(payload, meta, dest):
        with open(payload, "rb") as f:
            assert f.read() == b"BYTES"
        os.makedirs(dest)
        with open(os.path.join(dest, "manifest.json"), "w") as f:
            json.dump({"id": meta["id"]}, f)
    monkeypatch.setitem(ingestd.PIPELINES, "LAYER_KIND_READONLY", read_only)

    ingestd.scan_once(flags=Flags())

    assert ingestd.STATE["ingested"] == 1
    assert (out / "cloud" / "manifest.json").exists()
    assert not (spool / "cloud").exists(), "the payload survived a successful ingest"
    assert not (spool / "cloud.meta.json").exists()


def test_a_failed_ingest_keeps_its_payload_for_the_retry(monkeypatch, tmp_path):
    # cleanup is the reward for success; a transient failure must not lose the
    # bytes it is about to retry against
    spool, _ = _spool(monkeypatch, tmp_path, {"id": "flaky", "kind": "LAYER_KIND_FLAKY"})

    def flaky(payload, meta, dest):
        raise OSError("disk hiccup")
    monkeypatch.setitem(ingestd.PIPELINES, "LAYER_KIND_FLAKY", flaky)

    ingestd.scan_once(flags=Flags())

    assert ingestd.STATE["errors"] == 1
    assert (spool / "flaky").exists()
    assert (spool / "flaky.meta.json").exists()
