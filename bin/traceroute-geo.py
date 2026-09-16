#!/usr/bin/env python3
# the geolocation half of traceroute-map.sh -- ITS OWN FILE because a
# python -c inside shell single-quotes silently ate h['ip'] into h[ip],
# NameError'd every lookup, and except-pass buried it: 24 hops, zero
# located, no error anywhere. The lookup failures are COUNTED now too.
import sys, os, json, re, time, urllib.request

hops = []
for line in sys.stdin:
    m = re.match(r"\s*(\d+)\s+([0-9.]+)\s+([0-9.]+) ms", line)
    if m:
        hops.append({"hop": int(m.group(1)), "ip": m.group(2), "rtt": float(m.group(3))})
    elif re.match(r"\s*\d+\s+\*", line):
        hops.append({"hop": len(hops) + 1, "ip": None, "rtt": None})

def private(ip):
    a, b = (int(x) for x in ip.split(".")[:2])
    return a in (10, 127) or (a == 172 and 16 <= b <= 31) \
        or (a == 192 and b == 168) or (a == 100 and 64 <= b <= 127)

feats, lookup_errors = [], 0
for h in hops:
    props = {"hop": h["hop"], "ip": h["ip"] or "*", "rtt_ms": h["rtt"], "located": False}
    if h["ip"] and not private(h["ip"]):
        try:
            url = "http://ip-api.com/json/%s?fields=status,lat,lon,city,country,org,as" % h["ip"]
            g = json.load(urllib.request.urlopen(url, timeout=6))
            if g.get("status") == "success":
                props.update({"located": True, "city": g.get("city", ""),
                              "country": g.get("country", ""),
                              "org": g.get("org") or g.get("as", "")})
                feats.append({"type": "Feature", "properties": props,
                              "geometry": {"type": "Point",
                                           "coordinates": [g["lon"], g["lat"]]}})
                time.sleep(1.4)
                continue
        except Exception as e:
            lookup_errors += 1
            print(f"lookup failed for {h['ip']}: {e}", file=sys.stderr)
    feats.append({"type": "Feature", "properties": props, "geometry": None})

json.dump({"type": "FeatureCollection",
           "properties": {"target": os.environ.get("TRACE_TARGET", "?"),
                          "traced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "lookup_errors": lookup_errors},
           "features": feats}, sys.stdout, indent=1)
