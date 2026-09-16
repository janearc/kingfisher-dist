# shelf.py -- what is held, where it is, and how much of it you can afford.
#
# WHY THIS EXISTS. The shelf door used to offer exactly one answer: every cell
# of every dataset, at the resolution it was folded to. dodo's map pane asked
# that question on 2026-09-01 and was OOM-killed by the reply -- 2.1M res-12
# cells against a 256Mi limit. It had a fold, and the fold was correct, but it
# ran on cells the pane had already received, so it bounded the wire and
# nothing bounded the inbox.
#
# THE DOOR WAS UNBOUNDED, and that is the actual defect. There was no way for
# a caller to say "that is more than I can hold" and no way for kingfisher to
# say "that would kill you", so the only available answer was yes and the
# consumer found out by dying. Every cap downstream -- dodo's 60k, the bench's
# --limit, a terminal frame's pixel count -- is the same missing number,
# reinvented three times because the protocol never carried it.
#
# So the protocol carries it. A caller names a WINDOW and a BUDGET. It does
# not name a resolution: only kingfisher knows what is on the shelf, so only
# kingfisher can pick the finest resolution that fits, and a caller that had
# to guess would guess the same way dodo did.
#
# Everything here is streaming. A fold that read the shelf into memory to
# prevent an out-of-memory would be an unusually direct way to repeat it.

import json
import os

# H3 has seven children per parent, so a level of rollup divides the cell
# count by about this. "About" is doing real work: a parent on a pentagon has
# six, and a partly-covered parent still counts once, so the true shrink is
# always LESS than sevenfold and the estimate therefore UNDERSTATES the count.
#
# THAT IS THE UNSAFE DIRECTION FOR A BUDGET, and an earlier comment here
# claimed the opposite. An estimate that thinks the answer fits when it does
# not is how a 2,688-cell budget came back with 65 cells in a test and 287,098
# in production. So the estimate is used only to CHOOSE a starting resolution,
# and the budget is enforced on the finished result -- see read_folded, which
# folds again until the answer actually fits.
CHILDREN_PER_PARENT = 7

# What a caller gets if it names no budget. Deliberately small: the failure
# this module exists to prevent is a caller that did not think about size, and
# handing that caller the whole shelf is what used to happen.
DEFAULT_BUDGET = 20000

# THE MEMORY CEILING, MEASURED. Until 2026-09-04 `budget` had a floor and no
# ceiling, and a caller could ask for more cells than this process could build:
# kingfisher was OOM-killed by exactly that on 2026-09-01 (the bench, zoomed
# out) and again on 2026-09-04 (the first stress run, budget 512k on the Bay
# window, res 10 to 11 in one step). The limit truncated both measurements, so
# the ruler was written (bin/kingfisher-stress.sh) and run against a local
# serve.py over the real shelf:
#
#   peak heap per delivered cell    ~600 bytes   (598 measured, 510 settled)
#   wire per delivered cell         ~107 bytes
#   transient before the first cell  45..130 Mi  (json.load of whole datasets)
#   the same transient IN THE POD    ~90 Mi per request, measured 2026-09-04
#                                    17:50Z on the rolled pod: glibc keeps less
#                                    than macOS malloc gave back locally
#   heap after 15 identical asks     plateau, -0.5 Mi/request; not a leak
#   fresh process                    33 Mi local, 44 Mi in the pod
#
# So a request's peak is: heap now + a parse reserve + rows * BYTES_PER_ROW,
# where rows is what /shelf already prices. The ceiling is not a number picked
# once; it is computed per request from the process's own cgroup limit and
# live heap, so it moves with the limit and shrinks as the heap plateaus. A
# request that would not fit is served COARSER and says so (budget_served,
# clipped_by), which is what the fold already does when an estimate was
# optimistic; a request that cannot fit even at res 0 is refused with 503,
# because the honest fix for that is a restart, not a smaller answer. Above
# MAX_BUDGET a request is refused with 413 before anything is priced: nothing
# on this estate can hold a million cells, and a client asking for them is
# wrong, not ambitious.
BYTES_PER_ROW = 600
# the parse reserve is set from the POD's transient, not the laptop's: 96Mi
# covers the measured 90 with a margin, and the 20 percent headroom below is
# no longer what absorbs the difference
INPUT_RESERVE = 96 << 20
MAX_BUDGET = 1_000_000
HEADROOM = 0.8
CGROUP_LIMIT_FILES = ("/sys/fs/cgroup/memory.max",
                      "/sys/fs/cgroup/memory/memory.limit_in_bytes")


