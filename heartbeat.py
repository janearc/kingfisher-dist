#!/usr/bin/env python3
# heartbeat -- kingfisher's lease, because THE HEARTBEAT IS THE LEASE.
#
# The mesh's admission design (lease.v1, hall-monitor PR 11): there is no
# lease request and no grant. A service emits the standard
# observability.v1.ServiceHealthHeartbeat on an interval; hall-monitor
# learns the cadence by observation and judges silence against three gaps.
# Authorization is visible at hm.test/truth, expiry is the VERDICT'S state,
# and this file is everything kingfisher has to do about any of it.
#
# BEST-EFFORT, NEVER FATAL. A bus hiccup must not take the maps down: a
# missed beat costs a row flickering toward EXPIRING in hm's table, which
# is the system WORKING -- silence is supposed to be visible. Failures are
# counted (kingfisher_heartbeat_total{outcome}) and logged, never raised.
#
# THE WIRE, and why it is hand-built here: the Confluent protobuf framing is
#
#     byte 0    : magic 0x00
#     bytes 1-4 : schema id, big-endian
#     byte 5    : message-index 0x00 -- VALID ONLY because
#                 ServiceHealthHeartbeat is the FIRST message in
#                 observability.proto; any other message needs the computed
#                 zig-zag index (see blm emit/publisher.go, which owns the
#                 authoritative Go implementation and documents the trap)
#     rest      : serialized protobuf
#
# A framing bug is SILENT -- produce succeeds and only consumers break -- so
# the frame is pinned by an exact-bytes test. blm's own python doctrine
# (frood/emit.py) is "Python never touches kafka, POST to the Go sidecar";
# no sidecar runs beside it yet, so this produces directly with
# infrastructure's blessing. When a sidecar or a blm python framing helper
# lands, this file shrinks to a POST and nothing else changes.
#
# gen/observability/v1 is VENDORED from big-little-mesh @ d045b19 (consumers
# vendor-generate); refresh by re-copying from blm's gen/python, never by
# hand-editing.

import json
import os
import struct
import sys
import threading
import time
import urllib.request
import uuid

import net
from log import log

# parameterised: ingestd heartbeats as its own citizen and hm lists it as
# its own row -- one module, every process it rides in
SERVICE = os.environ.get("KINGFISHER_SERVICE_NAME", "kingfisher")
TOPIC = os.environ.get("KINGFISHER_HEARTBEAT_TOPIC", "observability.events")

# counted, then surfaced through /metrics -- a heartbeat that fails silently
# would be the exact lie the health handler used to tell
BEATS = {"ok": 0, "error": 0}
_BEATS_LOCK = threading.Lock()


def _count(outcome):
    with _BEATS_LOCK:
        BEATS[outcome] = BEATS.get(outcome, 0) + 1


def schema_id(registry_url, subject):
    # the schema is REGISTERED by hall-monitor's own emitter; kingfisher only
    # looks it up. Registering here would race two writers over one subject.
    # a GET, so net retries it freely. The registry is in-cluster and a failed
    # lookup means the heartbeat cannot be framed at all, so riding out a
    # restart is strictly better than dropping the beat.
    return int(net.get_json(
        f"{registry_url}/subjects/{subject}/versions/latest",
        policy=net.INTERACTIVE.replace(timeout_s=5))["id"])


def frame(sid, payload):
    # magic, schema id big-endian, single-0x00 index (first message in file),
    # payload. Pinned by test_heartbeat's exact-bytes case.
    return b"\x00" + struct.pack(">i", sid) + b"\x00" + payload


def build_beat(uptime_s, inflight):
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "gen"))
    from google.protobuf.timestamp_pb2 import Timestamp
    from observability.v1 import observability_pb2 as ob
    ts = Timestamp()
    ts.GetCurrentTime()
    return ob.ServiceHealthHeartbeat(
        service_name=SERVICE,
        current_state=ob.HEALTH_STATE_GREEN,
        uptime_seconds=int(uptime_s),
        internal_load_metric=int(inflight),
        timestamp=ts,
        idempotency_key=str(uuid.uuid4()),
    )


def _new_producer(bootstrap):
    # imported here, not at module top: a laptop checkout without kafka
    # installed must still import heartbeat, and a pod whose bus is down
    # must get an exception it can count rather than a dead thread
    from kafka import KafkaProducer
    return KafkaProducer(bootstrap_servers=bootstrap.split(","))


def run_loop(started, inflight_fn, producer=None, interval=None, once=False):
    # the daemon loop. producer is injectable for the suite; the real one is
    # built lazily so importing this module costs nothing.
    bootstrap = os.environ.get("KINGFISHER_KAFKA_BOOTSTRAP", "")
    registry = os.environ.get("KINGFISHER_SCHEMA_REGISTRY", "")
    # RecordNameStrategy, measured against the live registry: the subject is
    # the fully-qualified MESSAGE name, not topic-value. hm registered it.
    subject = os.environ.get("KINGFISHER_SCHEMA_SUBJECT",
                             "observability.v1.ServiceHealthHeartbeat")
    interval = interval or float(os.environ.get("KINGFISHER_HEARTBEAT_S", "30"))
    if producer is None and not (bootstrap and registry):
        # not configured is not an error: a laptop checkout has no bus.
        # The pod HAS both, set in the overlay.
        log("info", "heartbeat_disabled", reason="no bus configured")
        return
    sid = None
    while True:
        try:
            # THE PRODUCER IS BUILT INSIDE THE TRY. It used to be built above
            # this loop, so if kafka had not bootstrapped at the moment the
            # daemon started, KafkaTimeoutError came out of run_loop itself,
            # killed the thread, and nothing retried: both counters sat at
            # zero for two days and hall-monitor never listed kingfisher
            # (issue 77). The module header calls this beat the lease. A
            # failed bootstrap is now one dropped beat, counted, and tried
            # again next tick like any other failure.
            if producer is None:
                producer = _new_producer(bootstrap)
            if sid is None:
                sid = schema_id(registry, subject)
            beat = build_beat(time.time() - started, inflight_fn())
            producer.send(TOPIC, key=SERVICE.encode(),
                          value=frame(sid, beat.SerializeToString()))
            producer.flush(timeout=5)
            _count("ok")
        except Exception as e:  # noqa: BLE001 -- best-effort by design; see header
            _count("error")
            log("warn", "heartbeat_dropped", err=str(e))
            sid = None  # re-resolve next tick; the registry may have moved
        if once:
            return
        time.sleep(interval)


def start(started, inflight_fn):
    t = threading.Thread(target=run_loop, args=(started, inflight_fn),
                         daemon=True, name="heartbeat")
    t.start()
    return t
