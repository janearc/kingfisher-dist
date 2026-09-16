#!/usr/bin/env python3
# The retry primitive, pinned by behaviour rather than by shape.
#
# WHY THESE TESTS ARE THE POINT. net.py exists because policy written in prose
# is advisory -- "i can add stuff to claude.md but that only
# matters some of the time and usually at the wrong time. so we should probably
# try to do this in a library everyone owns." A library only enforces anything
# if its behaviour is nailed down; otherwise the next edit quietly turns full
# jitter into fixed backoff and nothing notices until the herd arrives.
#
# Every test here is about a property somebody depends on, not an internal:
# that retries decorrelate, that a permanent failure is never retried, that a
# POST is not repeated, that Retry-After outranks our own schedule. A rewrite
# that keeps those passes.
#
# NOTHING HERE TOUCHES THE NETWORK. The suite drives net._urlopen, the same
# injectable door providers.py and routing.py already use.

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net  # noqa: E402


# a urlopen result: context manager, status, headers, body
class _Resp:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b"nope", headers=None):
    return urllib.error.HTTPError("http://x.test/a", code, "err", headers or {},
                                  None)


# HTTPError.read() needs a payload; the real one reads from the socket
class _BodiedHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body=b"nope", headers=None):
        super().__init__("http://x.test/a", code, "err", headers or {}, None)
        self._b = body

    def read(self):
        return self._b


# a policy with no real waiting in it, for tests about control flow
FAST = net.RetryPolicy(attempts=4, base_s=0.01, max_s=0.04,
                       timeout_s=1, total_budget_s=100)


# records the sleeps that WOULD have been taken, and runs a fake clock
class Clock:
    def __init__(self):
        self.slept = []
        self.t = 0.0

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def now(self):
        return self.t


# ------------------------------------------------------------------ backoff

def test_backoff_is_full_jitter_so_the_floor_is_zero():
    # The whole reason for full jitter over equal jitter: no shared floor. If
    # rand() returns 0 the sleep is 0, at every attempt. Equal jitter keeps half
    # the window fixed, and that fixed half is what every caller has in common
    # -- which is how a herd reassembles one beat later. gibsd, openskyd and
    # weatherd all wake on their own timers and would otherwise line up the
    # moment one provider has a bad minute.
    for a in range(6):
        assert net.backoff_s(a, net.DEFAULT, lambda: 0.0) == 0.0


def test_backoff_doubles_then_clamps():
    p = net.RetryPolicy(base_s=1.0, max_s=8.0)
    ceil = lambda a: net.backoff_s(a, p, lambda: 1.0)  # noqa: E731
    assert ceil(0) == 1.0
    assert ceil(1) == 2.0
    assert ceil(2) == 4.0
    assert ceil(3) == 8.0
    assert ceil(4) == 8.0     # clamped -- 2^n must not run away
    assert ceil(9) == 8.0


# ----------------------------------------------------------------- retrying

def test_a_success_costs_no_sleep():
    c = Clock()
    out = net.retrying("t", lambda n: "ok", policy=FAST, sleep=c.sleep, now=c.now)
    assert out == "ok"
    assert c.slept == []


def test_a_transient_failure_is_retried_then_succeeds():
    c = Clock()
    calls = []

    def fn(n):
        calls.append(n)
        if len(calls) < 3:
            raise OSError("connection reset")
        return "ok"

    assert net.retrying("t", fn, policy=FAST, sleep=c.sleep, now=c.now) == "ok"
    assert len(calls) == 3
    assert len(c.slept) == 2, "one backoff before each retry, none after success"


def test_a_permanent_failure_is_not_retried_even_once():
    # The poison-queue guard. kingfisher retried 119 undecodable files 4,463
    # times each in a day because nothing could say "this will never work".
    c = Clock()
    calls = []

    def fn(n):
        calls.append(n)
        raise net.HttpStatusError(404, "http://x.test/a")

    with pytest.raises(net.RetriesExhausted):
        net.retrying("t", fn, policy=FAST, sleep=c.sleep, now=c.now)
    assert len(calls) == 1
    assert c.slept == []


def test_exhaustion_carries_the_last_cause():
    c = Clock()
    boom = OSError("still down")
    with pytest.raises(net.RetriesExhausted) as e:
        net.retrying("label-here", lambda n: (_ for _ in ()).throw(boom),
                     policy=FAST, sleep=c.sleep, now=c.now)
    assert e.value.cause is boom, "a caller must still be able to branch on WHY"
    assert e.value.attempts == FAST.attempts
    assert "label-here" in str(e.value)


