import json
import os
import re

import serve

# THE CONTRACT: the metric families kingfisher publishes. This set is a
# promise, not a description -- renaming or removing a name here is a break
# for anything that scraped it, and adding one is free.
#
# It used to say the consumer was delightd's dashboard, in another repository,
# merged on main, where a rename here would silently break a panel over there.
# delightd was turned down in 2026-09. The dashboard that binds these names is
# now kube/template/dashboard-kingfisher.json in this repo, and a second test
# below asserts every name it binds is in this set, so the two cannot drift.
# Three families were added 2026-08-28 (per-RPC instrumentation and the mount
# readability gauge); additions are always safe.
CONTRACT = {
    "kingfisher_bytes_total",
    "kingfisher_errors_total",
    "kingfisher_generation",
    "kingfisher_heartbeat_total",
    "kingfisher_inflight_requests",
    "kingfisher_heap_bytes",
    "kingfisher_memory_limit_bytes",
    "kingfisher_kind_duration_seconds",
    "kingfisher_mount_bytes",
    "kingfisher_mount_chunks",
    "kingfisher_mount_files",
    "kingfisher_mount_readable",
    "kingfisher_mount_resolutions",
    "kingfisher_mount_series",
    "kingfisher_mount_tile_bytes",
    "kingfisher_mount_tiles",
    "kingfisher_mounts",
    "kingfisher_outbound_duration_seconds",
    "kingfisher_outbound_requests_total",
    "kingfisher_outbound_retries_total",
    "kingfisher_provider_bytes_total",
    "kingfisher_provider_requests_total",
    "kingfisher_request_duration_seconds",
    "kingfisher_requests_total",
    "kingfisher_responses_total",
    "kingfisher_rpc_duration_seconds",
    "kingfisher_rpc_requests_total",
    "kingfisher_start_time_seconds",
    "kingfisher_tile_bytes_total",
    "kingfisher_tiles_by_kind_total",
    "kingfisher_tiles_by_mount_total",
    "kingfisher_tiles_total",
    "kingfisher_uptime_seconds",
}

# The families that CANNOT render a series until something has happened, because
# their labels come from the event -- a host that was called, a mount that was
# read, a status that was returned. There is no honest zero for these: inventing
# a placeholder host would put a fake series on a dashboard, which is worse than
# an empty panel because it looks like an answer.
#
# Everything NOT in this set must publish at zero. That is the other half of the
# contract and it is the half that catches a collector which has quietly stopped
# collecting -- absence must mean breakage, not idleness.
#
# NO ANTI-ROT TEST HERE, and the absence is deliberate. The obvious guard is to
# assert that nothing in this set publishes at zero, so a family that learns to
# would have to leave the list. It cannot be written honestly in this suite:
# these tests share one server, so by the time it runs the earlier tests have
# generated exactly the traffic that makes these families publish, and the guard
# fires on every one of them. It was written, it failed that way, and removing
# it was the correct outcome -- a test whose result depends on what ran before
# it is the thing this whole rework existed to delete. Prune this list by hand
# when a family changes.
DATA_DEPENDENT = {
    "kingfisher_kind_duration_seconds",
    "kingfisher_outbound_duration_seconds",
    "kingfisher_outbound_requests_total",
    "kingfisher_outbound_retries_total",
    "kingfisher_provider_bytes_total",
    "kingfisher_provider_requests_total",
    "kingfisher_responses_total",
    "kingfisher_rpc_duration_seconds",
    "kingfisher_rpc_requests_total",
    "kingfisher_tiles_by_kind_total",
    "kingfisher_tiles_by_mount_total",
}


def declared(text, keyword, family):
    # a histogram declares HELP and TYPE once, under its BASE name; _bucket,
    # _sum and _count are samples of that one family rather than families of
    # their own. asserting a HELP line per sample name is asserting something
    # prometheus does not do.
    if f"# {keyword} {family} " in text:
        return True
    for suffix in ("_bucket", "_sum", "_count"):
        if family.endswith(suffix) and \
                f"# {keyword} {family[:-len(suffix)]} " in text:
            return True
    return False


