# The map-to-text pipeline. Every case here pins something that is wrong
# WITHOUT LOOKING WRONG -- the failure mode this renderer actually has. A
# viewport that silently refits, a rollup that averages a count, a flat layer
# that normalises to blank: none of them raise, all of them draw a picture,
# and the picture is a lie.

import pytest

pytest.importorskip("h3")

import h3  # noqa: E402

from textmap import (  # noqa: E402
    CellSource,
    Globe,
    TextMap,
    Viewport,
    RAMP,
    ramp_char,
    ramp_rgb,
)


def _cells(res=9, n=6, lat0=37.80, lon0=-122.40, step=0.004, value=1.0):
    """A small square of real H3 cells, so nothing here is a fake id."""
    out = {}
    for i in range(n):
        for j in range(n):
            c = h3.latlng_to_cell(lat0 + i * step, lon0 + j * step, res)
            out[c] = {"height": value + i, "count": 1.0}
    return out


# ---------------------------------------------------------------------------
# the ramps


def test_ramp_clamps_outside_zero_to_one():
    assert ramp_rgb(-5) == ramp_rgb(0.0)
    assert ramp_rgb(5) == ramp_rgb(1.0)
    assert ramp_char(-5) == ramp_char(0.0)
    assert ramp_char(5) == ramp_char(1.0)


def test_ramp_char_never_indexes_past_the_ramp():
    # the off-by-one that turns the top of every scale into an IndexError
    assert ramp_char(1.0) == "@"


def test_the_ascii_ramp_holds_no_blank():
    """A SPACE IS NEVER A VALUE. The ramp led with one, so the bottom of every
    scale drew exactly like ground nobody surveyed -- the lowest valley in a
    tile and the ocean beside it, identical. Colour never had this bug; ascii
    did, and the rule holds in every renderer or in none."""
    assert " " not in RAMP
    assert ramp_char(0.0) != " "


# ---------------------------------------------------------------------------
# CellSource


def test_accepts_both_cell_shapes():
    flat = {c: 3.0 for c in _cells()}
    src = CellSource("flat", flat)
    assert src.layers() == ["value"]
    assert CellSource("nested", _cells()).layers() == ["count", "height"]


def test_non_numeric_values_are_dropped_not_coerced():
    c = next(iter(_cells()))
    src = CellSource("mixed", {c: {"height": 4.0, "label": "hillside"}})
    assert src.layers() == ["height"]


def test_native_res_is_read_from_the_data():
    assert CellSource("s", _cells(res=11)).native_res() == 11


def test_rollup_cannot_go_finer_than_the_data():
    # asking for res 12 from res 9 cells must not invent detail; it clamps
    src = CellSource("s", _cells(res=9))
    assert len(src.points(12, "height")) == len(src.points(9, "height"))


def test_intensive_layers_average_up_a_level():
    src = CellSource("s", _cells(res=10), {"height": "LAYER_KIND_INTENSIVE"})
    fine = src.points(10, "height")
    coarse = src.points(7, "height")
    assert len(coarse) < len(fine)
    # a mean of means stays inside the original range
    vals = [p[2] for p in fine]
    assert min(vals) <= min(p[2] for p in coarse)
    assert max(p[2] for p in coarse) <= max(vals)


def test_extensive_layers_sum_up_a_level():
    """THE ONE THAT LOOKS LIKE A MAP EITHER WAY.

    point_density is returns per cell. Averaging four children into their
    parent divides the count by four and draws a quieter, wronger map. The
    manifest says which layers are counts; honour it.
    """
    src = CellSource("s", _cells(res=10), {"count": "LAYER_KIND_EXTENSIVE"})
    fine = sum(p[2] for p in src.points(10, "count"))
    coarse = sum(p[2] for p in src.points(6, "count"))
    assert coarse == pytest.approx(fine)


def test_unlabelled_layers_default_to_intensive():
    # safe to be wrong about: a mean of a count is visibly flat, a sum of an
    # elevation is visibly absurd
    src = CellSource("s", _cells(res=10))
    assert not src.is_extensive("count")


def test_points_are_cached_per_resolution_and_layer():
    src = CellSource("s", _cells())
    assert src.points(8, "height") is src.points(8, "height")


def test_missing_layer_yields_no_points():
    assert CellSource("s", _cells()).points(9, "nope") == []


# ---------------------------------------------------------------------------
# Viewport


