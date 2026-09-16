# The priced door. A caller names a window and a budget; kingfisher picks the
# resolution and folds to fit.
#
# The bug this replaces: the shelf door offered exactly one answer -- every
# cell of every dataset at full resolution -- so an unbounded request was the
# only request there was. dodo's map pane made it on 2026-09-01 and was
# OOM-killed by the reply. Every case here pins a way that could come back.

import json
import os

import pytest

pytest.importorskip("h3")

import h3  # noqa: E402

import serve  # noqa: E402
import shelf  # noqa: E402


def _tile(root, name, lat0, lon0, n=6, res=9, density=10.0, height=100.0):
    """A dataset dir shaped exactly like ingestd writes one."""
    d = os.path.join(root, name)
    os.makedirs(d)
    cells = {}
    for i in range(n):
        for j in range(n):
            c = h3.latlng_to_cell(lat0 + i * 0.004, lon0 + j * 0.004, res)
            cells[c] = {"elevation_surface": height, "point_density": density}
    from hexify import bounds_of
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({
            "id": name, "title": name, "content": "cells.json",
            "layers": {
                "elevation_surface": {"kind": "LAYER_KIND_INTENSIVE"},
                "point_density": {"kind": "LAYER_KIND_EXTENSIVE"},
            },
            "pipeline": {"res": res, "cells": len(cells),
                         "bbox": bounds_of(cells, h3)},
        }, f)
    with open(os.path.join(d, "cells.json"), "w") as f:
        json.dump({"res": res, "cells": cells}, f)
    return len(cells)


@pytest.fixture
def barn(tmp_path):
    """Two tiles in the Bay and one in New York -- the shape of the real
    shelf, and the reason a window matters."""
    root = str(tmp_path / "ingested")
    os.makedirs(root)
    a = _tile(root, "usgs-sf-a", 37.80, -122.44)
    b = _tile(root, "usgs-sf-b", 37.80, -122.41, height=200.0)
    c = _tile(root, "usgs-ny", 40.70, -74.01, height=50.0)
    return {"root": root, "sf": a + b, "ny": c, "all": a + b + c}


# ---------------------------------------------------------------------------
# the index


def test_index_reads_manifests_only(barn, monkeypatch):
    """The index has to be cheap enough to answer a query. Opening cells.json
    to find out where the cells are is the exact cost this removes."""
    real = open

    def no_cells(path, *a, **k):
        assert "cells.json" not in str(path), "index opened a cells file"
        return real(path, *a, **k)

    monkeypatch.setattr("builtins.open", no_cells)
    assert len(shelf.index(barn["root"])) == 3


def test_index_skips_things_that_are_not_cells(barn):
    d = os.path.join(barn["root"], "gibs-image")
    os.makedirs(d)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"id": "gibs-image", "content": "content.jpg"}, f)
    assert len(shelf.index(barn["root"])) == 3


def test_index_of_a_missing_root_is_empty_not_a_crash(tmp_path):
    assert shelf.index(str(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# the window


def test_a_window_selects_only_what_it_overlaps(barn):
    entries = shelf.index(barn["root"])
    sf = shelf.overlapping(entries, [-122.5, 37.7, -122.3, 37.9])
    assert {e["id"] for e in sf} == {"usgs-sf-a", "usgs-sf-b"}


def test_a_dataset_with_no_bounds_is_excluded_not_included(barn):
    """The backfill has not reached it yet. Guessing that an unknown dataset
    might be relevant is how the query grows back into the whole shelf."""
    d = os.path.join(barn["root"], "usgs-unbounded")
    os.makedirs(d)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"id": "usgs-unbounded", "content": "cells.json",
                   "pipeline": {"res": 9, "cells": 10}}, f)
    entries = shelf.index(barn["root"])
    sel = shelf.overlapping(entries, [-122.5, 37.7, -122.3, 37.9])
    assert "usgs-unbounded" not in {e["id"] for e in sel}


# ---------------------------------------------------------------------------
# the budget


def test_a_budget_that_fits_keeps_full_resolution(barn):
    entries = shelf.index(barn["root"])
    res, _ = shelf.choose_res(entries, 10_000)
    assert res == 9


def test_a_small_budget_coarsens(barn):
    entries = shelf.index(barn["root"])
    res, est = shelf.choose_res(entries, 10)
    assert res < 9
    assert est <= 10