def families(text):
    # DECLARATIONS. See the CONTRACT comment: sample lines are traffic-dependent
    # and a name that publishes nothing yet is still a name a dashboard is
    # built on.
    return set(re.findall(r"^# TYPE (\S+) ", text, re.M))


def sampled(text):
    # the names that actually carry series, for the zero-render check
    return {line.split("{")[0].split(" ")[0]
            for line in text.splitlines() if line.startswith("kingfisher_")}


# /health and /ready moved to tests/test_health_ready.py when /health stopped
# withholding the mount state. The test that lived here asserted, in prose, that
# it deliberately did not report whether the mounts held anything -- which is no
# longer true, and a test whose comment lies is worse than no test. It also
# passed only because the new wording happens not to contain the word it
# checked for, which is not a property anybody should rely on.


def test_health_survives_a_trailing_slash(get):
    assert get("/health/").status == 200


def test_introspection_does_not_count_itself(get):
    # a widget polling /stats every second must not be able to move the numbers
    # it is displaying. hit all four, then ask what was counted.
    for path in ("/health", "/stats", "/metrics", "/reload"):
        assert get(path).status == 200
    assert get("/stats").json()["requests_total"] == 0


def test_serving_a_tile_does_count(get):
    get("/econ/res8/8b01.json")
    stats = get("/stats").json()
    assert stats["requests_total"] == 1
    assert stats["tiles_total"] == 1
    assert stats["by_mount"] == {"/econ/": 1}
    assert stats["duration"]["count"] == 1


def test_metrics_declares_the_whole_contract(get):
    # NO TRAFFIC FIRST, deliberately. This asserts what a freshly started pod
    # publishes, because that is when a dashboard is most likely to be looked at
    # and least likely to have anything in it. It needs no seeding and no
    # ordering: a declaration is there or it is not.
    resp = get("/metrics")
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert families(resp.text) == CONTRACT


def test_every_family_that_can_publish_at_zero_does(get):
    # The other half. Declaring a family and never emitting a series is how a
    # collector that has quietly stopped collecting looks exactly like a quiet
    # system -- absence must mean breakage, not idleness.
    #
    # DATA_DEPENDENT is the honest exception list: those families take their
    # labels from the event, so there is no zero to publish. Everything else has
    # no excuse.
    body = get("/metrics").text
    have = sampled(body)
    for f in sorted(CONTRACT - DATA_DEPENDENT):
        assert any(n == f or n.startswith(f + "_") for n in have), \
            f"{f} is declared but publishes no series at zero"



def test_metrics_is_parseable_prometheus(get):
    get("/econ/res8/8b01.json")
    text = get("/metrics").text
    # every sample line is "name value", and every family carries HELP and TYPE
    for line in text.splitlines():
        if line.startswith("kingfisher_"):
            assert len(line.rsplit(" ", 1)) == 2
            float(line.rsplit(" ", 1)[1])
    for family in families(text):
        assert declared(text, "HELP", family)
        assert declared(text, "TYPE", family)


def test_metrics_counts_per_mount_and_kind(get):
    get("/econ/res8/8b01.json")
    get("/econ/res8.json")
    text = get("/metrics").text
    assert 'kingfisher_tiles_by_mount_total{mount="/econ/"} 2' in text
    # a chunk under res8/ is kind "res8"; the manifest itself is a manifest
    assert 'kingfisher_tiles_by_kind_total{kind="res8"} 1' in text
    assert 'kingfisher_tiles_by_kind_total{kind="res8-manifest"} 1' in text


def test_reload_bumps_the_generation(get):
    assert get("/stats").json()["generation"] == 1
    body = get("/reload").json()
    assert body["reloaded"] is True
    assert body["generation"] == 2
    assert get("/stats").json()["generation"] == 2


def test_reload_is_safe_to_call_twice(get):
    get("/reload")
    second = get("/reload").json()
    assert second["generation"] == 3
    assert second["mounts"]["/econ/"]["tiles"] == 3


