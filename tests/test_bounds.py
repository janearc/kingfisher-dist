# Where a dataset IS. Recorded at ingest, backfilled for the shelf that
# predates it, and the reason both exist: without bounds the only way to learn
# where a tile sits is to download it, so "what overlaps my window" costs the
# whole shelf. dodo's map pane was OOM-killed doing exactly that on
# 2026-09-01 -- 2.1M cells against a 256Mi limit.

import json
import os

import pytest

pytest.importorskip("h3")

import h3  # noqa: E402

import ingestd  # noqa: E402
from hexify import bbox_overlaps, bounds_of  # noqa: E402


def _cells(lat0=37.80, lon0=-122.40, n=4, step=0.01, res=9):
    return [h3.latlng_to_cell(lat0 + i * step, lon0 + j * step, res)
            for i in range(n) for j in range(n)]


# ---------------------------------------------------------------------------
# bounds_of


def test_bounds_contain_every_cell():
    cells = _cells()
    w, s, e, n = bounds_of(cells, h3)
    for c in cells:
        lat, lon = h3.cell_to_latlng(c)
        assert w <= lon <= e
        assert s <= lat <= n


def test_bounds_of_nothing_is_none():
    assert bounds_of([], h3) is None


def test_a_straddling_set_refuses_rather_than_lying():
    """A set spanning the antimeridian would report a box the width of the
    world -- plausible, wrong, and it would pull every dataset into every
    query. Absent and obviously so beats that."""
    cells = [h3.latlng_to_cell(0.0, 179.0, 4), h3.latlng_to_cell(0.0, -179.0, 4)]
    assert bounds_of(cells, h3) is None


def test_a_single_cell_has_bounds():
    b = bounds_of([h3.latlng_to_cell(37.8, -122.4, 9)], h3)
    assert b is not None and b[0] <= b[2] and b[1] <= b[3]


# ---------------------------------------------------------------------------
# bbox_overlaps


def test_overlap_is_true_for_a_box_against_itself():
    b = bounds_of(_cells(), h3)
    assert bbox_overlaps(b, b)


def test_distant_boxes_do_not_overlap():
    sf = bounds_of(_cells(), h3)
    ny = bounds_of(_cells(lat0=40.70, lon0=-74.00), h3)
    assert not bbox_overlaps(sf, ny)


def test_touching_edges_count_as_overlap():
    # a tile whose northern edge is the window's southern edge does hold
    # cells on that line; excluding it drops a row of data at every seam
    assert bbox_overlaps([0, 0, 1, 1], [1, 1, 2, 2])


def test_a_missing_box_never_overlaps():
    # bounds are None for a straddling or empty set; None must not match
    # everything, which is the failure that makes the filter pointless
    assert not bbox_overlaps(None, [0, 0, 1, 1])
    assert not bbox_overlaps([0, 0, 1, 1], None)


# ---------------------------------------------------------------------------
# the backfill


def _shelve(root, name, cells, bbox="omit", content="cells.json"):
    """A dataset dir shaped like ingestd writes them."""
    d = os.path.join(root, name)
    os.makedirs(d)
    pipeline = {"res": 9, "cells": len(cells)}
    if bbox != "omit":
        pipeline["bbox"] = bbox
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"id": name, "content": content, "pipeline": pipeline}, f)
    if content == "cells.json":
        with open(os.path.join(d, "cells.json"), "w") as f:
            json.dump({"res": 9, "cells": {c: {"height": 1.0} for c in cells}}, f)
    return d


def test_backfill_records_bounds_for_an_old_dataset(tmp_path):
    root = str(tmp_path)
    _shelve(root, "usgs-old", _cells())
    assert ingestd.backfill_bounds(out=root) == 1
    m = json.load(open(os.path.join(root, "usgs-old", "manifest.json")))
    assert m["pipeline"]["bbox"] is not None
    assert len(m["pipeline"]["bbox"]) == 4


def test_backfill_leaves_the_rest_of_the_manifest_alone(tmp_path):
    root = str(tmp_path)
    _shelve(root, "usgs-old", _cells())
    before = json.load(open(os.path.join(root, "usgs-old", "manifest.json")))
    ingestd.backfill_bounds(out=root)
    after = json.load(open(os.path.join(root, "usgs-old", "manifest.json")))
    before["pipeline"].pop("bbox", None)
    after["pipeline"].pop("bbox", None)
    assert before == after


def test_backfill_is_idempotent(tmp_path):
    """Costs one manifest read per dataset once the shelf is done, and never
    rewrites anything again."""
    root = str(tmp_path)
    _shelve(root, "usgs-old", _cells())
    assert ingestd.backfill_bounds(out=root) == 1
    assert ingestd.backfill_bounds(out=root) == 0


def test_backfill_respects_its_cap(tmp_path):
    # the cap is what keeps ingestion from waiting behind the debt
    root = str(tmp_path)
    for i in range(5):
        _shelve(root, f"usgs-{i}", _cells(lat0=37.0 + i))
    assert ingestd.backfill_bounds(limit=2, out=root) == 2
    assert ingestd.backfill_bounds(limit=2, out=root) == 2
    assert ingestd.backfill_bounds(limit=2, out=root) == 1


def test_backfill_skips_datasets_that_are_not_cells(tmp_path):
    # imagery has no cells and therefore no bounds to take
    root = str(tmp_path)
    d = os.path.join(root, "gibs-image")
    os.makedirs(d)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"id": "gibs-image", "content": "content.jpg"}, f)
    assert ingestd.backfill_bounds(out=root) == 0


def test_backfill_survives_a_broken_dataset(tmp_path):
    """One unreadable dataset must not stop the shelf being backfilled --
    it is the whole shelf being walked, and there are 400 of them."""
    root = str(tmp_path)
    bad = _shelve(root, "usgs-bad", _cells())
    with open(os.path.join(bad, "cells.json"), "w") as f:
        f.write("{not json")
    _shelve(root, "usgs-good", _cells(lat0=38.0))
    assert ingestd.backfill_bounds(out=root) == 1
    m = json.load(open(os.path.join(root, "usgs-good", "manifest.json")))
    assert m["pipeline"]["bbox"] is not None


def test_backfill_of_a_missing_root_is_not_a_crash(tmp_path):
    assert ingestd.backfill_bounds(out=str(tmp_path / "nope")) == 0


def test_a_global_set_gets_global_bounds_not_none():
    """GLOBAL IS NOT STRADDLING. A hexed world snapshot spans the full 360, and
    refusing it as an antimeridian case gave the one dataset that overlaps
    every window no bounds at all -- so it was excluded from every query while
    the lidar beside it drew fine."""
    cells = [h3.latlng_to_cell(lat, lon, 2)
             for lat in range(-80, 81, 20) for lon in range(-180, 180, 20)]
    b = bounds_of(cells, h3)
    assert b is not None
    assert b[0] == -180.0 and b[2] == 180.0


def test_a_global_box_overlaps_every_window():
    cells = [h3.latlng_to_cell(lat, lon, 2)
             for lat in range(-80, 81, 20) for lon in range(-180, 180, 20)]
    world = bounds_of(cells, h3)
    assert bbox_overlaps(world, [-122.5, 37.7, -122.3, 37.9])
    assert bbox_overlaps(world, [-74.3, 40.4, -73.6, 41.0])


def test_the_ambiguous_span_is_still_refused():
    """Between a strip at each edge and the whole world lies a span nothing
    here produces -- and that is the case to refuse rather than approximate."""
    cells = [h3.latlng_to_cell(0.0, lon, 2) for lon in (-170, -60, 170)]
    assert bounds_of(cells, h3) is None
