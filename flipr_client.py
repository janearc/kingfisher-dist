# flipr_client -- the reference python client. Stdlib only, and now TWO files.
#
# stdlib only ON PURPOSE: a client a service cannot vendor is a client it will
# not use. The original reason given here was that kingfisher is "single-file
# stdlib python with no dependency layer", and that premise is gone -- the operator
# retired it on 2026-08-28 ("stdlib-only is not a thing") and kingfisher now
# takes brotli, kafka-python and h3. The PORTABILITY is still worth keeping
# though, because this file is the reference other services copy.
#
# So as of 2026-09-01 vendoring this means taking net.py alongside it. That is
# the deliberate cost of the backoff: every call to flipr now goes through the
# shared retry policy rather than a bare urlopen, and a flag store that the
# whole mesh reads through on every request is the last place that should be
# hand-rolling its own retry -- or worse, having none. net.py is stdlib and
# logs through a try/except import, so it vendors as cleanly as this does.
#
# THE SEMANTICS ARE RULED AND THIS FILE IMPLEMENTS THEM EXACTLY.
# "clients should cache the last read, but they should
# always check that flipr is there."
#
#   - the cache is a PERFORMANCE optimisation, never a fallback
#   - every check() verifies flipr is reachable before trusting the cache
#   - flipr unreachable means FliprDown, raised, every time. a client that
#     quietly serves a stale flag when flipr is gone can sit for hours acting
#     on a value somebody turned off during an incident, and nothing anywhere
#     reports a problem. an outage is visible; a lie is not.
#
# usage, for the common gate-an-expensive-thing shape:
#
#     flags = FliprClient("http://flipr.test:9800", "kingfisher", COMMIT)
#     if flags.check("fetch.enabled"):
#         do_the_expensive_fetch()
#
# check() raises FliprDown if flipr cannot be reached. do not catch it and
# carry on -- failing is the designed behaviour, and the network being down
# when flipr is down is the point of flipr.

import net
import json
import time
import urllib.request
import urllib.error


class FliprDown(Exception):
    # flipr is unreachable. the caller STOPS. this is not a transient to
    # retry around: if flipr is down the network is down, by design.
    pass


class FlagMissing(Exception):
    # the flag was never declared. distinct from False on purpose: an absent
    # flag is a wiring mistake, and reading it as "off" would hide that
    # mistake until someone wondered why a feature never ran.
    pass


class FliprClient:
    # one client = one service at one version, matching flipr's namespace
    # scheme. version is the commit hash at deploy.

    def __init__(self, base, service, version, cache_ttl=5.0, timeout=2.0):
        # base like "http://flipr.test:9800" -- a NAME, never an ip:port.
        # which flipr answers that name is the network's decision, and that
        # is the whole environment story: dev resolves flipr.test, prod
        # resolves prod's name, and this code never knows which it is.
        self._base = base.rstrip("/")
        self._service = service
        self._version = version
        self._ttl = cache_ttl
        self._timeout = timeout
        self._cache = {}
        self._fetched = 0.0

    def _post(self, method, body):
        # one rpc, protojson over http. errors collapse to FliprDown except
        # a 404, which the callers that care about distinguish themselves.
        req = urllib.request.Request(
            f"{self._base}/flipr.v1.FliprService/{method}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # INTERACTIVE, not the default. Every flag check reads through -- a
        # flag is never cached as a fallback -- so this is on the hot path of
        # everything. The 4-attempt/120s default would turn a flipr outage from
        # a fast refusal into a two-minute stall per call, and a daemon full of
        # calls waiting on a dependency already known to be down is worse than
        # one that refused: it stops doing the work that needs no flag.
        #
        # Two attempts rides out a pod mid-restart, which is the case where
        # refusing was always wrong. It does not try to ride out an outage,
        # because the ruling says an outage IS a refusal and this client does
        # not get to soften that.
        #
        # retry_non_idempotent because flipr speaks connect-protocol: every RPC
        # is a POST, and Ping and GetNamespace only read.
        try:
            _status, _headers, raw = net.request(
                f"{self._base}/flipr.v1.FliprService/{method}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
                retry_non_idempotent=True,
                policy=net.INTERACTIVE.replace(timeout_s=self._timeout))
            return json.loads(raw)
        except net.RetriesExhausted as e:
            # net reports one failure shape; the flipr contract needs two. net
            # never retries a 404, so an undeclared flag still costs exactly one
            # round trip and cannot become a poison queue.
            cause = e.cause
            if isinstance(cause, net.HttpStatusError):
                if cause.status == 404:
                    raise FlagMissing(f"{method}: {cause.body}") from None
                raise FliprDown(f"flipr answered {cause.status} to {method}") from None
            raise FliprDown(f"flipr unreachable at {self._base}: {cause}") from None

    def ping(self):
        # the liveness check. cheap by contract: flipr's Ping never touches
        # its store. raises FliprDown if flipr is not there.
        return self._post("Ping", {})["version"]

    def refresh(self):
        # fetch the whole namespace in one round trip and replace the cache.
        resp = self._post(
            "GetNamespace",
            {"service": self._service, "version": self._version},
        )
        flags = {}
        for f in resp.get("namespace", {}).get("flags", []):
            # a PRESENT null is not a missing key. flipr accepts SetFlag with no
            # value and serves "value": null (issue 54); .get("value", {}) hands
            # back None and the `in` below raises TypeError -- which is neither
            # FliprDown nor FlagMissing, so every daemon blamed flipr for a
            # flag an operator half-set. `or {}` makes it a flag with no kind,
            # which the else branch already maps to None on purpose.
            v = f.get("value") or {}
            # the oneof arrives as exactly one of these keys
            if "boolValue" in v:
                flags[f["key"]] = v["boolValue"]
            elif "stringValue" in v:
                flags[f["key"]] = v["stringValue"]
            elif "intValue" in v:
                flags[f["key"]] = int(v["intValue"])
            else:
                flags[f["key"]] = None
        self._cache = flags
        self._fetched = time.monotonic()
        return flags

    def check(self, key):
        # the call sites use this. semantics, in order:
        #   1. flipr must be THERE -- a fresh cache does not skip the
        #      liveness check, per the ruling
        #   2. a stale cache is refreshed; a fresh one is served
        #   3. an undeclared flag raises rather than defaulting
        age = time.monotonic() - self._fetched
        if age >= self._ttl:
            self.refresh()
        else:
            self.ping()
        if key not in self._cache:
            raise FlagMissing(
                f"flag {key!r} is not declared in {self._service}@{self._version}; "
                "publish it before gating on it"
            )
        return self._cache[key]

    def publish(self, flags):
        # build-time onboarding: declare this service's flags. idempotent,
        # and it never overwrites an operator's value -- flipr guarantees
        # that server-side. `flags` is a list of dicts shaped like the
        # protojson Flag message.
        return self._post(
            "PublishNamespace",
            {"namespace": {
                "service": self._service,
                "version": self._version,
                "flags": flags,
            }},
        )["flagsPublished"]
