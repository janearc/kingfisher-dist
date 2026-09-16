# textmap.py -- the map-to-text pipeline, as a library.
#
# WHY THIS EXISTS. _draw() in bin/kingfisher was a good renderer trapped in a
# CLI: it fit the frame to the data, painted once, and exited. That is the
# right shape for `render` and the wrong shape for a bench, which needs the
# opposite -- an explicit viewport you can move, an H3 resolution you can
# change, and a redraw cheap enough to run on every keypress.
#
# So the pipeline comes out of the script and becomes three objects:
#
#     CellSource   cells for one dataset, re-aggregated to any H3 resolution
#     Viewport     the window on the world: centre, span, and the grid it fills
#     TextMap      points + viewport -> a list of strings
#
# Nothing here knows what lidar is, or what kingfisher is. It knows H3 cells
# and floats, which is the property that lets anything in the estate that can
# emit cells become a picture.

import math

# A terminal cell is about twice as tall as it is wide, and U+2584 (lower half
# block) puts two map rows in one character row -- foreground paints the
# bottom, background the top. So a grid of C columns by R character rows is a
# pixel grid of C by 2R, and those pixels are roughly square.
PIXEL_ROWS_PER_CHAR = 2
LOWER_HALF_BLOCK = "▄"

# The density ramp, for when colour cannot survive the trip -- a pipe, a paste,
# a terminal that does not do 24-bit.
#
# A SPACE IS NEVER A VALUE. This ramp used to lead with one, which drew the
# BOTTOM OF EVERY SCALE exactly like ground nobody surveyed -- the lowest
# valley in a lidar tile and the ocean beside it, identical blanks. The colour
# path has always kept those apart (absence is the only thing that paints
# nothing); the ascii path quietly did not, and the rule holds in every
# renderer or in none. So the ramp is data only, and blank means absent.
RAMP = ".:-=+*#%@"

# H3 tops out at 15. Nothing can be rendered finer than the data was folded,
# so the real ceiling is per-source (native_res) and this is only the clamp.
MAX_RES = 15

# cold to hot, through the same slots the map uses: blue, cyan, green, amber,
# red. Linear between stops; nobody needs a colour science library to make a
# terminal look good.
_STOPS = [(0x2b, 0x4b, 0xd9), (0x00, 0xc8, 0xe0), (0x3f, 0xd0, 0x6b),
          (0xe8, 0xa3, 0x3d), (0xe0, 0x4f, 0x4f)]


# the colour for a normalised value in 0..1
def ramp_rgb(t):
    """The colour for a normalised value in 0..1."""
    t = max(0.0, min(1.0, t))
    x = t * (len(_STOPS) - 1)
    i = int(x)
    if i >= len(_STOPS) - 1:
        return _STOPS[-1]
    a, b, f = _STOPS[i], _STOPS[i + 1], x - i
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


# the density character for a normalised value in 0..1
def ramp_char(t):
    """The density character for a normalised value in 0..1."""
    t = max(0.0, min(1.0, t))
    return RAMP[min(len(RAMP) - 1, int(t * len(RAMP)))]


