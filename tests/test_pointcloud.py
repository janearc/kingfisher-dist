# The point-cloud pipeline, and specifically the ways it can be WRONG WITHOUT
# LOOKING WRONG. A projection mistake does not raise; it renders a beautiful
# map of the wrong ocean. Every test here pins a failure that shipped or was
# one commit away from shipping on 2026-08-30.

import numpy as np
import pytest

laspy = pytest.importorskip("laspy")
pytest.importorskip("pyproj")
pytest.importorskip("h3")

from pointcloud import US_SURVEY_FOOT_M, hexify_las  # noqa: E402


def _write_las(path, xs, ys, zs, classes, epsg=26943, linear_unit=None, scale=0.01):
    """A LAS 1.2 file with geokeys we control, so the contradictory-header
    case can be reproduced deterministically instead of waiting for USGS."""
    hdr = laspy.LasHeader(version="1.2", point_format=1)
    hdr.scales = [scale, scale, scale]
    hdr.offsets = [0.0, 0.0, 0.0]
    las = laspy.LasData(hdr)
    las.x, las.y, las.z = np.array(xs), np.array(ys), np.array(zs)
    las.classification = np.array(classes, dtype="uint8")
    from pyproj import CRS
    las.header.add_crs(CRS.from_epsg(epsg))
    if linear_unit is not None:
        # overwrite ProjLinearUnitsGeoKey to contradict the EPSG code, which
        # is exactly what LAStools writes and what put California in the
        # Atlantic
        for vlr in las.header.vlrs:
            if getattr(vlr, "record_id", None) == 34735:
                for k in vlr.geo_keys:
                    if k.id == 3076:
                        k.value_offset = linear_unit
                        break
                else:
                    from laspy.vlrs.known import GeoKeyEntryStruct
                    e = GeoKeyEntryStruct()
                    e.id, e.tiff_tag_location, e.count, e.value_offset = 3076, 0, 1, linear_unit
                    vlr.geo_keys.append(e)
                    vlr.geo_keys[0].value_offset = len(vlr.geo_keys) - 1
    las.write(str(path))
    return path


# Santa Cruz, in California zone 3. The FEET numbers are the real tile's.
FT_X, FT_Y = 6118000.0, 1806000.0


def test_feet_declared_as_a_metre_crs_still_lands_in_california(tmp_path):
    # THE BUG. The file says EPSG:26943 (metres) and ProjLinearUnitsGeoKey
    # 9003 (US survey feet), and the coordinates are feet. Trusting the EPSG
    # code alone transformed feet as metres and produced lon -71.2 -- the
    # Atlantic off Massachusetts -- with no error of any kind.
    p = _write_las(tmp_path / "ft.laz",
                   [FT_X, FT_X + 10, FT_X + 20], [FT_Y, FT_Y + 10, FT_Y + 20],
                   [10.0, 20.0, 30.0], [2, 2, 2], epsg=26943, linear_unit=9003)
    cells, meta = hexify_las(str(p), res=10)
    assert meta["horizontal_scale_applied"] == pytest.approx(US_SURVEY_FOOT_M)
    import h3
    lat, lon = h3.cell_to_latlng(next(iter(cells)))
    assert 36.0 < lat < 38.0, f"latitude {lat} is not the central coast"
    assert -123.0 < lon < -121.0, f"longitude {lon} is not the central coast"


def test_heights_convert_out_of_feet(tmp_path):
    # 30 survey feet is 9.14m. Read as metres the terrain stands 3.28x too
    # tall, which renders as a plausible and entirely wrong landscape.
    p = _write_las(tmp_path / "z.laz", [FT_X], [FT_Y], [30.0], [2],
                   epsg=26943, linear_unit=9003)
    cells, meta = hexify_las(str(p), res=10)
    assert meta["vertical_unit_to_metres"] == pytest.approx(US_SURVEY_FOOT_M)
    assert next(iter(cells.values()))["elevation_ground"] == pytest.approx(9.144, abs=1e-3)


def test_a_projection_that_leaves_its_own_crs_is_refused(tmp_path):
    # the guard that would have caught the bug above the moment it happened.
    # Coordinates far outside California zone 3's area of use must not quietly
    # produce cells somewhere plausible-looking.
    # scale 1.0: LAS stores coordinates as scaled int32, and these values
    # deliberately sit far outside the zone -- at the default 0.01 scale they
    # overflow the storage before they can reach the guard under test.
    p = _write_las(tmp_path / "wrong.laz",
                   [50_000_000.0, 50_000_100.0], [40_000_000.0, 40_000_100.0],
                   [1.0, 2.0], [2, 2], epsg=26943, scale=1.0)
    with pytest.raises(ValueError, match="area of use"):
        hexify_las(str(p), res=10)


def test_ground_surface_and_water_are_separate_answers(tmp_path):
    # one cell, four returns: bare earth, a treetop, and two water returns.
    # elevation_ground is the MEAN of ground (intensive); elevation_surface is
    # the highest return of any class; water_share is the fraction.
    xs = [FT_X] * 4
    ys = [FT_Y] * 4
    zs = [10.0, 12.0, 100.0, 11.0]        # feet
    cl = [2, 2, 1, 9]
    p = _write_las(tmp_path / "mix.laz", xs, ys, zs, cl, epsg=26943, linear_unit=9003)
    cells, _ = hexify_las(str(p), res=8)   # coarse: one cell for all four
    row = next(iter(cells.values()))
    assert row["point_density"] == 4
    assert row["elevation_ground"] == pytest.approx(11.0 * US_SURVEY_FOOT_M, abs=1e-3)
    assert row["elevation_surface"] == pytest.approx(100.0 * US_SURVEY_FOOT_M, abs=1e-3)
    assert row["water_share"] == pytest.approx(0.25)


def test_a_cell_with_no_ground_return_omits_elevation_ground(tmp_path):
    # absence and zero are different facts. A cell of pure water has NO bare
    # earth measurement, and reporting 0m would put the seabed at sea level.
    p = _write_las(tmp_path / "wet.laz", [FT_X, FT_X], [FT_Y, FT_Y],
                   [1.0, 2.0], [9, 9], epsg=26943, linear_unit=9003)
    cells, _ = hexify_las(str(p), res=8)
    row = next(iter(cells.values()))
    assert "elevation_ground" not in row
    assert row["water_share"] == pytest.approx(1.0)


def test_labelled_noise_is_dropped(tmp_path):
    # ASPRS class 7 is the file saying "this return is junk"
    p = _write_las(tmp_path / "n.laz", [FT_X] * 3, [FT_Y] * 3,
                   [10.0, 11.0, 9999.0], [2, 2, 7], epsg=26943, linear_unit=9003)
    cells, meta = hexify_las(str(p), res=8)
    assert meta["dropped_noise_or_nonfinite"] == 1
    assert next(iter(cells.values()))["point_density"] == 2


def test_a_file_with_no_crs_is_refused_rather_than_guessed(tmp_path):
    hdr = laspy.LasHeader(version="1.2", point_format=1)
    hdr.scales, hdr.offsets = [0.01] * 3, [0.0] * 3
    las = laspy.LasData(hdr)
    las.x, las.y, las.z = np.array([1.0]), np.array([2.0]), np.array([3.0])
    las.classification = np.array([2], dtype="uint8")
    p = tmp_path / "nocrs.laz"
    las.write(str(p))
    with pytest.raises(ValueError, match="no CRS"):
        hexify_las(str(p), res=10)