def test_the_estimate_never_promises_more_shrink_than_it_delivers(barn):
    """The estimate divides by seven per level; a partly-covered parent still
    counts once. Underestimating the shrink is the safe direction -- the other
    way round hands a caller more cells than its budget."""
    entries = shelf.index(barn["root"])
    res, est = shelf.choose_res(entries, 20)
    cells, _, _ = shelf.read_folded(barn["root"], entries, res, h3=h3)
    assert len(cells) >= est


def test_price_reads_no_cells_and_still_answers(barn, monkeypatch):
    real = open

    def no_cells(path, *a, **k):
        assert "cells.json" not in str(path), "pricing opened a cells file"
        return real(path, *a, **k)

    monkeypatch.setattr("builtins.open", no_cells)
    q = shelf.price(shelf.index(barn["root"]), None, 10)
    assert q["cells_native"] == barn["all"]
    assert q["folded"] is True


# ---------------------------------------------------------------------------
# the fold


def test_folding_preserves_a_count(barn):
    """point_density is EXTENSIVE. Averaging it up a level quietly divides it
    by the number of children and draws a thinner, wronger map."""
    entries = shelf.index(barn["root"])
    fine, _, _ = shelf.read_folded(barn["root"], entries, 9, h3=h3)
    coarse, _, _ = shelf.read_folded(barn["root"], entries, 5, h3=h3)
    assert len(coarse) < len(fine)
    assert sum(r["point_density"] for r in coarse.values()) == pytest.approx(
        sum(r["point_density"] for r in fine.values()))


def test_folding_averages_a_measurement_within_range(barn):
    entries = shelf.index(barn["root"])
    coarse, _, _ = shelf.read_folded(barn["root"], entries, 4, h3=h3)
    for row in coarse.values():
        assert 50.0 <= row["elevation_surface"] <= 200.0


def test_the_mean_is_weighted_by_the_returns_behind_each_cell(tmp_path):
    """UNWEIGHTED IS WRONG AND LOOKS FINE: a cell holding two points would
    count as much as one holding five hundred, and at res 12 most cells are
    the former."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "light", 37.80, -122.44, n=2, density=1.0, height=0.0)
    _tile(root, "heavy", 37.80, -122.43, n=2, density=999.0, height=100.0)
    entries = shelf.index(root)
    coarse, _, _ = shelf.read_folded(root, entries, 3, h3=h3)
    got = [r["elevation_surface"] for r in coarse.values()]
    # weighted, the heavy tile dominates; an unweighted mean would sit at 50
    assert max(got) > 90.0


def test_the_window_clips_cells_not_just_datasets(barn):
    """Selecting the right tiles and then serving all of them is most of the
    problem still on the wire."""
    entries = shelf.index(barn["root"])
    tight = [-122.44, 37.80, -122.43, 37.81]
    cells, read, _ = shelf.read_folded(barn["root"], entries, 9, bbox=tight, h3=h3)
    assert 0 < read < barn["all"]


def test_a_broken_dataset_does_not_take_the_answer_down(barn):
    with open(os.path.join(barn["root"], "usgs-sf-a", "cells.json"), "w") as f:
        f.write("{not json")
    entries = shelf.index(barn["root"])
    cells, _, _ = shelf.read_folded(barn["root"], entries, 9, h3=h3)
    assert cells


def test_asking_for_one_layer_returns_only_it(barn):
    entries = shelf.index(barn["root"])
    cells, _, _ = shelf.read_folded(barn["root"], entries, 9, h3=h3,
                                 layer="elevation_surface")
    assert all(set(r) == {"elevation_surface"} for r in cells.values())


# ---------------------------------------------------------------------------
# the doors, over a real socket


@pytest.fixture
def shelved(barn, mounted):
    serve.MOUNTS["/ingested/"] = barn["root"]
    yield barn
    serve.MOUNTS.pop("/ingested/", None)


def test_shelf_prices_without_serving(shelved, get):
    r = get("/shelf?budget=10")
    assert r.status == 200
    q = r.json()
    assert q["cells_native"] == shelved["all"]
    assert q["folded"] is True
    assert len(q["shelf"]) == 3
    assert "cells" not in q          # a price is not a delivery


def test_cells_honours_the_budget(shelved, get):
    r = get("/cells?budget=8")
    assert r.status == 200
    body = r.json()
    assert body["res"] < 9
    assert len(body["cells"]) < shelved["all"]


def test_cells_in_a_window_never_mentions_new_york(shelved, get):
    r = get("/cells?bbox=-122.5,37.7,-122.3,37.9&budget=10000")
    body = r.json()
    assert body["datasets"] == 2
    for cell in body["cells"]:
        lat, lon = h3.cell_to_latlng(cell)
        assert lon < -100


def test_an_explicit_res_cannot_bust_the_budget(shelved, get):
    """res is an override for looking AT the rollup, not a way around the
    number the caller gave. It can only ever ask for less than it can hold."""
    r = get("/cells?budget=8&res=9")
    assert r.json()["res"] < 9


def test_a_bad_bbox_is_refused_by_name(shelved, get):
    r = get("/cells?bbox=1,2,3")
    assert r.status == 400
    assert "four numbers" in r.json()["error"]


def test_a_bad_budget_is_refused_by_name(shelved, get):
    assert get("/cells?budget=nope").status == 400
    assert get("/cells?budget=0").status == 400


def test_the_doors_say_so_when_there_is_no_shelf(mounted, get):
    r = get("/shelf")
    assert r.status == 503
    assert "ingested" in r.json()["error"]


def test_a_terminal_sized_budget_fits_a_terminal(shelved, get):
    """THE TEST CONSUMER. An 80x24 frame is 80*44 pixels, and every cell past
    that averages into a bin that already has one. It is the only caller whose
    budget is exact rather than guessed."""
    budget = 80 * 44
    body = get(f"/cells?budget={budget}").json()
    assert len(body["cells"]) <= budget


def test_a_coarse_dataset_is_not_dropped_by_a_fine_request(tmp_path):
    """MIXED RESOLUTIONS ON ONE SHELF. A global basemap is res 4 and lidar is
    res 12. cell_to_parent cannot go finer than the data, so a fine target
    made the coarse dataset raise per cell and vanish -- silently, with the
    other layers still drawing, which is the shape of bug you only notice by
    missing something you were not looking at."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "fine", 37.80, -122.44, res=9)
    _tile(root, "coarse", 37.80, -122.44, res=4)
    entries = shelf.index(root)
    cells, _, _ = shelf.read_folded(root, entries, 9, h3=h3)
    got = {h3.get_resolution(c) for c in cells}
    assert 4 in got, "the coarse dataset was dropped"
    assert 9 in got