class CellSource:
    """Cells for one dataset, re-aggregated to any H3 resolution on demand.

    Takes the two shapes that already exist in the estate, and neither is
    wrong: a mapping of cell -> value, and a mapping of cell -> {layer: value}.
    A bare list of cell ids is a COVERAGE map -- every cell at value 1 -- and
    the caller turns it into the first shape before it arrives here.
    """

    # name is what the bench puts in the header; layer_kinds comes from the
    # dataset manifest and decides how values combine when cells are rolled up
    def __init__(self, name, cells, layer_kinds=None):
        self.name = name
        self.layer_kinds = layer_kinds or {}
        # normalise both input shapes to cell -> {layer: value}
        self.cells = {}
        for cell, row in cells.items():
            if isinstance(row, dict):
                keep = {k: float(v) for k, v in row.items()
                        if isinstance(v, (int, float))}
                if keep:
                    self.cells[cell] = keep
            elif isinstance(row, (int, float)):
                self.cells[cell] = {"value": float(row)}
        self._native = None
        self._cache = {}

    # the resolution the data was actually folded at -- the ceiling for any
    # rollup, because nothing can be drawn finer than it was measured
    def native_res(self):
        if self._native is None:
            import h3
            self._native = 0
            for cell in self.cells:
                self._native = h3.get_resolution(cell)
                break
        return self._native

    # every measure any cell carries, sorted so the bench's layer cycling is
    # stable between runs
    def layers(self):
        seen = set()
        for row in self.cells.values():
            seen.update(row)
        return sorted(seen)

    # EXTENSIVE means the value is a COUNT OF THINGS IN THE CELL (returns,
    # people, rides) and rolling children into a parent must ADD them.
    # INTENSIVE means the value is a PROPERTY OF THE PLACE (elevation,
    # temperature) and the parent takes the mean. Averaging an extensive layer
    # up a level silently divides it by the number of children, which is the
    # kind of wrong that still looks like a map.
    def is_extensive(self, layer):
        return "EXTENSIVE" in str(self.layer_kinds.get(layer, ""))

    # VALUE BY CONTAINMENT, for when a cell is bigger than a pixel.
    #
    # points() scatters cell centres, which is right while cells are finer
    # than the grid -- several land per pixel and the picture fills. Zoom out
    # far enough and the ratio inverts: at res 2 a cell is 150km across, a
    # world-sized window is 13,000km, and 1,254 cells landed in 1,254 of
    # 12,936 pixels. The rest stayed blank and the map came out as confetti.
    #
    # So the loop turns round. Instead of asking a cell which pixel it lands
    # in, the pixel asks which cell it is inside. Note this is the exact
    # inverse of hexify's cell-driven rule and for the same reason: whichever
    # side is COARSER has to drive, or the finer side gets holes.
    #
    # Walks coarser on a miss because one answer can hold mixed resolutions --
    # a res-4 basemap beside res-8 lidar -- so the cell containing a pixel is
    # not always at the resolution the caller asked for.
    def sampler(self, res, layer):
        import h3
        table = {}
        for cell, row in self.cells.items():
            v = row.get(layer)
            if v is not None:
                table[cell] = float(v)
        if not table:
            return None
        finest = max(h3.get_resolution(c) for c in table)
        top = min(res, finest)

        def at(lat, lon):
            for r in range(top, -1, -1):
                hit = table.get(h3.latlng_to_cell(lat, lon, r))
                if hit is not None:
                    return hit
            return None
        return at

    # points for one layer at one resolution, as (lon, lat, value). Cached,
    # because the bench calls this on every keypress and converting a million
    # cells through h3 is not free.
    def points(self, res, layer):
        key = (res, layer)
        if key in self._cache:
            return self._cache[key]
        import h3
        native = self.native_res()
        res = max(0, min(res, native))
        acc = {}
        for cell, row in self.cells.items():
            v = row.get(layer)
            if v is None:
                continue
            parent = cell if res == native else h3.cell_to_parent(cell, res)
            a = acc.setdefault(parent, [0.0, 0])
            a[0] += v
            a[1] += 1
        extensive = self.is_extensive(layer)
        pts = []
        for parent, (total, n) in acc.items():
            lat, lon = h3.cell_to_latlng(parent)
            pts.append((lon, lat, total if extensive else total / n))
        self._cache[key] = pts
        return pts


