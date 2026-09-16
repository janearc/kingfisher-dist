#!/usr/bin/env python3
# The three outward-facing daemons actually go through net.py.
#
# WHY THIS FILE EXISTS SEPARATELY. gibsd, weatherd and openskyd all have test
# files already, and all three stub the FETCH FUNCTION itself -- test_gibsd.py
# replaces fetch_tile with a lambda -- because those suites are about the poll
# loop, the flag gates and the snapshot, not about HTTP. That is the right
# shape for those tests and it means the conversion to net.py on 2026-09-01 was
# invisible to every one of them: the suite stayed green while the converted
# code was never once executed.
#
# So these tests stub one layer LOWER, at net._urlopen, and assert the property
# the conversion was for. on these specific endpoints: "yes those also
# should be wrapped because if we make them mad we will get banned!" -- the
# thing worth pinning is not that the call works, it is that a flaky provider
# gets backoff and a provider saying no does not get asked again.

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gibsd  # noqa: E402
import net  # noqa: E402
import openskyd  # noqa: E402
import weatherd  # noqa: E402


class _Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self._b = body
        self.headers = headers or {}

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Err(urllib.error.HTTPError):
    def __init__(self, code, body=b"no", headers=None):
        super().__init__("http://p.test/a", code, "err", headers or {}, None)
        self._b = body

    def read(self):
        return self._b


# no real sleeping in any of these
@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda s: None)


def test_gibs_tile_rides_out_a_transient_500(monkeypatch):
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise _Err(503)
        return _Resp(b"\x89PNG-bytes")

    monkeypatch.setattr(net, "_urlopen", flaky)
    assert gibsd.fetch_tile(3, 4) == b"\x89PNG-bytes"
    assert len(calls) == 3, "a CDN wobble must not lose the tile"


def test_gibs_does_not_retry_a_404(monkeypatch):
    # A tile that does not exist will not exist on the fourth ask either, and
    # asking is what a rate limiter counts.
    calls = []

    def gone(req, timeout=None):
        calls.append(1)
        raise _Err(404, b"no such tile")

    monkeypatch.setattr(net, "_urlopen", gone)
    with pytest.raises(net.RetriesExhausted):
        gibsd.fetch_tile(3, 4)
    assert len(calls) == 1


def test_weather_honours_retry_after_rather_than_its_own_jitter(monkeypatch):
    # open-meteo is a free tier. When it says wait, arguing is how a free tier
    # stops being available.
    import email.message
    waited = []
    monkeypatch.setattr(net.time, "sleep", waited.append)  # works now; see net.py
    hdr = email.message.Message()
    hdr["Retry-After"] = "45"

    def limited(req, timeout=None):
        raise _Err(429, b"slow down", headers=hdr)

    monkeypatch.setattr(net, "_urlopen", limited)
    with pytest.raises(net.RetriesExhausted):
        weatherd._get_json("http://api.open-meteo.test/v1/forecast")
    assert waited and max(waited) >= 45.0, \
        "the server's own number must outrank our backoff"


def test_weather_parses_through_the_wrapper(monkeypatch):
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=None: _Resp(b'{"current":{"x":1}}'))
    assert weatherd._get_json("http://api.open-meteo.test/v1/forecast") \
        == {"current": {"x": 1}}


def test_opensky_does_not_spend_credits_on_a_refusal(monkeypatch):
    # OpenSky meters anonymous callers by daily credits: every attempt spends
    # budget that does not come back until tomorrow. A 403 is a decision, and
    # asking three more times converts a decision into a pattern.
    calls = []

    def refused(req, timeout=None):
        calls.append(1)
        raise _Err(403, b"forbidden")

    monkeypatch.setattr(net, "_urlopen", refused)
    with pytest.raises(net.RetriesExhausted):
        openskyd.fetch_states()
    assert len(calls) == 1


def test_opensky_still_rides_out_a_blip(monkeypatch):
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 2:
            raise OSError("connection reset")
        return _Resp(b'{"states":[]}')

    monkeypatch.setattr(net, "_urlopen", flaky)
    assert openskyd.fetch_states() == {"states": []}
    assert len(calls) == 2


def test_the_credentials_never_reach_argv(monkeypatch):
    # lights leaked a Hue bridge key into ps and into its own logs for eleven
    # days by passing it as a curl argument. openskyd builds an Authorization
    # header instead; this pins that it stays a header.
    monkeypatch.setattr(openskyd, "USER", "u")
    monkeypatch.setattr(openskyd, "PASS", "p")
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = dict(req.header_items())
        seen["url"] = req.full_url
        return _Resp(b'{"states":[]}')

    monkeypatch.setattr(net, "_urlopen", capture)
    openskyd.fetch_states()
    assert any(k.lower() == "authorization" for k in seen["headers"])
    assert "u:p" not in seen["url"], "credentials must never ride in the URL"