def test_the_total_budget_cuts_the_attempts_short():
    c = Clock()
    p = net.RetryPolicy(attempts=9, base_s=1.0, max_s=1.0,
                        timeout_s=1, total_budget_s=2.5)
    calls = []

    def fn(n):
        calls.append(n)
        c.t += 0.8            # each attempt burns wall time before failing
        raise OSError("slow and down")

    with pytest.raises(net.RetriesExhausted):
        net.retrying("t", fn, policy=p, rand=lambda: 1.0, sleep=c.sleep, now=c.now)
    assert len(calls) < p.attempts
    assert sum(c.slept) <= p.total_budget_s


def test_retry_after_outranks_our_own_jitter():
    # A server that troubles itself to say how long to wait is the one we least
    # want to ignore. on the providers: "if we make them mad we will get
    # banned!" -- ignoring Retry-After on a 429 is how that happens.
    c = Clock()
    err = net.HttpStatusError(429, "http://usgs.test/a", retry_after_s=30.0)
    calls = []

    def fn(n):
        calls.append(n)
        raise err

    with pytest.raises(net.RetriesExhausted):
        net.retrying("t", fn, policy=net.RetryPolicy(attempts=2, base_s=0.01,
                     max_s=0.01, timeout_s=1, total_budget_s=600),
                     rand=lambda: 1.0, sleep=c.sleep, now=c.now)
    assert c.slept == [30.0], "our 10ms jitter must not override the server's 30s"


# -------------------------------------------------------------- retryability

def test_only_not_now_statuses_are_retried():
    for s in (500, 502, 503, 504, 429, 408):
        assert net.http_retryable(net.HttpStatusError(s, "u")) is True, s
    for s in (400, 401, 403, 404, 409, 410, 422):
        assert net.http_retryable(net.HttpStatusError(s, "u")) is False, s
    assert net.http_retryable(OSError("ECONNREFUSED")) is True


def test_retry_after_parses_both_forms_and_never_goes_negative():
    assert net.retry_after_s("120") == 120.0
    assert net.retry_after_s("0") == 0.0
    assert net.retry_after_s("-30") == 0.0
    assert net.retry_after_s(None) is None
    assert net.retry_after_s("") is None
    assert net.retry_after_s("soon") is None
    # an HTTP-date already in the past means "you may go now"
    past = net.retry_after_s("Tue, 01 Sep 2026 09:59:00 GMT",
                             now=__import__("calendar").timegm(
                                 (2026, 9, 1, 10, 0, 0, 0, 0, 0)))
    assert past == 0.0


def test_host_of_keeps_label_cardinality_bounded():
    # Paths carry dataset ids. A host label is a handful of values; a URL label
    # is unbounded and prometheus holds every one of them in memory.
    assert net.host_of("https://tnmaccess.nationalmap.gov/api/v1/products?q=1") \
        == "tnmaccess.nationalmap.gov"
    assert net.host_of("not a url") == "unparseable"


# -------------------------------------------------------------- idempotency

def test_may_retry_only_where_repetition_is_free():
    for m in ("GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE", "get", "Put"):
        assert net.may_retry(m) is True, m
    for m in ("POST", "PATCH", "post"):
        assert net.may_retry(m) is False, m
    assert net.may_retry(None) is True, "no method means GET"
    assert net.may_retry("POST", opt_in=True) is True


def test_a_post_is_attempted_exactly_once(monkeypatch):
    # THE HAZARD, and for kingfisher it is the expensive one. RefreshIndex and
    # Fetch are how this service is told to pull from somebody else's servers.
    # A retried Fetch is a second download of the same dataset from a host that
    # is already unhappy, and a timeout is the worst case because the request
    # very often arrived and only the answer was lost.
    calls = []

    def boom(req, timeout=None):
        calls.append(1)
        raise OSError("connection reset")

    monkeypatch.setattr(net, "_urlopen", boom)
    c = Clock()
    with pytest.raises(net.RetriesExhausted):
        net.request("http://usgs.test/fetch", data=b"{}", method="POST",
                    policy=FAST, sleep=c.sleep, now=c.now)
    assert len(calls) == 1
    assert c.slept == []


def test_a_get_with_the_same_policy_still_retries(monkeypatch):
    # The control. If the guard collapsed everything the library would be inert.
    calls = []

    def boom(req, timeout=None):
        calls.append(1)
        raise OSError("connection reset")

    monkeypatch.setattr(net, "_urlopen", boom)
    c = Clock()
    with pytest.raises(net.RetriesExhausted):
        net.request("http://usgs.test/a", policy=FAST, sleep=c.sleep, now=c.now)
    assert len(calls) == FAST.attempts


