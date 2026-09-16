#!/usr/bin/env python3
# openskyd -- the sky, polled. Same shape as ingestd (periodic daemon, own
# pod, own port, own /health and /metrics): a background loop does not
# belong in the serving pod's request-handling thread, and OpenSky's own
# rate ceiling means the poll cadence is a service-level concern, not a
# per-request one.
#
# WHAT THIS IS, per providers.py's opensky entry: not a catalog-and-fetch-one
# source like usgs or noaa -- one endpoint, one constantly-changing snapshot.
# The product is a per-aircraft (icao24) TRAJECTORY assembled by polling and
# accumulating state vectors, the same shape as a GPS ride track. Kept
# entirely in memory: the state is inherently ephemeral (stale within one
# poll interval regardless), so a restart losing it costs one empty sky for
# one interval, not a durability incident -- unlike the fence store, this
# is not user data.
#
# THE OUTPUT is a snapshot file (tmp+rename, ingest_asset's atomicity),
# mounted read-only into the serving pod exactly like ingestd's OUT dir --
# kingfisher returns positions, never a look; rendering is the pane's job.
#
# THE RATE CEILING, measured against providers.py's own numbers: anonymous
# OpenSky is 400 credits/day, and a bbox this size (Bay Area, ~0.5 sq deg)
# costs 1 credit/call. Default interval is 300s -- 288 calls/day, comfortably
# under the ceiling with headroom for the credit-cost table being coarser
# than a single measurement can prove. Raise OPENSKYD_INTERVAL_S down once
# registered credentials raise the ceiling to 4000/day; this file does not
# guess at that raise for you.

import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net
import version
from log import log  # noqa: E402
from api import descriptor_bytes  # noqa: E402

OUT = os.environ.get("OPENSKYD_OUT", "flights")
PORT = int(os.environ.get("OPENSKYD_PORT", "15023"))
INTERVAL = float(os.environ.get("OPENSKYD_INTERVAL_S", "300"))
STATES_URL = os.environ.get("OPENSKYD_STATES_URL", "https://opensky-network.org/api/states/all")
# the Bay Area, matching the bbox providers.py verified live against
BBOX = {
    "lamin": float(os.environ.get("OPENSKYD_LAMIN", "37.2")),
    "lomin": float(os.environ.get("OPENSKYD_LOMIN", "-122.6")),
    "lamax": float(os.environ.get("OPENSKYD_LAMAX", "38.0")),
    "lomax": float(os.environ.get("OPENSKYD_LOMAX", "-121.7")),
}
# a trace goes cold (evicted from memory) after this many missed polls --
# "let a trace go cold the way a ride ends" (providers.py's own scoping)
COLD_AFTER_POLLS = int(os.environ.get("OPENSKYD_COLD_AFTER_POLLS", "5"))
TRAIL_LEN = int(os.environ.get("OPENSKYD_TRAIL_LEN", "30"))
USER, PASS = os.environ.get("OPENSKY_USER", ""), os.environ.get("OPENSKY_PASS", "")

STATE_LOCK = threading.Lock()
STATE = {"polls": 0, "polls_ok": 0, "aircraft_seen": 0, "paused_flag": 0,
         "errors": 0, "last_error": "", "flipr_ok": True, "flag_missing": "", "last_poll_at": 0.0}

# icao24 -> {callsign, lat, lng, altitude_m, heading, velocity_mps,
#            vertical_rate_mps, on_ground, last_contact, trail: [[lat,lng,ts],...]}
AIRCRAFT = {}
AIRCRAFT_LOCK = threading.Lock()


def _flags():
    import flipr_client
    return flipr_client.FliprClient(
        os.environ.get("KINGFISHER_FLIPR_URL", "http://flipr.test:9800"),
        "kingfisher", "v1")