class Viewport:
    """The window on the world: a centre, a span, and the grid it fills.

    THE WHOLE REASON THIS EXISTS is that a renderer which fits itself to its
    data cannot be panned -- every move re-derives the same extent and nothing
    appears to happen. An explicit viewport is what makes hjkl mean something.

    Work happens in PROJECTED units: x = lon * cos(centre latitude), y = lat.
    That is equirectangular with the aspect corrected for where you are
    standing, and it keeps a degree of x and a degree of y the same size on
    screen. Web mercator would be wrong here: it is the projection for SQUARE
    tiles, and a terminal cell is not square.
    """

    # span_x is the width of the window in projected degrees; the height is
    # derived from the grid so pixels stay square
    def __init__(self, lon, lat, span_x, cols, rows):
        self.lon = lon
        self.lat = lat
        self.span_x = max(1e-9, span_x)
        self.cols = max(1, cols)
        self.rows = max(1, rows)

    # the pixel grid behind the character grid: two map rows per character row
    def pixels(self):
        return self.cols, self.rows * PIXEL_ROWS_PER_CHAR

    # degrees of longitude per projected degree, at this latitude
    def _kx(self):
        return max(1e-6, math.cos(math.radians(self.lat)))

    # the window's height in projected degrees, from the grid's aspect
    def span_y(self):
        w, h = self.pixels()
        return self.span_x * h / w

    # west, south, east, north -- in real degrees, for clipping and headers
    def bounds(self):
        half_x = self.span_x / 2 / self._kx()
        half_y = self.span_y() / 2
        return (self.lon - half_x, self.lat - half_y,
                self.lon + half_x, self.lat + half_y)

    # a point's pixel coordinates, or None when it falls outside the window.
    # CLIPPING IS THE POINT: a viewport that drew everything would be a fit.
    def project(self, lon, lat):
        w, h = self.pixels()
        kx = self._kx()
        x = (lon - self.lon) * kx / self.span_x * w + w / 2
        y = h / 2 - (lat - self.lat) / self.span_y() * h
        if x < 0 or y < 0 or x >= w or y >= h:
            return None
        return int(x), int(y)

    # THE SAME POINT, ADDRESSED THE WAY A COMPOSITOR THINKS. project() answers
    # in pixels; a character grid is half as tall, and which half a pixel
    # landed in decides whether it is the block's foreground or its
    # background. Three lines of arithmetic -- but three lines that would
    # otherwise exist on both sides of a socket, derived from a half-block
    # convention that lives here. One number, one place.
    #
    # Returns (col, row, half), half 0 for the top of the cell and 1 for the
    # bottom, or None when the point is outside the window. NEVER CLAMPED: a
    # sprite pinned to the border because its city is off-screen is a wrong
    # answer wearing the costume of a right one.
    def char_cell(self, lon, lat):
        xy = self.project(lon, lat)
        if xy is None:
            return None
        x, y = xy
        return x, y // PIXEL_ROWS_PER_CHAR, y % PIXEL_ROWS_PER_CHAR

    # the coordinate at the centre of a pixel -- project() run backwards.
    # Needed because a cell coarser than a pixel cannot be painted by
    # scattering its centre; the pixel has to ask which cell it is inside.
    def unproject(self, x, y):
        w, h = self.pixels()
        kx = self._kx()
        lon = self.lon + (x + 0.5 - w / 2) * self.span_x / w / kx
        lat = self.lat - (y + 0.5 - h / 2) * self.span_y() / h
        return lon, lat

    # move by a fraction of the window -- 0.25 is a comfortable hjkl step,
    # 1.0 is a full page. Panning north near the pole would flip the frame
    # inside out, so latitude is clamped short of it.
    def pan(self, fx, fy):
        self.lon += fx * self.span_x / self._kx()
        self.lat = max(-85.0, min(85.0, self.lat + fy * self.span_y()))

    # zoom about the centre. factor < 1 moves in, > 1 moves out.
    def zoom(self, factor):
        self.span_x = max(1e-7, min(360.0, self.span_x * factor))

    # the window that shows all of it, with a little air around the edge
    @classmethod
    def fit(cls, pts, cols, rows, margin=1.06):
        if not pts:
            return cls(0.0, 0.0, 360.0, cols, rows)
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        lon = (min(lons) + max(lons)) / 2
        lat = (min(lats) + max(lats)) / 2
        kx = max(1e-6, math.cos(math.radians(lat)))
        ext_x = max(1e-7, (max(lons) - min(lons)) * kx)
        ext_y = max(1e-7, (max(lats) - min(lats)))
        w = max(1, cols)
        h = max(1, rows) * PIXEL_ROWS_PER_CHAR
        # take whichever axis is the binding constraint, so nothing is cropped
        span_x = max(ext_x, ext_y * w / h) * margin
        return cls(lon, lat, span_x, cols, rows)