def test_an_rpc_over_post_can_opt_back_in(monkeypatch):
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 2:
            raise OSError("reset")
        return _Resp(200, b'{"ok":true}')

    monkeypatch.setattr(net, "_urlopen", flaky)
    c = Clock()
    status, _h, body = net.request("http://flipr.test/Ping", data=b"{}",
                                   method="POST", retry_non_idempotent=True,
                                   policy=FAST, sleep=c.sleep, now=c.now)
    assert status == 200 and body == b'{"ok":true}'
    assert len(calls) == 2


# ------------------------------------------------------------------ request

def test_a_404_is_not_retried_and_carries_what_the_server_said(monkeypatch):
    calls = []

    def gone(req, timeout=None):
        calls.append(1)
        raise _BodiedHTTPError(404, b"dataset usgs:foo is not indexed")

    monkeypatch.setattr(net, "_urlopen", gone)
    c = Clock()
    with pytest.raises(net.RetriesExhausted) as e:
        net.request("http://usgs.test/a", policy=FAST, sleep=c.sleep, now=c.now)
    assert len(calls) == 1, "retrying a 404 is how a poison queue starts"
    cause = e.value.cause
    assert isinstance(cause, net.HttpStatusError) and cause.status == 404
    # "answered 404" sends the reader to the wrong place; the body ends the
    # investigation. urllib gives it up exactly once, so it is read there.
    assert "not indexed" in cause.body


def test_an_error_body_is_truncated(monkeypatch):
    def big(req, timeout=None):
        raise _BodiedHTTPError(500, b"x" * 5000)

    monkeypatch.setattr(net, "_urlopen", big)
    c = Clock()
    with pytest.raises(net.RetriesExhausted) as e:
        net.request("http://x.test/a", policy=net.RetryPolicy(
            attempts=1, timeout_s=1, total_budget_s=1), sleep=c.sleep, now=c.now)
    assert len(e.value.cause.body) == net.BODY_SNIPPET


def test_accept_status_lets_a_caller_keep_a_refusals_body(monkeypatch):
    # For the callers that read the status themselves rather than treating it
    # as an exception -- and narrowly, so other failures still fail.
    def degraded(req, timeout=None):
        raise _BodiedHTTPError(503, b'{"ready":false}')

    monkeypatch.setattr(net, "_urlopen", degraded)
    c = Clock()
    status, _h, body = net.request("http://delightd.test/readyz",
                                   accept_status=lambda s: s == 503,
                                   policy=FAST, sleep=c.sleep, now=c.now)
    assert status == 503 and body == b'{"ready":false}'

    def broken(req, timeout=None):
        raise _BodiedHTTPError(500, b"boom")

    monkeypatch.setattr(net, "_urlopen", broken)
    with pytest.raises(net.RetriesExhausted):
        net.request("http://delightd.test/readyz",
                    accept_status=lambda s: s == 503,
                    policy=net.RetryPolicy(attempts=1, timeout_s=1,
                                           total_budget_s=1),
                    sleep=c.sleep, now=c.now)


def test_metrics_move_and_stay_coarse(monkeypatch):
    # A collector that cannot report on itself is the metricsd problem again.
    # Labels stay host+outcome: three values a dashboard can chart, not the
    # open set of error strings.
    def ok(req, timeout=None):
        return _Resp(200, b"hi")

    monkeypatch.setattr(net, "_urlopen", ok)
    before = dict(net.CALLS.get("m.test", {}))
    net.request("http://m.test/a", policy=FAST)
    assert net.CALLS["m.test"]["ok"] == before.get("ok", 0) + 1
    assert net.DURATION["m.test"]["count"] >= 1


def test_a_refusal_and_a_failure_are_counted_differently(monkeypatch):
    # "gave up" and "refused to try" are different operational stories, and a
    # dashboard that conflates them sends the on-call reader looking for
    # capacity problems that do not exist.
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            _BodiedHTTPError(404, b"no")))
    c = Clock()
    with pytest.raises(net.RetriesExhausted):
        net.request("http://r.test/a", policy=FAST, sleep=c.sleep, now=c.now)
    assert net.CALLS["r.test"].get("refused", 0) >= 1

    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            OSError("reset")))
    with pytest.raises(net.RetriesExhausted):
        net.request("http://f.test/a", policy=FAST, sleep=c.sleep, now=c.now)
    assert net.CALLS["f.test"].get("failed", 0) >= 1


