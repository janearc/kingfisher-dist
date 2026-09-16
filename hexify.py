#!/usr/bin/env python3
# hexify -- raster tiles in, h3 cells out. The piece kingfisher was named for
# and never had.
#
# kingfisher's pyproject has described it as "H3 map data over HTTP" since the
# start, with "protobuf first, h3 next" in the comment above its dependency
# list. h3 never arrived, so every ground layer stayed raster: gibsd shelves
# nine square GOES PNGs with bounds, and the display layer reached past
# kingfisher entirely for satellite, elevation and VIIRS. This module is the
# missing half. "hex. hex. hex."
#
# CELL-DRIVEN, NOT PIXEL-DRIVEN. The obvious way round is to walk the raster
# and bin each pixel into whatever cell it lands in. That leaves holes -- a
# cell whose centre falls between sampled pixels gets nothing, and the holes
# move when the zoom changes, which reads as noise. So this walks the CELLS,
# asks each one where it is, and samples the raster there. Every cell in the
# region gets a value or is honestly absent, and the output is stable across
# zooms because it is keyed on geography, not on pixels.
#
# The web-mercator arithmetic is here rather than borrowed: it is eight lines,
# it is exact, and a wrong projection is the failure that puts data on the
# wrong continent while looking entirely plausible.

import math

# terrarium encodes elevation in the RGB channels, documented by Mapzen:
# height = (R * 256 + G + B / 256) - 32768, in metres. It is not a colour
# ramp -- reading it as one gives a pretty picture of nothing.
TERRARIUM_OFFSET = 32768.0


