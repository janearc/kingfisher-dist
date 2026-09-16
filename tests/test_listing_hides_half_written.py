# Issue 80: half-written names appeared in mount listings.
#
# ingestd builds <dataset>.tmp/ and renames it; gibsd writes manifest.json.tmp
# and renames it; the fetcher downloads to <id>.part. All correct for readers of
# the final path -- and all visible in the directory listing while they existed.

import os


def test_temporary_names_are_not_listed(mounted, get):
    econ = str(mounted["econ"])
    os.makedirs(os.path.join(econ, "ds-half.tmp"))
    os.makedirs(os.path.join(econ, "ds-good"))
    open(os.path.join(econ, "manifest.json.tmp"), "w").write("{")
    open(os.path.join(econ, "arriving.part"), "wb").write(b"half")
    open(os.path.join(econ, "whole.json"), "w").write("{}")

    body = get("/econ/").json()
    assert "ds-good/" in body["directories"]
    assert "ds-half.tmp/" not in body["directories"]
    names = {f["name"] for f in body["files"]}
    assert "whole.json" in names
    assert "manifest.json.tmp" not in names
    assert "arriving.part" not in names


def test_a_finished_rename_appears(mounted, get):
    # the complement: the moment the rename lands, the final name is listed
    econ = str(mounted["econ"])
    os.makedirs(os.path.join(econ, "ds.tmp"))
    assert "ds/" not in get("/econ/").json()["directories"]
    os.rename(os.path.join(econ, "ds.tmp"), os.path.join(econ, "ds"))
    assert "ds/" in get("/econ/").json()["directories"]
