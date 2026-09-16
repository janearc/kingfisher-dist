# api.py -- the daemons' shared /api backing (ingestd, openskyd). Mirrors
# test_api.py's coverage of serve.py's own descriptor, for the SEPARATE
# descriptor-daemons.binpb that publishes what the daemons consume.

import os

import api


def _committed():
    with open(os.path.join(os.path.dirname(os.path.abspath(api.__file__)),
                            "descriptor-daemons.binpb"), "rb") as f:
        return f.read()


def test_descriptor_bytes_matches_the_committed_file():
    body = api.descriptor_bytes()
    assert body == _committed()
    assert len(body) > 0
    assert b"flipr/v1/flipr.proto" in body


def test_descriptor_bytes_reads_disk_once(monkeypatch):
    # monkeypatch, not raw assignment: this module-level cache is a SHARED
    # singleton every other test file's import of `api` sees too, and a
    # raw assignment here leaked past this test and poisoned ingestd's and
    # openskyd's /api tests when the full suite ran -- found by running the
    # full suite, not this file alone.
    monkeypatch.setattr(api, "_cache", None)
    monkeypatch.setattr(api, "_tried", False)
    first = api.descriptor_bytes()
    monkeypatch.setattr(api, "DESCRIPTOR_PATH", "/nonexistent/path/wont/matter")
    second = api.descriptor_bytes()
    assert second is first  # cached, never re-read after the first call


def test_missing_descriptor_reads_as_none_and_is_cached(monkeypatch):
    monkeypatch.setattr(api, "_cache", None)
    monkeypatch.setattr(api, "_tried", False)
    monkeypatch.setattr(api, "DESCRIPTOR_PATH", "/nonexistent/path/really")
    assert api.descriptor_bytes() is None
    assert api._tried is True
    assert api.descriptor_bytes() is None  # still None, no re-stat needed to prove it
