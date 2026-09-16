# Issue 76: ListDatasets answered 200 with nothing for an unadapted source --
# the same answer an adapted source gives before anything is indexed. A caller
# could not tell "nothing here yet" from "nothing can ever be here". RefreshIndex
# already answered 501 {"unimplemented"} for the same source; the two doors now
# agree.

import json

import pytest

import providers

RPC = "/kingfisher.discovery.v1.DiscoveryService/ListDatasets"


def _post(get, body):
    return get(RPC, method="POST", data=json.dumps(body).encode(),
               headers={"content-type": "application/json"})


@pytest.fixture
def unadapted():
    ids = [s["id"] for s in providers.SOURCES if s["id"] not in providers.ADAPTED]
    assert ids, "the fixture needs at least one registered-but-unadapted source"
    return ids[0]


@pytest.fixture
def adapted():
    return sorted(providers.ADAPTED)[0]


def test_an_unadapted_source_says_so_like_refresh_index_does(get, unadapted):
    r = _post(get, {"sourceId": unadapted})
    assert r.status == 501
    assert r.json() == {"unimplemented": unadapted}


def test_an_unknown_source_is_404_not_an_empty_list(get):
    r = _post(get, {"sourceId": "no-such-source"})
    assert r.status == 404
    assert "unknown source" in r.json()["error"]


def test_an_adapted_source_with_nothing_indexed_is_an_honest_empty_list(get, adapted):
    # this is what the empty list is FOR: could have datasets, has none
    r = _post(get, {"sourceId": adapted})
    assert r.status == 200


def test_no_source_filter_still_lists_everything(get):
    assert _post(get, {}).status == 200