def test_a_coarse_dataset_cannot_blow_the_budget(tmp_path):
    """FOLDING IS ONE-WAY. A res-4 basemap asked for at res 8 does not shrink,
    it arrives whole. Estimating from one shelf-wide total missed that: a
    2,688-cell budget came back with 287,098 cells and a 10MB payload, because
    the coarse dataset was counted as if it would fold like the fine ones."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "fine", 37.80, -122.44, n=8, res=11)
    _tile(root, "coarse", 37.80, -122.44, n=8, res=4)
    entries = shelf.index(root)
    budget = 12
    res, _ = shelf.choose_res(entries, budget)
    cells, _, got = shelf.read_folded(root, entries, res, h3=h3, budget=budget)
    assert len(cells) <= budget, f"{len(cells)} cells against a {budget} budget"
    assert got <= res


def test_the_estimate_accounts_for_each_dataset_separately(tmp_path):
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "coarse", 37.80, -122.44, n=4, res=3)
    entries = shelf.index(root)
    # asking finer than native cannot shrink it: the estimate must say so
    assert shelf.estimate(entries, 9) == shelf.estimate(entries, 3)


def test_an_impossible_budget_terminates_rather_than_looping(tmp_path):
    """THE FLOOR IS RES 0 AND IT IS A REAL ANSWER. Folding stops when it stops
    making progress: if even the coarsest form is more than the caller said it
    could hold, it gets the coarsest form rather than a hang or a lie."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "big", 37.80, -122.44, n=8, res=11)
    entries = shelf.index(root)
    cells, _, got = shelf.read_folded(root, entries, 11, h3=h3, budget=1)
    assert 0 < len(cells) <= 1
    # it stops as soon as the answer fits, which is well above the res-0 floor
    assert 0 <= got < 11


