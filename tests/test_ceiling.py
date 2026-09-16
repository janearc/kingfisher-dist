# The memory ceiling on /cells, and who asked.
#
# kingfisher was OOM-killed twice by a /cells request whose budget nothing
# capped: 2026-09-01 by the bench zoomed out, 2026-09-04 by the first run of
# bin/kingfisher-stress.sh (budget 512k on the Bay window, res 10 to 11 in one
# step). The ceiling is computed per request from the process's cgroup limit,
# its live heap, the row count /shelf already prices, and the bytes per row
# the stress harness measured (shelf.py, THE MEMORY CEILING). A request that
# would not fit is served coarser and told; one that cannot fit at all is
# refused; one above MAX_BUDGET is refused before pricing.

import json

import pytest

import serve
import shelf
from test_shelf import barn  # noqa: F401  -- the three-tile shelf, shared

MI = 1 << 20


# ---------------------------------------------------------------------------
# reading the room


def test_memory_limit_reads_cgroup_v2_then_v1_then_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("KINGFISHER_MEMORY_LIMIT", raising=False)
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    v2.write_text("268435456\n")
    assert shelf.memory_limit(paths=(str(v2), str(v1))) == 256 * MI
    # "max" is v2's word for no limit; fall through to v1
    v2.write_text("max\n")
    v1.write_text("536870912\n")
    assert shelf.memory_limit(paths=(str(v2), str(v1))) == 512 * MI
    # v1's "unlimited" is an enormous number, which is not a limit
    v1.write_text(str(1 << 63) + "\n")
    assert shelf.memory_limit(paths=(str(v2), str(v1))) is None
    assert shelf.memory_limit(paths=(str(tmp_path / "absent"),)) is None


def test_the_env_override_wins_for_local_runs(monkeypatch):
    monkeypatch.setenv("KINGFISHER_MEMORY_LIMIT", str(128 * MI))
    assert shelf.memory_limit(paths=()) == 128 * MI


def test_heap_bytes_is_a_positive_number_on_any_platform():
    assert shelf.heap_bytes() > 1 * MI


def test_the_parse_reserve_covers_the_transient_measured_in_the_pod():
    """Measured 2026-09-04 on the rolled pod with bin/kingfisher-stress.sh:
    about 90Mi of heap per request before the first cell, the json.load of
    whole datasets. The reserve is what keeps that out of the headroom; a
    reserve below the measurement would make the 20 percent headroom the
    thing absorbing it, which is not what headroom is for."""
    assert shelf.INPUT_RESERVE >= 90 * MI
    # and not so large that a fresh 256Mi pod could serve nothing: room must
    # remain for the smallest real answer on the shelf at a fresh heap
    fresh = 44 * MI
    assert 256 * MI * shelf.HEADROOM - fresh - shelf.INPUT_RESERVE > 1000 * shelf.BYTES_PER_ROW


# ---------------------------------------------------------------------------
# fit: choose_res with the memory in the room


def test_fit_without_a_limit_is_choose_res(barn):
    entries = shelf.index(barn["root"])
    res, est, clipped = shelf.fit(entries, 8, limit=None)
    assert (res, est) == shelf.choose_res(entries, 8)
    assert clipped is None


def test_fit_leaves_a_budget_that_fits_alone(barn):
    entries = shelf.index(barn["root"])
    res, est, clipped = shelf.fit(entries, 10**6, limit=1 << 40, heap=10 * MI)
    assert clipped is None
    assert res == max(e["res"] for e in entries)


def test_fit_serves_coarser_when_the_room_is_short(barn):
    """The budget alone would allow full resolution; the memory does not.
    The answer is a coarser res and the number of rows that did fit, which
    is the budget the caller effectively got."""
    entries = shelf.index(barn["root"])
    full_res, full_est = shelf.choose_res(entries, 10**6)
    # room = 0.8*limit - heap - reserve; make it fit a few rows and no more
    limit = int((shelf.INPUT_RESERVE + 10 * MI + 3 * shelf.BYTES_PER_ROW) / shelf.HEADROOM) + 1
    res, est, clipped = shelf.fit(entries, 10**6, limit=limit, heap=10 * MI)
    assert res < full_res
    assert clipped is not None and clipped >= 1
    assert est * shelf.BYTES_PER_ROW <= limit * shelf.HEADROOM - 10 * MI - shelf.INPUT_RESERVE


