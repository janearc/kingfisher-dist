# The poison queue, and the one property that fixes it: a permanently
# unprocessable item must LEAVE the spool.
#
# WHAT HAPPENED. ingestd's spool pass had a single failure path -- log it, leave
# the file -- which is right for a transient failure and catastrophic for one
# that can never succeed. Measured on 2026-09-01 against the live estate: 119
# USGS tiles declaring no coordinate reference system, re-read and re-refused
# 4,463 times EACH in one day. 531,097 failures, ~200MB of log volume, 9.2GB of
# payloads pinned in /spool, and zero items ingested. The queue did continuous
# work and made no progress.
#
# These tests pin the behaviour rather than the mechanism, so a future rewrite
# that keeps the property passes: after a permanent refusal the item is not in
# the spool, it IS somewhere recoverable, and a transient failure still retries.

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingestd  # noqa: E402
from errors import Unprocessable  # noqa: E402


def _spool(tmp_path, monkeypatch, kind="las"):
    """A spool holding one item: a meta file and its payload."""
    spool = tmp_path / "spool"
    out = tmp_path / "out"
    spool.mkdir()
    out.mkdir()
    (spool / "thing.bin").write_bytes(b"payload")
    (spool / "thing.meta.json").write_text(json.dumps({"id": "thing.bin", "kind": kind}))
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "DEAD", str(spool / "dead"))
    monkeypatch.setattr(ingestd, "OUT", str(out))
    return spool


class _Flags:
    """A flag plane that says yes, so the pass runs."""
    def check(self, key):
        return True


def test_a_permanent_refusal_leaves_the_spool(tmp_path, monkeypatch):
    # THE REGRESSION. Before the fix this item stayed and was re-read forever.
    spool = _spool(tmp_path, monkeypatch)

    def refuses(payload, meta, dest):
        raise Unprocessable("the file declares no CRS")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": refuses})
    ingestd.scan_once(flags=_Flags())

    assert not (spool / "thing.meta.json").exists(), \
        "a permanently unprocessable item must not remain in the spool"
    assert (spool / "dead" / "thing.meta.json").exists(), \
        "it must be recoverable, not deleted -- kingfisher re-fetches, so a " \
        "deletion is a re-download waiting to happen"
    assert (spool / "dead" / "thing.bin").exists(), \
        "the payload moves too, or 9.2GB stays pinned in the spool forever"


def test_the_next_pass_does_not_see_it_again(tmp_path, monkeypatch):
    # The actual point. One removal is worthless if the scan picks it back up.
    spool = _spool(tmp_path, monkeypatch)
    calls = []

    def refuses(payload, meta, dest):
        calls.append(meta["id"])
        raise Unprocessable("no CRS")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": refuses})
    for _ in range(5):
        ingestd.scan_once(flags=_Flags())

    assert calls == ["thing.bin"], \
        f"tried {len(calls)} times across 5 passes; a permanent failure must be " \
        f"attempted exactly once"


def test_a_transient_failure_is_kept_and_retried(tmp_path, monkeypatch):
    # The guard on the fix. Dead-lettering a transient failure would be worse
    # than the bug it replaces: a poison queue is loud and recoverable, a
    # wrongly discarded dataset is neither.
    spool = _spool(tmp_path, monkeypatch)
    calls = []

    def flaky(payload, meta, dest):
        calls.append(meta["id"])
        raise OSError("disk hiccup")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": flaky})
    monkeypatch.setattr(ingestd, "BACKOFF_BASE_S", 0)   # no waiting, in a test
    monkeypatch.setattr(ingestd, "BACKOFF_MAX_S", 0)
    for _ in range(3):
        ingestd.scan_once(flags=_Flags())

    assert len(calls) == 3, "with backoff disabled it should attempt every pass"
    assert (spool / "thing.meta.json").exists(), \
        "a transient failure must NOT be dead-lettered"


def test_backoff_stops_it_hammering_every_pass(tmp_path, monkeypatch):
    # The COST half, distinct from termination. Before backoff each failing item
    # was attempted on every pass -- measured at 4,463 attempts per item per day
    # against one flaky government endpoint, with 119 items doing it in lockstep.
    spool = _spool(tmp_path, monkeypatch)
    calls = []

    def flaky(payload, meta, dest):
        calls.append(1)
        raise OSError("usgs is having a day")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": flaky})
    monkeypatch.setattr(ingestd, "BACKOFF_BASE_S", 3600)   # a long first wait
    for _ in range(10):
        ingestd.scan_once(flags=_Flags())

    assert len(calls) == 1, \
        f"attempted {len(calls)} times in 10 passes; after a failure the item " \
        f"must wait rather than be retried immediately"
    meta = json.loads((spool / "thing.meta.json").read_text())
    assert meta["_next_attempt_at"] > meta["_first_failed_at"], \
        "a next-attempt time must be scheduled into the future"


