#!/bin/sh
# the end-to-end check against a live cluster: kingfisher talks to valhalla,
# flipr, kafka and hm, and product X comes out. Exit 0 = all pass.
set -e
cd "$(dirname "$0")/.."
exec uv run python tests/e2e_live.py
