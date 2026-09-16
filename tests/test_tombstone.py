# A tombstone is an operator's word written into the index: never fetch this
# dataset again, and why. Born 2026-09-05 when 119 CA_ALAMEDACO_2006 lidar
# tiles were dead-lettered (their LAZ declares no CRS; ingestd refuses to
# guess) and their bytes were to be deleted: without a state in the index the
# next refresh would have offered to download every one of them again. These
# pin the four facts that make a tombstone safe: Fetch refuses it before it
# looks at a flag; a re-index cannot erase it; it survives a restart; and it
# is reversible through the same door it was made.

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gen"))
from google.protobuf import json_format  # noqa: E402
from kingfisher.discovery.v1 import discovery_pb2  # noqa: E402
import providers  # noqa: E402
import serve  # noqa: E402

REASON = "LAZ declares no CRS; ingestd refuses to guess; issue 69; tombstoned 2026-09-05 on the operator's word"


class NeverAsked:
    """A flag plane that must not be consulted: a tombstone outranks a flag."""
    def check(self, key):
        raise AssertionError(f"flags consulted ({key}) for a tombstoned dataset")


class On:
    def check(self, key):
        return True


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("KINGFISHER_INDEX", str(tmp_path / "index.json"))
    monkeypatch.setenv("KINGFISHER_SPOOL", str(tmp_path))
    with providers._INDEX_LOCK:
        providers._INDEX.clear()
        providers._INDEX["usgs-a1"] = {"id": "usgs-a1", "source_id": "usgs", "title": "USGS Lidar Point Cloud CA_ALAMEDACO_2006 000380",
                                       "kind": "LAYER_KIND_INTENSIVE", "state": "FETCH_STATE_HELD",
                                       "download_url": "https://rockyweb.usgs.gov/x/000380.laz", "bytes_downloaded": 5}
        providers._INDEX["usgs-b2"] = {"id": "usgs-b2", "source_id": "usgs", "title": "another tile",
                                       "kind": "LAYER_KIND_INTENSIVE", "state": "FETCH_STATE_INDEXED",
                                       "download_url": "https://rockyweb.usgs.gov/x/b2.laz"}
    yield tmp_path
    with providers._INDEX_LOCK:
        providers._INDEX.clear()


def test_tombstone_writes_state_reason_and_date_and_keeps_the_url(indexed):
    rec = providers.tombstone("usgs-a1", REASON)
    assert rec["state"] == "FETCH_STATE_TOMBSTONED"
    assert rec["tombstone_reason"] == REASON
    assert rec["tombstoned_at"].endswith("Z") and rec["tombstoned_at"][:2] == "20"
    assert rec["download_url"] == "https://rockyweb.usgs.gov/x/000380.laz", "a revivable record keeps its url"
    assert "last_error" not in rec
    assert providers.tombstone("nope", REASON) == {"error": "unknown dataset nope"}


def test_fetch_refuses_a_tombstoned_dataset_before_any_flag_is_read(indexed, monkeypatch):
    providers.tombstone("usgs-a1", REASON)
    puts = []
    monkeypatch.setattr(providers._FETCH_Q, "put", lambda item: puts.append(item))
    out = providers.start_fetch("usgs-a1", NeverAsked(), str(indexed))
    assert "refused" in out and "tombstoned" in out["refused"] and "no CRS" in out["refused"]
    assert puts == [] and "ticket" not in out
    # the neighbour is untouched: flags on, it queues as before
    out = providers.start_fetch("usgs-b2", On(), str(indexed))
    assert "ticket" in out and puts


def test_a_reindex_cannot_erase_a_tombstone(indexed, monkeypatch):
    providers.tombstone("usgs-a1", REASON)
    fresh = [{"id": "usgs-a1", "source_id": "usgs", "title": "USGS Lidar Point Cloud CA_ALAMEDACO_2006 000380",
              "kind": "LAYER_KIND_INTENSIVE", "state": "FETCH_STATE_INDEXED",
              "download_url": "https://rockyweb.usgs.gov/x/000380.laz", "bytes_estimate": 99}]
    monkeypatch.setattr(providers, "_usgs_products", lambda bbox, limit: fresh)
    providers.refresh_index("usgs", On(), bbox=(0, 0, 1, 1), limit=5)
    d = providers.list_datasets("usgs")
    a1 = [x for x in d if x["id"] == "usgs-a1"][0]
    assert a1["state"] == "FETCH_STATE_TOMBSTONED" and a1["tombstone_reason"] == REASON and a1["tombstoned_at"]
    assert a1["bytes_estimate"] == 99, "the provider is still authoritative about the catalogue"


def test_a_tombstone_survives_a_restart(indexed):
    providers.tombstone("usgs-a1", REASON)
    at = providers._INDEX["usgs-a1"]["tombstoned_at"]
    with providers._INDEX_LOCK:
        providers._INDEX.clear()
    providers.load_index()
    d = providers._INDEX["usgs-a1"]
    assert d["state"] == "FETCH_STATE_TOMBSTONED" and d["tombstone_reason"] == REASON and d["tombstoned_at"] == at


def test_revive_takes_the_word_back(indexed):
    providers.tombstone("usgs-a1", REASON)
    rec = providers.tombstone("usgs-a1", "", revive=True)
    assert rec["state"] == "FETCH_STATE_INDEXED"
    assert "tombstone_reason" not in rec and "tombstoned_at" not in rec
    assert rec["download_url"]


def _post(get, rpc, body):
    return get(f"/kingfisher.discovery.v1.DiscoveryService/{rpc}", method="POST",
               data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


def test_the_tombstone_rpc_round_trips_through_the_generated_types(indexed, get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", On())
    resp = _post(get, "Tombstone", {"datasetId": "usgs-a1", "reason": REASON})
    assert resp.status == 200, resp.text
    parsed = json_format.Parse(resp.body, discovery_pb2.TombstoneResponse())
    assert parsed.record.state == discovery_pb2.FETCH_STATE_TOMBSTONED
    assert parsed.record.tombstone_reason == REASON and parsed.record.tombstoned_at
    # Fetch now refuses it, 403, naming the tombstone
    resp = _post(get, "Fetch", {"datasetId": "usgs-a1"})
    assert resp.status == 403 and "tombstoned" in resp.text
    # ListDatasets carries the state and the reason
    resp = _post(get, "ListDatasets", {"sourceId": "usgs"})
    listed = json_format.Parse(resp.body, discovery_pb2.ListDatasetsResponse())
    a1 = [r for r in listed.datasets if r.id == "usgs-a1"][0]
    assert a1.state == discovery_pb2.FETCH_STATE_TOMBSTONED and a1.tombstone_reason == REASON
    # revive through the same door
    resp = _post(get, "Tombstone", {"datasetId": "usgs-a1", "revive": True})
    assert json_format.Parse(resp.body, discovery_pb2.TombstoneResponse()).record.state == discovery_pb2.FETCH_STATE_INDEXED


def test_the_tombstone_rpc_refuses_no_reason_and_names_an_unknown_dataset(indexed, get, monkeypatch):
    monkeypatch.setattr(serve, "_FLIPR", On())
    assert _post(get, "Tombstone", {"datasetId": "usgs-a1"}).status == 400
    assert _post(get, "Tombstone", {"datasetId": "usgs-a1", "reason": "   "}).status == 400
    assert _post(get, "Tombstone", {"datasetId": "ghost", "reason": REASON}).status == 404
