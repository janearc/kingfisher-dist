#!/usr/bin/env python3
# gibsd -- live clouds from GOES-West, via NASA GIBS. Same shape as
# weatherd: a periodic daemon, its own pod, own port, /health and
# /metrics, flag-gated per pass. the ask: "i meant the
# atmospheric conditions. can i see clouds. can we pull it off poes and
# goes or whatever" -- this is the goes half; the poes half (VIIRS daily
# true colour) the display layer reads straight off GIBS's mercator
# endpoint, no daemon needed.
#
# WHAT IT SHELVES: the GeoColor composite (true colour by day, IR at
# night) as epsg4326 WMTS tiles for a fixed window over the bay and the
# California coast, every ten minutes -- GIBS serves the latest frame at
# time "default", measured about an hour behind the satellite. The 4326
# grid is LINEAR, so every tile's geographic bounds are exact arithmetic
# written into the manifest -- the display places bitmaps from the
# manifest and invents nothing (the interferogram lesson: imagery
# without bounds cannot be placed honestly).
#
# Flag chain, the estate's own: network.enabled AND fetch.enabled AND
# fetch.nasa_gibs. All three off means quietly paused, said in the log.

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

OUT = os.environ.get("GIBSD_OUT", "clouds")
PORT = int(os.environ.get("GIBSD_PORT", "15025"))
INTERVAL = float(os.environ.get("GIBSD_INTERVAL_S", "600"))
# the contact NASA's imagery service asks every client to declare. it is
# the operator's, so it comes from the environment and never from here; a
# daemon with no contact says so and stops rather than asking anonymously.
UA = os.environ.get("KINGFISHER_CONTACT", "")
if not UA:
    raise SystemExit(
        "gibsd: set KINGFISHER_CONTACT to a name and an address the "
        "imagery providers can reach, which they ask of every client"
    )
UA = "kingfisher-gibsd (" + UA + ")"

LAYER = "GOES-West_ABI_GeoColor"
TMS = "1km"          # the layer's one matrix set in the 4326 endpoint
LEVEL = 6            # 128x64 tiles, 2.8125 degrees per tile, 512px each
TILE_DEG = 360.0 / (2 ** (LEVEL + 1))
# the window: rows 17-19, cols 19-21 -- a 3x3 covering roughly 42N..34N,
# 127W..118W: the bay, the valley, the coast to LA
ROWS = range(17, 20)
COLS = range(19, 22)

STATE_LOCK = threading.Lock()
STATE = {"polls": 0, "polls_ok": 0, "tiles_ok": 0, "tiles_failed": 0,
         "paused_flag": 0, "errors": 0, "last_error": "", "flipr_ok": True, "flag_missing": "",
         "last_poll_at": 0.0}


def tile_bounds(row, col):
    # 4326 grid arithmetic: TopLeftCorner -180,90; row grows south, col
    # grows east; exact, no projection involved.
    west = -180.0 + col * TILE_DEG
    north = 90.0 - row * TILE_DEG
    return [west, north - TILE_DEG, west + TILE_DEG, north]


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
        allowed = flags.check("network.enabled") and flags.check("fetch.enabled") and flags.check("fetch.nasa_gibs")
    except flipr_client.FlagMissing as e:
        with STATE_LOCK:
            STATE["flag_missing"] = str(e)[:200]
        log("warn", "gibs_flag_not_declared", err=str(e)[:200])
        return False
    with STATE_LOCK:
        STATE["flag_missing"] = ""
    return allowed


def fetch_tile(row, col):
    url = (f"https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/{LAYER}"
           f"/default/default/{TMS}/{LEVEL}/{row}/{col}.png")
    # Through net.py: NASA's GIBS is a public tile service and a poll loop is
    # exactly the shape that turns one bad minute into a rate-limit. net honours
    # Retry-After, backs off with full jitter, and gives up rather than
    # hammering -- gibsd, weatherd and openskyd all wake on their own timers,
    # and full jitter is what stops them lining up when a CDN has a wobble.
    _status, _headers, body = net.request(
        url, headers={"User-Agent": UA},
        policy=net.DEFAULT.replace(timeout_s=30))
    return body


def poll_once(flags=None):
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
        log("warn", "gibs_paused", reason="flag plane unreachable", err=str(e)[:120])
        return
    if not allowed:
        with STATE_LOCK:
            STATE["paused_flag"] += 1
        log("info", "gibs_paused", reason="fetch.nasa_gibs chain is off")
        return
    goes_dir = os.path.join(OUT, "goes")
    os.makedirs(goes_dir, exist_ok=True)
    tiles = []
    for row in ROWS:
        for col in COLS:
            name = f"{LEVEL}_{row}_{col}.png"
            try:
                body = fetch_tile(row, col)
                tmp = os.path.join(goes_dir, name + ".tmp")
                with open(tmp, "wb") as fh:
                    fh.write(body)
                os.rename(tmp, os.path.join(goes_dir, name))
                tiles.append({"row": row, "col": col, "file": f"goes/{name}",
                              "bounds": tile_bounds(row, col)})
                with STATE_LOCK:
                    STATE["tiles_ok"] += 1
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                with STATE_LOCK:
                    STATE["tiles_failed"] += 1
                    STATE["last_error"] = str(e)[:200]
                log("warn", "gibs_tile_failed", row=row, col=col, err=str(e)[:150])
    if tiles:
        manifest = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "layer": LAYER, "level": LEVEL, "tile_deg": TILE_DEG,
            "source": "NASA GIBS / NOAA GOES-West ABI GeoColor, time=default (latest processed)",
            "tiles": tiles,
        }
        tmp = os.path.join(OUT, "manifest.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(manifest, fh)
        os.rename(tmp, os.path.join(OUT, "manifest.json"))
        with STATE_LOCK:
            STATE["polls_ok"] += 1
            STATE["last_poll_at"] = time.time()
    log("info", "gibs_polled", tiles=len(tiles), of=len(ROWS) * len(COLS))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with STATE_LOCK:
            s = dict(STATE)
        if self.path == "/health":
            ok = s["flipr_ok"] and not s["flag_missing"]
            body = json.dumps({"ok": ok, "commit": version.commit(), "degraded": ([] if s["flipr_ok"] else ["flipr unreachable"])
                                          + ([f"flag not declared in flipr: {s['flag_missing']}"]
                                             if s["flag_missing"] else []),
                               **{k: v for k, v in s.items() if k != "flipr_ok"}}).encode()
            self._send(200 if ok else 503, "application/json", body)
        elif self.path == "/metrics":
            L = []
            for k in ("polls", "polls_ok", "tiles_ok", "tiles_failed", "paused_flag", "errors"):
                L.append(f"# TYPE gibsd_{k}_total counter")
                L.append(f"gibsd_{k}_total {s[k]}")
            L.append("# TYPE gibsd_flipr_reachable gauge")
            L.append(f"gibsd_flipr_reachable {1 if s['flipr_ok'] else 0}")
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
        pass


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
    log("info", "gibsd_up", out=OUT, port=PORT, interval_s=INTERVAL,
        layer=LAYER, window=f"rows {ROWS.start}-{ROWS.stop - 1} cols {COLS.start}-{COLS.stop - 1}")
    while True:
        poll_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
