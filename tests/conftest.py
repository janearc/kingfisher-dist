import os

# the two daemons refuse to import without a contact for the providers
# they call, which is right for a daemon and wrong for a test that never
# calls anyone. a reserved example address satisfies the check and can
# reach nothing.
os.environ.setdefault("KINGFISHER_CONTACT", "a test <test@example.com>")

import copy
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import serve

# serve.py keeps its counters in module-level dicts, which is fine for a process
# that lives as long as the pod and awful for a test suite running in one
# interpreter. Snapshot the pristine values once, at import, before any test has
# had a chance to move them.
PRISTINE = {
    "STATS": copy.deepcopy(serve.STATS),
    "DUR": copy.deepcopy(serve.DUR),
    "INFLIGHT": copy.deepcopy(serve.INFLIGHT),
    "GENERATION": copy.deepcopy(serve.GENERATION),
    "INV_CACHE": copy.deepcopy(serve.INV_CACHE),
}


@pytest.fixture(autouse=True)
def reset_state():
    # every test starts from zeroed counters and an empty mount table, so an
    # assertion on "requests_total == 1" means this test's one request
    for name, value in PRISTINE.items():
        getattr(serve, name).clear()
        getattr(serve, name).update(copy.deepcopy(value))
    serve.MOUNTS.clear()
    root_before, cap_before = serve.ROOT, serve.MANIFEST_PARSE_CAP
    yield
    serve.MOUNTS.clear()
    serve.ROOT = root_before
    serve.MANIFEST_PARSE_CAP = cap_before


def _manifest(series, chunks=(), generated_at="2026-08-01T00:00:00Z", cells=4096):
    # the shape _walk_inventory reads: a layers dict keyed by series name, and a
    # chunks list whose LENGTH is all anyone counts
    return {
        "generated_at": generated_at,
        "cells": cells,
        "layers": series,
        "chunks": list(chunks),
    }


def _series_block(n):
    # twelve series, because the landing page truncates the list at ten and the
    # "and N more" branch only runs when there are more than ten
    out = {}
    for i in range(n):
        out[f"series_{i:02d}"] = {
            "kind": "quantity",
            "unit": "USD",
            "frame": "acs",
            # a period on most, a bare year on one: serve.py formats them
            # differently and both paths should be walked
            "vintage": {"period": "2019-2023"} if i else {"year": 2021},
            "domain": {"min": 1.5, "max": 2.5e9, "p50": 512.0, "n": 1_200_000},
            "reliability": {"mute_above": 0.3},
        }
    # a series declared with no metadata at all. serve.py guards this with
    # `layers[name] or {}` and the guard should be exercised, not assumed.
    out["series_null"] = None
    return out


@pytest.fixture
def mapdata(tmp_path):
    # a small tree shaped like the real thing: manifests at several resolutions,
    # chunks under resN/, a mount with no manifest, and one that will not parse
    econ = tmp_path / "econ"
    (econ / "res7").mkdir(parents=True)
    (econ / "res8").mkdir(parents=True)

    # res3 is not chunked. it is found first, so it is the one the catalogue
    # comes from -- which is exactly the bug the "chunks SUM" comment describes.
    (econ / "res3.json").write_text(json.dumps(_manifest(_series_block(12))))
    (econ / "res7.json").write_text(
        json.dumps(_manifest(_series_block(12), chunks=["res7/a", "res7/b"])))
    (econ / "res8.json").write_text(
        json.dumps(_manifest(_series_block(12), chunks=["res8/a"])))
    (econ / "res7" / "8a2a.json").write_text('{"cells":{"8a2a":1}}')
    (econ / "res7" / "8a2b.json").write_text('{"cells":{"8a2b":2}}')
    (econ / "res8" / "8b01.json").write_text('{"cells":{"8b01":3}}')
    # a file in the mount root that is not a tile: counted in files/bytes, not
    # in tiles/tile_bytes
    (econ / "notes.txt").write_text("not a tile")

    # no manifest here at all. this is /layers/ and /rides/ in production, and
    # the JSON directory index is the only way a client can discover it.
    layers = tmp_path / "layers"
    (layers / "sub").mkdir(parents=True)
    (layers / "world.json").write_text('{"type":"FeatureCollection"}')
    (layers / "sub" / "more.json").write_text("{}")

    # a manifest that is present and unreadable. inventory() records the
    # exception on the entry rather than failing the whole walk.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "res3.json").write_text("{this is not json")

    # what gets served at / -- deliberately without an index.html, so the
    # landing page renders instead of a static file
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    (viewer / "app.js").write_text("console.log('viewer')\n")

    return {
        "tmp": tmp_path,
        "econ": econ,
        "layers": layers,
        "broken": broken,
        "viewer": viewer,
    }


@pytest.fixture
def bench_sheet(mapdata, monkeypatch):
    # the suite must not care whether bench-ui happens to be installed on the
    # machine running it. the laptop default exists on one machine and
    # does not exist in CI, and a test that renders differently in the two
    # places is not a test.
    sheet = mapdata["tmp"] / "mesh-ui" / "bench.css"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_text(
        ":root{--ground:#0b0e14;--ink:#e6edf7;--dim:#8b98ad;--faint:#5f6b7e;"
        "--rule-strong:#2b3648;--rule-faint:#1d2533;--amber:#e8a33d;"
        "--mono:monospace;--fs-min:17px;--fs-sm:18px}\n"
        "/* test double for bench-ui */\n")
    monkeypatch.setenv("KINGFISHER_BENCH_CSS", str(sheet))
    return sheet


@pytest.fixture
def mounted(mapdata, bench_sheet):
    # the mount table under test, matching the deployment's shape
    serve.MOUNTS.update({
        "/econ/": str(mapdata["econ"]),
        "/layers/": str(mapdata["layers"]),
        "/broken/": str(mapdata["broken"]),
    })
    serve.ROOT = str(mapdata["viewer"])
    return mapdata


@pytest.fixture
def server(mounted):
    # the real Server on a real socket. port 0 lets the kernel pick, so a
    # suite running beside a live kingfisher cannot collide with it.
    httpd = serve.Server(("127.0.0.1", 0), serve.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


def fetch(base, path, headers=None, method="GET", data=None):
    # urllib raises on anything outside 2xx, including the 304 we very much
    # want to assert on, so every status comes back through one door
    req = urllib.request.Request(base + path, method=method, data=data)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return Response(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers), exc.read())


@pytest.fixture
def get(server):
    def _get(path, headers=None, method="GET", data=None):
        return fetch(server, path, headers=headers, method=method, data=data)
    return _get