class NoRoom(Exception):
    """This process cannot build even the coarsest fold without risking the
    kernel. Carries heap and limit so the refusal can say the numbers."""
    def __init__(self, heap, limit):
        super().__init__(f"heap {heap} of limit {limit}: no room for a fold")
        self.heap = heap
        self.limit = limit


# memory_limit reads the cgroup's ceiling for this process: v2, then v1, then
# an env override for tests and local runs. None means no limit was found,
# and the ceiling then rests on MAX_BUDGET alone.
def memory_limit(paths=CGROUP_LIMIT_FILES):
    env = os.environ.get("KINGFISHER_MEMORY_LIMIT")
    if env:
        return int(env)
    for path in paths:
        try:
            with open(path) as f:
                v = f.read().strip()
        except OSError:
            continue
        # "max" is v2's word for unlimited; v1 says a very large number
        if v.isdigit() and int(v) < (1 << 60):
            return int(v)
    return None


# heap_bytes is this process's resident set NOW: /proc/self/status on Linux,
# which is what the pod has. Elsewhere `ps` answers the same question; ru_maxrss
# is the last resort and is a HIGH-WATER mark, not the present. The first
# local run of the ceiling used ru_maxrss and, after one large answer, believed
# the process was out of room for good and refused every request with 503
# while the real resident set had fallen back by 44Mi. The present matters.
def heap_bytes():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    import subprocess
    try:
        kb = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                            capture_output=True, text=True, timeout=2).stdout.strip()
        if kb.isdigit():
            return int(kb) * 1024
    except (OSError, subprocess.SubprocessError):
        pass
    import resource
    import sys
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


# fit is choose_res with the memory in the room. Returns (res, estimate,
# clipped): clipped is None when the budget alone decided, else the number of
# rows the memory allowed, which is the budget the caller effectively got.
# Raises NoRoom when not even res 0 fits.
def fit(entries, budget, bbox=None, limit=None, heap=None):
    res, est = choose_res(entries, budget, bbox)
    if limit is None:
        return res, est, None
    room = limit * HEADROOM - (heap or 0) - INPUT_RESERVE
    if est * BYTES_PER_ROW <= room:
        return res, est, None
    for r in range(res - 1, -1, -1):
        e = estimate(entries, r, bbox)
        if e * BYTES_PER_ROW <= room:
            return r, e, max(e, 1)
    raise NoRoom(heap or 0, limit)


# limits is the contract a client can plan against, published on /directory:
# the constants above and this process's live numbers.
def limits():
    limit = memory_limit()
    heap = heap_bytes()
    room = None if limit is None else max(int((limit * HEADROOM - heap - INPUT_RESERVE) / BYTES_PER_ROW), 0)
    return {
        "max_budget": MAX_BUDGET,
        "bytes_per_row": BYTES_PER_ROW,
        "input_reserve_bytes": INPUT_RESERVE,
        "headroom": HEADROOM,
        "memory_limit_bytes": limit,
        "heap_bytes": heap,
        "rows_that_fit_now": room,
    }

# Rolling up a count and rolling up a measurement are different operations and
# BOTH MISTAKES ARE SILENT. A summed elevation is absurd but plausible-looking
# in a colour ramp; an averaged density is quietly divided by the number of
# children and draws a thinner, wronger map. The manifest declares which is
# which for its own layers; this is the fallback for datasets whose manifest
# predates that, and it names the lidar pipeline's own layers.
EXTENSIVE_LAYERS = {"point_density"}


