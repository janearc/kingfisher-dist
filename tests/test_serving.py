import http.client
import os
import urllib.parse

import pytest

import serve


def raw_get(base, path):
    # http.client sends the request target verbatim. urllib normalises "../"
    # away before it ever leaves the process, which would quietly make the
    # traversal test pass for the wrong reason.
    parts = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_serves_a_chunk_out_of_a_mount(get):
    resp = get("/econ/res8/8b01.json")
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {"cells": {"8b01": 3}}
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert resp.headers["etag"]
    assert resp.headers["last-modified"]


def test_serves_a_manifest(get):
    body = get("/econ/res7.json").json()
    assert len(body["chunks"]) == 2
    assert "series_00" in body["layers"]


def test_missing_file_under_a_mount_is_404_with_a_reason(get):
    resp = get("/econ/res9/nope.json")
    assert resp.status == 404
    # loud and structured: a viewer showing an empty picker because a mount is
    # missing is worse than one that says so
    assert "no such file under mount" in resp.json()["error"]


def test_traversal_out_of_a_mount_is_refused(server):
    status, body = raw_get(server, "/econ/../../../../etc/passwd")
    assert status == 403
    assert b"escapes its mount" in body


def test_symlink_out_of_a_mount_is_refused(server, mounted):
    # normpath cannot catch this one -- only realpath can, which is why the
    # check is done after resolving rather than on the string
    outside = mounted["tmp"] / "secret.json"
    outside.write_text('{"not":"yours"}')
    os.symlink(outside, mounted["econ"] / "escape.json")
    status, body = raw_get(server, "/econ/escape.json")
    assert status == 403
    assert b"escapes its mount" in body


def test_mount_root_returns_a_json_index(get):
    # /layers/ carries no manifest, so this index is the only way a client can
    # discover what is in it
    resp = get("/layers/")
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/json"
    body = resp.json()
    assert body["mount"] == "/layers/"
    assert body["manifest"] is None
    assert body["directories"] == ["sub/"]
    assert [f["name"] for f in body["files"]] == ["world.json"]
    assert body["files"][0]["bytes"] > 0


def test_mount_root_index_names_the_manifest_when_there_is_one(get):
    body = get("/econ/").json()
    assert body["manifest"] == "res3.json"
    assert body["directories"] == ["res7/", "res8/"]


def test_subdirectory_listing_is_off(get):
    # only the mount ROOT is listable. a general autoindex over map data is a
    # different and much larger promise.
    resp = get("/econ/res8/")
    assert resp.status == 403
    assert "directory listing is off" in resp.json()["error"]


def test_index_is_not_counted_as_a_tile(get):
    get("/layers/")
    stats = get("/stats").json()
    assert stats["requests_total"] == 1
    assert stats["tiles_total"] == 0


def test_static_root_is_served_and_counted(get):
    resp = get("/app.js")
    assert resp.status == 200
    assert b"viewer" in resp.body
    stats = get("/stats").json()
    # a request, but not a tile: the viewer's own assets are not map data
    assert stats["requests_total"] == 1
    assert stats["tiles_total"] == 0
    assert stats["bytes_total"] > 0


def test_unmounted_missing_path_is_a_404_from_the_static_root(get):
    resp = get("/no-such-asset.js")
    assert resp.status == 404
    assert get("/stats").json()["errors_total"] == 1


def test_error_paths_are_recorded_with_their_status(get):
    get("/econ/missing-a.json")
    get("/econ/missing-a.json")
    get("/econ/missing-b.json")
    paths = get("/stats").json()["error_paths"]
    assert paths["404 /econ/missing-a.json"] == 2
    assert paths["404 /econ/missing-b.json"] == 1


def test_error_path_cardinality_is_capped(get, monkeypatch):
    # an attacker walking random URLs must not be able to grow this dict without
    # bound. the cap is the whole point; lower it so the test is quick.
    monkeypatch.setattr(serve, "ERROR_PATH_CAP", 3)
    for i in range(10):
        get(f"/econ/missing-{i}.json")
    stats = get("/stats").json()
    assert len(stats["error_paths"]) == 3
    assert stats["error_paths_capped"] == 7


def test_a_failure_to_list_a_mount_root_is_a_500(get, monkeypatch):
    real = serve.os.listdir

    def boom(path, *a, **kw):
        if path.endswith("layers"):
            raise OSError("disk went away")
        return real(path, *a, **kw)

    monkeypatch.setattr(serve.os, "listdir", boom)
    resp = get("/layers/")
    assert resp.status == 500
    assert "cannot list /layers/" in resp.json()["error"]


def test_a_file_that_vanishes_after_the_check_is_a_500(get, monkeypatch):
    # the genuine race: isfile() says yes, and the file is gone by the time
    # stat() runs. patching os.stat outright is too blunt -- isdir() and
    # isfile() go through it too, so the error fires before the try that is
    # meant to catch it and the client just sees the connection drop.
    real_isfile = serve.os.path.isfile
    real_stat = serve.os.stat
    armed = {"yet": False}

    def isfile(path, *a, **kw):
        result = real_isfile(path, *a, **kw)
        if str(path).endswith("8b01.json"):
            armed["yet"] = True
        return result

    def stat(path, *a, **kw):
        if armed["yet"] and str(path).endswith("8b01.json"):
            raise FileNotFoundError("removed between the check and the open")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(serve.os.path, "isfile", isfile)
    monkeypatch.setattr(serve.os, "stat", stat)
    resp = get("/econ/res8/8b01.json")
    assert resp.status == 500
    assert "FileNotFoundError" in resp.json()["error"]


def test_a_mount_that_is_not_a_directory_is_still_answerable(get, mounted):
    # a mount pointed at a path that vanished. requests 404 rather than crashing
    # the process -- serving three of four datasets beats refusing to start.
    serve.MOUNTS["/gone/"] = str(mounted["tmp"] / "never-existed")
    assert get("/gone/anything.json").status == 404
