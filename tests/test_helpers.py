import pytest

import serve


@pytest.mark.parametrize("path,expected", [
    # a chunk lives under resN/, and the directory is what names its kind
    ("/econ/res8/8b01.json", "res8"),
    ("/econ/res7/8a2a.json", "res7"),
    # the manifest itself is a different kind of thing from the chunks it lists
    ("/econ/res8.json", "res8-manifest"),
    ("/econ/res3.json", "res3-manifest"),
    # the viewer's own assets are requests, not tiles
    ("/app.js", "other"),
    ("/", "other"),
    # a query string is not part of the kind
    ("/econ/res8/8b01.json?v=3", "res8"),
])
def test_kind_of(path, expected):
    assert serve.kind_of(path) == expected


@pytest.mark.parametrize("value,expected", [
    (None, "&mdash;"),
    (2.5e9, "2.50B"),
    (2e6, "2.00M"),
    (1234, "1,234"),
    (99.456, "99.46"),
    (0, "0"),
    (-2.5e9, "-2.50B"),
    # not a number at all: escaped rather than crashed, because it is going
    # straight into an HTML table
    ("<script>", "&lt;script&gt;"),
])
def test_num(value, expected):
    assert serve.num(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0, "0 B"),
    (512, "512 B"),
    (2048, "2.0 KB"),
    (5 * 1024 ** 2, "5.0 MB"),
    (3 * 1024 ** 3, "3.0 GB"),
    (2 * 1024 ** 4, "2.0 TB"),
    # TB is the last unit, so anything larger stays in TB rather than falling
    # off the end and returning None
    (5000 * 1024 ** 4, "5000.0 TB"),
])
def test_human(value, expected):
    assert serve.human(value) == expected


def test_observe_places_a_fast_request_in_the_right_bucket():
    serve.observe(0.002, "res8")
    assert serve.DUR["buckets"][0.0025] == 1
    assert serve.DUR["buckets"][0.001] == 0
    assert serve.DUR["count"] == 1
    assert serve.DUR["by_kind"]["res8"]["count"] == 1


def test_observe_puts_a_slow_request_in_the_overflow():
    # past a second means something is wrong rather than busy
    serve.observe(30.0, "other")
    assert serve.DUR["inf"] == 1
    assert sum(serve.DUR["buckets"].values()) == 0


def test_record_separates_tiles_from_requests():
    serve.record("/app.js", None, 200, 100)
    serve.record("/econ/res8/8b01.json", "/econ/", 200, 900)
    assert serve.STATS["requests_total"] == 2
    assert serve.STATS["bytes_total"] == 1000
    # only the one served out of a mount is a tile
    assert serve.STATS["tiles_total"] == 1
    assert serve.STATS["tile_bytes_total"] == 900


def test_record_truncates_an_absurd_path():
    serve.record("/econ/" + "a" * 500, "/econ/", 404, 0)
    key = next(iter(serve.STATS["error_paths"]))
    # the status, a space, then at most 200 characters of path
    assert len(key) <= 204