def _entry(path, name):
    # one shelf entry from a dataset's manifest, or None if it is not one
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            m = json.load(f)
    except Exception:
        return None
    if m.get("content") != "cells.json":
        return None
    pipe = m.get("pipeline") or {}
    return {
        "id": m.get("id", name),
        "title": m.get("title", name),
        "dir": name,
        "res": pipe.get("res"),
        "cells": int(pipe.get("cells") or 0),
        # the returns behind the cells. Carried because a consumer showing
        # "N cells from M tiles" wants to say what was actually surveyed, and
        # counting it here costs nothing -- the manifest already recorded it.
        "points": int(pipe.get("points_used") or 0),
        "bbox": pipe.get("bbox"),
        "layers": sorted((m.get("layers") or {}).keys()),
        "kinds": {k: v.get("kind", "") for k, v in (m.get("layers") or {}).items()},
    }


def index(root):
    """Every cell dataset on the shelf, with where it is and how big it is.

    MANIFESTS ONLY -- no cells.json is opened. That is the whole point: the
    index has to be cheap enough to answer a query, and reading the cells to
    find out where they are is the cost this exists to remove.
    """
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        e = _entry(path, name)
        if e:
            out.append(e)
    return out


def overlapping(entries, bbox):
    """The entries whose bounds meet the window. A dataset with no recorded
    bounds is EXCLUDED, not included: the backfill has not reached it yet, and
    guessing that an unknown dataset might be relevant is how the query grows
    back into the whole shelf."""
    from hexify import bbox_overlaps
    if not bbox:
        return list(entries)
    return [e for e in entries if bbox_overlaps(e.get("bbox"), bbox)]


def _covered_fraction(entry, bbox):
    """How much of a dataset the window actually asks for, by area.

    A GLOBAL DATASET IS MOSTLY NOT IN YOUR WINDOW. Counting its whole cell
    count against every request made the 287k-cell basemap dominate every
    estimate -- a Bay-sized window came back at res 1 holding ONE cell,
    because the arithmetic believed a third of a million cells were about to
    arrive when the true number was less than one.

    Area ratio is crude and that is fine: it is an estimate feeding a choice
    that is enforced later anyway, and it is right about the thing that
    matters -- the difference between all of a tile and a millionth of a
    basemap.
    """
    box = entry.get("bbox")
    if not bbox or not box:
        return 1.0
    whole = (box[2] - box[0]) * (box[3] - box[1])
    if whole <= 0:
        return 1.0
    w = max(0.0, min(box[2], bbox[2]) - max(box[0], bbox[0]))
    h = max(0.0, min(box[3], bbox[3]) - max(box[1], bbox[1]))
    return max(0.0, min(1.0, (w * h) / whole))


def estimate(entries, res, bbox=None):
    """Cells a request would return at this resolution.

    PER DATASET, FROM ITS OWN NATIVE RESOLUTION. A shelf holds things folded
    at different resolutions, and folding is a one-way operation: a res-4
    basemap asked for at res 8 does not shrink, it arrives whole. Estimating
    from one shelf-wide total missed that completely -- a 2,688-cell budget
    came back with 287,098 cells and a 10MB payload, because the coarse
    dataset was counted as if it would fold like the fine ones.
    """
    total = 0.0
    for e in entries:
        native = e.get("res")
        levels = 0 if native is None else max(0, native - res)
        total += (e["cells"] * _covered_fraction(e, bbox)
                  / (CHILDREN_PER_PARENT ** levels))
    return int(total)


def choose_res(entries, budget, bbox=None):
    """The finest resolution whose cell count fits the budget.

    THE CALLER DOES NOT PICK THIS. A caller that named a resolution would be
    guessing at what is on the shelf, and the guess is wrong the moment
    another tile lands. It names what it can hold; this decides what it gets.

    Walks DOWN from the finest thing on the shelf and stops at the first
    resolution that fits. Res 0 is the floor and it is a real answer: if even
    the whole planet as 122 cells is more than the caller said it could hold,
    the caller gets 122 cells rather than a lie.
    """
    native = max((e["res"] for e in entries if e.get("res") is not None),
                 default=0)
    for res in range(native, -1, -1):
        est = estimate(entries, res, bbox)
        if est <= budget:
            return res, est
    return 0, estimate(entries, 0, bbox)


