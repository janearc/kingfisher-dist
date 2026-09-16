#!/bin/sh
# visual traceroute: run the route, geolocate the public hops, drop a
# GeoJSON into the viewer's traces/ shelf. Open corvid.test:9800/out/trace.html
# and pick it. Host-side tool -- traceroute needs the host's network, and
# ip-api.com's free tier (45/min, keyless) does the locating.
set -eu
TARGET="$1"
OUT="${SHELF_ROOT:-/srv/kingfisher/maps}/viewer/out/traces/$TARGET.geojson"
traceroute -n -q 1 -w 2 -m 24 "$TARGET" 2>/dev/null \
  | TRACE_TARGET="$TARGET" python3 "$(dirname "$0")/traceroute-geo.py" > "$OUT"
echo "wrote $OUT ($(grep -c '"hop"' "$OUT") hops)"
