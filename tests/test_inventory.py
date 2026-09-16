import json
import os
import time

import serve


def walk(mounted):
    return {e["prefix"]: e for e in serve.inventory(force=True)}


def test_resolutions_are_read_from_the_manifests_on_disk(mounted):
    econ = walk(mounted)["/econ/"]
    # declared nowhere; discovered by looking. it cannot drift from the disk.
    assert econ["resolutions"] == [3, 7, 8]


def test_chunks_sum_across_resolutions(mounted):
    # the bug this pins: reading only the first manifest reported 0 chunks for
    # econ, because the first one found is res3 and res3 is not chunked
    assert walk(mounted)["/econ/"]["chunks"] == 3


def test_the_series_list_comes_from_the_first_manifest(mounted):
    econ = walk(mounted)["/econ/"]
    assert econ["catalogue_res"] == 3
    assert len(econ["series"]) == 13
    assert econ["series"][0] == "series_00"


def test_the_catalogue_carries_the_metadata_from_the_manifest(mounted):
    catalogue = {c["name"]: c for c in walk(mounted)["/econ/"]["catalogue"]}
    assert catalogue["series_01"]["unit"] == "USD"
    assert catalogue["series_01"]["vintage"] == "2019-2023"
    # a bare year is stringified rather than dropped
    assert catalogue["series_00"]["vintage"] == "2021"
    assert catalogue["series_01"]["mute_above"] == 0.3


def test_a_series_declared_with_no_metadata_does_not_break_the_walk(mounted):
    catalogue = {c["name"]: c for c in walk(mounted)["/econ/"]["catalogue"]}
    assert catalogue["series_null"]["unit"] == ""
    assert catalogue["series_null"]["min"] is None


def test_tiles_are_counted_separately_from_files(mounted):
    econ = walk(mounted)["/econ/"]
    # three chunks under resN/, plus three manifests and a notes.txt
    assert econ["tiles"] == 3
    assert econ["files"] == 7
    assert econ["tile_bytes"] < econ["bytes"]


def test_a_mount_with_no_manifest_reports_no_series(mounted):
    layers = walk(mounted)["/layers/"]
    assert layers["series"] is None
    assert layers["resolutions"] == []
    assert layers["files"] == 2


def test_an_unparseable_manifest_is_recorded_not_raised(mounted):
    broken = walk(mounted)["/broken/"]
    # the walk keeps going. one bad dataset must not take out the inventory for
    # every other mount.
    assert "JSONDecodeError" in broken["error"]
    assert broken["resolutions"] == [3]


def test_a_manifest_over_the_cap_is_counted_but_not_parsed(mounted):
    # this was an OOMKill, not a tuning problem: parsing every resN.json at
    # startup, with a 38.3MB res6 among them, is exit 137 in a 256Mi container.
    serve.MANIFEST_PARSE_CAP = 10
    econ = walk(mounted)["/econ/"]
    assert econ["manifests_skipped"] == ["res3", "res7", "res8"]
    # still counted as published resolutions: existence is a stat, not a parse
    assert econ["resolutions"] == [3, 7, 8]
    assert econ["series"] is None


def test_a_manifest_that_cannot_be_sized_is_skipped(mounted, monkeypatch):
    real = os.path.getsize

    def boom(path, *a, **kw):
        if str(path).endswith("res7.json"):
            raise OSError("vanished mid-walk")
        return real(path, *a, **kw)

    monkeypatch.setattr(serve.os.path, "getsize", boom)
    econ = walk(mounted)["/econ/"]
    assert econ["resolutions"] == [3, 7, 8]
    # res7's two chunks are not added, because res7 was never opened
    assert econ["chunks"] == 1


def test_a_file_that_vanishes_mid_walk_is_skipped(mounted, monkeypatch):
    real = os.path.getsize

    def boom(path, *a, **kw):
        if str(path).endswith("8a2a.json"):
            raise OSError("vanished mid-walk")
        return real(path, *a, **kw)

    monkeypatch.setattr(serve.os.path, "getsize", boom)
    econ = walk(mounted)["/econ/"]
    assert econ["tiles"] == 2
    assert econ["files"] == 6


def test_the_inventory_is_cached(mounted):
    first = serve.inventory(force=True)
    (mounted["econ"] / "res8" / "extra.json").write_text("{}")
    # prometheus scrapes every 30s and the walk is half a gigabyte of small
    # JSON. within the TTL, the cached answer is the answer.
    assert serve.inventory() is first


def test_force_rewalks(mounted):
    serve.inventory(force=True)
    (mounted["econ"] / "res8" / "extra.json").write_text("{}")
    assert serve.inventory(force=True)[1]["tiles"] == 4


def test_an_expired_cache_rewalks(mounted):
    serve.inventory(force=True)
    (mounted["econ"] / "res8" / "extra.json").write_text("{}")
    serve.INV_CACHE["at"] = time.time() - serve.INV_TTL - 1
    assert serve.inventory()[1]["tiles"] == 4