def price(entries, bbox, budget):
    """What a request would cost, WITHOUT SERVING IT.

    Answerable from manifests alone, because every one records its cell count.
    A caller that asks this first cannot be surprised; a caller that asks for
    a budget cannot be hurt. Neither has to know how many tiles are shelved.
    """
    sel = overlapping(entries, bbox)
    res, est = choose_res(sel, budget, bbox)
    native = max((e["res"] for e in sel if e.get("res") is not None), default=0)
    return {
        "datasets": len(sel),
        "points": sum(e["points"] for e in sel),
        "cells_native": sum(e["cells"] for e in sel),
        "res_native": native,
        "res": res,
        "cells_estimate": est,
        "budget": budget,
        "folded": res != native,
    }


def _fold_once(acc, ext, h3):
    """Roll an accumulator up one H3 level, in its own units.

    Operates on the ACCUMULATOR, not on finished rows, so the weights survive:
    an intensive layer carries its running (sum, weight) pair and keeps
    averaging correctly across as many levels as it takes.

    Cells already coarser than the target stay put. A shelf holds mixed
    resolutions and folding is one-way; dropping what cannot fold further is
    how a basemap disappears from a query aimed at it.
    """
    out = {}
    for cell, layers in acc.items():
        res = h3.get_resolution(cell)
        if res <= 0:
            parent = cell
        else:
            try:
                parent = h3.cell_to_parent(cell, res - 1)
            except Exception:
                parent = cell
        a = out.setdefault(parent, {})
        for k, (total, weight) in layers.items():
            slot = a.setdefault(k, [0.0, 0.0])
            slot[0] += total
            slot[1] += weight
    return out


def _fold_to_fit(acc, ext, h3, limit):
    """Fold until the accumulator is within `limit`, or until it cannot fold.

    TERMINATION IS ON RESOLUTION, NOT ON PROGRESS. Folding one level merges
    nothing when the cells are far apart -- sparse survey points, or tiles
    scattered across a continent -- and reading "no progress at this level" as
    "no progress possible" stops the fold dead while the answer is still
    hundreds of times over budget. That is what it did: it broke out after one
    pass and returned 64 cells against a budget of 1. Two levels further down
    they merge.

    Resolution strictly decreases and stops at 0, so this always ends. The
    floor is a real answer: the whole planet is 122 cells at res 0, and a
    caller who cannot hold that gets it anyway rather than a lie.
    """
    if not limit:
        return acc
    while len(acc) > limit:
        if max((h3.get_resolution(c) for c in acc), default=0) <= 0:
            return acc
        acc = _fold_once(acc, ext, h3)
    return acc


