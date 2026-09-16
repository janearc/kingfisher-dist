#!/usr/bin/env python3
# weatherd -- atmospheric conditions, polled from two free providers so one
# disliking us does not mean going dark. Same shape as openskyd: a periodic
# daemon, its own pod, own port, /health and /metrics, flag-gated per pass.
#
# THE REDUNDANCY, the framing ("sometimes providers don't like us"):
# Open-Meteo is primary (global, generous free tier, no key). NWS/
# api.weather.gov is the fallback (US-only, genuinely zero rate limit, but
# a government API whose reliability varies) -- tried only when Open-Meteo's
# call fails for that point this poll. Both are free with no account and no
# key, measured live 2026-08-28: Open-Meteo's /v1/forecast?current=... and
# NWS's /points -> /gridpoints/.../stations -> /stations/{id}/observations/
# latest chain, the last of which needs a real User-Agent (their docs ask
# for one; an anonymous UA is how NWS rate-limits you).
#
# ONE FLAG, not two: fetch.weather gates the whole feature. The choice of
# which provider actually answered a given point this poll is not a
# configuration decision, it is what happened -- recorded per-point, not
# flagged.

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

OUT = os.environ.get("WEATHERD_OUT", "weather")
PORT = int(os.environ.get("WEATHERD_PORT", "15024"))
INTERVAL = float(os.environ.get("WEATHERD_INTERVAL_S", "600"))
# the contact the weather service asks every client to declare, from the
# environment, for the same reason gibsd's is.
UA = os.environ.get("KINGFISHER_CONTACT", "")
if not UA:
    raise SystemExit(
        "weatherd: set KINGFISHER_CONTACT to a name and an address the "
        "providers can reach, which they ask of every client"
    )
UA = "kingfisher-weatherd (" + UA + ")"

# a small fixed set, not a bbox sweep: weather is a per-point query, not an
# areal broadcast the way ADS-B state vectors are. Three Bay Area points is
# enough to prove the redundancy works; the pane decides how to interpolate
# or grid this, same "display is not decided here" split as flights.
POINTS = [
    {"name": "san-francisco", "lat": 37.7749, "lon": -122.4194},
    {"name": "oakland", "lat": 37.8044, "lon": -122.2712},
    {"name": "san-jose", "lat": 37.3382, "lon": -121.8863},
]

STATE_LOCK = threading.Lock()
STATE = {"polls": 0, "polls_ok": 0, "open_meteo_ok": 0, "open_meteo_failed": 0,
         "nws_ok": 0, "nws_failed": 0, "paused_flag": 0, "errors": 0,
         "last_error": "", "flipr_ok": True, "flag_missing": "", "last_poll_at": 0.0}

# point name -> latest reading (dict) or None if never answered
READINGS = {}
READINGS_LOCK = threading.Lock()
# point name -> NWS stationId, resolved once (the points->gridpoint->station
# chain is fixed for a fixed lat/lon; re-resolving it every poll would be
# three calls where one suffices after the first)
NWS_STATION = {}


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
        allowed = flags.check("network.enabled") and flags.check("fetch.enabled") and flags.check("fetch.weather")
    except flipr_client.FlagMissing as e:
        with STATE_LOCK:
            STATE["flag_missing"] = str(e)[:200]
        log("warn", "weather_flag_not_declared", err=str(e)[:200])
        return False
    with STATE_LOCK:
        STATE["flag_missing"] = ""
    return allowed


def _get_json(url):
    # open-meteo is free and asks politely that you not hammer it. net.py
    # honours Retry-After and backs off with full jitter; a poll daemon that
    # retries immediately is how a free tier stops being available.
    return net.get_json(url,
                        headers={"Accept": "application/json", "User-Agent": UA},
                        policy=net.DEFAULT.replace(timeout_s=15))


def fetch_open_meteo(lat, lon):
    j = _get_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,wind_speed_10m,wind_direction_10m,precipitation,"
        "cloud_cover,pressure_msl,relative_humidity_2m&timezone=UTC")
    c = j["current"]
    return {
        "source": "open-meteo", "observed_at": c["time"] + ":00Z",
        "temperature_c": c["temperature_2m"], "wind_speed_kmh": c["wind_speed_10m"],
        "wind_direction_deg": c["wind_direction_10m"], "precipitation_mm": c["precipitation"],
        "cloud_cover_pct": c["cloud_cover"], "pressure_hpa": c["pressure_msl"],
        "humidity_pct": c["relative_humidity_2m"],
    }