def lonlat_to_pixel(lon, lat, zoom, tile_size=256):
    # web mercator, world pixel space at this zoom
    n = tile_size * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    # clamp: the mercator projection has no north or south pole, and a lat of
    # exactly 90 divides by zero rather than failing usefully
    s = max(-0.9999, min(0.9999, s))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def tile_of(lon, lat, zoom):
    x, y = lonlat_to_pixel(lon, lat, zoom)
    return int(x // 256), int(y // 256)


def tile_range(bbox, zoom):
    # bbox is (west, south, east, north). Returns the inclusive x/y tile range
    # covering it -- north maps to the SMALLER y, which is the sign error this
    # function exists to make once, here, instead of at every call site.
    west, south, east, north = bbox
    x0, y0 = tile_of(west, north, zoom)
    x1, y1 = tile_of(east, south, zoom)
    return range(min(x0, x1), max(x0, x1) + 1), range(min(y0, y1), max(y0, y1) + 1)


def cells_in(bbox, res, h3):
    # every h3 cell whose centre lies in the bbox, found by scanning at half a
    # cell's spacing and deduplicating. Deliberately not h3's polyfill: that
    # helper has been renamed twice across h3 versions (polyfill ->
    # polygon_to_cells -> h3shape_to_cells) and a scan cannot be broken by the
    # next rename. The cost is a set insert per sample, which is nothing next
    # to the network fetch this feeds.
    west, south, east, north = bbox
    edge_km = h3.average_hexagon_edge_length(res, unit="km")
    step = max(edge_km / 111.0, 1e-4)  # degrees, roughly, and never zero
    out = set()
    lat = south
    while lat <= north:
        lon = west
        while lon <= east:
            out.add(h3.latlng_to_cell(lat, lon, res))
            lon += step
        lat += step
    return out


def sample_rgb(tiles, lon, lat, zoom, tile_size=256):
    # tiles maps (x, y) -> a pixel-access object (PIL .load()). Returns the
    # RGB under this coordinate, or None when the tile was never fetched --
    # absent is a legitimate answer and is never faked as black, which would
    # read as "sea level" or "midnight" downstream.
    px, py = lonlat_to_pixel(lon, lat, zoom, tile_size)
    tx, ty = int(px // tile_size), int(py // tile_size)
    t = tiles.get((tx, ty))
    if t is None:
        return None
    ix = min(tile_size - 1, max(0, int(px) - tx * tile_size))
    iy = min(tile_size - 1, max(0, int(py) - ty * tile_size))
    p = t[ix, iy]
    return (p[0], p[1], p[2])


def terrarium_metres(rgb):
    r, g, b = rgb
    return (r * 256.0 + g + b / 256.0) - TERRARIUM_OFFSET


def hexify(tiles, bbox, zoom, res, h3, mode="rgb", tile_size=256):
    # the whole job: for every cell in the region, sample the raster at its
    # centre and record the value. mode "rgb" keeps colour (imagery); mode
    # "elevation" decodes terrarium metres (relief).
    out = {}
    for cell in cells_in(bbox, res, h3):
        lat, lon = h3.cell_to_latlng(cell)
        rgb = sample_rgb(tiles, lon, lat, zoom, tile_size)
        if rgb is None:
            continue
        out[cell] = list(rgb) if mode == "rgb" else round(terrarium_metres(rgb), 1)
    return out


def bounds_of(cells, h3):
    # the geographic extent of a set of H3 cells, as [w, s, e, n].
    #
    # WHY THIS IS RECORDED RATHER THAN DERIVED ON DEMAND. "Which datasets
    # overlap the window I am looking at" is the difference between a consumer
    # fetching what it can draw and a consumer fetching the whole shelf and
    # being OOM-killed by it -- which is exactly what happened to dodo's map
    # pane on 2026-09-01, at 2.1M cells against a 256Mi limit. Deriving the
    # answer costs a read of every cell; storing it costs four numbers. So it
    # is computed once, here, while the cells are already in hand.
    #
    # THE ANTIMERIDIAN IS NOT HANDLED, deliberately and audibly. A set
    # straddling +/-180 would report a box the width of the world, which is a
    # wrong answer that looks entirely plausible and would silently pull every
    # dataset into every query. Nothing on the shelf does this today (USGS is
    # CONUS; GIBS snapshots are already whole-world). So a straddling set
    # returns None -- absent, and obviously so -- rather than lying.
    lats, lons = [], []
    for c in cells:
        lat, lon = h3.cell_to_latlng(c)
        lats.append(lat)
        lons.append(lon)
    if not lats:
        return None
    w, e = min(lons), max(lons)
    # GLOBAL IS NOT STRADDLING, and conflating them cost an afternoon's
    # basemap. A hexed world snapshot legitimately spans the full 360, and the
    # first guard here refused it as an antimeridian case -- so the one dataset
    # that overlaps every window recorded no bounds at all and was excluded
    # from every query, silently, while the lidar beside it drew fine.
    #
    # EXTENT CANNOT TELL THEM APART: two cells at +/-179 span 358 degrees and
    # are a straddle; a world grid spans 360 and is not. THE GAP CAN. A global
    # set is continuous in longitude; a straddling one is two strips with a
    # hole between them. So look for the hole.
    if e - w > 180.0:
        ordered = sorted(lons)
        gap = max((b - a for a, b in zip(ordered, ordered[1:])), default=0.0)
        if gap > 180.0:
            # a real hole in the middle: this wraps the antimeridian, and a
            # box drawn round it would claim the whole planet. Refuse it --
            # absent and obviously so beats plausible and wrong.
            return None
        return [-180.0, math.floor(min(lats) * 1e6) / 1e6,
                180.0, math.ceil(max(lats) * 1e6) / 1e6]
    # ROUNDED OUTWARD, NEVER round(). Rounding a bound to the nearest
    # micro-degree moves it the wrong way half the time, and a box that
    # excludes its own cells is a dataset that vanishes from a query aimed
    # straight at it. Six decimals is about 10cm; the outward bias is free.
    q = 1e6
    return [math.floor(w * q) / q, math.floor(min(lats) * q) / q,
            math.ceil(e * q) / q, math.ceil(max(lats) * q) / q]


def bbox_overlaps(a, b):
    # do two [w, s, e, n] boxes intersect? Touching edges count: a tile whose
    # northern edge is the window's southern edge does hold cells on that line.
    if not a or not b:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# Rec. 709 luminance. The weights are not decorative: green carries most of
# perceived brightness, and a naive (r+g+b)/3 makes a blue ocean and a green
# forest read as the same value, which is a map of nothing.
def luminance(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def sample_equirect(px, size, bbox, lon, lat):
    # A PLATE-CARREE IMAGE IS NOT A TILE PYRAMID. A GIBS snapshot is one image
    # for one bbox in EPSG:4326, so the mapping from a coordinate to a pixel is
    # linear in both axes -- no mercator, no zoom, no tile arithmetic. Doing it
    # the tile way would be eight lines of correct-looking mathematics applied
    # to the wrong projection, which is the failure that renders as a map.
    w, s, e, n = bbox
    if not (s <= lat <= n and w <= lon <= e):
        return None
    iw, ih = size
    x = int((lon - w) / (e - w) * (iw - 1))
    y = int((n - lat) / (n - s) * (ih - 1))
    p = px[x, y]
    return (p[0], p[1], p[2])


def hexify_equirect(px, size, bbox, res, h3, mode="luminance"):
    """One equirectangular image -> H3 cells.

    CELL-DRIVEN, like hexify() above and for the same reason: walking the
    pixels leaves holes where a cell centre falls between samples, and the
    holes move when the zoom changes, which reads as noise. Walking the cells
    means every cell in the window gets a value or is honestly absent.

    mode "luminance" gives one float per cell, which is what a value ramp and
    the text renderer both want. mode "rgb" keeps all three channels -- but
    note that a renderer taking cell -> number will drop an [r,g,b] list, so
    luminance is the mode that draws.
    """
    out = {}
    for cell in cells_in(bbox, res, h3):
        lat, lon = h3.cell_to_latlng(cell)
        rgb = sample_equirect(px, size, bbox, lon, lat)
        if rgb is None:
            continue
        out[cell] = list(rgb) if mode == "rgb" else round(luminance(rgb), 2)
    return out