class TextMap:
    """Points plus a viewport, painted as a list of strings.

    One frame, one object, no I/O. The bench writes what comes back; the tests
    assert on it without a terminal anywhere in sight.
    """

    # colour is 24-bit half-blocks; ascii is the density ramp that survives a
    # pipe. scale pins lo..hi across frames when the caller has a frame set --
    # see the note on normalisation in frame().
    def __init__(self, ascii_only=False, scale=None):
        self.ascii_only = ascii_only
        self.scale = scale
        self.lo = 0.0
        self.hi = 0.0
        self.plotted = 0
        self.filled = 0

    # bin the points into the pixel grid, taking the mean of whatever lands in
    # each bin. This is a SCREEN-SPACE average and always a mean, even for an
    # extensive layer -- the rollup to H3 resolution already did the summing,
    # and adding again here would make the picture depend on the window size.
    def _bin(self, pts, vp):
        acc = {}
        for lon, lat, v in pts:
            xy = vp.project(lon, lat)
            if xy is None:
                continue
            a = acc.setdefault((xy[1], xy[0]), [0.0, 0])
            a[0] += v
            a[1] += 1
        return acc

    # THE ONE NORMALISATION, shared by every output shape. frame() and cells()
    # draw the same picture in two encodings, and the moment they each decided
    # their own lo..hi they would be two pictures that merely resembled each
    # other. Returns at(y, x) -> 0..1, or None for "nobody surveyed this".
    def _field(self, pts, vp, fill=None):
        acc = self._bin(pts, vp)
        self.plotted = sum(a[1] for a in acc.values())
        self.filled = 0
        # PAINT THE CELL, NOT ITS CENTRE. Every pixel the scatter missed asks
        # which cell contains it. Done BEFORE the scale is taken, so a coarse
        # cell whose centre sits outside the window still contributes its
        # value to lo..hi -- otherwise the fill would draw values the ramp was
        # never told about, and clip them to the ends.
        if fill is not None:
            w, h = vp.pixels()
            for y in range(h):
                for x in range(w):
                    if (y, x) in acc:
                        continue
                    got = vp.unproject(x, y)
                    if got is None:
                        continue          # space: the globe's pixels off-disc
                    lon, lat = got
                    # OFF THE GLOBE IS ABSENT, not the nearest pole. A frame
                    # wider than the world runs past +/-90, and sampling there
                    # smeared the polar row across everything above the data --
                    # a band of confident colour describing latitude 108.
                    # Longitude DOES wrap and is wrapped; latitude does not.
                    if not -90.0 <= lat <= 90.0:
                        continue
                    v = fill(lat, ((lon + 180.0) % 360.0) - 180.0)
                    if v is not None:
                        acc[(y, x)] = [v, 1]
                        self.filled += 1
        if not acc:
            self.lo = self.hi = 0.0
            return lambda y, x: None

        vals = [a[0] / a[1] for a in acc.values()]
        if self.scale is not None:
            lo, hi = self.scale
        else:
            lo, hi = min(vals), max(vals)
        self.lo, self.hi = lo, hi
        # A FLAT SOURCE IS A COVERAGE MAP, not an empty one. When every bin
        # holds the same value there is no magnitude to show, only presence --
        # and normalising a constant to zero renders the whole thing blank,
        # which is exactly backwards: every one of those bins is data.
        flat = (hi - lo) < 1e-12
        span = 1.0 if flat else (hi - lo)

        def at(y, x):
            a = acc.get((y, x))
            if a is None:
                return None
            return 1.0 if flat else (a[0] / a[1] - lo) / span

        return at

    # a whole frame, top row first.
    #
    # ALWAYS A FULL RECTANGLE, vp.rows strings of vp.cols columns, even when
    # nothing landed in the window. An empty string is a row that silently is
    # not there, which is the shape of bug where the map looks fine and is one
    # row short -- harmless in a terminal, wrong for anything compositing onto
    # a fixed region.
    def frame(self, pts, vp, fill=None):
        at = self._field(pts, vp, fill)
        return [self._row(at, y, vp.cols) for y in range(0, vp.rows * 2, 2)]

    # the same picture as styled cells rather than strings: a grid of
    # (rune, fg, bg), vp.rows by vp.cols, each colour an (r, g, b) or None.
    #
    # FOR COMPOSITORS. Handing out ANSI and asking a caller to parse it back
    # is worse than lossy, it is STATEFUL -- escapes are emitted only when the
    # colour changes, so a cell inherits the last colour set and no cell can
    # be read on its own. This is the same field, before any of that.
    #
    # bg is the northern half of the character cell and fg the southern, which
    # is the half-block convention the whole renderer is built on. Note that
    # BOTH ARE MAP DATA: there is no backdrop here to blend against.
    def cells(self, pts, vp, fill=None):
        at = self._field(pts, vp, fill)
        grid = []
        for row in range(vp.rows):
            y = row * PIXEL_ROWS_PER_CHAR
            line = []
            for x in range(vp.cols):
                top, bot = at(y, x), at(y + 1, x)
                bg = ramp_rgb(top) if top is not None else None
                fg = ramp_rgb(bot) if bot is not None else None
                line.append((LOWER_HALF_BLOCK if fg is not None else " ",
                             fg, bg))
            grid.append(line)
        return grid

    # one character row, from the two pixel rows it stands for
    def _row(self, at, y, cols):
        if self.ascii_only:
            return self._row_ascii(at, y, cols)
        return self._row_colour(at, y, cols)

    # the density ramp: no escapes at all, so the output survives a pipe, a
    # paste, and a terminal that does not do 24-bit colour
    def _row_ascii(self, at, y, cols):
        line = []
        for x in range(cols):
            vs = [v for v in (at(y, x), at(y + 1, x)) if v is not None]
            line.append(" " if not vs else ramp_char(sum(vs) / len(vs)))
        return "".join(line)

    # ESCAPES ONLY WHEN THE COLOUR CHANGES. Set-and-reset per character made a
    # 96-column frame 106KB, which is most of a megabyte for one page of map
    # and visibly slow to scroll.
    def _row_colour(self, at, y, cols):
        line = []
        cfg = cbg = None
        for x in range(cols):
            top, bot = at(y, x), at(y + 1, x)
            if top is None and bot is None:
                # ABSENCE IS BLANK, never a dark colour. A cell nobody
                # surveyed and one measuring zero are different facts, and the
                # rule holds in every renderer or in none.
                if cbg is not None:
                    line.append("\x1b[49m")
                    cbg = None
                line.append(" ")
                continue
            bg = ramp_rgb(top) if top is not None else None
            fg = ramp_rgb(bot) if bot is not None else None
            if bg != cbg:
                line.append("\x1b[49m" if bg is None
                            else f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                cbg = bg
            if fg is None:
                line.append(" ")
            else:
                if fg != cfg:
                    line.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
                    cfg = fg
                line.append(LOWER_HALF_BLOCK)
        line.append("\x1b[0m")
        return "".join(line)


class Globe:
    """The same window, wrapped round a ball. Orthographic, one hemisphere.

    INTERCHANGEABLE WITH Viewport, deliberately: pixels(), bounds(), project(),
    unproject(), pan(), zoom(). TextMap never learns which one it is holding,
    which is the whole reason a globe is cheap here -- the fill already asks
    every pixel WHICH CELL CONTAINS IT, so a projection only has to answer
    "what coordinate is this pixel" and "is this pixel even on the planet".

    Orthographic is the view from infinitely far away: the earth as a disc,
    the far side genuinely hidden rather than squashed round the edge. That
    hiding is the point. A globe that drew the far hemisphere would be a
    flat map with extra steps.
    """

    # radius is in PIXELS, so zoom is a scale and pan is an angle -- the same
    # two verbs the flat viewport has, meaning the same two things on screen
    def __init__(self, lon, lat, cols, rows, radius=None):
        self.lon = lon
        self.lat = lat
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        w, h = self.pixels()
        self.radius = radius or min(w, h) / 2 * 0.98

    # two map rows per character row, exactly as the flat viewport
    def pixels(self):
        return self.cols, self.rows * PIXEL_ROWS_PER_CHAR

    # the centre of the disc, in pixels
    def _centre(self):
        w, h = self.pixels()
        return w / 2, h / 2

    # a coordinate's pixel, or None when it is behind the planet or off frame.
    # THE COSINE OF THE ANGULAR DISTANCE decides visibility: negative means the
    # point is on the far side, and drawing it would fold the hemispheres on
    # top of each other.
    def project(self, lon, lat):
        p, l0 = math.radians(lat), math.radians(self.lon)
        p0, dl = math.radians(self.lat), math.radians(lon) - math.radians(self.lon)
        cosc = math.sin(p0) * math.sin(p) + math.cos(p0) * math.cos(p) * math.cos(dl)
        if cosc < 0:
            return None
        cx, cy = self._centre()
        x = cx + self.radius * math.cos(p) * math.sin(dl)
        y = cy - self.radius * (math.cos(p0) * math.sin(p)
                                - math.sin(p0) * math.cos(p) * math.cos(dl))
        w, h = self.pixels()
        if x < 0 or y < 0 or x >= w or y >= h:
            return None
        return int(x), int(y)

    # the coordinate under a pixel, or None for SPACE. Off the disc is not off
    # the map -- it is not the planet at all, and the difference is the whole
    # look of a globe.
    def unproject(self, x, y):
        cx, cy = self._centre()
        dx, dy = x + 0.5 - cx, y + 0.5 - cy
        rho = math.hypot(dx, dy)
        if rho > self.radius:
            return None
        if rho < 1e-9:
            return self.lon, self.lat
        c = math.asin(min(1.0, rho / self.radius))
        p0 = math.radians(self.lat)
        sin_c, cos_c = math.sin(c), math.cos(c)
        lat = math.asin(cos_c * math.sin(p0) + (-dy * sin_c * math.cos(p0)) / rho)
        lon = math.radians(self.lon) + math.atan2(
            dx * sin_c, rho * cos_c * math.cos(p0) + dy * sin_c * math.sin(p0))
        return (math.degrees(lon) + 540.0) % 360.0 - 180.0, math.degrees(lat)

    # what to ASK KINGFISHER FOR. A hemisphere's bounding box wraps the
    # antimeridian about half the time and swallows a pole whenever the view
    # is tilted, and both are cases the shelf's bbox filter refuses rather
    # than approximates. So when the visible disc does either, ask for the
    # world: the budget bounds the cost anyway, and an honest over-fetch beats
    # a box that quietly means the opposite of what it says.
    def bounds(self):
        w, h = self.pixels()
        lons, lats = [], []
        steps = 64
        for i in range(steps):
            a = 2 * math.pi * i / steps
            cx, cy = self._centre()
            got = self.unproject(cx + self.radius * 0.999 * math.cos(a) - 0.5,
                                 cy + self.radius * 0.999 * math.sin(a) - 0.5)
            if got:
                lons.append(got[0])
                lats.append(got[1])
        if not lons:
            return [-180.0, -90.0, 180.0, 90.0]
        ordered = sorted(lons)
        gap = max((b - a for a, b in zip(ordered, ordered[1:])), default=0.0)
        wrapped = gap > 180.0 or (max(lons) - min(lons)) > 180.0
        # a pole inside the disc means latitude is not bounded by the rim
        pole = any(self.project(0.0, s) is not None for s in (89.9, -89.9))
        if wrapped or pole:
            return [-180.0, min(lats) if not pole else -90.0,
                    180.0, max(lats) if not pole else 90.0]
        return [min(lons), min(lats), max(lons), max(lats)]

    # SPIN AND TILT, not slide. Panning a globe turns it, and the step is in
    # degrees rather than a fraction of the window -- a fraction would mean
    # something different at every zoom because the disc does not change size
    # with the data. Latitude stops just short of the pole, where the maths
    # is fine and the picture is not.
    def pan(self, fx, fy):
        self.lon = (self.lon + fx * 90.0 + 540.0) % 360.0 - 180.0
        self.lat = max(-89.0, min(89.0, self.lat + fy * 60.0))

    def zoom(self, factor):
        w, h = self.pixels()
        self.radius = max(2.0, min(self.radius / factor, min(w, h) * 20.0))

    # the flat viewport's span, for a status line that has to print something
    @property
    def span_x(self):
        w, _ = self.pixels()
        return 360.0 * w / (2 * self.radius)