def test_get_json_parses_and_inherits_the_retry_behaviour(monkeypatch):
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 2:
            raise OSError("reset")
        return _Resp(200, b'{"n":7}')

    monkeypatch.setattr(net, "_urlopen", flaky)
    c = Clock()
    assert net.get_json("http://x.test/a", policy=FAST,
                        sleep=c.sleep, now=c.now) == {"n": 7}
    assert len(calls) == 2


# ----------------------------------------------------------------- download

def test_download_streams_to_disk_and_reports_progress(monkeypatch, tmp_path):
    class _Stream:
        def __init__(self):
            self.parts = [b"a" * 10, b"b" * 10, b""]
            self.i = 0

        def read(self, _n):
            out = self.parts[self.i]
            self.i += 1
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(net, "_urlopen", lambda req, timeout=None: _Stream())
    seen = []
    dest = tmp_path / "tile.las"
    n = net.download("http://usgs.test/t.las", str(dest), on_progress=seen.append)
    assert n == 20
    assert dest.read_bytes() == b"a" * 10 + b"b" * 10
    assert seen == [10, 20], "progress must be published as it goes, not at the end"


def test_a_retried_download_truncates_rather_than_appending(monkeypatch, tmp_path):
    # THE CORRUPTION THIS PREVENTS. A retry that appends to what the failed
    # attempt left behind produces a file that is the wrong length and the
    # wrong content, and reports success. That is worse than any number of
    # retries, because nothing downstream can tell it happened.
    calls = []

    class _Half:
        def __init__(self, fail):
            self.fail = fail
            self.parts = [b"x" * 8, b""]
            self.i = 0

        def read(self, _n):
            if self.fail and self.i == 1:
                raise OSError("connection dropped at 90%")
            out = self.parts[self.i]
            self.i += 1
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        calls.append(1)
        return _Half(fail=len(calls) == 1)

    monkeypatch.setattr(net, "_urlopen", opener)
    c = Clock()
    dest = tmp_path / "tile.las"
    n = net.download("http://usgs.test/t.las", str(dest),
                     policy=net.RetryPolicy(attempts=2, base_s=0, max_s=0,
                                            timeout_s=1, total_budget_s=100),
                     sleep=c.sleep, now=c.now)
    assert len(calls) == 2
    assert n == 8 and dest.read_bytes() == b"x" * 8, \
        "the second attempt must start the file over, not append to the first"


def test_the_download_ceiling_is_two_attempts():
    # "the government's websites are pretty flaky and a retry is
    # important, but.. there's a limit." A restarted 400MB tile costs the
    # provider the whole transfer again; one restart is worth it, a second is
    # us being the problem.
    assert net.DOWNLOAD.attempts == 2


def test_headers_come_back_case_insensitive(monkeypatch):
    # THE BUG THIS PINS. dict(r.headers) turns urllib's case-insensitive
    # Message into a case-sensitive dict. providers.py reads "Content-Encoding"
    # to decide whether to gunzip, and the CDNs it talks to do not agree on the
    # spelling -- worldview's compresses whether or not you asked. A missed
    # header means gzip bytes handed to json.loads, which fails as a 0x8b in
    # position 1 and looks like a corrupt provider rather than our own bug.
    import email.message

    def gz(req, timeout=None):
        m = email.message.Message()
        m["content-encoding"] = "gzip"     # lowercase, as a real CDN sends it
        return _Resp(200, b"body", headers=m)

    monkeypatch.setattr(net, "_urlopen", gz)
    _s, headers, _b = net.request("http://cdn.test/a", policy=FAST)
    assert headers.get("Content-Encoding") == "gzip", \
        "a caller asking with different capitalisation must still find it"


def test_the_injection_points_resolve_at_call_time(monkeypatch):
    # THE WART THIS PINS. `sleep=time.sleep` in a signature binds the function
    # object once, at def time. A test that patches net.time.sleep afterwards
    # changes nothing, the retry sleeps for real, and the only symptom is a
    # slow suite -- measured as 90 seconds of genuine waiting in a test written
    # to be instant. Resolving inside the call makes patching work.
    slept = []
    monkeypatch.setattr(net.time, "sleep", slept.append)
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("reset")))
    with pytest.raises(net.RetriesExhausted):
        net.request("http://x.test/a",
                    policy=net.RetryPolicy(attempts=3, base_s=5.0, max_s=5.0,
                                           timeout_s=1, total_budget_s=100))
    assert len(slept) == 2, "the module-level patch must have been seen"
