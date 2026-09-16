# Issue 79, first half: the payload lands before the meta.
#
# start_fetch used to write the meta sidecar BEFORE the download began, on
# purpose, so a crash between the two left meta-without-payload -- a failure the
# daemon would report -- rather than an orphan payload nothing could classify.
# The cost was that every download looked like an ingest failure for as long as
# it ran: the scanner found the meta, missed the payload, counted an error, and
# put the item into exponential backoff with full jitter up to an hour. A long
# download appeared on the shelf up to an hour after it finished, and its
# 24-hour give-up clock ran from the start of the transfer.
#
# Now the bytes go to <id>.part, are renamed into place when complete, and only
# then is the meta written (itself via tmp+rename). From the scanner's side,
# "meta without payload" cannot happen by this path -- which is what makes the
# second half of 79 safe: that shape is broken, and it dead-letters.

import json
import os

import pytest

import providers


@pytest.fixture
def indexed(monkeypatch, tmp_path):
    """One dataset in the index, a spool under tmp, and no index.json writes."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(providers, "_save_index_locked", lambda: None)
    with providers._INDEX_LOCK:
        providers._INDEX["ds-1"] = {
            "id": "ds-1", "source_id": "usgs", "kind": "LAYER_KIND_ASSET",
            "title": "a tile", "download_url": "https://example.test/tile.laz",
            "state": "FETCH_STATE_QUEUED",
        }
    yield str(spool)
    with providers._INDEX_LOCK:
        providers._INDEX.pop("ds-1", None)


def test_the_meta_lands_only_after_the_payload(indexed, monkeypatch):
    seen = {}

    def fake_download(url, dest, headers=None, on_progress=None):
        # DURING the transfer: no meta may exist, and the bytes go to a name
        # the scanner does not read
        seen["meta_during"] = os.path.exists(os.path.join(indexed, "ds-1.meta.json"))
        seen["dest"] = dest
        with open(dest, "wb") as f:
            f.write(b"LASF" * 4)
        return 16

    monkeypatch.setattr(providers.net, "download", fake_download)
    providers._run_fetch("ds-1", indexed)

    assert seen["meta_during"] is False, "the meta was written before the download"
    assert seen["dest"].endswith(".part"), "the download must not land under the final name"
    assert os.path.exists(os.path.join(indexed, "ds-1"))
    assert os.path.exists(os.path.join(indexed, "ds-1.meta.json"))
    assert not os.path.exists(os.path.join(indexed, "ds-1.part"))
    assert not os.path.exists(os.path.join(indexed, "ds-1.meta.json.tmp"))
    assert providers._INDEX["ds-1"]["state"] == "FETCH_STATE_HELD"
    meta = json.load(open(os.path.join(indexed, "ds-1.meta.json")))
    assert meta["id"] == "ds-1" and meta["kind"] == "LAYER_KIND_ASSET"


def test_a_failed_download_leaves_nothing_for_the_scanner(indexed, monkeypatch):
    """No meta was ever written and the partial is removed, so the scanner has
    nothing to find and nothing to back off on. The failure lives in the index,
    where the ticket reads it."""
    def fake_download(url, dest, headers=None, on_progress=None):
        with open(dest, "wb") as f:
            f.write(b"half")
        raise OSError("connection reset by peer")

    monkeypatch.setattr(providers.net, "download", fake_download)
    providers._run_fetch("ds-1", indexed)

    assert os.listdir(indexed) == [], os.listdir(indexed)
    assert providers._INDEX["ds-1"]["state"] == "FETCH_STATE_FAILED"
    assert "reset" in providers._INDEX["ds-1"]["last_error"]


def test_start_fetch_writes_no_meta(indexed, monkeypatch):
    # the whole first half in one assertion: queueing a fetch leaves the spool
    # empty until the bytes are there
    monkeypatch.setattr(providers, "_require_flags", lambda flags, sid: True)
    monkeypatch.setattr(providers, "_ensure_worker", lambda: None)
    # A FRESH QUEUE, not the module's. Another test module may already have
    # started the real worker thread, and it is bound to the old queue object:
    # it drained this item before the assertion could read it and went off to
    # attempt a real download of example.test. A queue only this test holds
    # cannot be consumed by anything else.
    import queue
    q = queue.Queue()
    monkeypatch.setattr(providers, "_FETCH_Q", q)
    resp = providers.start_fetch("ds-1", flags=None, spool_dir=indexed)
    assert "ticket" in resp
    assert os.listdir(indexed) == []
    assert q.get_nowait() == ("ds-1", indexed)
