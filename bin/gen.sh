#!/bin/sh
# regenerate the contract's Python types. Output is committed; a diff after
# running this is a gen-freshness violation.
set -e
cd "$(dirname "$0")/.."
# proto ALONE, same reason as the descriptor build below: flipr_client.py
# is stdlib-only BY DESIGN (protojson over raw urllib, no compiled types),
# so generating Python stubs for vendor/proto's flipr.proto would be dead
# code nothing imports -- an unscoped `buf generate` produced exactly that
# on the first run here.
buf generate proto
# proto ALONE: /api publishes what kingfisher serves, and vendor/proto
# (below) is what the daemons consume, not what this pod offers -- the
# same leak dodo's own contract test guards against. A bare `buf build`
# with two modules declared folds both in; this must stay scoped.
buf build proto -o descriptor.binpb
# ingestd and openskyd serve no RPC of their own -- their /api is what they
# consume (flipr's FliprService, vendored above), not kingfisher's own
# surface. "a daemon's contract is what it consumes" (mitigation-1,
# 2026-08-28).
buf build vendor/proto -o descriptor-daemons.binpb
