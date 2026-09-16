#!/bin/sh
# serve-local.sh -- a kingfisher over the real shelf, on a loopback port.
#
# For measuring, not for serving: bin/kingfisher-stress.sh points at this by
# default so a memory ramp can find the process's limit without anything in
# the cluster dying. The shelf is the same directory the pod mounts,
# read-only in effect (serve.py only reads under a mount), so the numbers are
# the pod's numbers to within the allocator's habits: macOS malloc against the
# image's glibc. The slope per cell is what transfers; the absolute floor is
# the pod's to measure.
#
# Not a service: no init.claude verb, no port anyone else knows, torn down
# when the terminal goes. KINGFISHER_COMMIT is stamped so /health names the
# checkout the way the image would.
#
#   bin/serve-local.sh            listen on 127.0.0.1:15099
#   PORT=15098 bin/serve-local.sh another port, for a second copy
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-15099}"
SHELF="${SHELF:-/srv/kingfisher/maps/ingested}"
[ -d "$SHELF" ] || { echo "serve-local: no shelf at $SHELF" >&2; exit 1; }
cd "$HERE"
KINGFISHER_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
export KINGFISHER_COMMIT
exec uv run --quiet python serve.py --port "$PORT" --bind 127.0.0.1 --mount "/ingested/=$SHELF"