def test_the_budget_is_enforced_on_the_result_not_the_estimate(tmp_path):
    """choose_res only picks a starting point. The estimate divides by seven
    per level while a partly-covered parent still counts once, so it UNDERSTATES
    the count -- the unsafe direction for a budget. Enforcement lives on the
    finished answer."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    for i in range(4):
        _tile(root, f"t{i}", 37.0 + i, -122.44, n=8, res=10)
    entries = shelf.index(root)
    for budget in (5, 25, 100):
        cells, _, _ = shelf.read_folded(root, entries, 10, h3=h3, budget=budget)
        assert len(cells) <= budget, f"{len(cells)} > {budget}"


def test_the_door_weights_the_estimate_by_the_window(shelved, get):
    """A GLOBAL DATASET IS MOSTLY NOT IN YOUR WINDOW. shelf.price() threaded
    the window into the estimate and the /cells handler did not, so a
    Bay-sized request counted a global basemap's whole cell count against
    itself and came back at res 1 holding ONE cell. The two call sites must
    ask the same question."""
    # a global dataset beside the local ones, the shape that exposed it
    root = shelved["root"]
    d = os.path.join(root, "basemap")
    os.makedirs(d)
    cells = {h3.latlng_to_cell(lat, lon, 4): {"brightness": 10.0}
             for lat in range(-80, 81, 8) for lon in range(-180, 180, 8)}
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"id": "basemap", "content": "cells.json",
                   "layers": {"brightness": {"kind": "LAYER_KIND_INTENSIVE"}},
                   "pipeline": {"res": 4, "cells": 300000,
                                "bbox": [-180.0, -85.0, 180.0, 85.0]}}, f)
    with open(os.path.join(d, "cells.json"), "w") as f:
        json.dump({"res": 4, "cells": cells}, f)

    r = get("/cells?bbox=-122.5,37.79,-122.40,37.83&budget=2000")
    body = r.json()
    assert body["res"] >= 8, f"the window was ignored; got res {body['res']}"


# ---------------------------------------------------------------------------
# the map directory


def test_the_directory_groups_tiles_into_collections(barn):
    """A SHELF OF 397 DATASETS IS NOT A MENU. Six surveys is. Every consumer
    that tried to show the shelf ended up with a hardcoded layer list or no
    chooser at all, because kingfisher published what it HOLDS and never how
    it is organised."""
    d = shelf.directory(shelf.index(barn["root"]))
    srcs = {s["source"] for s in d["sources"]}
    assert srcs == {"other"}          # the fixture's titles are bare ids
    assert d["datasets"] == 3


def test_usgs_tiles_collapse_by_survey(tmp_path):
    root = str(tmp_path / "ing")
    os.makedirs(root)
    for i in range(3):
        n = _tile(root, f"usgs-gg-{i}", 37.8 + i * 0.05, -122.44, res=9)
        p = os.path.join(root, f"usgs-gg-{i}", "manifest.json")
        m = json.load(open(p))
        m["title"] = f"USGS Lidar Point Cloud ARRA_CA_GOLDENGATE_2010 00098{i}"
        json.dump(m, open(p, "w"))
    d = shelf.directory(shelf.index(root))
    usgs = [s for s in d["sources"] if s["source"] == "usgs"]
    assert len(usgs) == 1
    col = usgs[0]["collections"][0]
    assert col["collection"] == "ARRA_CA_GOLDENGATE_2010"
    assert col["datasets"] == 3
    # every node carries its own extent and size, so a menu can be PRICED
    assert col["bbox"] is not None and col["cells"] > 0
    assert col["res"] == [9]
    assert "example" in col          # one id, so a caller can look at one tile


def test_the_directory_answers_what_can_i_draw(tmp_path):
    """`measures` is the question a display layer actually has: it names a
    layer and needs to know where that layer exists and at what resolution."""
    root = str(tmp_path / "ing")
    os.makedirs(root)
    _tile(root, "usgs-a", 37.8, -122.44, res=9)
    d = shelf.directory(shelf.index(root))
    by = {m["layer"]: m for m in d["measures"]}
    assert set(by) == {"elevation_surface", "point_density"}
    assert "EXTENSIVE" in by["point_density"]["kind"]
    assert by["elevation_surface"]["bbox"] is not None
    assert by["elevation_surface"]["res"] == [9]


def test_the_directory_reads_no_cells(barn, monkeypatch):
    """A menu that costs a read of every cells.json is a menu nobody puts in
    a UI."""
    real = open

    def no_cells(path, *a, **k):
        assert "cells.json" not in str(path), "the directory opened a cells file"
        return real(path, *a, **k)

    monkeypatch.setattr("builtins.open", no_cells)
    assert shelf.directory(shelf.index(barn["root"]))["datasets"] == 3


def test_a_window_narrows_the_directory(barn):
    d = shelf.directory(shelf.index(barn["root"]), [-122.5, 37.7, -122.3, 37.9])
    assert d["datasets"] == 2
    assert d["bbox"] is not None


def test_the_directory_door_is_a_menu(shelved, get):
    r = get("/directory")
    assert r.status == 200
    d = r.json()
    assert d["datasets"] == 3
    assert d["sources"] and d["measures"]


def test_the_directory_door_refuses_a_bad_window(shelved, get):
    assert get("/directory?bbox=1,2").status == 400
