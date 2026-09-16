# per-RPC instrumentation -- every method measured, at the router.
#
# The property under test is COVERAGE BY CONSTRUCTION: any request, whatever
# it hits, lands in exactly one method series with an outcome. A handler
# that someone adds later cannot dodge it, because the observation happens
# in _timed's finally, not in the handler.

import serve


def _series(text, family):
    return [l for l in text.splitlines()
            if l.startswith(family + "{")]


def test_every_endpoint_lands_in_a_method_series(get):
    get("/api")
    get("/health")
    get("/stats")
    get("/reload")
    get("/econ/")
    get("/econ/res8/8b01.json")
    get("/")
    # /metrics cannot see ITSELF: rpc_done fires in _timed's finally, after
    # the response body is already built. The second scrape sees the first.
    get("/metrics")
    text = get("/metrics").text
    lines = _series(text, "kingfisher_rpc_requests_total")
    methods = {l.split('method="')[1].split('"')[0] for l in lines}
    for m in ("api", "health", "stats", "reload", "mount_index",
              "tile", "index", "metrics"):
        assert m in methods, f"method {m} has no series"


def test_outcomes_distinguish_ok_from_not_found(get):
    get("/econ/res8/8b01.json")
    get("/econ/res8/does-not-exist.json")
    get("/metrics")
    text = get("/metrics").text
    lines = _series(text, "kingfisher_rpc_requests_total")
    tile = [l for l in lines if 'method="tile"' in l]
    outcomes = {l.split('outcome="')[1].split('"')[0] for l in tile}
    assert "ok" in outcomes
    assert "not_found" in outcomes


def test_duration_counted_per_method(get):
    get("/econ/res8/8b01.json")
    text = get("/metrics").text
    assert _series(text, "kingfisher_rpc_duration_seconds_sum")
    assert _series(text, "kingfisher_rpc_duration_seconds_count")


def test_mount_readable_reports_per_mount(get):
    text = get("/metrics").text
    lines = _series(text, "kingfisher_mount_readable")
    assert lines, "no mount_readable series"
    # the fixture's mounts exist on disk, so every one reads 1
    for l in lines:
        assert l.endswith(" 1")


def test_method_of_covers_the_surface():
    serve.MOUNTS["/econ/"] = "/tmp/x"
    assert serve.method_of("/api") == "api"
    assert serve.method_of("/") == "index"
    assert serve.method_of("/econ/") == "mount_index"
    assert serve.method_of("/econ/res8/a.json") == "tile"
    assert serve.method_of("/viewer.html") == "static"
    assert serve.outcome_of(0) == "dropped"
    assert serve.outcome_of(200) == "ok"
    assert serve.outcome_of(304) == "not_modified"
    assert serve.outcome_of(403) == "forbidden"
    assert serve.outcome_of(404) == "not_found"
    assert serve.outcome_of(500) == "error"
