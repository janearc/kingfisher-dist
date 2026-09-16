# /api -- the service publishes its own contract.
#
# The invariant under test: the bytes /api serves are EXACTLY the committed
# descriptor.binpb, which buf built from proto/. Byte-equality is the whole
# check -- a served descriptor that differs from the committed one is the
# spec drifting from the repo, which is the failure /api exists to prevent.

import os

import serve


def _committed():
    path = os.path.join(os.path.dirname(os.path.abspath(serve.__file__)),
                        "descriptor.binpb")
    with open(path, "rb") as f:
        return f.read()


def test_api_serves_committed_descriptor_bytes(get):
    resp = get("/api")
    assert resp.status == 200
    assert resp.body == _committed()
    assert len(resp.body) > 0
    assert "protobuf" in resp.headers.get("content-type", "")


def test_api_missing_descriptor_is_a_loud_503(get, monkeypatch):
    # a deploy without the descriptor is a defect the endpoint must NAME,
    # not a quiet 404 that reads as "no such route"
    monkeypatch.setitem(serve._DESCRIPTOR, "read", True)
    monkeypatch.setitem(serve._DESCRIPTOR, "bytes", None)
    resp = get("/api")
    assert resp.status == 503
    assert b"descriptor.binpb" in resp.body


def test_api_reads_disk_once(get):
    get("/api")
    assert serve._DESCRIPTOR["read"] is True
    first = serve._DESCRIPTOR["bytes"]
    get("/api")
    assert serve._DESCRIPTOR["bytes"] is first


def test_descriptor_missing_on_disk_reads_as_none(monkeypatch, tmp_path):
    # exercise the real OSError path: point the module at a directory with no
    # descriptor and let the open() fail, asserting the answer -- including
    # "not there" -- is cached so a broken deploy is not re-statted per request
    monkeypatch.setitem(serve._DESCRIPTOR, "read", False)
    monkeypatch.setitem(serve._DESCRIPTOR, "bytes", None)
    monkeypatch.setattr(serve, "__file__", str(tmp_path / "serve.py"))
    assert serve.descriptor_bytes() is None
    assert serve._DESCRIPTOR["read"] is True