def test_reload_reports_what_is_on_disk(get):
    mounts = get("/reload").json()["mounts"]
    # three chunk files under econ's resN/ directories
    assert mounts["/econ/"]["tiles"] == 3
    assert mounts["/econ/"]["series"] == 13
    assert mounts["/layers/"]["tiles"] == 0


def test_stats_reports_the_mount_table(get):
    stats = get("/stats").json()
    assert set(stats["mounts"]) == {"/econ/", "/layers/", "/broken/"}
    inventory = {e["prefix"]: e for e in stats["inventory"]}
    assert inventory["/econ/"]["resolutions"] == [3, 7, 8]
    # chunks SUM across resolutions: two in res7, one in res8
    assert inventory["/econ/"]["chunks"] == 3
    assert inventory["/econ/"]["series"] == 13


def test_landing_page_renders(get):
    resp = get("/")
    assert resp.status == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>kingfisher</title>" in resp.text
    assert "/econ/" in resp.text


def test_landing_page_truncates_a_long_series_list(get):
    # thirteen series, ten shown
    assert "and 3 more" in get("/").text


def test_landing_page_shows_the_catalogue(get):
    text = get("/").text
    assert "13 series" in text
    assert "stats from res3" in text
    assert "generated 2026-08-01T00:00:00Z" in text


def test_landing_page_lists_failures_once_there_are_some(get):
    assert "none" in get("/").text
    get("/econ/nope.json")
    assert "404 /econ/nope.json" in get("/").text


def test_landing_page_yields_to_a_real_index_html(get, mounted):
    (mounted["viewer"] / "index.html").write_text("<h1>the viewer</h1>")
    resp = get("/")
    assert resp.status == 200
    assert "the viewer" in resp.text
    assert "<title>kingfisher</title>" not in resp.text


def test_a_directory_named_metrics_cannot_shadow_the_counter(get, mounted):
    # /metrics is resolved before the mount table, on purpose
    (mounted["viewer"] / "metrics").mkdir()
    assert "kingfisher_uptime_seconds" in get("/metrics").text


def test_head_returns_headers_and_no_body(get):
    resp = get("/econ/res8/8b01.json", method="HEAD")
    assert resp.status == 200
    assert resp.body == b""
    assert int(resp.headers["content-length"]) > 0


def test_head_on_a_static_root_file(get):
    resp = get("/app.js", method="HEAD")
    assert resp.status == 200
    assert resp.body == b""


def test_landing_page_says_when_a_manifest_was_too_big_to_parse(get):
    # the note used to be appended to `shown` AFTER the row had already
    # interpolated it, so it was computed and thrown away. a warning nobody
    # can see is not a warning.
    serve.MANIFEST_PARSE_CAP = 10
    assert "not parsed, over 10 B: res3, res7, res8" in get("/").text


def test_landing_page_with_no_mounts_at_all(get):
    serve.MOUNTS.clear()
    assert "no mounts declared" in get("/").text


def test_every_metric_the_dashboard_binds_is_in_the_contract():
    """The consumer that actually exists. kube/template/dashboard-kingfisher.json
    is rendered and shipped to grafana by the overlay; a name it binds that this contract does
    not pin is a rename waiting to break a panel with a green suite."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "kube", "template", "dashboard-kingfisher.json")) as f:
        bound = set(re.findall(r"kingfisher_[a-z_]+", f.read()))
    assert bound, "the dashboard binds no kingfisher metrics; is the file where it was?"
    # the contract pins FAMILIES; a dashboard binds SERIES. A histogram or
    # summary family kingfisher_x is bound as kingfisher_x_count / _sum /
    # _bucket, so strip the series suffix before asking whether it is pinned.
    def family(name):
        return re.sub(r"_(bucket|count|sum)$", "", name)
    missing = {n for n in bound if n not in CONTRACT and family(n) not in CONTRACT}
    assert not missing, f"bound by the dashboard, not pinned by the contract: {sorted(missing)}"
