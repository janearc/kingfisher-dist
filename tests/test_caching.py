import email.utils
import os
import time

import serve

TILE = "/econ/res8/8b01.json"


def test_a_tile_carries_an_etag_and_a_last_modified(get):
    resp = get(TILE)
    assert resp.status == 200
    # the generation is baked into the tag, so a poke invalidates every tag at
    # once without anyone having to know which files moved
    assert resp.headers["etag"].startswith('"1-')
    assert resp.headers["last-modified"]


def test_if_none_match_gets_a_304_with_no_body(get):
    etag = get(TILE).headers["etag"]
    resp = get(TILE, headers={"If-None-Match": etag})
    assert resp.status == 304
    assert resp.body == b""
    assert resp.headers["etag"] == etag


def test_a_stale_etag_gets_the_file(get):
    resp = get(TILE, headers={"If-None-Match": '"nonsense"'})
    assert resp.status == 200
    assert resp.body


def test_a_304_is_not_counted_as_a_tile(get):
    etag = get(TILE).headers["etag"]
    get(TILE, headers={"If-None-Match": etag})
    stats = get("/stats").json()
    # two requests, but only one tile actually left the building. anything
    # measuring throughput would otherwise count the cheapest possible
    # response as a delivery.
    assert stats["requests_total"] == 2
    assert stats["tiles_total"] == 1
    assert stats["by_status"]["304"] == 1


def test_if_modified_since_is_honoured(get):
    # the defect this replaced: we advertised last-modified, then ignored the
    # request header that goes with it. a polite client asked, got a full 200,
    # and re-shipped every byte. three times out of three.
    future = email.utils.formatdate(time.time() + 3600, usegmt=True)
    resp = get(TILE, headers={"If-Modified-Since": future})
    assert resp.status == 304
    assert resp.body == b""


def test_if_modified_since_in_the_past_gets_the_file(get):
    past = email.utils.formatdate(time.time() - 86400, usegmt=True)
    resp = get(TILE, headers={"If-Modified-Since": past})
    assert resp.status == 200
    assert resp.body


def test_if_modified_since_is_compared_at_one_second_resolution(get, mounted):
    # HTTP dates have one-second resolution and mtimes almost never do, so a
    # file saved mid-second looks perpetually newer unless both are truncated
    path = mounted["econ"] / "res8" / "8b01.json"
    mtime = os.stat(path).st_mtime
    os.utime(path, (mtime, int(mtime) + 0.75))
    exact = email.utils.formatdate(int(mtime) + 0.75, usegmt=True)
    assert get(TILE, headers={"If-Modified-Since": exact}).status == 304


def test_an_unparseable_if_modified_since_gets_the_file(get):
    resp = get(TILE, headers={"If-Modified-Since": "yesterday-ish"})
    assert resp.status == 200
    assert resp.body


def test_if_none_match_wins_when_both_validators_are_sent(get):
    # RFC 9110: the entity tag is the stronger validator, and it is the one
    # carrying the generation
    etag = get(TILE).headers["etag"]
    past = email.utils.formatdate(time.time() - 86400, usegmt=True)
    resp = get(TILE, headers={"If-None-Match": etag, "If-Modified-Since": past})
    assert resp.status == 304


def test_reload_invalidates_every_etag_at_once(get):
    etag = get(TILE).headers["etag"]
    assert get(TILE, headers={"If-None-Match": etag}).status == 304
    get("/reload")
    # nothing on disk changed. the generation did, and that is enough.
    resp = get(TILE, headers={"If-None-Match": etag})
    assert resp.status == 200
    assert resp.headers["etag"] != etag
    assert resp.headers["etag"].startswith('"2-')


def test_reload_does_not_reach_an_if_modified_since_client(get):
    # the documented caveat, pinned so it stays documented: mtime-based
    # freshness cannot see a generation bump, because nothing it can observe
    # has changed. ETag is the better validator and this is why.
    future = email.utils.formatdate(time.time() + 3600, usegmt=True)
    assert get(TILE, headers={"If-Modified-Since": future}).status == 304
    get("/reload")
    assert get(TILE, headers={"If-Modified-Since": future}).status == 304


def test_a_changed_file_gets_a_new_etag(get, mounted):
    etag = get(TILE).headers["etag"]
    path = mounted["econ"] / "res8" / "8b01.json"
    path.write_text('{"cells":{"8b01":99}}')
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert get(TILE).headers["etag"] != etag


def test_introspection_responses_are_never_cached(get):
    for path in ("/health", "/stats", "/metrics", "/reload"):
        assert get(path).headers["cache-control"] == "no-store"
