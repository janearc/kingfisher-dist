#!/usr/bin/env python3
# pointcloud -- lidar returns become H3 cells.
#
# The pipeline DESIGN.md promised and left unwritten: "Everything else (lidar
# to smoothed surfaces, pairs to cells): the daemon says 'no pipeline for kind
# X yet' ... and LEAVES THE FILE." A USGS 3DEP tile has been sitting in the
# spool doing exactly that. This is that pipeline.
#
# FOUR FACTS COME OUT OF THE FILE, NEVER OUT OF AN ASSUMPTION. The tile that
# prompted this (ARRA_CA_CENTRALCOAST_Z3_2010) declares, in its own header:
#
#   EPSG:26943    NAD83 / California State Plane zone III -- a projected CRS,
#                 so the coordinates are not degrees and hexing them directly
#                 puts the whole tile in the Gulf of Guinea
#   units 9003    US SURVEY FEET, horizontally AND vertically. Read as metres,
#                 every hill stands 3.28x too tall and the map is confidently
#                 wrong, which is worse than blank
#   laszip VLR    the points are compressed; struct will not read them
#   1,497,933     points in one 6000x4000 ft tile
#
# So: decompress, read the CRS, transform to WGS84, convert the vertical unit,
# and only then hex. In that order, every time.
#
# WHAT IT EMITS, and why more than one layer. A lidar return is not an
# elevation -- it is a thing the beam hit, which may be ground, a rooftop or a
# tree. Folding all returns together produces a surface model and calling it
# terrain, which is a quiet lie about what the data says. The chunk therefore
# carries three layers that disagree honestly:
#
#   elevation_ground   mean of class-2 (ground) returns -- the bare earth
#   elevation_surface  max of ALL returns -- the canopy and rooftops
#   point_density      returns per cell -- EXTENSIVE, folds by sum
#
# The first two are INTENSIVE and fold by MEAN when resolutions are navigated;
# the third is EXTENSIVE and folds by SUM. That distinction is in the contract
# (kingfisher.map.v1.LayerKind) precisely because folding counts by mean
# silently deflates dense areas.

import json
import os
import time

from errors import Unprocessable

# LAS classification 2 is bare earth, by the ASPRS standard every one of these
# files follows. Not a heuristic: the number is in the spec.
GROUND_CLASS = 2
# ASPRS noise classes: 7 is low noise, 18 is high noise. Both are the file
# telling you a return is junk -- a bird, a cloud, a multipath ghost. They
# matter here because elevation_surface is a MAX, and one uncaught high-noise
# return makes its cell 1300m tall in coastal Santa Cruz and flattens the
# colour scale for every honest cell around it. Measured: this tile's raw max
# was 4313.88 ft where the terrain tops out near 40 ft.
NOISE_CLASSES = (7, 18)
WATER_CLASS = 9

# A ROBUST CEILING, because the class labels are not enough. This tile is 98.8%
# class 9 (water) -- a coastal strip, mostly ocean -- and its sixteen highest
# returns are ALSO class 9, sitting at 4313 ft where the 99.9th percentile is
# 45 ft. A water surface is not 1.3km above sea level; those are unlabelled
# noise wearing a legitimate class, and no classification filter will catch
# them.
#
# So the ceiling is statistical rather than nominal: keep what is within a
# generous margin of the 99.9th percentile. The margin is in METRES and sized
# for the tallest real thing that can stand above terrain (redwoods here, and
# they are the local answer at about 100m). Anything beyond it is instrument
# noise by elimination. The count that gets dropped is recorded, because a
# filter that silently eats data is how a tile quietly loses its mountain.
OUTLIER_MARGIN_M = 120.0

# US survey foot -> metre, exact by definition (not 0.3048, which is the
# INTERNATIONAL foot -- they differ by 2ppm, which is 8mm over this tile and
# would be invisible and wrong).
US_SURVEY_FOOT_M = 1200.0 / 3937.0


