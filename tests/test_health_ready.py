# /health and /ready, which answer two different questions with two different
# consequences.
#
# The failure these exist for is named in README.md as the single most
# expensive mistake available in this repo: a hostPath that resolves to nothing
# inside the node, so the pod starts cleanly, reports healthy, and serves 404s
# forever. The manifests wire a readinessProbe, so an instance in that state
# stays in the Service and keeps answering 404 to everyone.

import json
import time

import serve


def test_health_is_liveness_and_always_answers_200(get):
    # Liveness means "do not restart me". kingfisher restarting will not fix a
    # wrong mount, so /health must not be the thing that says no.
    resp = get("/health")
    assert resp.status == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["mounts"] == 3


def test_health_now_reports_what_it_used_to_withhold(get):
    # It used to answer {"ok": true} and deliberately say nothing about whether
    # the mounts held anything. Liveness was the right STATUS for it; silence
    # was not the right BODY.
    body = get("/health").json()
    assert "status" in body and body["status"] in ("healthy", "degraded")
    assert "ready" in body
    assert "reason" in body
    assert "mounts_empty" in body


def test_a_mount_with_files_but_no_manifest_is_not_empty(get):
    # /layers/ and /rides/ in production have no manifest at all and are
    # discovered through the JSON directory index. They report zero TILES and
    # are entirely healthy, so an emptiness check written against tiles would
    # take them out of rotation for being what they are.
    get("/stats")  # populate the inventory
    body = get("/ready").json()
    assert "/layers/" not in body["mounts_empty"], \
        "a manifest-less mount was called empty; the check is reading tiles, not files"
    assert body["ready"] is True
    assert get("/ready").status == 200


def test_ready_refuses_when_a_mount_resolved_to_nothing(get, tmp_path):
    # The README's failure, made concrete: a mount pointing at a directory that
    # exists and holds nothing.
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    serve.MOUNTS["/nowhere/"] = str(nowhere)
    serve.inventory(force=True)

    resp = get("/ready")
    assert resp.status == 503, "an instance serving 404 forever must leave the Service"
    body = resp.json()
    assert body["ready"] is False
    assert body["ok"] is False
    assert "/nowhere/" in body["mounts_empty"]
    assert "hostPath" in body["reason"], "the reason must name the cause, not the symptom"

    # and liveness is unmoved: restarting would not fix this, so /health still
    # says 200 rather than putting the pod in a crash loop.
    live = get("/health")
    assert live.status == 200
    assert live.json()["status"] == "degraded"
    assert live.json()["ready"] is False


def test_health_never_walks_the_tree_to_answer(get, monkeypatch):
    # It is polled every ten seconds. An endpoint that walks 397 datasets to
    # answer turns one outage into two, and a probe that occasionally takes
    # seconds is a probe that flaps.
    walked = {"n": 0}
    real = serve._walk_inventory

    def counting():
        walked["n"] += 1
        return real()

    monkeypatch.setattr(serve, "_walk_inventory", counting)
    with serve.INV_LOCK:
        serve.INV_CACHE["at"] = 0.0
        serve.INV_CACHE["value"] = None

    for _ in range(5):
        assert get("/health").status == 200
    assert walked["n"] == 0, "health caused an inventory walk; it must only observe"


def test_a_flipr_refusal_shows_up_in_the_posture(get):
    # The fetch surface already answers 503 naming flipr when the flag plane is
    # down. That posture was promised to /health in a comment and never given
    # to it, so a monitor watching /health saw ok while the expensive surface
    # refused everything.
    serve.FLIPR_DOWN["at"] = time.time()
    serve.FLIPR_DOWN["err"] = "connection refused"
    try:
        body = get("/health").json()
        assert body["status"] == "degraded"
        assert "flipr" in body["reason"]
    finally:
        serve.FLIPR_DOWN["at"] = 0.0
        serve.FLIPR_DOWN["err"] = ""


def test_an_old_flipr_refusal_stops_counting(get):
    # A refusal from an hour ago is history, not a posture. Without a window,
    # one blip would leave the instance degraded until it restarted.
    serve.FLIPR_DOWN["at"] = time.time() - (serve.FLIPR_DOWN_WINDOW + 60)
    serve.FLIPR_DOWN["err"] = "connection refused"
    try:
        assert "flipr" not in get("/health").json()["reason"]
    finally:
        serve.FLIPR_DOWN["at"] = 0.0
        serve.FLIPR_DOWN["err"] = ""


def test_both_are_labelled_in_metrics(get):
    # every RPC labelled by method and outcome, says the header. A new endpoint
    # that falls through to a generic label is a hole in that claim.
    get("/health")
    get("/ready")
    text = get("/metrics").text
    assert 'method="health"' in text
    assert 'method="ready"' in text