def _permitted(flags):
    # THREE ANSWERS, NOT ONE. A declared false is an operator choice and the
    # daemon is healthy and paused. FliprDown is the flag plane gone, and
    # the caller's except already records it -- so it PROPAGATES; this used
    # to swallow it and hand back False, which made a dead flipr read as
    # "chain is off" with health ok. FlagMissing is flipr healthy but the
    # namespace unpublished (a wiped or replayed-empty store, issues 67/45):
    # not off, not unreachable, and the layer it gates vanishes with a 200
    # health unless it is named. It is named.
    import flipr_client
    try:
        # the same three answers as gibsd and weatherd: the network switch, the
        # fetch master, and this source. Until 2026-09-05 only the first was read,
        # so fetch.opensky and fetch.enabled were declared switches that did
        # nothing here; a throwaway with fetch.opensky false polled OpenSky.
        allowed = flags.check("network.enabled") and flags.check("fetch.enabled") and flags.check("fetch.opensky")
    except flipr_client.FlagMissing as e:
        with STATE_LOCK:
            STATE["flag_missing"] = str(e)[:200]
        log("warn", "opensky_flag_not_declared", err=str(e)[:200])
        return False
    with STATE_LOCK:
        STATE["flag_missing"] = ""
    return allowed


def fetch_states():
    # one call, one bbox, credentials optional -- anonymous works (measured
    # 2026-08-28, 149 aircraft live), registered just raises the ceiling.
    qs = "&".join(f"{k}={v}" for k, v in BBOX.items())
    url = f"{STATES_URL}?{qs}"
    headers = {"Accept": "application/json"}
    if USER and PASS:
        import base64
        cred = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        # a header, never argv -- lights leaked a bridge key into ps and into
        # its own logs for eleven days doing the other thing
        headers["Authorization"] = f"Basic {cred}"
    # OpenSky meters anonymous callers by daily credits, so a retry is not free
    # here in the way it is against an in-cluster service: every attempt spends
    # budget that does not come back until tomorrow. net's 4-attempt default is
    # the ceiling, Retry-After is honoured, and a 4xx is never retried -- being
    # told "no" and asking three more times is how the "no" becomes permanent.
    return net.get_json(url, headers=headers,
                        policy=net.DEFAULT.replace(timeout_s=20))


def _row(sv):
    # OpenSky's state-vector column order -- MEASURED against the live
    # endpoint 2026-08-28 (not taken from memory of the docs): icao24,
    # callsign, origin_country, time_position, last_contact, lon, lat,
    # baro_altitude, on_ground, velocity, true_track, vertical_rate,
    # sensors, geo_altitude, squawk, spi, position_source[, category]. A
    # field reorder here is silent data corruption, so every index below
    # is a literal, named once, in this one place.
    icao24 = sv[0]
    callsign = (sv[1] or "").strip()
    lon, lat = sv[5], sv[6]
    if lat is None or lon is None:
        return None  # a state vector with no fix is not a position
    return {
        "icao24": icao24,
        "callsign": callsign,
        "lat": lat, "lng": lon,
        "altitude_m": sv[13] if sv[13] is not None else sv[7],
        "on_ground": bool(sv[8]),
        "velocity_mps": sv[9],
        "heading": sv[10],
        "vertical_rate_mps": sv[11],
        "last_contact": sv[4],
    }