# GeoTIFF linear-unit codes, the ones that appear in LAS geokeys. The US
# survey foot and the international foot differ by 2 parts per million, which
# is 8mm across a 6000ft tile -- invisible, and wrong.
LINEAR_UNIT_M = {9001: 1.0, 9002: 0.3048, 9003: US_SURVEY_FOOT_M}
PROJ_LINEAR_UNITS_KEY = 3076      # ProjLinearUnitsGeoKey
VERTICAL_UNITS_KEY = 4099         # VerticalUnitsGeoKey


def _geokey(header, key_id):
    """One GeoTIFF geokey out of the LAS header, or None."""
    for vlr in header.vlrs:
        if getattr(vlr, "record_id", None) == 34735:
            for k in getattr(vlr, "geo_keys", []):
                if getattr(k, "id", None) == key_id:
                    return getattr(k, "value_offset", None)
    return None


def _units(header, crs):
    """(horizontal scale into the CRS's unit, vertical metres-per-unit).

    THE FILE CAN CONTRADICT ITSELF AND ROUTINELY DOES. The USGS 3DEP tile that
    prompted this declares ProjectedCSTypeGeoKey 26943 -- NAD83 / California
    zone 3, whose axis unit is the METRE -- while ProjLinearUnitsGeoKey says
    9003, the US survey foot, and its own citation string ends "..._Feet".
    LAStools writes this shape constantly: the EPSG code names the metre
    variant, the coordinates are feet.

    Taking parse_crs() at its word transformed feet as though they were metres
    and put a Central Coast tile at lon -71.2, in the Atlantic off
    Massachusetts. It rendered perfectly. That is the failure mode worth
    naming: a projection mistake does not look like an error, it looks like a
    map.

    Resolving it needs no lookup table of feet-variant EPSG codes. If the
    numbers are feet and the CRS expects metres, scale the numbers -- the
    projection maths is identical either way.
    """
    declared = LINEAR_UNIT_M.get(_geokey(header, PROJ_LINEAR_UNITS_KEY))
    try:
        crs_unit = float(crs.axis_info[0].unit_conversion_factor)
    except Exception:  # noqa: BLE001
        crs_unit = 1.0
    hscale = 1.0
    if declared and abs(declared - crs_unit) > 1e-9:
        hscale = declared / crs_unit

    vertical = LINEAR_UNIT_M.get(_geokey(header, VERTICAL_UNITS_KEY))
    if vertical is None:
        # no vertical geokey: heights are almost always in the horizontal
        # unit, and that is a far better default than assuming metres. The
        # manifest records whatever was used.
        vertical = declared if declared else crs_unit
    return hscale, float(vertical)


def _assert_inside(crs, lon, lat):
    """Refuse a result that falls outside the source CRS's own area of use.

    The cheapest possible guard against the whole class of projection bug, and
    it would have caught the feet-as-metres error the moment it happened
    rather than at the point where somebody noticed California was in the
    Atlantic. A CRS knows where it is for; a transform that leaves that box is
    wrong by the CRS's own account.
    """
    import numpy as np
    a = crs.area_of_use
    if a is None:
        return
    pad = 1.0                       # degrees, for tiles that graze the edge
    ok = ((lon >= a.west - pad) & (lon <= a.east + pad)
          & (lat >= a.south - pad) & (lat <= a.north + pad))
    frac = float(np.count_nonzero(ok)) / max(1, ok.size)
    if frac < 0.5:
        raise ValueError(
            f"reprojection left the source CRS's area of use: only {frac:.1%} of "
            f"points fall inside {a.name} "
            f"(lon {a.west}..{a.east}, lat {a.south}..{a.north}); "
            f"got lon {float(np.nanmin(lon)):.4f}..{float(np.nanmax(lon)):.4f}, "
            f"lat {float(np.nanmin(lat)):.4f}..{float(np.nanmax(lat)):.4f}. "
            "Refusing to emit cells: a wrong projection renders as a map, not as an error.")