def _nws_station(name, lat, lon):
    cached = NWS_STATION.get(name)
    if cached:
        return cached
    pt = _get_json(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
    stations = _get_json(pt["observationStations"])["features"]
    if not stations:
        raise RuntimeError(f"NWS named no station near {lat},{lon}")
    station = stations[0]["properties"]["stationIdentifier"]
    NWS_STATION[name] = station
    return station


def fetch_nws(name, lat, lon):
    station = _nws_station(name, lat, lon)
    p = _get_json(f"https://api.weather.gov/stations/{station}/observations/latest")["properties"]
    v = lambda field: (p.get(field) or {}).get("value")
    pressure_pa = v("barometricPressure")
    return {
        "source": "nws", "station": station, "observed_at": p["timestamp"],
        "temperature_c": v("temperature"), "wind_speed_kmh": v("windSpeed"),
        "wind_direction_deg": v("windDirection"), "precipitation_mm": None,
        # NWS names cloud fraction only via coded cloudLayers, not a percent
        # -- left null rather than invented
        "cloud_cover_pct": None,
        "pressure_hpa": pressure_pa / 100 if pressure_pa is not None else None,
        "humidity_pct": v("relativeHumidity"),
    }


def poll_point(point):
    name, lat, lon = point["name"], point["lat"], point["lon"]
    try:
        reading = fetch_open_meteo(lat, lon)
        with STATE_LOCK:
            STATE["open_meteo_ok"] += 1
        return reading
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        with STATE_LOCK:
            STATE["open_meteo_failed"] += 1
        log("warn", "open_meteo_failed_falling_back", point=name, err=str(e)[:150])
    try:
        reading = fetch_nws(name, lat, lon)
        with STATE_LOCK:
            STATE["nws_ok"] += 1
        return reading
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, RuntimeError) as e:
        with STATE_LOCK:
            STATE["nws_failed"] += 1
        log("warn", "nws_also_failed", point=name, err=str(e)[:150])
        return None


def poll_once(flags=None, points=None):
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
        log("warn", "weather_paused", reason="flag plane unreachable", err=str(e)[:120])
        return
    if not allowed:
        with STATE_LOCK:
            STATE["paused_flag"] += 1
        log("info", "weather_paused", reason="fetch.weather is off")
        return
    answered = 0
    with READINGS_LOCK:
        for point in (points if points is not None else POINTS):
            reading = poll_point(point)
            if reading is not None:
                READINGS[point["name"]] = {"lat": point["lat"], "lon": point["lon"], **reading}
                answered += 1
        _write_snapshot()
    with STATE_LOCK:
        STATE["polls_ok"] += 1
        STATE["last_poll_at"] = time.time()
        STATE["last_error"] = ""
    log("info", "weather_polled", points=len(points if points is not None else POINTS), answered=answered)


def _write_snapshot():
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, "current.json.tmp")
    dest = os.path.join(OUT, "current.json")
    body = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "points": [{"name": k, **v} for k, v in READINGS.items()],
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
            body = json.dumps({"ok": ok, "commit": version.commit(), "degraded": ([] if s["flipr_ok"] else ["flipr unreachable"])
                                          + ([f"flag not declared in flipr: {s['flag_missing']}"]
                                             if s["flag_missing"] else []),
                               **{k: v for k, v in s.items() if k != "flipr_ok"}}).encode()
            self._send(200 if ok else 503, "application/json", body)
        elif self.path == "/metrics":
            L = []
            for k in ("polls", "polls_ok", "open_meteo_ok", "open_meteo_failed",
                      "nws_ok", "nws_failed", "paused_flag", "errors"):
                L.append(f"# TYPE weatherd_{k}_total counter")
                L.append(f"weatherd_{k}_total {s[k]}")
            L.append("# TYPE weatherd_flipr_reachable gauge")
            L.append(f"weatherd_flipr_reachable {1 if s['flipr_ok'] else 0}")
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
    log("info", "weatherd_up", out=OUT, port=PORT, interval_s=INTERVAL, points=[p["name"] for p in POINTS])
    while True:
        poll_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
