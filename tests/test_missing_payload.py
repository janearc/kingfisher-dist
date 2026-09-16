# Issue 79, second half: a meta whose payload is gone is Unprocessable.
#
# Because the fetcher now lands the payload before the meta, a meta with no
# payload cannot be a download in progress. It is bytes that were removed or
# never landed, and no number of passes will bring them back. It used to raise
# FileNotFoundError, which the loop treats as transient: an error every pass,
# exponential backoff with full jitter, and a day before giving up. After the
# operation 1 roll exactly one such item was left in the live spool doing that,
# on a ten-second interval, forever.

import json
import os

import ingestd


class Flags:
    def check(self, key):
        return True


def _reset(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    out = tmp_path / "out"
    spool.mkdir()
    out.mkdir()
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "DEAD", str(spool / "dead"))
    monkeypatch.setattr(ingestd, "OUT", str(out))
    with ingestd.STATE_LOCK:
        for k in ("errors", "dead_lettered", "backing_off", "ingested"):
            ingestd.STATE[k] = 0
        ingestd.STATE["last_error"] = ""
    return spool, out


def test_a_meta_with_no_payload_dead_letters_on_the_first_pass(monkeypatch, tmp_path):
    spool, _ = _reset(monkeypatch, tmp_path)
    (spool / "gone.meta.json").write_text(json.dumps({"id": "gone", "kind": "LAYER_KIND_ASSET"}))

    ingestd.scan_once(flags=Flags())

    assert ingestd.STATE["dead_lettered"] == 1
    assert ingestd.STATE["errors"] == 0, "this is not a transient and must not count as one"
    assert ingestd.STATE["backing_off"] == 0
    assert not (spool / "gone.meta.json").exists()
    assert (spool / "dead" / "gone.meta.json").exists()


def test_it_does_not_come_back_on_the_next_pass(monkeypatch, tmp_path):
    # the point of dead-lettering: the scanner stops seeing it
    spool, _ = _reset(monkeypatch, tmp_path)
    (spool / "gone.meta.json").write_text(json.dumps({"id": "gone", "kind": "LAYER_KIND_ASSET"}))
    ingestd.scan_once(flags=Flags())
    ingestd.scan_once(flags=Flags())
    assert ingestd.STATE["dead_lettered"] == 1
    assert ingestd.STATE["errors"] == 0


def test_a_partial_download_is_invisible_to_the_scanner(monkeypatch, tmp_path):
    """<id>.part is the fetcher's name for bytes still arriving. It has no meta,
    so the scanner never looks at it; it is neither an error nor a dataset."""
    spool, _ = _reset(monkeypatch, tmp_path)
    (spool / "arriving.part").write_bytes(b"half a tile")

    ingestd.scan_once(flags=Flags())

    assert ingestd.STATE["errors"] == 0
    assert ingestd.STATE["dead_lettered"] == 0
    assert (spool / "arriving.part").exists()


def test_a_real_transient_still_backs_off_rather_than_dead_lettering(monkeypatch, tmp_path):
    # the distinction this rests on: a payload that IS there and a pipeline
    # that fails for an ordinary reason is still a retry
    spool, _ = _reset(monkeypatch, tmp_path)
    (spool / "flaky").write_bytes(b"bytes")
    (spool / "flaky.meta.json").write_text(json.dumps({"id": "flaky", "kind": "LAYER_KIND_FLAKY"}))

    def flaky(payload, meta, dest):
        raise OSError("disk hiccup")
    monkeypatch.setitem(ingestd.PIPELINES, "LAYER_KIND_FLAKY", flaky)

    ingestd.scan_once(flags=Flags())

    assert ingestd.STATE["errors"] == 1
    assert ingestd.STATE["dead_lettered"] == 0
    assert (spool / "flaky.meta.json").exists()