def hexify_las(path, res=12, max_points=0, chunk=500_000):
    """Read a LAS/LAZ file and fold its returns into H3 cells.

    STREAMED, IN TWO PASSES, and the reason is an OOM kill rather than taste.
    The first version called laspy.read() and built five full-length arrays on
    top of the whole decompressed file -- and then `idx`, a Python LIST OF H3
    STRINGS, one per point, at roughly 72 bytes each. For a 363MB 3DEP tile
    that list alone is gigabytes. ingestd died at exit 137 against a 1Gi limit,
    which is exactly the failure DESIGN.md predicted when it argued ingestion
    deserved its own deployment: "memory-hungry (a decompressed lidar tile is
    hundreds of MB)".

    Now memory is O(chunk + distinct cells) and does not grow with the file.
    Cells are the only thing that accumulates, and cells are what we are for.

    WHY TWO PASSES. The outlier ceiling is a percentile, and a percentile needs
    to have seen the distribution before it can filter it. Pass one streams the
    file collecting a systematic sample of z; pass two streams it again and
    aggregates with the bounds applied. That costs a second decompression on a
    job that runs once per tile, and it buys a filter that is computed from the
    data rather than guessed at -- which is the difference between dropping the
    16 bogus water returns and dropping somebody's mountain.
    """
    import h3
    import laspy
    import numpy as np
    from pyproj import CRS, Transformer

    t0 = time.time()
    with laspy.open(path) as f:
        header = f.header
        n_total = header.point_count
        src = header.parse_crs()
        if src is None:
            # Unprocessable, not ValueError: no number of retries will add a
            # CRS to a file that has none, and ingestd needs to know that from
            # the refusal itself rather than by matching on this message.
            raise Unprocessable(
                "the file declares no CRS. Refusing to guess: an unprojected guess "
                "puts the tile somewhere confidently wrong rather than nowhere.")
        src = CRS.from_user_input(src)

    # the geokeys live on the VLRs, which laspy exposes on a lightweight read
    with laspy.open(path) as f:
        hscale, vfactor = _units(f.header, src)

    tf = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)

    # a systematic sample, taken across the WHOLE file rather than from the
    # first chunk: LAS is usually stored in acquisition order, so a
    # leading-edge sample is one flight line and not the tile.
    stride = max(1, n_total // 200_000)
    zs = []
    with laspy.open(path) as f:
        for pts in f.chunk_iterator(chunk):
            z = np.asarray(pts.z, dtype="float64")[::stride] * vfactor
            cl = np.asarray(pts.classification, dtype="uint8")[::stride]
            good = np.isfinite(z)
            for nc in NOISE_CLASSES:
                good &= cl != nc
            if good.any():
                zs.append(z[good])
    lo = hi = None
    if zs:
        sample = np.concatenate(zs)
        if sample.size > 100:
            a, b = np.percentile(sample, [0.1, 99.9])
            lo, hi = float(a) - OUTLIER_MARGIN_M, float(b) + OUTLIER_MARGIN_M
    del zs

    g_sum, g_n, s_max, n_all, w_n = {}, {}, {}, {}, {}
    n_used = n_noise = n_outliers = 0
    cls_counts = {}
    step = 1
    if max_points and n_total > max_points:
        step = int(np.ceil(n_total / max_points))

    with laspy.open(path) as f:
        for pts in f.chunk_iterator(chunk):
            x = np.asarray(pts.x, dtype="float64")[::step] * hscale
            y = np.asarray(pts.y, dtype="float64")[::step] * hscale
            z = np.asarray(pts.z, dtype="float64")[::step] * vfactor
            cls = np.asarray(pts.classification, dtype="uint8")[::step]

            keep = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
            for nc in NOISE_CLASSES:
                keep &= cls != nc
            n_noise += int(np.count_nonzero(~keep))
            if hi is not None:
                before = int(np.count_nonzero(~keep))
                keep &= (z >= lo) & (z <= hi)
                n_outliers += int(np.count_nonzero(~keep)) - before
            if not keep.any():
                continue

            x, y, z, cls = x[keep], y[keep], z[keep], cls[keep]
            lon, lat = tf.transform(x, y)
            fin = np.isfinite(lon) & np.isfinite(lat)
            lon, lat, z, cls = lon[fin], lat[fin], z[fin], cls[fin]
            if lon.size == 0:
                continue
            if n_used == 0:
                # guard on the first real chunk: a wrong projection is wrong
                # from the first point, and failing now beats failing after
                # forty million of them
                _assert_inside(src, lon, lat)

            for k, v in zip(*np.unique(cls, return_counts=True)):
                cls_counts[int(k)] = cls_counts.get(int(k), 0) + int(v)

            ground = cls == GROUND_CLASS
            water = cls == WATER_CLASS
            for i in range(lon.size):
                c = h3.latlng_to_cell(float(lat[i]), float(lon[i]), res)
                n_all[c] = n_all.get(c, 0) + 1
                zi = float(z[i])
                if zi > s_max.get(c, -1e30):
                    s_max[c] = zi
                if ground[i]:
                    g_sum[c] = g_sum.get(c, 0.0) + zi
                    g_n[c] = g_n.get(c, 0) + 1
                if water[i]:
                    w_n[c] = w_n.get(c, 0) + 1
            n_used += int(lon.size)

    cells = {}
    for c, n in n_all.items():
        row = {"point_density": n, "elevation_surface": round(s_max[c], 3)}
        if w_n.get(c):
            # share of the cell that is water, 0..1. INTENSIVE (a ratio folds
            # by mean); it is what makes a coastline legible on a tile that is
            # mostly ocean.
            row["water_share"] = round(w_n[c] / n, 3)
        if g_n.get(c):
            # MEAN, because elevation is intensive. A cell with no ground
            # return gets NO elevation_ground key at all rather than a zero:
            # absence and zero are different facts and the contract has a
            # NoData reason for exactly this.
            row["elevation_ground"] = round(g_sum[c] / g_n[c], 3)
        cells[c] = row

    # WHERE THIS TILE IS, recorded now because the cells are in hand and the
    # answer costs four numbers. Without it the only way to know where a
    # dataset sits is to download it, so a consumer asking "what overlaps my
    # window" has to read the whole shelf to find out -- which is how dodo's
    # map pane got OOM-killed at 2.1M cells.
    from hexify import bounds_of
    bbox = bounds_of(cells, h3)

    meta = {
        "res": res,
        "cells": len(cells),
        "bbox": bbox,
        "points_read": int(n_total),
        "points_used": int(n_used),
        "sampled": step > 1,
        "subsample_step": step,
        "source_crs": src.to_string(),
        "source_epsg": src.to_epsg(),
        "horizontal_scale_applied": hscale,
        "vertical_unit_to_metres": vfactor,
        "ground_returns": int(cls_counts.get(GROUND_CLASS, 0)),
        "dropped_noise_or_nonfinite": n_noise,
        "dropped_outliers": n_outliers,
        "outlier_bounds_m": [round(lo, 2), round(hi, 2)] if hi is not None else None,
        # what the file SAYS its returns are. Recorded because it changes what
        # the tile means: the first one ingested is 98.8% water, so a sparse
        # elevation_ground is the coastline being mostly ocean, not a broken
        # pipeline.
        "classification_counts": cls_counts,
        "elapsed_s": round(time.time() - t0, 2),
    }
    return cells, meta


def ingest_pointcloud(payload, item, dest_dir, res=None, max_points=None):
    """The ingestd pipeline entry for LAYER_KIND_INTENSIVE point clouds."""
    res = int(res if res is not None else os.environ.get("INGESTD_H3_RES", "12"))
    # 2,000,000 while the pipeline held whole files in memory; the cap was the
    # only thing standing between a normal tile and the OOM killer. Streaming
    # retired that reason -- 28 million points ran at 206MB -- so the cap now
    # guards TIME rather than memory, and a normal 3DEP tile should be ingested
    # whole. Measured: 28M points in 28s. Subsampling is still recorded in the
    # manifest when it bites, because a thinned surface that claims to be
    # complete is the kind of wrong nobody catches.
    max_points = int(max_points if max_points is not None
                     else os.environ.get("INGESTD_MAX_POINTS", "50000000"))
    cells, stats = hexify_las(payload, res=res, max_points=max_points)

    tmp = dest_dir + ".tmp"
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "cells.json"), "w") as f:
        json.dump({"res": stats["res"], "cells": cells}, f)

    manifest = {
        "id": item["id"],
        "title": item.get("title", item["id"]),
        "description": item.get("description")
            or (f"{stats['cells']:,} H3 cells at resolution {stats['res']}, folded from "
                f"{stats['points_used']:,} lidar returns. elevation_ground is the mean of "
                f"bare-earth returns; elevation_surface is the highest return in the cell "
                f"(canopy and rooftops); point_density is returns per cell."),
        "kind": item.get("kind", "LAYER_KIND_INTENSIVE"),
        "layers": {
            "elevation_ground": {"kind": "LAYER_KIND_INTENSIVE", "unit": "m"},
            "elevation_surface": {"kind": "LAYER_KIND_INTENSIVE", "unit": "m"},
            "point_density": {"kind": "LAYER_KIND_EXTENSIVE", "unit": "returns"},
            "water_share": {"kind": "LAYER_KIND_INTENSIVE", "unit": "fraction"},
        },
        "content": "cells.json",
        "content_type": "application/json",
        "vintage_id": item.get("vintage_id", ""),
        "source_id": item.get("source_id", ""),
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provenance": item.get("download_url", ""),
        "pipeline": stats,
    }
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    os.rename(tmp, dest_dir)
    return stats


