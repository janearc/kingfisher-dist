# The running commit, and the headers that name kingfisher on every response.
#
# hygiene.py roots every source-derived check on the commit /health reports;
# without one there is no source to read and every verdict is UNKNOWN. Until
# 2026-09-04 kingfisher carried none, and the fleet ran an image nobody could
# name from git -- which is how a review read HEAD beside a live service that
# was a different program with no way to tell (K5).
#
# Issue 75 is the other half: the only server-identifying header on a tile was
# Python's default, so a consumer could not tell a kingfisher tile from a
# fallback's or a cache's, and the generation lived only inside the ETag.

import os
import re

import pytest

import serve
import version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hdr(resp, name):
    # http.client preserves the server's header casing; be indifferent to it
    for k, v in resp.headers.items():
        if k.lower() == name.lower():
            return v
    return None


# ---------------------------------------------------------------------------
# the commit on /health


def test_health_carries_the_commit_from_the_environment(get, monkeypatch):
    monkeypatch.setenv("KINGFISHER_COMMIT", "abc1234")
    body = get("/health").json()
    assert body["commit"] == "abc1234"


def test_an_unset_commit_is_named_unknown_not_blank(get, monkeypatch):
    """A bare `docker build` gets "unknown", and hygiene fails it on purpose.
    Blank would be a missing field, which reads as an older kingfisher."""
    monkeypatch.delenv("KINGFISHER_COMMIT", raising=False)
    assert get("/health").json()["commit"] == "unknown"


def test_the_commit_is_read_when_asked_not_at_import(monkeypatch):
    # so build order and import order cannot bake a stale value in
    monkeypatch.setenv("KINGFISHER_COMMIT", "first00")
    assert version.commit() == "first00"
    monkeypatch.setenv("KINGFISHER_COMMIT", "second1")
    assert version.commit() == "second1"


# ---------------------------------------------------------------------------
# every response names kingfisher (issue 75)


@pytest.mark.parametrize("path", ["/health", "/econ/", "/definitely-not-here.json", "/api"])
def test_every_response_carries_a_kingfisher_server_header(get, monkeypatch, path):
    """Tiles, indexes, 404s and the contract door all exit through the same
    handler, so they all say who served them -- not `SimpleHTTP/0.6`."""
    monkeypatch.setenv("KINGFISHER_COMMIT", "cafe123")
    resp = get(path)
    assert _hdr(resp, "server") == "kingfisher/cafe123", (path, dict(resp.headers))


@pytest.mark.parametrize("path", ["/health", "/econ/", "/definitely-not-here.json"])
def test_every_response_carries_the_generation(get, path):
    """The generation that invalidates caches used to live only inside a
    tile's ETag. A cache should not have to parse an ETag to learn whether
    kingfisher reloaded."""
    g = _hdr(get(path), "x-kingfisher-generation")
    assert g is not None and g.isdigit(), path


def test_the_generation_header_moves_on_reload(get):
    before = int(_hdr(get("/health"), "x-kingfisher-generation"))
    get("/reload")
    after = int(_hdr(get("/health"), "x-kingfisher-generation"))
    assert after > before


def test_a_static_404_is_json_like_a_mount_404(get):
    """A missing file under the static root answered text/html while one
    under a mount answered JSON. One shape, so a consumer parses one thing."""
    resp = get("/definitely-not-here.json")
    assert resp.status == 404
    assert (_hdr(resp, "content-type") or "").startswith("application/json")
    body = resp.json()
    assert body["status"] == 404 and "error" in body


def test_no_python_version_leaks_in_the_server_header(get):
    # the default sys_version appends "Python/3.x"; the token is ours alone
    assert "Python" not in (_hdr(get("/health"), "server") or "")


# ---------------------------------------------------------------------------
# the plumbing that gets the sha into the image


def test_the_dockerfile_declares_the_commit_as_arg_and_env():
    with open(os.path.join(ROOT, "Dockerfile")) as f:
        text = f.read()
    assert re.search(r"^ARG KINGFISHER_COMMIT=", text, re.M)
    assert re.search(r"^ENV KINGFISHER_COMMIT=\$KINGFISHER_COMMIT", text, re.M)


def test_the_build_script_passes_the_short_sha():
    """A short sha, not a version string: hygiene treats the value as a git
    ref and reads the source at that commit. "dev" would fail that on
    purpose, and a bare docker build would produce it."""
    with open(os.path.join(ROOT, "bin", "build.sh")) as f:
        text = f.read()
    assert "git rev-parse --short HEAD" in text
    assert '--build-arg "KINGFISHER_COMMIT=$sha"' in text
    assert "git status --porcelain" in text, "must refuse to label a dirty tree"


def test_the_readme_builds_through_the_script():
    with open(os.path.join(ROOT, "OPERATION.md")) as f:
        assert "bin/build.sh" in f.read()


@pytest.mark.parametrize("daemon", ["gibsd.py", "openskyd.py", "weatherd.py", "ingestd.py"])
def test_every_daemon_reports_the_commit_on_health(daemon):
    """Pinned statically: each daemon builds its /health body in one place,
    and hygiene needs the same value from all five workloads."""
    with open(os.path.join(ROOT, daemon)) as f:
        text = f.read()
    assert "import version" in text, daemon
    assert '"commit": version.commit()' in text, daemon