def test_fit_refuses_when_not_even_the_floor_fits(barn):
    entries = shelf.index(barn["root"])
    with pytest.raises(shelf.NoRoom) as ex:
        shelf.fit(entries, 100, limit=100 * MI, heap=99 * MI)
    assert ex.value.heap == 99 * MI and ex.value.limit == 100 * MI


# ---------------------------------------------------------------------------
# the door


@pytest.fixture
def shelved(barn, mounted):
    serve.MOUNTS["/ingested/"] = barn["root"]
    yield barn
    serve.MOUNTS.pop("/ingested/", None)


def test_a_budget_above_the_ceiling_is_413_before_any_pricing(shelved, get, monkeypatch):
    calls = []
    monkeypatch.setattr(shelf, "index", lambda root: calls.append(root) or [])
    r = get(f"/cells?budget={shelf.MAX_BUDGET + 1}")
    assert r.status == 413
    body = r.json()
    assert body["max_budget"] == shelf.MAX_BUDGET
    assert body["limits"]["bytes_per_row"] == shelf.BYTES_PER_ROW
    assert calls == [], "the shelf must not be read for a request refused on its face"


def test_a_request_that_fits_is_served_as_asked_and_says_so(shelved, get, monkeypatch):
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: 1 << 40)
    monkeypatch.setattr(shelf, "heap_bytes", lambda: 10 * MI)
    body = get("/cells?budget=8").json()
    assert body["budget"] == 8 and body["budget_served"] == 8
    assert "clipped_by" not in body
    assert len(body["cells"]) <= 8


def test_a_request_the_memory_cannot_hold_is_served_coarser_and_told(shelved, get, monkeypatch):
    entries = shelf.index(shelved["root"])
    full_res, _ = shelf.choose_res(entries, 10**5)
    limit = int((shelf.INPUT_RESERVE + 10 * MI + 3 * shelf.BYTES_PER_ROW) / shelf.HEADROOM) + 1
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: limit)
    monkeypatch.setattr(shelf, "heap_bytes", lambda: 10 * MI)
    r = get("/cells?budget=100000")
    assert r.status == 200
    body = r.json()
    assert body["clipped_by"] == "memory"
    assert body["budget"] == 100000
    assert body["budget_served"] < body["budget"]
    assert body["res"] < full_res
    # the fold's guarantee applies to the budget actually served
    assert len(body["cells"]) <= body["budget_served"]


def test_no_room_at_all_is_a_503_with_the_numbers(shelved, get, monkeypatch):
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: 100 * MI)
    monkeypatch.setattr(shelf, "heap_bytes", lambda: 99 * MI)
    r = get("/cells?budget=8")
    assert r.status == 503
    body = r.json()
    assert body["heap_bytes"] == 99 * MI and body["memory_limit_bytes"] == 100 * MI
    assert "fold" in body["error"]


def test_the_directory_declares_the_limits(shelved, get, monkeypatch):
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: 256 * MI)
    monkeypatch.setattr(shelf, "heap_bytes", lambda: 44 * MI)
    lim = get("/directory").json()["limits"]
    assert lim["max_budget"] == shelf.MAX_BUDGET
    assert lim["memory_limit_bytes"] == 256 * MI and lim["heap_bytes"] == 44 * MI
    expect = int((256 * MI * shelf.HEADROOM - 44 * MI - shelf.INPUT_RESERVE) / shelf.BYTES_PER_ROW)
    assert lim["rows_that_fit_now"] == expect


def test_metrics_carry_the_heap_and_the_limit(get, monkeypatch):
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: 256 * MI)
    text = get("/metrics").text
    assert "kingfisher_heap_bytes " in text
    assert f"kingfisher_memory_limit_bytes {256 * MI}" in text


def test_an_unknown_limit_reads_zero_not_absent(get, monkeypatch):
    monkeypatch.setattr(shelf, "memory_limit", lambda paths=None: None)
    assert "kingfisher_memory_limit_bytes 0" in get("/metrics").text


# ---------------------------------------------------------------------------
# who asked


def test_a_door_request_logs_its_peer_query_and_status(shelved, get, capfd):
    get("/cells?budget=8")
    out = capfd.readouterr()
    lines = [l for l in (out.out + out.err).splitlines() if '"request"' in l and "/cells" in l]
    assert lines, "no request line was logged for a door"
    rec = json.loads(lines[-1][lines[-1].index("{"):])
    assert rec["peer"] == "127.0.0.1"
    assert rec["query"] == "budget=8"
    assert str(rec["status"]) == "200"
    assert "ua" in rec
