# How the daemons answer three different things flipr can say, and how ingestd
# notices it is stuck.
#
# Issue 71: when flipr answered 404 to GetNamespace (a wiped or replayed-empty
# store), the client raised FlagMissing, the three poll daemons caught it in
# _permitted, returned False, and reported ok: true with "chain is off". The
# clouds, flights and weather layers vanished behind a green health. ingestd
# said "flipr unreachable", which was false. Worse than filed: _permitted also
# swallowed FliprDown, so a DEAD flipr read as "off" with health ok too.
#
# Three answers, three postures: declared false is an operator choice and the
# daemon is healthy and paused; FliprDown is the plane gone; FlagMissing is
# flipr fine and the namespace unpublished. Each is named. None is "off".
#
# Issue 69, the health half: ingestd ran 54 hours at eleven failures a second
# with ingested_total at zero and reported ok, because the two things it
# checked were true. Now a third thing is checked: work waiting, failing right
# now, and nothing succeeded for a long time.

import http.server
import json
import threading
import time
import urllib.request

import pytest

import flipr_client
import gibsd
import ingestd
import openskyd
import weatherd

DAEMONS = [gibsd, openskyd, weatherd]


class Flags:
    """A flag plane with one of three postures."""
    def __init__(self, mode):
        self.mode = mode

    def check(self, key):
        if self.mode == "down":
            raise flipr_client.FliprDown("flipr unreachable at http://flipr.test")
        if self.mode == "missing":
            raise flipr_client.FlagMissing(f"flag {key!r} is not declared in kingfisher@v1")
        return self.mode == "on"


