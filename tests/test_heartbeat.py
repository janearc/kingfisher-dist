# the lease is the heartbeat, and the frame is the part that fails SILENTLY
# if wrong -- produce succeeds, only consumers break. So the frame is pinned
# to exact bytes, and the loop is tested with an injected producer.

import io
import json

import heartbeat
import net


def test_frame_exact_bytes():
    # magic 0x00, schema id 7 big-endian, single-0x00 index, payload.
    # ServiceHealthHeartbeat is the FIRST message in observability.proto,
    # which is the only reason the single-byte index is valid.
    assert heartbeat.frame(7, b"PB") == b"\x00\x00\x00\x00\x07\x00PB"
    assert heartbeat.frame(0x01020304, b"") == b"\x00\x01\x02\x03\x04\x00"


def test_beat_carries_the_contract_fields():
    b = heartbeat.build_beat(uptime_s=61.7, inflight=3)
    assert b.service_name == "kingfisher"
    assert b.uptime_seconds == 61
    assert b.internal_load_metric == 3
    assert b.current_state == 1  # HEALTH_STATE_GREEN
    assert len(b.idempotency_key) == 36
    assert b.timestamp.seconds > 0


def test_loop_produces_one_framed_beat(monkeypatch):
    sent = []

    class Producer:
        def send(self, topic, key=None, value=None):
            sent.append((topic, key, value))
        def flush(self, timeout=None):
            pass

    monkeypatch.setenv("KINGFISHER_SCHEMA_REGISTRY", "http://sr.test")
    monkeypatch.setattr(heartbeat, "schema_id", lambda url, subj: 42)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 1,
                       producer=Producer(), interval=999, once=True)
    assert len(sent) == 1
    topic, key, value = sent[0]
    assert topic == "observability.events"
    assert key == b"kingfisher"
    assert value[:5] == b"\x00\x00\x00\x00\x2a"
    assert value[5:6] == b"\x00"
    assert heartbeat.BEATS["ok"] >= 1


def test_loop_counts_errors_and_never_raises(monkeypatch):
    class Boom:
        def send(self, *a, **k):
            raise OSError("bus gone")
        def flush(self, timeout=None):
            pass

    monkeypatch.setenv("KINGFISHER_SCHEMA_REGISTRY", "http://sr.test")
    monkeypatch.setattr(heartbeat, "schema_id", lambda url, subj: 1)
    before = heartbeat.BEATS.get("error", 0)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 0,
                       producer=Boom(), interval=999, once=True)
    assert heartbeat.BEATS["error"] == before + 1


def test_unconfigured_means_no_beat_not_an_error(monkeypatch, capsys):
    monkeypatch.delenv("KINGFISHER_KAFKA_BOOTSTRAP", raising=False)
    monkeypatch.delenv("KINGFISHER_SCHEMA_REGISTRY", raising=False)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 0, once=True)
    assert "heartbeat_disabled" in capsys.readouterr().out


def test_schema_id_lookup(monkeypatch):
    class R(io.BytesIO):
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # net._urlopen, not heartbeat.urllib.request.urlopen. The latter IS the
    # urllib module, so that patch was global -- exactly the thing providers.py
    # kept its own seam to avoid, because patching urllib for the whole process
    # hijacks the test harness's own HTTP client. Since 2026-09-01 there is one
    # door for the service and it is net's.
    monkeypatch.setattr(net, "_urlopen",
                        lambda req, timeout=5: R(json.dumps({"id": 9}).encode()))
    assert heartbeat.schema_id("http://sr.test", "observability.events-value") == 9


# ---------------------------------------------------------------------------
# issue 77: a bus that is down at startup must not kill the thread


def test_a_failed_bootstrap_is_a_counted_beat_not_a_dead_thread(monkeypatch):
    """The producer was built above the loop's try. KafkaTimeoutError at startup
    came straight out of run_loop, the daemon thread died, nothing retried, and
    both counters sat at zero for two days while hall-monitor never listed
    kingfisher. Now it is one dropped beat, counted."""
    monkeypatch.setenv("KINGFISHER_KAFKA_BOOTSTRAP", "kafka.test:9092")
    monkeypatch.setenv("KINGFISHER_SCHEMA_REGISTRY", "http://sr.test")

    def no_bus(bootstrap):
        raise RuntimeError("Unable to bootstrap from ['kafka.test:9092']")
    monkeypatch.setattr(heartbeat, "_new_producer", no_bus)

    before = heartbeat.BEATS.get("error", 0)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 0, interval=999, once=True)  # must return, not raise
    assert heartbeat.BEATS["error"] == before + 1


def test_the_bus_coming_up_later_is_enough(monkeypatch):
    """Retry semantics: the producer is None until it can be built, and the
    next tick tries again. Fail once, succeed once, one of each counted."""
    monkeypatch.setenv("KINGFISHER_KAFKA_BOOTSTRAP", "kafka.test:9092")
    monkeypatch.setenv("KINGFISHER_SCHEMA_REGISTRY", "http://sr.test")
    monkeypatch.setattr(heartbeat, "schema_id", lambda url, subj: 1)

    class Producer:
        def send(self, topic, key=None, value=None):
            pass
        def flush(self, timeout=None):
            pass

    attempts = {"n": 0}
    def flaky_bus(bootstrap):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("not yet")
        return Producer()
    monkeypatch.setattr(heartbeat, "_new_producer", flaky_bus)

    err0, ok0 = heartbeat.BEATS.get("error", 0), heartbeat.BEATS.get("ok", 0)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 0, interval=999, once=True)
    heartbeat.run_loop(started=0, inflight_fn=lambda: 0, interval=999, once=True)
    assert heartbeat.BEATS["error"] == err0 + 1
    assert heartbeat.BEATS["ok"] == ok0 + 1
    assert attempts["n"] == 2