def test_fit_shows_every_point():
    src = CellSource("s", _cells())
    pts = src.points(9, "height")
    vp = Viewport.fit(pts, 60, 20)
    assert all(vp.project(lon, lat) is not None for lon, lat, _ in pts)


def test_fit_of_nothing_is_the_whole_world():
    vp = Viewport.fit([], 40, 10)
    assert vp.span_x == 360.0


def test_project_clips_instead_of_wrapping():
    """A viewport that drew out-of-window points would be a fit with extra
    steps, and panning would appear to do nothing."""
    vp = Viewport(-122.4, 37.8, 0.02, 40, 10)
    assert vp.project(-122.4, 37.8) is not None
    assert vp.project(-100.0, 37.8) is None
    assert vp.project(-122.4, 10.0) is None


def test_pan_moves_by_a_fraction_of_the_window():
    # one press should cross the same amount of SCREEN at every zoom level
    near = Viewport(-122.4, 37.8, 0.02, 40, 10)
    far = Viewport(-122.4, 37.8, 2.0, 40, 10)
    near.pan(0.5, 0)
    far.pan(0.5, 0)
    assert (far.lon - -122.4) == pytest.approx(100 * (near.lon - -122.4))


def test_pan_round_trips():
    vp = Viewport(-122.4, 37.8, 0.02, 40, 10)
    vp.pan(0.25, 0.25)
    vp.pan(-0.25, -0.25)
    assert vp.lon == pytest.approx(-122.4)
    assert vp.lat == pytest.approx(37.8)


def test_pan_clamps_short_of_the_pole():
    vp = Viewport(0.0, 84.0, 10.0, 40, 10)
    for _ in range(20):
        vp.pan(0, 1.0)
    assert vp.lat <= 85.0


def test_zoom_scales_the_span_and_holds_the_centre():
    vp = Viewport(-122.4, 37.8, 0.02, 40, 10)
    vp.zoom(0.5)
    assert vp.span_x == pytest.approx(0.01)
    assert (vp.lon, vp.lat) == (-122.4, 37.8)


def test_zoom_is_bounded_at_both_ends():
    vp = Viewport(0.0, 0.0, 1.0, 40, 10)
    for _ in range(200):
        vp.zoom(2.0)
    assert vp.span_x <= 360.0
    for _ in range(400):
        vp.zoom(0.5)
    assert vp.span_x > 0


def test_pixels_are_square_ish():
    """Two map rows per character row is the whole reason the aspect works."""
    vp = Viewport(0.0, 0.0, 1.0, 80, 20)
    assert vp.pixels() == (80, 40)
    # span_y / height should equal span_x / width, in projected units
    assert vp.span_y() / 40 == pytest.approx(vp.span_x / 80)


# ---------------------------------------------------------------------------
# TextMap


def _frame(ascii_only=True, rows=10, cols=40, scale=None, value=1.0):
    src = CellSource("s", _cells(value=value))
    pts = src.points(9, "height")
    vp = Viewport.fit(pts, cols, rows)
    tm = TextMap(ascii_only=ascii_only, scale=scale)
    return tm, vp, pts, tm.frame(pts, vp)


def test_frame_is_one_string_per_character_row():
    _, _, _, lines = _frame(rows=13)
    assert len(lines) == 13


def test_ascii_frames_carry_no_escapes():
    # the property that makes this survive a pipe, a paste and a file
    _, _, _, lines = _frame(ascii_only=True)
    assert "\x1b" not in "".join(lines)


def test_colour_frames_reset_at_end_of_row():
    # a row that does not reset bleeds its background across the terminal
    _, _, _, lines = _frame(ascii_only=False)
    assert all(line.endswith("\x1b[0m") for line in lines)


def test_a_flat_layer_is_coverage_not_blank():
    """A cell nobody surveyed and a cell measuring the same as its neighbour
    are different facts. Normalising a constant to zero drew the second as
    the first, which is exactly backwards."""
    src = CellSource("s", {c: 7.0 for c in _cells()})
    pts = src.points(9, "value")
    vp = Viewport.fit(pts, 40, 10)
    lines = TextMap(ascii_only=True).frame(pts, vp)
    assert "@" in "".join(lines)