def _health(mod):
    """The daemon's own /health, over a real socket, as a probe would see it.

    The body deliberately omits flipr_ok (the daemons strip it); the posture
    is asserted from STATE, the body from what a probe can actually read."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/health")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    finally:
        srv.shutdown()
        srv.server_close()


def _reset(mod, monkeypatch, tmp_path):
    with mod.STATE_LOCK:
        mod.STATE["flipr_ok"] = True
        mod.STATE["flag_missing"] = ""
        mod.STATE["paused_flag"] = 0
        mod.STATE["last_error"] = ""
    if hasattr(mod, "OUT"):
        monkeypatch.setattr(mod, "OUT", str(tmp_path / "out"))


# ---------------------------------------------------------------------------
# the three poll daemons


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_a_declared_false_is_paused_and_healthy(mod, monkeypatch, tmp_path):
    """An operator turned it off. That is not degradation."""
    _reset(mod, monkeypatch, tmp_path)
    mod.poll_once(flags=Flags("off"))
    status, body = _health(mod)
    assert status == 200 and body["ok"] is True
    assert body["paused_flag"] >= 1
    assert body["degraded"] == []


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_a_missing_flag_is_degraded_with_its_own_reason(mod, monkeypatch, tmp_path):
    """flipr is fine and the namespace is not there. Saying "off" hid a wiped
    store behind a green health; saying "unreachable" sent someone to look at a
    flipr that was answering."""
    _reset(mod, monkeypatch, tmp_path)
    mod.poll_once(flags=Flags("missing"))
    status, body = _health(mod)
    assert status == 503 and body["ok"] is False
    assert mod.STATE["flipr_ok"] is True, "flipr answered; it is not unreachable"
    assert any("not declared" in d for d in body["degraded"]), body["degraded"]
    assert not any("unreachable" in d for d in body["degraded"]), body["degraded"]


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_a_dead_flipr_is_unreachable_not_off(mod, monkeypatch, tmp_path):
    """_permitted used to swallow FliprDown and return False, so the caller's
    handler for it could never fire and a dead flipr read as "chain is off"
    with health ok."""
    _reset(mod, monkeypatch, tmp_path)
    mod.poll_once(flags=Flags("down"))
    status, body = _health(mod)
    assert status == 503 and body["ok"] is False
    assert mod.STATE["flipr_ok"] is False
    assert "flipr unreachable" in body["degraded"]


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_a_missing_flag_clears_when_it_is_declared_again(mod, monkeypatch, tmp_path):
    # the reason is state, not a counter: it goes away when the cause does
    _reset(mod, monkeypatch, tmp_path)
    mod.poll_once(flags=Flags("missing"))
    assert mod.STATE["flag_missing"]
    mod.poll_once(flags=Flags("off"))
    assert mod.STATE["flag_missing"] == ""
    assert _health(mod)[1]["ok"] is True


# ---------------------------------------------------------------------------
# ingestd: the same three postures


def _ingestd_reset(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "x.meta.json").write_text(json.dumps({"id": "x", "kind": "LAYER_KIND_ASSET"}))
    monkeypatch.setattr(ingestd, "SPOOL", str(spool))
    monkeypatch.setattr(ingestd, "OUT", str(tmp_path / "out"))
    with ingestd.STATE_LOCK:
        ingestd.STATE.update({"flipr_ok": True, "flag_missing": "", "paused_flag": 0,
                              "last_error": "", "spool_depth": 0,
                              "last_ingested_at": 0.0, "last_error_at": 0.0})


def test_ingestd_names_a_missing_flag_instead_of_blaming_flipr(monkeypatch, tmp_path):
    _ingestd_reset(monkeypatch, tmp_path)
    ingestd.scan_once(flags=Flags("missing"))
    status, body = _health(ingestd)
    assert status == 503
    assert ingestd.STATE["flipr_ok"] is True
    assert any("not declared" in d for d in body["degraded"]), body["degraded"]
    assert "flipr unreachable" not in body["degraded"]


def test_ingestd_still_says_unreachable_when_flipr_is_down(monkeypatch, tmp_path):
    _ingestd_reset(monkeypatch, tmp_path)
    ingestd.scan_once(flags=Flags("down"))
    _, body = _health(ingestd)
    assert ingestd.STATE["flipr_ok"] is False
    assert "flipr unreachable" in body["degraded"]


# ---------------------------------------------------------------------------
# ingestd: stuck (issue 69, the health half)


def _stuck_state(monkeypatch, depth, error_age, progress_age):
    now = time.time()
    monkeypatch.setattr(ingestd, "STARTED", now - 10_000)
    with ingestd.STATE_LOCK:
        ingestd.STATE.update({
            "flipr_ok": True, "flag_missing": "", "last_error": "",
            "spool_depth": depth,
            "last_error_at": now - error_age,
            "last_ingested_at": (now - progress_age) if progress_age is not None else 0.0,
        })


def test_work_waiting_and_failing_with_no_progress_is_degraded(monkeypatch):
    """THE LOOP. 120 waiting, an error a moment ago, nothing ingested since
    start. Each clause alone is innocent; all three is 54 hours of green."""
    _stuck_state(monkeypatch, depth=120, error_age=2, progress_age=None)
    status, body = _health(ingestd)
    assert status == 503 and body["ok"] is False
    assert any("nothing ingested" in d for d in body["degraded"]), body["degraded"]


def test_an_idle_spool_is_not_stuck(monkeypatch):
    _stuck_state(monkeypatch, depth=0, error_age=2, progress_age=None)
    assert _health(ingestd)[1]["ok"] is True


def test_recent_progress_is_not_stuck(monkeypatch):
    # a busy queue draining well throws errors too; the difference is that
    # things are still landing on the shelf
    _stuck_state(monkeypatch, depth=120, error_age=2, progress_age=30)
    assert _health(ingestd)[1]["ok"] is True


def test_an_old_error_is_not_stuck(monkeypatch):
    # a transient from an hour ago with work still queued is a slow download
    # in backoff, not a loop
    _stuck_state(monkeypatch, depth=3, error_age=3600, progress_age=None)
    assert _health(ingestd)[1]["ok"] is True


def test_the_stuck_reason_carries_the_numbers(monkeypatch):
    _stuck_state(monkeypatch, depth=120, error_age=1, progress_age=None)
    reason = [d for d in _health(ingestd)[1]["degraded"] if "nothing ingested" in d][0]
    assert reason.startswith("120 waiting")
    assert "for " in reason and reason.rstrip().endswith("s")


# ---------------------------------------------------------------------------
# each fetch daemon reads THREE flags: the network switch, the fetch master,
# and its own source. Flags above answers every key alike, so a daemon that
# read only network.enabled passed the tests above; openskyd did exactly that
# until 2026-09-05 and polled OpenSky in a throwaway whose fetch.opensky was
# false. These pin each daemon to its own source flag, by key.

SOURCE_FLAG = {"gibsd": "fetch.nasa_gibs", "openskyd": "fetch.opensky", "weatherd": "fetch.weather"}


class KeyedFlags:
    """A flag plane that answers by key; an unlisted key is a missing flag."""
    def __init__(self, **values):
        self.values, self.asked = values, []

    def check(self, key):
        self.asked.append(key)
        if key not in self.values:
            raise flipr_client.FlagMissing(f"flag {key!r} is not declared in kingfisher@v1")
        return self.values[key]


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_each_fetch_daemon_pauses_when_its_own_source_flag_is_off(mod, monkeypatch, tmp_path):
    _reset(mod, monkeypatch, tmp_path)
    src = SOURCE_FLAG[mod.__name__]
    flags = KeyedFlags(**{"network.enabled": True, "fetch.enabled": True, src: False})
    mod.poll_once(flags=flags)
    assert src in flags.asked, f"{mod.__name__} never read {src}"
    status, body = _health(mod)
    assert status == 200 and body["ok"] is True and body["paused_flag"] >= 1
    assert body["degraded"] == []


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_each_fetch_daemon_pauses_when_the_fetch_master_is_off(mod, monkeypatch, tmp_path):
    _reset(mod, monkeypatch, tmp_path)
    src = SOURCE_FLAG[mod.__name__]
    flags = KeyedFlags(**{"network.enabled": True, "fetch.enabled": False, src: True})
    mod.poll_once(flags=flags)
    assert "fetch.enabled" in flags.asked, f"{mod.__name__} never read fetch.enabled"
    _, body = _health(mod)
    assert body["ok"] is True and body["paused_flag"] >= 1


@pytest.mark.parametrize("mod", DAEMONS, ids=lambda m: m.__name__)
def test_each_fetch_daemon_reads_exactly_its_three_flags(mod, monkeypatch, tmp_path):
    _reset(mod, monkeypatch, tmp_path)
    src = SOURCE_FLAG[mod.__name__]
    # everything off at the network switch: the read stops there, nothing fetched
    flags = KeyedFlags(**{"network.enabled": False, "fetch.enabled": True, src: True})
    mod.poll_once(flags=flags)
    assert flags.asked[0] == "network.enabled"
    assert set(flags.asked) <= {"network.enabled", "fetch.enabled", src}
