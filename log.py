#!/usr/bin/env python3
# structured json to stdout, one line per event -- the estate's pattern
# (peacock carries the same file). A bare print is a lint error here; every
# runtime event goes through log() so the collector under the log root
# receives machine-readable lines, not prose. There is no logs.events topic
# and that is deliberate: raw JSON on the bus is off-contract and hm refuses
# it (measured, 164MB of refusals); files are the transport until a
# CONTRACTED shipper exists.
import json
import sys
import time


def log(level, event, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "event": event, "svc": "kingfisher"}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()
