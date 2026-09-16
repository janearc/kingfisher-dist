#!/bin/sh
# publish kingfisher@v1 to flipr by hand, without a restart. Every kingfisher
# process publishes the same list at startup (flags_decl.py, since
# 2026-09-05); this is the operator's verb for a store that was wiped or
# replayed empty while the daemons keep running. Idempotent, and flipr never
# overwrites an operator's value server-side.
#
# The list lives in flags_decl.py and nowhere else.
set -e
cd "$(dirname "$0")/.."
python3 - <<'PYEOF'
import flags_decl
n = flags_decl.publish(flags_decl.client())
if n < 0:
    raise SystemExit(1)
print(f"published {n} flags to {flags_decl.SERVICE}@{flags_decl.VERSION}")
PYEOF