def _inside(lat, lon, bbox):
    # is a cell centre in the window? Bounds are inclusive: a cell exactly on
    # the southern edge is in the window, and dropping it scars every seam.
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def read_folded(root, entries, res, bbox=None, h3=None, layer=None, budget=None):
    """Cells from every named dataset, clipped to the window and folded to
    `res`, accumulated one dataset at a time.

    ONE DATASET IN MEMORY AT A TIME. Each cells.json is opened, folded into
    the accumulator, and dropped before the next is opened, so peak memory is
    one tile plus the output -- and the output is bounded by the resolution,
    which was chosen from the budget. That is the difference between this and
    the thing it replaces.
    """
    if h3 is None:
        import h3 as _h3
        h3 = _h3
    # per parent: summed extensive values, and intensive values weighted by
    # the returns behind each child. UNWEIGHTED IS WRONG AND LOOKS FINE: a
    # cell holding two points would count as much as one holding five
    # hundred, and at res 12 most cells are the former.
    acc = {}
    # which layer names fold by SUM, decided once from the manifests rather
    # than re-derived per cell -- two datasets cannot disagree about whether
    # point_density is a count
    ext = set()
    read = 0
    # BACKPRESSURE, NOT A FINAL TRIM. Folding only at the end still requires
    # building the whole un-folded answer first, which is precisely what
    # OOM-killed kingfisher itself on 2026-09-01 when a world-sized window
    # accumulated 287,098 rows against a 256Mi limit. Two minutes after it had
    # been deployed to stop dodo doing the same thing, one level down.
    #
    # So the accumulator is capped and folds as it fills. Peak memory is the
    # ceiling plus one tile, whatever the window asks for.
    ceiling = max(4 * budget, 10000) if budget else None
    for e in entries:
        path = os.path.join(root, e["dir"], "cells.json")
        try:
            with open(path) as f:
                cells = json.load(f).get("cells") or {}
        except Exception:
            continue
        # NOTHING IS EVER UPSAMPLED. A shelf holds datasets folded at
        # different resolutions -- lidar at 12, a global basemap at 4 -- and
        # cell_to_parent cannot go finer than the data. Asking it to raises,
        # and the first version of this swallowed that per cell, so a coarse
        # dataset vanished from any request whose budget chose a finer res:
        # silently, with the other layers still drawing. Each dataset now
        # contributes at its own resolution or coarser, and a mixed-resolution
        # answer is the honest one -- the cells simply differ in size.
        native = e.get("res")
        target = res if native is None else min(res, native)
        for cell, row in cells.items():
            if bbox is not None:
                lat, lon = h3.cell_to_latlng(cell)
                if not _inside(lat, lon, bbox):
                    continue
            try:
                parent = cell if native == target else h3.cell_to_parent(cell, target)
            except Exception:
                continue
            read += 1
            a = acc.setdefault(parent, {})
            if not isinstance(row, dict):
                row = {"value": row}
            weight = float(row.get("point_density") or 0.0) or 1.0
            for k, v in row.items():
                if layer and k != layer and k != "point_density":
                    continue
                if not isinstance(v, (int, float)):
                    continue
                slot = a.setdefault(k, [0.0, 0.0])
                if _extensive(k, e):
                    ext.add(k)
                    slot[0] += v
                else:
                    slot[0] += v * weight
                    slot[1] += weight
        del cells
        # A HARD STOP, NOT A RATCHET. Folding between datasets drove the whole
        # accumulator coarser every time a new tile pushed it over -- and
        # since each fold is permanent, a window with many tiles ratcheted all
        # the way down to res 1 holding one cell. The estimate keeps the
        # accumulator near the budget; this only catches an estimate that was
        # badly wrong, at ten times the ceiling, before memory becomes a
        # problem rather than a resolution one.
        if ceiling and len(acc) > ceiling * 10:
            acc = _fold_to_fit(acc, ext, h3, ceiling)

    # THE BUDGET IS A GUARANTEE, NOT AN ESTIMATE. choose_res only picks a
    # starting point; the real count is not knowable without doing the work,
    # and the estimate is optimistic by construction. So fold the finished
    # answer until it actually fits.
    acc = _fold_to_fit(acc, ext, h3, budget)

    out = {}
    for parent, layers in acc.items():
        row = {}
        for k, (total, w) in layers.items():
            if layer and k != layer:
                continue
            # a count is its sum; a measurement is its weighted mean. w is 0
            # only for a layer that was extensive everywhere it appeared.
            row[k] = round(total if k in ext else (total / w if w else 0.0), 4)
        if row:
            out[parent] = row
    # the resolution ACTUALLY DELIVERED, which is not the one asked for when
    # backpressure or the budget folded it further. Reporting the requested
    # res would be a caller drawing coarse cells while told they are fine.
    got = max((h3.get_resolution(c) for c in out), default=res)
    return out, read, got


def _extensive(layer, entry):
    # the manifest's word first, the pipeline's own layer names as fallback
    if entry:
        kind = (entry.get("kinds") or {}).get(layer, "")
        if kind:
            return "EXTENSIVE" in kind
    return layer in EXTENSIVE_LAYERS


