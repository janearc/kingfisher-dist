#!/usr/bin/env python3
# e2e against the live deployment -- the charter's test: service A
# talks to service B and product X comes out. Everything here crosses a
# real service boundary; nothing is stubbed. Run it with the cluster up:
#
#     uv run python tests/e2e_live.py
#
# Non-destructive except ONE deliberate mutation: the flag-authority check
# flips routing.valhalla off and back (reason recorded in flipr's oplog),
# restoring in a finally even when the assertion fails. Exit 0 all-pass.

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gen"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from google.protobuf import json_format  # noqa: E402
from kingfisher.discovery.v1 import discovery_pb2  # noqa: E402
from kingfisher.routing.v1 import routing_pb2  # noqa: E402

KF = os.environ.get("E2E_KINGFISHER", "http://kingfisher.test:9800")
FLIPR = os.environ.get("E2E_FLIPR", "http://flipr.test:9800")
HM = os.environ.get("E2E_HM", "http://hm.test:9800")
SF = {"lat": 37.7749, "lng": -122.4194}
OAK = {"lat": 37.8044, "lng": -122.2712}

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def http(url, body=None, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def rpc(base, svc, name, body, msg_type):
    raw = http(f"{base}/kingfisher.{svc}.v1.{svc.capitalize()}Service/{name}", body)
    return json_format.Parse(raw, msg_type())  # strict: proto conformance


def main():
    print(f"e2e against {KF}")

    # 1. the contract on the wire is the contract in the repo, byte for byte
    served = http(f"{KF}/api")
    committed = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "descriptor.binpb"), "rb").read()
    check("/api == committed descriptor", served == committed,
          f"{len(served)} bytes")

    # 2. tiles: real bytes off the seeded mount
    tile = http(f"{KF}/econ/res8.json")
    check("econ res8 manifest serves", len(tile) > 50000, f"{len(tile)} bytes")

    # 3. discovery conforms to the proto, strictly parsed
    src = rpc(KF, "discovery", "ListSources", {}, discovery_pb2.ListSourcesResponse)
    check("discovery: sources conform, national programs present",
          len(src.sources) >= 59 and sum(1 for x in src.sources if x.country) >= 55,
          f"{len(src.sources)} sources")

    # 4. kingfisher -> valhalla: the top rung answers, and says so
    r = rpc(KF, "routing", "Route",
            {"mode": "MODE_AUTO", "origin": SF, "destination": OAK},
            routing_pb2.RouteResponse)
    check("route via LIVE valhalla, labelled",
          r.method == routing_pb2.METHOD_VALHALLA,
          f"method={routing_pb2.Method.Name(r.method)} meters={r.meters:.0f}")
    road = r.meters

    # 5. kingfisher -> flipr: the flag AUTHORITY is real. Flip off, expect
    # the labelled floor; restore in finally, verify restoration.
    def flip(on):
        http(f"{FLIPR}/flipr.v1.FliprService/SetFlag",
             {"service": "kingfisher", "version": "v1",
              "key": "routing.valhalla", "value": {"boolValue": on},
              "reason": "e2e_live: proving the flag controls the rung; restored by the same run"})
    try:
        flip(False)
        import time
        time.sleep(6)  # the client's 5s cache TTL
        r2 = rpc(KF, "routing", "Route",
                 {"mode": "MODE_AUTO", "origin": SF, "destination": OAK},
                 routing_pb2.RouteResponse)
        check("flag OFF floors the same route, labelled",
              r2.method == routing_pb2.METHOD_HAVERSINE and r2.meters < road,
              f"method={routing_pb2.Method.Name(r2.method)} meters={r2.meters:.0f}")
    finally:
        flip(True)
    import time
    time.sleep(6)
    r3 = rpc(KF, "routing", "Route",
             {"mode": "MODE_AUTO", "origin": SF, "destination": OAK},
             routing_pb2.RouteResponse)
    check("flag restored, rung back",
          r3.method == routing_pb2.METHOD_VALHALLA)

    # 6. kingfisher -> kafka -> hm: the heartbeat IS the lease, and the
    # authority lists us. Product X: a row in someone else's truth table.
    truth = json.loads(http(f"{HM}/truth"))
    row = next((a for a in truth.get("authorized", [])
                if a["service"] == "kingfisher"), None)
    check("hm lists kingfisher AUTHORIZED",
          row is not None and row["state"] == "authorized",
          f"last_heartbeat={row['last_heartbeat'][:19] if row else 'ABSENT'}")

    # 7. the metrics pipeline: our own beat counter is nonzero and scraped
    metrics = http(f"{KF}/metrics").decode()
    beat_ok = [l for l in metrics.splitlines()
               if l.startswith('kingfisher_heartbeat_total{outcome="ok"}')]
    check("heartbeat counter live in /metrics",
          bool(beat_ok) and float(beat_ok[0].rsplit(" ", 1)[1]) > 0,
          beat_ok[0] if beat_ok else "series absent")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