def poll_once(flags=None, fetch=fetch_states):
    with STATE_LOCK:
        STATE["polls"] += 1
    try:
        f = flags or _flags()
        allowed = _permitted(f)
        with STATE_LOCK:
            STATE["flipr_ok"] = True
    except Exception as e:
        with STATE_LOCK:
            STATE["flipr_ok"] = False
            STATE["paused_flag"] += 1
        log("warn", "opensky_paused", reason="flag plane unreachable", err=str(e)[:120])
        return
    if not allowed:
        with STATE_LOCK:
            STATE["paused_flag"] += 1
        log("info", "opensky_paused", reason="fetch.opensky is off")
        return
    try:
        resp = fetch()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        with STATE_LOCK:
            STATE["errors"] += 1
            STATE["last_error"] = f"fetch failed: {e}"
        log("warn", "opensky_fetch_failed", err=str(e)[:200])
        return
    seen = set()
    with AIRCRAFT_LOCK:
        for sv in resp.get("states") or []:
            row = _row(sv)
            if row is None:
                continue
            icao24 = row.pop("icao24")
            seen.add(icao24)
            entry = AIRCRAFT.setdefault(icao24, {"trail": [], "misses": 0})
            entry.update(row)
            entry["misses"] = 0
            trail = entry["trail"]
            point = [row["lat"], row["lng"], row["last_contact"]]
            if not trail or trail[-1][2] != point[2]:
                trail.append(point)
                del trail[:-TRAIL_LEN]
        # cold eviction: every tracked aircraft not seen this poll ages by
        # one miss; past the threshold it goes cold, the way a ride ends
        for icao24 in list(AIRCRAFT):
            if icao24 in seen:
                continue
            AIRCRAFT[icao24]["misses"] += 1
            if AIRCRAFT[icao24]["misses"] >= COLD_AFTER_POLLS:
                del AIRCRAFT[icao24]
        _write_snapshot()
        count = len(AIRCRAFT)
    with STATE_LOCK:
        STATE["polls_ok"] += 1
        STATE["aircraft_seen"] = count
        STATE["last_poll_at"] = time.time()
        STATE["last_error"] = ""
    log("info", "opensky_polled", aircraft=count, states=len(resp.get("states") or []))


def _write_snapshot():
    # tmp+rename: a crash mid-write can never leave a half-file, same
    # discipline as ingest_asset's manifest placement.
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, "positions.json.tmp")
    dest = os.path.join(OUT, "positions.json")
    body = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bbox": BBOX,
        "count": len(AIRCRAFT),
        "aircraft": [dict(icao24=k, **{f: v for f, v in e.items() if f != "misses"})
                     for k, e in AIRCRAFT.items()],
    }
    with open(tmp, "w") as fh:
        json.dump(body, fh)
    os.rename(tmp, dest)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with STATE_LOCK:
            s = dict(STATE)
        if self.path == "/health":
            ok = s["flipr_ok"] and not s["flag_missing"]
            body = json.dumps({"ok": ok, "commit": version.commit(),
                               "degraded": ([] if s["flipr_ok"] else ["flipr unreachable"])
                                          + ([f"flag not declared in flipr: {s['flag_missing']}"]
                                             if s["flag_missing"] else []),
                               **{k: v for k, v in s.items() if k != "flipr_ok"}}).encode()
            self._send(200 if ok else 503, "application/json", body)
        elif self.path == "/metrics":
            L = []
            for k in ("polls", "polls_ok", "aircraft_seen", "paused_flag", "errors"):
                L.append(f"# TYPE openskyd_{k}_total counter" if k != "aircraft_seen"
                          else "# TYPE openskyd_aircraft_tracked gauge")
                name = "openskyd_aircraft_tracked" if k == "aircraft_seen" else f"openskyd_{k}_total"
                L.append(f"{name} {s[k]}")
            L.append("# TYPE openskyd_flipr_reachable gauge")
            L.append(f"openskyd_flipr_reachable {1 if s['flipr_ok'] else 0}")
            self._send(200, "text/plain; version=0.0.4", "\n".join(L).encode() + b"\n")
        elif self.path == "/api":
            payload = descriptor_bytes()
            if payload is None:
                log("error", "descriptor-daemons.binpb not deployed beside the code; /api answering 503")
                self._send(503, "application/json", b'{"error": "descriptor-daemons.binpb not deployed beside the code"}')
                return
            self._send(200, "application/x-protobuf; messageType=google.protobuf.FileDescriptorSet", payload)
        else:
            self._send(404, "application/json", b'{"error": "health, metrics, or api"}')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # structured logging only


def main():
    os.makedirs(OUT, exist_ok=True)
    import heartbeat

    def inflight():
        return 0
    heartbeat.start(time.time(), inflight)
    # declare our flags before the first check: the service is the source of
    # what it declares, and a fresh flipr has no kingfisher@v1 until we say so
    import flags_decl
    flags_decl.publish(_flags())
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("info", "openskyd_up", out=OUT, port=PORT, interval_s=INTERVAL, bbox=BBOX)
    while True:
        poll_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