def test_empty_viewport_draws_a_full_blank_rectangle():
    """An empty string is a row that silently is not there -- fine in a
    terminal, and the shape of bug where a compositor's region is one row
    short and the map still looks fine."""
    src = CellSource("s", _cells())
    pts = src.points(9, "height")
    vp = Viewport(0.0, 0.0, 0.01, 30, 8)      # nowhere near the data
    tm = TextMap(ascii_only=True)
    lines = tm.frame(pts, vp)
    assert lines == [" " * 30] * 8
    assert tm.plotted == 0


def test_every_frame_is_the_full_rectangle():
    tm, vp, pts, lines = _frame(rows=9, cols=33)
    assert len(lines) == 9
    assert all(len(line) == 33 for line in lines)


def test_plotted_counts_only_what_landed_in_the_window():
    tm, vp, pts, _ = _frame()
    assert tm.plotted == len(pts)
    vp.zoom(0.1)
    tm.frame(pts, vp)
    assert tm.plotted < len(pts)


def test_char_cell_agrees_with_project():
    """The arithmetic that would otherwise live on both sides of a socket."""
    vp = Viewport(-122.4, 37.8, 0.02, 40, 10)
    px, py = vp.project(-122.4, 37.8)
    assert vp.char_cell(-122.4, 37.8) == (px, py // 2, py % 2)


def test_char_cell_is_none_off_screen_never_clamped():
    # a sprite pinned to the border because its city is out of frame is a
    # wrong answer that looks like a right one
    vp = Viewport(-122.4, 37.8, 0.02, 40, 10)
    assert vp.char_cell(-74.0, 40.7) is None


def test_char_cell_rows_are_half_the_pixel_rows():
    vp = Viewport(0.0, 0.0, 1.0, 40, 10)
    seen = {vp.char_cell(0.0, lat)[1] for lat in
            [vp.bounds()[1] + 1e-9, 0.0, vp.bounds()[3] - 1e-9]}
    assert max(seen) <= 9


def test_cells_grid_matches_the_viewport():
    src = CellSource("s", _cells())
    pts = src.points(9, "height")
    vp = Viewport.fit(pts, 21, 7)
    grid = TextMap().cells(pts, vp)
    assert len(grid) == 7
    assert all(len(row) == 21 for row in grid)


def test_cells_and_frame_are_the_same_picture():
    """Two encodings of one field. The moment they normalise separately they
    become two pictures that merely resemble each other."""
    src = CellSource("s", _cells())
    pts = src.points(9, "height")
    vp = Viewport.fit(pts, 30, 8)
    grid = TextMap().cells(pts, vp)
    lines = TextMap(ascii_only=True).frame(pts, vp)
    # ascii draws a space exactly where neither half of the cell was
    # surveyed, and cells() reports (" ", None, None) in the same places.
    # Anything else means the two encodings disagree about what is data.
    for row, line in zip(grid, lines):
        blank_text = [ch == " " for ch in line]
        blank_grid = [fg is None and bg is None for _, fg, bg in row]
        assert blank_text == blank_grid


def test_cells_carry_absence_as_none_not_a_dark_colour():
    src = CellSource("s", _cells())
    pts = src.points(9, "height")
    vp = Viewport(0.0, 0.0, 0.01, 12, 4)          # nowhere near the data
    grid = TextMap().cells(pts, vp)
    assert all(cell == (" ", None, None) for row in grid for cell in row)


def test_a_shared_scale_makes_frames_comparable():
    """WITHOUT THIS, ANIMATION LIES. Per-frame normalisation rescales every
    frame to its own extremes, so a time series shows shape changing and
    hides magnitude changing -- which is usually the thing you wanted."""
    lo_src = CellSource("a", _cells(value=1.0))
    hi_src = CellSource("b", _cells(value=100.0))
    lo_pts = lo_src.points(9, "height")
    hi_pts = hi_src.points(9, "height")
    vp = Viewport.fit(lo_pts + hi_pts, 40, 10)

    free = TextMap(ascii_only=True)
    a_free = free.frame(lo_pts, vp)
    b_free = free.frame(hi_pts, vp)
    assert set("".join(a_free)) == set("".join(b_free))   # identical ramps

    pinned = TextMap(ascii_only=True, scale=(1.0, 106.0))
    a = pinned.frame(lo_pts, vp)
    b = pinned.frame(hi_pts, vp)
    assert set("".join(a)) != set("".join(b))             # magnitude survives


# ---------------------------------------------------------------------------
# cells coarser than pixels


def _coarse():
    """A patch of res-3 cells around the origin. Each is roughly 1.4 degrees
    across; the viewport below puts ~17 pixels inside one, which is the ratio
    that actually breaks centre-scattering."""
    centre = h3.latlng_to_cell(0.0, 0.0, 3)
    ring = h3.grid_disk(centre, 2)
    return CellSource("coarse", {
        c: {"brightness": 10.0 + 40.0 * h3.cell_to_latlng(c)[0]} for c in ring})


def _coarse_view(cols=60, rows=12):
    # centred on the patch and zoomed IN, so cells are much bigger than pixels
    return Viewport(0.0, 0.0, 5.0, cols, rows)


def test_scattering_centres_leaves_a_lattice_of_holes():
    """The bug, pinned so the fix has something to be a fix OF. Cell centres
    land in one pixel each, so a frame with more pixels than cells comes out
    as confetti rather than a map."""
    src = _coarse()
    pts = src.points(3, "brightness")
    vp = _coarse_view()
    tm = TextMap(ascii_only=True)
    lines = tm.frame(pts, vp)
    painted = sum(1 for line in lines for ch in line if ch != " ")
    assert painted <= len(pts) * 2      # at most one per cell, two halves
    assert painted < 60 * 12 * 0.2      # and the frame is mostly holes


def test_a_fill_paints_the_cell_not_its_centre():
    """PAINT THE CELL. Every pixel the scatter missed asks which cell contains
    it -- the exact inverse of hexify's cell-driven rule, and for the same
    reason: whichever side is coarser has to drive."""
    src = _coarse()
    pts = src.points(3, "brightness")
    vp = _coarse_view()
    tm = TextMap(ascii_only=True)
    bare = sum(1 for line in tm.frame(pts, vp) for ch in line if ch != " ")
    full = sum(1 for line in tm.frame(pts, vp, fill=src.sampler(3, "brightness"))
               for ch in line if ch != " ")
    assert full > bare * 5
    assert tm.filled > 0


def test_the_fill_never_invents_data_outside_the_cells():
    """A pixel in no cell stays blank. Absence and zero are different facts,
    and a fill that guessed would make the coverage map a lie."""
    src = CellSource("one", {h3.latlng_to_cell(0.0, 0.0, 6): {"v": 5.0}})
    pts = src.points(6, "v")
    # a window far from the single cell
    vp = Viewport(120.0, -40.0, 1.0, 30, 8)
    tm = TextMap(ascii_only=True)
    lines = tm.frame(pts, vp, fill=src.sampler(6, "v"))
    assert all(ch == " " for line in lines for ch in line)


def test_the_fill_contributes_to_the_scale():
    """Filled values are taken BEFORE lo..hi, so a coarse cell whose centre
    sits outside the window still tells the ramp what range to use -- rather
    than being drawn at a value the ramp was never told about and clipped."""
    src = _coarse()
    pts = src.points(3, "brightness")
    vp = _coarse_view()
    tm = TextMap(ascii_only=True)
    tm.frame(pts, vp, fill=src.sampler(3, "brightness"))
    lo_hi_filled = (tm.lo, tm.hi)
    tm.frame(pts, vp)
    assert lo_hi_filled[0] <= tm.lo and lo_hi_filled[1] >= tm.hi


def test_the_sampler_walks_coarser_for_a_mixed_resolution_answer():
    """One reply can hold a res-4 basemap beside res-8 lidar, so the cell
    containing a pixel is not always at the resolution asked for."""
    fine = h3.latlng_to_cell(37.8, -122.4, 8)
    coarse = h3.latlng_to_cell(10.0, 20.0, 3)
    src = CellSource("mixed", {fine: {"v": 1.0}, coarse: {"v": 9.0}})
    at = src.sampler(8, "v")
    assert at(37.8, -122.4) == 1.0
    assert at(10.0, 20.0) == 9.0


def test_a_sampler_for_a_layer_nobody_carries_is_none():
    assert CellSource("s", _cells()).sampler(9, "nope") is None


def test_the_fill_never_samples_off_the_globe():
    """A frame wider than the world runs past +/-90, and sampling there
    smeared the polar row across everything above the data -- a band of
    confident colour describing latitude 108. Off the globe is ABSENT."""
    src = _coarse()
    pts = src.points(3, "brightness")
    # a viewport whose vertical span exceeds the planet
    vp = Viewport(0.0, 0.0, 350.0, 60, 40)
    assert vp.bounds()[3] > 90.0, "fixture must overflow the pole"
    tm = TextMap(ascii_only=True)
    lines = tm.frame(pts, vp, fill=src.sampler(3, "brightness"))
    w, h = vp.pixels()
    # every painted pixel must correspond to a real latitude
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            if ch == " ":
                continue
            lats = [vp.unproject(col, row * 2)[1], vp.unproject(col, row * 2 + 1)[1]]
            assert any(-90.0 <= la <= 90.0 for la in lats), \
                f"painted a pixel at latitude {lats}"


def test_longitude_wraps_but_latitude_does_not():
    """The globe wraps east-west and does not wrap north-south. Treating them
    the same is how the far side of the antimeridian ends up drawn at the
    pole."""
    src = _coarse()
    at = src.sampler(3, "brightness")
    # a longitude past the antimeridian names a real place; a latitude past
    # the pole names none
    assert at(0.0, 0.0) is not None
    vp = Viewport(0.0, 0.0, 10.0, 10, 4)
    lon, _ = vp.unproject(0, 0)
    assert -180.0 <= ((lon + 180.0) % 360.0) - 180.0 <= 180.0


# ---------------------------------------------------------------------------
# ball earth


def test_the_far_side_is_hidden_not_folded():
    """Orthographic is the view from infinitely far away. A globe that drew
    the far hemisphere would be a flat map with extra steps -- and it would
    fold two places onto the same pixel."""
    g = Globe(0.0, 0.0, 60, 20)
    assert g.project(0.0, 0.0) is not None
    assert g.project(180.0, 0.0) is None
    assert g.project(120.0, 0.0) is None


def test_off_the_disc_is_space_not_edge():
    """Off the disc is not off the map -- it is not the planet at all, and the
    difference is the whole look of a globe."""
    g = Globe(0.0, 0.0, 60, 20)
    assert g.unproject(0, 0) is None                    # a corner
    w, h = g.pixels()
    assert g.unproject(w // 2, h // 2) is not None      # the middle


def test_projection_round_trips_within_a_pixel():
    g = Globe(0.0, 0.0, 120, 40)
    for lon, lat in ((30.0, 20.0), (-45.0, -30.0), (10.0, 60.0)):
        lon2, lat2 = g.unproject(*g.project(lon, lat))
        assert abs(lon2 - lon) < 3.0
        assert abs(lat2 - lat) < 3.0


def test_panning_a_globe_spins_it_and_wraps():
    g = Globe(170.0, 0.0, 60, 20)
    g.pan(1.0, 0)
    assert -180.0 <= g.lon <= 180.0     # went past the antimeridian, came back


def test_panning_stops_short_of_the_pole():
    g = Globe(0.0, 0.0, 60, 20)
    for _ in range(20):
        g.pan(0, 1.0)
    assert g.lat <= 89.0


def test_zoom_changes_the_disc_and_never_inverts_it():
    g = Globe(0.0, 0.0, 60, 20)
    before = g.radius
    g.zoom(0.5)
    assert g.radius > before
    for _ in range(200):
        g.zoom(2.0)
    assert g.radius >= 2.0


def test_a_visible_pole_makes_the_request_the_whole_world():
    """A hemisphere's box wraps the antimeridian about half the time and
    swallows a pole whenever the view is tilted, and the shelf's bbox filter
    refuses both rather than approximating. An honest over-fetch beats a box
    that quietly means the opposite of what it says."""
    g = Globe(0.0, 0.0, 60, 20)
    w, s, e, n = g.bounds()
    assert (w, e) == (-180.0, 180.0)
    assert s <= -85.0 and n >= 85.0


def test_the_renderer_cannot_tell_which_projection_it_holds():
    """The whole reason a globe was cheap: both answer project, unproject and
    pixels, so TextMap never learns the difference."""
    src = _coarse()
    pts = src.points(3, "brightness")
    fill = src.sampler(3, "brightness")
    tm = TextMap(ascii_only=True)
    flat = tm.frame(pts, Viewport(0.0, 0.0, 60.0, 40, 12), fill=fill)
    ball = tm.frame(pts, Globe(0.0, 0.0, 40, 12), fill=fill)
    assert len(flat) == len(ball) == 12
    assert all(len(line) == 40 for line in ball)
    # and the ball has corners of space that the flat frame does not
    assert ball[0][0] == " "