# HOW A COLLECTION IS NAMED, and why it is derived rather than declared.
#
# A shelf of 397 datasets is not a menu. It is 397 tiles from six surveys, and
# every consumer that has tried to show it has ended up with either a
# hardcoded list of layer names (dodo's map pane, written when the shelf held
# five things) or no chooser at all. The grouping a person wants is the one
# the DATA already has: USGS ships tiles stamped with their survey project,
# GIBS ships one image per layer per date.
#
# So this reads the name the provider gave and takes the survey out of it.
# THE DURABLE FIX IS FOR THE ADAPTERS TO RECORD IT at index time, the way
# bounds are recorded at ingest now -- a derivation in the serving path is a
# rule that has to be maintained here rather than where the knowledge is. It
# is one function on purpose, so moving it later is one deletion.
_USGS_PROJECT = None


def collection_of(entry):
    """(source, collection) for a shelved dataset."""
    global _USGS_PROJECT
    if _USGS_PROJECT is None:
        import re
        _USGS_PROJECT = re.compile(r"USGS Lidar Point Cloud (\S+)")
    title = entry.get("title") or entry["id"]
    m = _USGS_PROJECT.match(title)
    if m:
        return "usgs", m.group(1)
    if entry["id"].startswith("gibs-"):
        # "Black Marble (Annual, 2012 & 2016) (H3 res 4)" -> "Black Marble"
        return "nasa_gibs", title.split(" (")[0]
    return "other", title


def _union(boxes):
    live = [b for b in boxes if b]
    if not live:
        return None
    return [min(b[0] for b in live), min(b[1] for b in live),
            max(b[2] for b in live), max(b[3] for b in live)]


def directory(entries, bbox=None):
    """The shelf as a menu: source -> collection -> what it holds.

    TWO WAYS IN, because there are two questions. `sources` answers "what is
    here", the way kingfisher organises its own shelf. `measures` answers
    "what can I draw", which is the question a display layer actually has --
    it names a layer and needs to know where that layer exists and at what
    resolution.

    Every node carries its own bounds, cell count and resolutions, so a menu
    can be built and PRICED without opening a single cells.json.
    """
    sel = overlapping(entries, bbox) if bbox else list(entries)
    groups = {}
    for e in sel:
        key = collection_of(e)
        g = groups.setdefault(key, {"datasets": [], "layers": {}})
        g["datasets"].append(e)
        for name in e["layers"]:
            g["layers"].setdefault(name, (e.get("kinds") or {}).get(name, ""))

    sources = {}
    for (source, name), g in sorted(groups.items()):
        ds = g["datasets"]
        sources.setdefault(source, []).append({
            "collection": name,
            "datasets": len(ds),
            "cells": sum(e["cells"] for e in ds),
            "points": sum(e["points"] for e in ds),
            "res": sorted({e["res"] for e in ds if e.get("res") is not None}),
            "bbox": _union([e.get("bbox") for e in ds]),
            "layers": sorted(g["layers"]),
            # one id, so a caller that wants to look at exactly one tile can,
            # without the menu having to list all 178 of them
            "example": ds[0]["id"],
        })

    measures = {}
    for (source, name), g in groups.items():
        for layer, kind in g["layers"].items():
            m = measures.setdefault(layer, {"layer": layer, "kind": kind,
                                            "in": [], "res": set(), "bbox": []})
            m["in"].append(f"{source}/{name}")
            m["res"].update(e["res"] for e in g["datasets"]
                            if e.get("res") is not None)
            m["bbox"].extend(e.get("bbox") for e in g["datasets"])

    return {
        "sources": [{"source": s, "collections": c}
                    for s, c in sorted(sources.items())],
        "measures": [{"layer": m["layer"], "kind": m["kind"],
                      "in": sorted(m["in"]), "res": sorted(m["res"]),
                      "bbox": _union(m["bbox"])}
                     for _, m in sorted(measures.items())],
        "datasets": len(sel),
        "bbox": bbox,
    }