def test_dead_lettering_is_counted(tmp_path, monkeypatch):
    # It has to be visible. The reason this ran for days unnoticed is that
    # nothing counted it -- the alerting lives with the cluster, not here
    # side of the same lesson.
    _spool(tmp_path, monkeypatch)
    before = ingestd.STATE["dead_lettered"]

    monkeypatch.setattr(ingestd, "PIPELINES",
                        {"las": lambda p, m, d: (_ for _ in ()).throw(Unprocessable("no CRS"))})
    ingestd.scan_once(flags=_Flags())

    assert ingestd.STATE["dead_lettered"] == before + 1, \
        "dead_lettered must increment, so ingestd_dead_lettered_total exists"


def test_an_unreadable_meta_is_also_unprocessable(tmp_path, monkeypatch):
    # A meta that will not parse cannot be fixed by re-reading it either, and
    # the mover must not itself throw on the way past.
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "broken.meta.json").write_text("{ not json")
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "DEAD", str(spool / "dead"))
    monkeypatch.setattr(ingestd, "OUT", str(tmp_path / "out"))
    (tmp_path / "out").mkdir()

    for _ in range(3):
        ingestd.scan_once(flags=_Flags())

    # It is malformed rather than declared-permanent, so it stays and retries --
    # which is the safe direction, and documents the current behaviour honestly
    # rather than asserting a fix that is not there.
    assert (spool / "broken.meta.json").exists()


def test_a_transient_failure_is_given_up_on_eventually(tmp_path, monkeypatch):
    # "the government's websites are pretty flaky and a retry is
    # important, but.. there's a limit." Without a ceiling, "retry forever" is
    # the same poison queue in slower motion.
    spool = _spool(tmp_path, monkeypatch)
    monkeypatch.setattr(ingestd, "GIVE_UP_AFTER_S", 60.0)
    monkeypatch.setattr(ingestd, "BACKOFF_BASE_S", 0)

    def flaky(payload, meta, dest):
        raise OSError("usgs is having a day")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": flaky})

    # first failure stamps the clock; the item stays and will be retried
    ingestd.scan_once(flags=_Flags())
    assert (spool / "thing.meta.json").exists()

    # backdate the stamp to older than the ceiling, as if it had been failing
    # all night, then run one more pass
    meta = json.loads((spool / "thing.meta.json").read_text())
    assert "_first_failed_at" in meta, "the first failure must be recorded"
    meta["_first_failed_at"] = meta["_first_failed_at"] - 120
    meta["_next_attempt_at"] = 0        # backoff elapsed; it is due again
    (spool / "thing.meta.json").write_text(json.dumps(meta))

    ingestd.scan_once(flags=_Flags())
    assert not (spool / "thing.meta.json").exists(), \
        "an item failing longer than GIVE_UP_AFTER_S must be set aside"
    assert (spool / "dead" / "thing.meta.json").exists()


def test_the_failure_clock_survives_a_restart(tmp_path, monkeypatch):
    # THE REASON IT IS ON DISK. An in-memory counter resets whenever ingestd
    # restarts, which is indistinguishable from having no ceiling at all.
    spool = _spool(tmp_path, monkeypatch)
    monkeypatch.setattr(ingestd, "GIVE_UP_AFTER_S", 1e9)  # never give up here
    monkeypatch.setattr(ingestd, "BACKOFF_BASE_S", 0)     # and never wait
    monkeypatch.setattr(ingestd, "BACKOFF_MAX_S", 0)

    def flaky(payload, meta, dest):
        raise OSError("flaky")

    monkeypatch.setattr(ingestd, "PIPELINES", {"las": flaky})
    ingestd.scan_once(flags=_Flags())
    first = json.loads((spool / "thing.meta.json").read_text())["_first_failed_at"]

    # a "restart" is just another pass with no memory carried over
    ingestd.scan_once(flags=_Flags())
    again = json.loads((spool / "thing.meta.json").read_text())
    assert again["_first_failed_at"] == first, \
        "the clock must start at the FIRST failure, not be reset on each pass"
    assert again["_attempts"] == 2, "attempts should still be counted, for the log"