def fold_up(cells, res_from, res_to):
    """Roll cells up to a coarser resolution, folding each layer by its kind.

    THE PROPERTY THAT MAKES THIS POSSIBLE is h3's containment hierarchy: every
    cell has exactly one parent at each coarser resolution, so a tile is hexed
    ONCE at the finest honest resolution and every coarser view is derived
    without touching the points again.

    THE FOLD OPERATOR IS PER LAYER AND GETTING IT WRONG IS SILENT. A count is
    EXTENSIVE and folds by SUM; an elevation is INTENSIVE and folds by MEAN.
    Fold a count by mean and dense areas deflate; fold an elevation by sum and
    the terrain reaches orbit. kingfisher.map.v1.LayerKind exists to carry this
    distinction precisely because neither mistake raises anything.

    AND THE MEAN MUST BE WEIGHTED. This is the trap inside the trap: averaging
    the child cells' averages treats a cell holding 2 returns as equal to one
    holding 500. At the coarse end that is not a rounding error -- a res-7 cell
    spans more than one 3DEP tile, so its children have wildly unequal support,
    and the unweighted answer is dominated by whichever cells happened to be
    sparse. Each child is weighted by the returns that actually backed it.

    Coverage rides along for the same reason: a coarse cell assembled from two
    tiles when it needs three is a partial answer, and it has to be able to say
    so rather than looking complete.
    """
    import h3
    if res_to >= res_from:
        raise ValueError(f"fold_up goes coarser: {res_from} -> {res_to} is not")

    INTENSIVE = ("elevation_ground", "elevation_surface", "water_share")
    EXTENSIVE = ("point_density",)

    acc = {}
    for cell, row in cells.items():
        parent = h3.cell_to_parent(cell, res_to)
        a = acc.setdefault(parent, {"w": {}, "wsum": {}, "sum": {}, "children": 0, "returns": 0})
        a["children"] += 1
        n = float(row.get("point_density", 0) or 0)
        a["returns"] += n
        for k in EXTENSIVE:
            if k in row:
                a["sum"][k] = a["sum"].get(k, 0.0) + float(row[k])
        for k in INTENSIVE:
            if k in row:
                # weight by the returns behind this child, never by 1
                a["w"][k] = a["w"].get(k, 0.0) + float(row[k]) * n
                a["wsum"][k] = a["wsum"].get(k, 0.0) + n

    out = {}
    for parent, a in acc.items():
        row = {k: (int(v) if float(v).is_integer() else round(v, 3))
               for k, v in a["sum"].items()}
        for k, num in a["w"].items():
            den = a["wsum"].get(k, 0.0)
            if den > 0:
                row[k] = round(num / den, 3)
        # how much of this parent actually has data. A parent has 7^(dr)
        # descendants nominally; h3's pentagons make that approximate, which is
        # why this is reported as observed support rather than asserted as a
        # percentage of a number that is not exactly right.
        row["child_cells"] = a["children"]
        out[parent] = row
    return out
