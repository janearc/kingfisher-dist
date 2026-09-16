#!/usr/bin/env python3
# kingfisher -- map data as a service.
#
# It sits above a large body and pulls out exactly the one thing asked for.
#
# WHAT IT IS. Two things, one process. The original job: a mount table over
# directories, serving map bytes read-only on the viewer's origin -- a page on
# one port cannot fetch another port without CORS headers, and that single
# browser fact is why this process exists. The grown job: the substrate --
# a protobuf contract served at /api, a discovery surface over external data
# providers, flipr-gated fetching, and per-RPC instrumentation. corvid and
# peacock are display layers; this is what they display.
#
# Data roots are read STRICTLY read-only -- we never write into another tree --
# and every resolved path is re-checked against its root, so a ../ cannot walk
# out of the window.
#
# THE MOUNT TABLE IS THE CONTAINER STORY. Map data is never baked into the
# image: the host's directories are bind-mounted read-only and named in
# KINGFISHER_MOUNTS, so the same code serves a laptop checkout and a container
# over the same bytes. The mounted data is kingfisher's OWN charge (ruled
# 2026-08-28: maps are kingfisher's job; producers write into staging, and
# what serves is what kingfisher mounts). See kube/ for the cluster shape.
#
#     ./serve.py --root ../distill-valhalla \
#                --mount /econ/=/srv/kingfisher/maps/econ
#
# ENDPOINTS
#     /            what this instance is serving, for a person
#     /health      liveness: the process is up and answering. Always 200, so a
#                  probe on it never restarts the pod. The BODY carries status,
#                  ready and reason.
#     /ready       readiness: should this instance get traffic. 503 when a
#                  configured mount is empty -- the hostPath resolved to
#                  nothing, the pod would serve 404 forever, and README.md
#                  calls that the most expensive mistake available here.
#     /api         the contract: buf-built FileDescriptorSet, == descriptor.binpb
#     /discovery   the librarian: every source and dataset known, held or not
#     /stats       counters as JSON
#     /metrics     prometheus text; every RPC labelled by method and outcome
#     /reload      the poke: bump the cache generation, re-read the inventory
#     /<mount>/    a JSON index of that mount
#     /kingfisher.discovery.v1.DiscoveryService/<rpc>
#                  protojson over POST, marshalled through generated types
#
# HISTORY THAT EARNED A COMMENT. This header once said the map data was
# "owned by somebody else" and named peacock as the regenerator; the ownership
# moved (2026-08-28) and the stale sentence taught the wrong architecture to
# everyone who read it, including one agent who scoped the service down on the
# strength of it. When this file and a ruling disagree, fix the file the same
# day. And the 2026-08-23 correction stands: peacock always served
# /econ/res8.json correctly -- the identical-27KB-HTML-for-every-path process
# was dodo's maps pane on 15008. The data was at a URL; it was never on the
# viewer's origin, which is the fact this process answers.

import argparse
import email.utils
import html
import http.server
import json
import os
import posixpath
import socketserver
import sys
import threading
import time
import version
import urllib.parse

from log import log

MOUNTS = {}
ROOT = os.getcwd()
STARTED = time.time()

# the buf-built FileDescriptorSet served at /api. Read once, beside serve.py
# regardless of cwd, cached including the not-there answer: a missing
# descriptor is a build defect that a restart will not fix, so re-statting it
# per request would be theater.
_DESCRIPTOR = {"read": False, "bytes": None}


def descriptor_bytes():
    if not _DESCRIPTOR["read"]:
        _DESCRIPTOR["read"] = True
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "descriptor.binpb")
        try:
            with open(path, "rb") as f:
                _DESCRIPTOR["bytes"] = f.read()
        except OSError:
            _DESCRIPTOR["bytes"] = None
    return _DESCRIPTOR["bytes"]
# the way back to the front door. DODO_URL is the fallback for a bare-IP
# visit, where the request itself cannot honestly name a same-network dodo.
DODO_URL = os.environ.get("KINGFISHER_DODO_URL", "http://localhost:15000/")


def _dodo_url_for(host_header):
    # derived from the REQUEST, never from an environment conditional --
    # same rule as lib/themes/nav.mjs's dodoOrigin() (janearc/dodo), which
    # this mirrors in Python because a server-rendered page cannot import
    # a browser ES module. Three estate services hand-rolled this escape
    # link with a hardcoded prod address before the pattern got named
    # (dodo's own lights pane, then kestrel, then here); the fix each time
    # is the same shape: name.<tld> shares dodo's <tld>, port carried,
    # scheme assumed http since every dev/prod host here answers plain.
    # A host this cannot honestly derive from (a bare IP, no dot) keeps
    # the configured DODO_URL rather than link somewhere wrong.
    if not host_header:
        return DODO_URL
    hostname = host_header.split(":", 1)[0]
    port = host_header.split(":", 1)[1] if ":" in host_header else ""
    parts = hostname.split(".")
    tld = parts[-1] if len(parts) > 1 else ""
    if tld not in ("test", "localhost"):
        return DODO_URL
    return f"http://dodo.{tld}{':' + port if port else ''}/"
# Anything differencing the monotonic counters needs to know when they were last
# zeroed, or a restart mid-measurement reads as a negative rate -- which renders
# as a nonsense spike rather than as a gap, and happens precisely when something
# interesting was going on. uptime_s answers it; this answers it absolutely, so
# two observers can agree on WHICH restart they saw.
STARTED_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED))

# What the SERVER emitted, as opposed to what one browser tab asked for. A
# counter here is a claim about the service; a counter in the page is a claim
# about that page.
#
# MONOTONIC, never a rate. A rate computed here would be wall-clock, and
# anything reading it against video time or a frame-stepped capture has to
# difference it itself. Reset only by restarting.
#
# ThreadingTCPServer means several handlers touch this at once, and += on an int
# is not guaranteed atomic, so it takes the lock.
STATS_LOCK = threading.Lock()
STATS = {"requests_total": 0, "bytes_total": 0,
         "tiles_total": 0, "tile_bytes_total": 0, "errors_total": 0,
         "by_mount": {}, "by_kind": {}, "by_status": {},
         # WHICH paths failed, not just how many. A count of 246 errors with no
         # paths cannot be diagnosed after the fact -- if that burst was a
         # viewer asking for something that should exist, the evidence is gone
         # by the time anyone looks. Capped so a scanner cannot grow it without
         # bound; the cap being hit is itself reported.
         "error_paths": {}, "error_paths_capped": 0}
ERROR_PATH_CAP = 64
# single ascending byte range: bytes=a-b, bytes=a-, bytes=-suffix
RANGE_RE = __import__("re").compile(r"^bytes=(\d*)-(\d*)$")

# Latency, as a prometheus histogram rather than an average. An average hides
# the only thing worth knowing here: a 1.3MB res-8 chunk and a 70KB manifest are
# different animals, and what matters is the tail, not the mean.
#
# Buckets are sized for reading a file off local disk and writing it to a
# socket. Sub-millisecond is the page cache, tens of milliseconds is real IO,
# and past a second means something is wrong rather than busy.
DUR_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05,
               0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
DUR = {"buckets": {b: 0 for b in DUR_BUCKETS}, "inf": 0, "sum": 0.0, "count": 0,
       "by_kind": {}}
INFLIGHT = {"n": 0}


def observe(seconds, kind):
    with STATS_LOCK:
        DUR["sum"] += seconds
        DUR["count"] += 1
        placed = False
        for b in DUR_BUCKETS:
            if seconds <= b:
                DUR["buckets"][b] += 1
                placed = True
                break
        if not placed:
            DUR["inf"] += 1
        k = DUR["by_kind"].setdefault(kind, {"sum": 0.0, "count": 0})
        k["sum"] += seconds
        k["count"] += 1


def method_of(path):
    # the RPC name for instrumentation. Derived from the path at the one
    # choke point every request passes, so "every RPC is measured" is a
    # property of the router rather than a habit per handler.
    clean = urllib.parse.urlparse(path).path.rstrip("/") or "/"
    if clean in ("/api", "/health", "/ready", "/stats", "/metrics", "/reload"):
        return clean[1:]
    if clean == "/":
        return "index"
    if clean.startswith("/kingfisher.") and "/" in clean[1:]:
        # /kingfisher.discovery.v1.DiscoveryService/Fetch -> "DiscoveryService.Fetch"
        svc, _, rpc = clean.rpartition("/")
        return svc.rsplit(".", 1)[-1] + "." + rpc
    first = "/" + clean.split("/", 2)[1] + "/"
    if first in MOUNTS:
        # a mount root is the JSON index RPC; anything deeper is a tile
        return "mount_index" if clean == first.rstrip("/") else "tile"
    return "static"


def outcome_of(status):
    # coarse on purpose: dashboards alert on outcomes, not status zoo.
    # "dropped" = no response was ever sent, which is its own defect.
    if status == 0:
        return "dropped"
    if status == 304:
        return "not_modified"
    if status < 400:
        return "ok"
    if status == 403:
        # a path that tried to walk out of a mount window. A security event,
        # countable and alertable without grepping logs.
        return "forbidden"
    if status == 404:
        return "not_found"
    return "error"


def rpc_done(method, status, seconds):
    # one call per request, from _timed's finally. Unlike the global
    # histogram, introspection endpoints ARE included here: per-method
    # labels keep them from skewing tile latency, which was the reason for
    # excluding them globally.
    with STATS_LOCK:
        m = STATS.setdefault("rpc", {}).setdefault(method, {})
        m[outcome_of(status)] = m.get(outcome_of(status), 0) + 1
        d = DUR.setdefault("by_method", {}).setdefault(
            method, {"sum": 0.0, "count": 0})
        d["sum"] += seconds
        d["count"] += 1


def kind_of(path):
    clean = path.split("?")[0]
    base = posixpath.basename(clean)
    parent = posixpath.basename(posixpath.dirname(clean))
    if parent.startswith("res"):
        return parent                       # res7/, res8/ -- the chunks
    if base.startswith("res") and base.endswith(".json"):
        return base[:-5] + "-manifest"
    return "other"


def record(path, mount_prefix, status, nbytes):
    clean_path = path.split("?")[0][:200]
    # a "tile" is a file served out of a MOUNT: a manifest or a chunk. the
    # viewer's html and the vendored libraries are requests but not tiles, and
    # conflating them inflates the number the moment somebody reloads the page.
    kind = kind_of(path)
    with STATS_LOCK:
        STATS["requests_total"] += 1
        STATS["bytes_total"] += nbytes
        STATS["by_status"][str(status)] = STATS["by_status"].get(str(status), 0) + 1
        if status >= 400:
            STATS["errors_total"] += 1
            key = f"{status} {clean_path}"
            ep = STATS["error_paths"]
            if key in ep or len(ep) < ERROR_PATH_CAP:
                ep[key] = ep.get(key, 0) + 1
            else:
                STATS["error_paths_capped"] += 1
        if mount_prefix:
            STATS["tiles_total"] += 1
            STATS["tile_bytes_total"] += nbytes
            STATS["by_mount"][mount_prefix] = STATS["by_mount"].get(mount_prefix, 0) + 1
            STATS["by_kind"][kind] = STATS["by_kind"].get(kind, 0) + 1


# inventory() walks every mounted tree, which is half a gigabyte of small JSON.
# Prometheus scrapes every 30s and that walk is not free, so it is cached. The
# numbers it produces -- how much map data exists, how many tiles -- move when
# somebody regenerates a dataset, which is hours apart, not seconds.
# CACHING, and the model behind it.
#
# Map data is immutable between regenerations. Nothing here changes on its own,
# so the honest policy is: cache until somebody says otherwise, and give them a
# way to say it. That is GENERATION -- a counter baked into every ETag. Bumping
# it invalidates every cached response at once, and nothing else does.
#
# What this deliberately does NOT do is watch the filesystem. An mtime poll is a
# guess about whether a multi-file regeneration has finished, and it can serve a
# half-written dataset with a straight face. An explicit poke cannot.
#
# We do not need a cache in front of the FILES. The kernel's page cache already
# holds all of it -- measured, a res-8 chunk serves in 1.83ms -- and a memcached
# in front of that would be a second copy of the same bytes in the same RAM.
# What a cache earns its keep for is COMPUTED responses: anything composited out
# of surreal on the fly, or a derived layer. Those are not built yet, and when
# they are, they hang off this same generation stamp.
GENERATION = {"n": 1}
# Do not parse a manifest bigger than this to read a list of key names.
#
# This was an OOMKill, not a tuning problem. inventory() parsed EVERY resN.json
# at startup, and peacock's res6.json is 38.3MB -- which as Python objects is
# several hundred megabytes. On a laptop with no cgroup that is merely wasteful;
# in a 256Mi container it is exit 137 one second after start, five times in a
# row. Raising the limit would have hidden the fact that we were deserialising
# 38MB to read a list of key names.
#
# Nothing is lost by skipping the big ones. The series list is IDENTICAL in
# every manifest, so the smallest one answers it. Only res7 and res8 carry
# chunks and both are about 70KB. The larger manifests contribute their
# existence, which is a stat() and not a parse.
MANIFEST_PARSE_CAP = 8 * 1024 * 1024
INV_TTL = 300.0
INV_CACHE = {"at": 0.0, "value": None}
INV_LOCK = threading.Lock()

# The last time the flag plane refused us. The fetch surface already catches
# FliprDown and answers 503; this is that same event, remembered, so /health can
# report the posture instead of running its own probe. Health must OBSERVE state,
# never cause work: it is polled every ten seconds and a probe that goes out on
# the network turns one outage into two.
FLIPR_DOWN = {"at": 0.0, "err": ""}
FLIPR_DOWN_WINDOW = 120.0


def _assess():
    # What this instance can honestly say about itself, without doing any work
    # to find out. Every input here is already cached or already recorded:
    # health is polled every ten seconds, and an endpoint that walks the tree
    # or calls the network to answer turns one outage into two.
    degraded = []
    ready = True

    if not MOUNTS:
        ready = False
        degraded.append("no mounts configured; this instance serves nothing")

    # The inventory as last walked, WITHOUT forcing a walk. None means nobody
    # has asked yet, which is a real and temporary answer rather than a fault.
    with INV_LOCK:
        inv = INV_CACHE["value"]
        walked_at = INV_CACHE["at"]

    empty = []
    if inv is None:
        degraded.append("the inventory has not been walked yet; ask /stats or "
                        "/reload to populate it")
    else:
        # FILES, not tiles. A mount with files and no tiles is the /layers/ and
        # /rides/ shape: no manifest, discovered through the JSON directory
        # index, and entirely healthy. A mount with no files at all is a
        # hostPath that resolved to nothing inside the node, which is the
        # failure README.md calls the most expensive mistake available here.
        # Reading tiles instead would take those production mounts out of
        # rotation for being what they are.
        empty = [e["prefix"] for e in inv if not e["files"]]
        if empty:
            ready = False
            degraded.append(
                "mounted but empty: " + ", ".join(sorted(empty)) + " -- the "
                "hostPath resolves to nothing inside the node, so these serve "
                "404 forever. See README.md; do not repoint them at a checkout.")

    since = time.time() - FLIPR_DOWN["at"] if FLIPR_DOWN["at"] else None
    if since is not None and since < FLIPR_DOWN_WINDOW:
        degraded.append(
            f"flipr refused us {round(since)}s ago, so the fetch surface is "
            f"failing closed: {FLIPR_DOWN['err']}")

    return {
        "status": "healthy" if not degraded else "degraded",
        "ready": ready,
        "uptime_s": round(time.time() - STARTED, 1),
        "started_at": STARTED_ISO,
        # the short sha this process was built from. hygiene.py roots every
        # source-derived check on it; "unknown" means a bare docker build.
        "commit": version.commit(),
        "mounts": len(MOUNTS),
        "mounts_empty": sorted(empty),
        "inventory_walked_at": round(walked_at, 1) if walked_at else None,
        "reason": "; ".join(degraded),
    }


def inventory(force=False):

    with INV_LOCK:
        fresh = INV_CACHE["value"] is not None and \
            (time.time() - INV_CACHE["at"]) < INV_TTL
        if fresh and not force:
            return INV_CACHE["value"]
    value = _walk_inventory()
    with INV_LOCK:
        INV_CACHE["at"] = time.time()
        INV_CACHE["value"] = value
    return value


def _walk_inventory():
    # what this instance knows about, read from the manifests THEMSELVES rather
    # than declared anywhere, so it cannot drift from what is on disk.
    out = []
    for prefix, root in sorted(MOUNTS.items()):
        entry = {"prefix": prefix, "root": root, "series": None, "chunks": None,
                 "resolutions": [], "bytes": 0, "files": 0, "generated_at": None,
                 # tiles ON DISK, which is a different question from tiles
                 # served: how much map there is, rather than how much of it
                 # anyone asked for.
                 "tiles": 0, "tile_bytes": 0}
        entry["manifests_skipped"] = []
        for res in range(1, 12):
            man = os.path.join(root, f"res{res}.json")
            if not os.path.isfile(man):
                continue
            entry["resolutions"].append(res)
            try:
                if os.path.getsize(man) > MANIFEST_PARSE_CAP:
                    # counted as a published resolution, not opened
                    entry["manifests_skipped"].append(f"res{res}")
                    continue
            except OSError:
                continue
            try:
                with open(man) as f:
                    d = json.load(f)
            except Exception as e:
                entry["error"] = f"{e.__class__.__name__}: {e}"
                continue
            # chunks SUM across resolutions. reading only the first manifest
            # reported 0 for econ, because the first one found is res3 and res3
            # is not chunked -- only res7 and res8 are. the series list is the
            # same in every manifest, so first-one-wins is right for that.
            entry["chunks"] = (entry["chunks"] or 0) + len(d.get("chunks", []))
            if entry["series"] is None:
                layers = d.get("layers", {})
                entry["series"] = sorted(layers)
                entry["generated_at"] = d.get("generated_at")
                entry["cells"] = d.get("cells")
                # read straight out of the manifest, so a series that changes
                # unit or vintage says so here without anyone remembering to
                # update a description somewhere else.
                cat = []
                for name in entry["series"]:
                    m = layers[name] or {}
                    vin = m.get("vintage") or {}
                    dom = m.get("domain") or {}
                    rel = m.get("reliability") or {}
                    cat.append({
                        "name": name,
                        "kind": m.get("kind") or "",
                        "unit": m.get("unit") or "",
                        "frame": m.get("frame") or "",
                        "vintage": vin.get("period") or (str(vin.get("year"))
                                                         if vin.get("year") else ""),
                        "min": dom.get("min"), "max": dom.get("max"),
                        "p50": dom.get("p50"), "n": dom.get("n"),
                        # peacock mutes cells above this coefficient of
                        # variation. carrying it lets a reader see which series
                        # are shaky rather than trusting all of them equally.
                        "mute_above": rel.get("mute_above"),
                    })
                entry["catalogue"] = cat
                entry["catalogue_res"] = res
        for dirpath, _, names in os.walk(root):
            # a chunk lives under resN/, which is what makes it a tile rather
            # than a manifest or a stray geojson sitting in the root
            in_tiles = posixpath.basename(dirpath).startswith("res")
            for n in names:
                try:
                    sz = os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    continue
                entry["bytes"] += sz
                entry["files"] += 1
                if in_tiles and n.endswith(".json"):
                    entry["tiles"] += 1
                    entry["tile_bytes"] += sz
        out.append(entry)
    return out


def num(v):
    # a number a person can read at a glance, and an em-dash when there is none
    if v is None:
        return "&mdash;"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 100:
        return f"{v:,.0f}"
    return f"{round(v, 2):g}"


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# THE SHARED BENCH LOOK, from bench-ui (janearc/bench-ui).
#
# kingfisher used to carry its own copy of the house palette -- #0b0e14 and
# #e6edf7 typed straight into the stylesheet below. That was one of four copies
# of the same decision living in four repositories, expressed as a contract in
# none of them. It now reads the one file every bench reads.
#
# /var/mesh-ui is the fleet convention for the container mount and matches the
# host path, so the volume line documents itself. The second candidate is the
# same file on a laptop, where it lives under $HOME and there is no mount.
BENCH_CSS_DEFAULTS = ("/var/mesh-ui/bench.css", "~/.config/kingfisher/bench.css")
# kingfisher's own rules, which spend bench-ui's tokens and add nothing to the
# palette. bench.css deliberately styles table.cands and never a bare table, so
# these are the page's own -- and they are where this page keeps its identity:
# it has no raised surface anywhere, and the two rule weights ARE the design.
KINGFISHER_CSS = """
.wrap{gap:22px}
h2{margin-bottom:10px}
table{border-collapse:collapse;width:100%;font-size:var(--fs-min)}
th{text-align:left;color:var(--faint);font:600 var(--fs-min) var(--mono);
   letter-spacing:.04em;border-bottom:2px solid var(--rule-strong);
   padding:.4rem .6rem .4rem 0}
td{border-bottom:1px solid var(--rule-faint);padding:.4rem .6rem .4rem 0;
   vertical-align:top;font-size:var(--fs-min)}
td.f{font-family:var(--mono)}
td.r{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono)}
td.s{color:var(--dim);font-family:var(--mono);padding-bottom:.9rem}
td.k{color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.dim{color:var(--dim)}
code{font-family:var(--mono);font-size:var(--fs-min)}
.back{font-size:var(--fs-sm)}
.back a{color:var(--dim)}
.back a:hover{color:var(--amber)}
/* The ONE literal colour left in this file, and it has to be. This rule only
   ever renders when the sheet could NOT be read, so var(--red) would resolve
   to nothing and the warning would have no border at all. A warning you
   cannot see is the failure mode this whole branch exists to avoid. */
.nostyle{border:2px solid #f4664c;padding:12px;margin:0 0 18px;font-weight:600}
"""


def bench_css():
    # READ AT REQUEST TIME, never cached at startup. install.sh writing a new
    # sheet is the whole deployment: no restart, no redeploy, the next refresh
    # is the update. A process that read this once at boot would put a restart
    # back into that loop, which is the property bench-ui exists to remove.
    override = os.environ.get("KINGFISHER_BENCH_CSS")
    candidates = (override,) if override else BENCH_CSS_DEFAULTS
    # name EVERY path tried, not just the last one. In a container the useful
    # name is /var/mesh-ui/bench.css -- the mount that is missing -- and that is
    # the first candidate, so reporting only the last one told an operator about
    # a $HOME path they have no reason to care about.
    errs = []
    for path in candidates:
        full = os.path.expanduser(path)
        try:
            with open(full, encoding="utf-8") as f:
                return f.read(), full, None
        except OSError as e:
            errs.append(f"{full}: {e.__class__.__name__}")
    return None, None, "; ".join(errs)


class Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1, so a connection is reused. The inherited default is 1.0,
    # which closes after every response -- every tile a new TCP connection,
    # every one of them queueing on the backlog above. Keep-alive is only
    # safe because every response here carries content-length: _send sets
    # it, the static handler sets it, send_error sets it. A response without
    # one would hang the client waiting for a body that never ends, which
    # is the failure the test for this pins with two requests on one socket.
    protocol_version = "HTTP/1.1"
    # AN IDLE CONNECTION IS RELEASED. Under 1.1 a handler thread waits in
    # readline() for the client's next request; without a bound on that
    # wait, a browser's six idle keep-alive sockets park six threads each,
    # forever, on a ThreadingTCPServer that spawns without limit. This is
    # the socket timeout BaseHTTPRequestHandler applies to the connection;
    # on expiry it closes cleanly and logs one line, no traceback.
    timeout = 30
    # EVERY RESPONSE NAMES KINGFISHER. BaseHTTPRequestHandler writes
    # `Server: <server_version> <sys_version>` from version_string(), and the
    # default was `SimpleHTTP/0.6 Python/3.12.14` -- so a consumer could not
    # tell a tile kingfisher served from one a fallback or a cache did. This
    # override reaches tiles and 404s as well as the JSON doors, because it
    # sits below all of them. Dynamic, so the env is read when asked.
    def version_string(self):
        return version.server()

    # a missing file under the static root used to answer text/html while a
    # missing file under a mount answered JSON. One shape.
    error_content_type = "application/json"
    error_message_format = '{"error": "%(message)s", "status": %(code)d}'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ------------------------------------------------------------- routing
    def _mounted(self, path):
        clean = urllib.parse.urlparse(path).path
        for prefix, root in MOUNTS.items():
            if not clean.startswith(prefix):
                continue
            rel = posixpath.normpath(clean[len(prefix):]).lstrip("/")
            full = os.path.realpath(os.path.join(root, rel))
            if full == root or full.startswith(root + os.sep):
                return full, prefix
            return False, prefix
        return None, None

    def do_GET(self):
        self._timed(True)

    def do_HEAD(self):
        self._timed(False)

    def do_POST(self):
        self._timed(True)

    def _timed(self, body):
        # counters and histogram are driven from one place, so they cannot
        # disagree about how many requests there were.
        #
        # THE INTROSPECTION ENDPOINTS DO NOT COUNT THEMSELVES. A widget polling
        # /stats once a second is a hundred requests over a hundred seconds, and
        # they are fast -- so they would have dragged p50 down and made the
        # server look quicker exactly while someone was watching it. The
        # counters were already clean because record() is only reached through
        # _serve, but the histogram was not: observe() ran in the finally for
        # every request including this one.
        #
        # Same reasoning as excluding the viewer HTML from "tiles": a number
        # that moves because you looked at it is worse than no number.
        t0 = time.perf_counter()
        with STATS_LOCK:
            INFLIGHT["n"] += 1
        introspection = False
        # _status is set by the existing send_response override -- the one
        # door every response shape exits through. Reset per request so a
        # keep-alive connection cannot leak the previous status forward.
        self._status = 0
        try:
            introspection = self._special()
            if introspection:
                return
            self._serve(body=body)
        finally:
            with STATS_LOCK:
                INFLIGHT["n"] -= 1
            if not introspection:
                observe(time.perf_counter() - t0, kind_of(self.path))
            rpc_done(method_of(self.path), getattr(self, "_status", 0),
                     time.perf_counter() - t0)

    def _special(self):
        # handled before the mount table and before the static root, so a
        # directory called "metrics" can never shadow the counter.
        clean = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if clean.startswith("/kingfisher.routing.v1.RoutingService/"):
            if self.command != "POST":
                return self._send(405, "application/json",
                                  b'{"error": "RPCs are POST"}')
            return self._routing_rpc(clean.rpartition("/")[2])
        if clean.startswith("/kingfisher.discovery.v1.DiscoveryService/"):
            if self.command != "POST":
                return self._send(405, "application/json",
                                  b'{"error": "RPCs are POST"}')
            return self._rpc(clean.rpartition("/")[2])
        if clean == "/geofences.geojson":
            # the store as a file any GIS tool opens. CORS open on purpose:
            # fence polygons are the least secret thing in the estate, and
            # the header is what lets a browser editor fetch them directly.
            try:
                qs = urllib.parse.urlparse(self.path).query
                tag = urllib.parse.parse_qs(qs).get("tag", [""])[0]
                payload = json.dumps(fences_geojson(tag), indent=1).encode()
            except Exception as e:
                log("warn", "geofence_store_unreachable", err=str(e)[:200])
                return self._send(503, "application/json", json.dumps(
                    {"error": "geofence store unreachable", "detail": str(e)[:200]}).encode())
            self.send_response(200)
            self.send_header("content-type", "application/geo+json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("access-control-allow-origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return True
        if clean == "/directory":
            # THE MAP DIRECTORY. A shelf of 397 datasets is not a menu; six
            # surveys is. Every consumer that has tried to show the shelf has
            # ended up with a hardcoded list of layer names or no chooser at
            # all, because kingfisher published what it HOLDS and never how it
            # is ORGANISED. This is the second, and it is the same grouping
            # kingfisher already uses on itself: source, then collection.
            #
            # Manifests only, like /shelf -- a menu that costs a read of every
            # cells.json is a menu nobody puts in a UI.
            root = MOUNTS.get("/ingested/")
            if not root:
                return self._send(503, "application/json", json.dumps(
                    {"error": "no /ingested/ mount on this instance"}).encode())
            import shelf
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bbox = None
            if q.get("bbox"):
                try:
                    bbox = [float(x) for x in q["bbox"][0].split(",")]
                    if len(bbox) != 4:
                        raise ValueError
                except ValueError:
                    return self._send(400, "application/json", json.dumps(
                        {"error": "bbox wants four numbers: w,s,e,n"}).encode())
            body = shelf.directory(shelf.index(root), bbox)
            # what a request may ask for, and what would fit right now, so a
            # consumer can size its ask instead of discovering the answer
            body["limits"] = shelf.limits()
            return self._send(200, "application/json", json.dumps(body, indent=1).encode())
        if clean in ("/shelf", "/cells"):
            # THE PRICED DOOR. /shelf says what is held and where; /cells
            # serves it clipped to a window and folded to fit a budget.
            #
            # A caller names a WINDOW and a BUDGET, never a resolution: only
            # kingfisher knows what is on the shelf, so only kingfisher can
            # pick the finest resolution that fits. The old door offered one
            # answer -- everything, at full resolution -- and dodo's map pane
            # was OOM-killed by it. An unbounded door can only say yes, and
            # the consumer finds out by dying.
            root = MOUNTS.get("/ingested/")
            if not root:
                return self._send(503, "application/json", json.dumps(
                    {"error": "no /ingested/ mount on this instance"}).encode())
            import shelf
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bbox = None
            if q.get("bbox"):
                try:
                    bbox = [float(x) for x in q["bbox"][0].split(",")]
                    if len(bbox) != 4:
                        raise ValueError
                except ValueError:
                    return self._send(400, "application/json", json.dumps(
                        {"error": "bbox wants four numbers: w,s,e,n"}).encode())
            try:
                budget = int(q.get("budget", [shelf.DEFAULT_BUDGET])[0])
            except ValueError:
                return self._send(400, "application/json", json.dumps(
                    {"error": "budget wants a number of cells"}).encode())
            if budget < 1:
                return self._send(400, "application/json", json.dumps(
                    {"error": "budget must be at least one cell"}).encode())
            if budget > shelf.MAX_BUDGET:
                # refused before anything is priced: no consumer on this
                # estate can hold a million cells, and the process that would
                # build them has 256Mi (shelf.py, THE MEMORY CEILING)
                return self._send(413, "application/json", json.dumps(
                    {"error": f"budget above the ceiling of {shelf.MAX_BUDGET} cells",
                     "max_budget": shelf.MAX_BUDGET, "limits": shelf.limits()}).encode())
            entries = shelf.index(root)
            if clean == "/shelf":
                # the price, and the index behind it. Answered from manifests
                # alone -- no cells.json is opened -- so asking what something
                # costs is always cheaper than being surprised by it.
                quote = shelf.price(entries, bbox, budget)
                quote["shelf"] = [
                    {k: e[k] for k in ("id", "title", "res", "cells", "points",
                                       "bbox", "layers")}
                    for e in (shelf.overlapping(entries, bbox) if bbox else entries)]
                return self._send(200, "application/json",
                                  json.dumps(quote, indent=1).encode())
            sel = shelf.overlapping(entries, bbox)
            # THE WINDOW IS PART OF THE QUESTION. Without it the estimate
            # counts a global basemap's whole 287k cells against a Bay-sized
            # request and drives it to res 1 holding one cell.
            # THE MEMORY IN THE ROOM. choose_res fits the budget; fit also
            # fits this process's live heap against its cgroup limit, priced
            # from the same estimate /shelf publishes, and serves coarser when
            # the caller's budget would not have fit (shelf.py, THE MEMORY
            # CEILING). The caller is told, in budget_served and clipped_by.
            try:
                res, _est, clipped = shelf.fit(sel, budget, bbox,
                                               limit=shelf.memory_limit(),
                                               heap=shelf.heap_bytes())
            except shelf.NoRoom as e:
                # not even 122 cells fit: the heap has plateaued too close to
                # the limit for any fold. A smaller window does not help; a
                # restart or a larger limit does. Say so, and let the health
                # of the thing be judged from /metrics.
                return self._send(503, "application/json", json.dumps(
                    {"error": "no memory left in this process for a fold",
                     "heap_bytes": e.heap, "memory_limit_bytes": e.limit,
                     "limits": shelf.limits()}).encode())
            if clipped:
                budget = min(budget, clipped)
            # an explicit res is an override for looking AT the rollup, not
            # the normal way to ask -- and it is still capped by the budget's
            # choice, so it can only ever ask for less than it can hold.
            if q.get("res"):
                try:
                    # ONLY COARSER. A higher H3 res is a finer grid and more
                    # cells, so honouring a caller's res upward would let it
                    # bust the budget it just declared -- which is the whole
                    # failure, re-entered through the override.
                    res = max(0, min(res, int(q["res"][0])))
                except ValueError:
                    return self._send(400, "application/json", json.dumps(
                        {"error": "res wants a number"}).encode())
            layer = (q.get("layer") or [None])[0]
            cells, read, res = shelf.read_folded(root, sel, res, bbox=bbox,
                                                 layer=layer, budget=budget)
            body = {"res": res, "datasets": len(sel), "cells_read": read,
                    "points": sum(e["points"] for e in sel),
                    "budget": int(q.get("budget", [shelf.DEFAULT_BUDGET])[0]),
                    "budget_served": budget, "bbox": bbox, "cells": cells}
            if clipped:
                body["clipped_by"] = "memory"
            return self._send(200, "application/json", json.dumps(body).encode())
        if clean == "/discovery":
            # the librarian's answer: what kingfisher knows about, one JSON
            # blob -- sources it can reach and every dataset in the index,
            # held or not. Listing is cheap and carries no flag; the acts
            # that reach other people's servers (refresh, fetch) are the
            # flipr-gated POST RPCs.
            import providers
            return self._send(200, "application/json", json.dumps({
                "sources": providers.list_sources(),
                "datasets": providers.list_datasets(),
            }, indent=1).encode())
        if clean == "/api":
            # the contract, as bytes: a FileDescriptorSet buf-built from
            # proto/ -- the SAME bytes the committed descriptor.binpb holds,
            # so the published spec cannot drift from the repository's. The
            # runtime stays stdlib-only by serving the descriptor rather
            # than importing generated code; parsing it is the caller's
            # side of the seam (buf curl, grpcurl, any protobuf runtime).
            payload = descriptor_bytes()
            if payload is None:
                # absent is a DEPLOY defect, not a quiet 404: the image was
                # built without the descriptor beside serve.py. Say so.
                return self._send(503, "application/json", json.dumps(
                    {"error": "descriptor.binpb not deployed beside serve.py"}).encode())
            return self._send(200, "application/x-protobuf; messageType=google.protobuf.FileDescriptorSet",
                              payload)
        if clean in ("/health", "/ready"):
            # /health is liveness: the process is up and answering, so do not
            # restart it. It is 200 whatever else is true, and the body carries
            # the nuance.
            #
            # /ready is the one that can say no. A kingfisher whose mounts are
            # configured but empty is exactly the failure README.md calls the
            # single most expensive mistake available here: the pod starts
            # cleanly, reports healthy, and serves 404s forever. The manifests
            # wire /health as a readinessProbe, so that pod stays in the Service
            # and keeps answering 404 to everyone. Pointing the probes at /ready
            # takes it out of rotation instead.
            state = _assess()
            path_ok = True if clean == "/health" else state["ready"]
            return self._send(200 if path_ok else 503, "application/json",
                              json.dumps(dict(state, ok=path_ok)).encode())
        if clean == "/stats":
            with STATS_LOCK:
                snap = json.loads(json.dumps(STATS))
            snap["uptime_s"] = round(time.time() - STARTED, 1)
            snap["started_at"] = STARTED_ISO
            snap["started_at_unix"] = round(STARTED, 3)
            snap["generation"] = GENERATION["n"]
            snap["mounts"] = dict(MOUNTS)
            snap["inventory"] = [
                dict(e, series=len(e["series"] or []))
                for e in inventory()]
            snap["inflight"] = INFLIGHT["n"]
            snap["duration"] = {
                "count": DUR["count"], "sum_s": round(DUR["sum"], 6),
                "mean_ms": round(DUR["sum"] / DUR["count"] * 1000, 3)
                           if DUR["count"] else None,
                "by_kind": {k: {"count": v["count"],
                                "mean_ms": round(v["sum"] / v["count"] * 1000, 3)}
                            for k, v in DUR["by_kind"].items()}}
            return self._send(200, "application/json",
                              json.dumps(snap, indent=1).encode())
        if clean == "/metrics":
            return self._send(200, "text/plain; version=0.0.4", self._prom())
        if clean == "/reload":
            # THE POKE. Bumps the generation, which invalidates every ETag we
            # have handed out, and drops the inventory so the next read walks
            # the tree again. It changes no data and touches no file, so it is
            # safe to call from anywhere and safe to call twice.
            with INV_LOCK:
                INV_CACHE["at"] = 0.0
                INV_CACHE["value"] = None
            with STATS_LOCK:
                GENERATION["n"] += 1
                gen = GENERATION["n"]
            inv = inventory(force=True)
            log("info", "reload", generation=gen,
                tiles_on_disk=sum(e["tiles"] for e in inv))
            return self._send(200, "application/json", json.dumps(
                {"reloaded": True, "generation": gen,
                 "mounts": {e["prefix"]: {"tiles": e["tiles"], "bytes": e["bytes"],
                                          "series": len(e["series"] or [])}
                            for e in inv}}, indent=1).encode())
        if clean == "/" and not os.path.isfile(os.path.join(ROOT, "index.html")):
            return self._send(200, "text/html; charset=utf-8", self._landing())
        return False

    def _send(self, status, ctype, payload):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        # everything except HEAD gets the body it was promised: the guard
        # exists so HEAD is header-only, and "GET" over-matched the moment
        # POST RPCs arrived -- a content-length with no bytes behind it is
        # an IncompleteRead in every client.
        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass
        return True

    # ------------------------------------------------------------ counters
    def _prom(self):
        with STATS_LOCK:
            s = json.loads(json.dumps(STATS))
        with STATS_LOCK:
            d = {"buckets": dict(DUR["buckets"]), "inf": DUR["inf"],
                 "sum": DUR["sum"], "count": DUR["count"],
                 "by_kind": json.loads(json.dumps(DUR["by_kind"])),
                 "by_method": json.loads(json.dumps(DUR.get("by_method", {})))}
            inflight = INFLIGHT["n"]
        L = ["# HELP kingfisher_uptime_seconds seconds since start",
             "# TYPE kingfisher_uptime_seconds gauge",
             f"kingfisher_uptime_seconds {time.time() - STARTED:.1f}",
             "# HELP kingfisher_generation cache generation; bumped by /reload",
             "# TYPE kingfisher_generation gauge",
             f"kingfisher_generation {GENERATION['n']}",
             "# HELP kingfisher_mounts total data roots mounted",
             "# TYPE kingfisher_mounts gauge",
             f"kingfisher_mounts {len(MOUNTS)}",
             "# HELP kingfisher_start_time_seconds unix time the process started",
             "# TYPE kingfisher_start_time_seconds gauge",
             f"kingfisher_start_time_seconds {STARTED:.3f}",
             "# HELP kingfisher_inflight_requests requests being served right now",
             "# TYPE kingfisher_inflight_requests gauge",
             f"kingfisher_inflight_requests {inflight}",
             # the two numbers the /cells ceiling is computed from, so the next
             # climb toward the limit is seen on a board before the kill
             "# HELP kingfisher_heap_bytes resident set of this process (VmRSS)",
             "# TYPE kingfisher_heap_bytes gauge",
             f"kingfisher_heap_bytes {_shelf_mod().heap_bytes()}",
             "# HELP kingfisher_memory_limit_bytes cgroup memory limit; 0 when none was found",
             "# TYPE kingfisher_memory_limit_bytes gauge",
             f"kingfisher_memory_limit_bytes {_shelf_mod().memory_limit() or 0}",
             "# HELP kingfisher_requests_total every request served",
             "# TYPE kingfisher_requests_total counter",
             f"kingfisher_requests_total {s['requests_total']}",
             "# HELP kingfisher_tiles_total files served out of a data mount",
             "# TYPE kingfisher_tiles_total counter",
             f"kingfisher_tiles_total {s['tiles_total']}",
             "# HELP kingfisher_tile_bytes_total bytes served out of a data mount",
             "# TYPE kingfisher_tile_bytes_total counter",
             f"kingfisher_tile_bytes_total {s['tile_bytes_total']}",
             "# HELP kingfisher_bytes_total bytes served, everything",
             "# TYPE kingfisher_bytes_total counter",
             f"kingfisher_bytes_total {s['bytes_total']}",
             "# HELP kingfisher_errors_total responses of 400 or worse",
             "# TYPE kingfisher_errors_total counter",
             f"kingfisher_errors_total {s['errors_total']}",
             "# HELP kingfisher_tiles_by_mount_total tiles per mount prefix",
             "# TYPE kingfisher_tiles_by_mount_total counter"]
        for k, n in sorted(s["by_mount"].items()):
            L.append(f'kingfisher_tiles_by_mount_total{{mount="{k}"}} {n}')
        L += ["# HELP kingfisher_tiles_by_kind_total tiles per resolution or manifest",
              "# TYPE kingfisher_tiles_by_kind_total counter"]
        for k, n in sorted(s["by_kind"].items()):
            L.append(f'kingfisher_tiles_by_kind_total{{kind="{k}"}} {n}')
        # WHAT EXISTS, as opposed to what was asked for. Cached; see INV_TTL.
        inv = inventory()
        L += ["# HELP kingfisher_mount_bytes bytes of map data on disk",
              "# TYPE kingfisher_mount_bytes gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_bytes{{mount="{e["prefix"]}"}} {e["bytes"]}')
        L += ["# HELP kingfisher_mount_files files on disk under a mount",
              "# TYPE kingfisher_mount_files gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_files{{mount="{e["prefix"]}"}} {e["files"]}')
        L += ["# HELP kingfisher_mount_tiles chunk files on disk under a mount",
              "# TYPE kingfisher_mount_tiles gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_tiles{{mount="{e["prefix"]}"}} {e["tiles"]}')
        L += ["# HELP kingfisher_mount_tile_bytes bytes of chunk data on disk",
              "# TYPE kingfisher_mount_tile_bytes gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_tile_bytes{{mount="{e["prefix"]}"}} {e["tile_bytes"]}')
        L += ["# HELP kingfisher_mount_series series declared in a mount's manifest",
              "# TYPE kingfisher_mount_series gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_series{{mount="{e["prefix"]}"}} '
                     f'{len(e["series"] or [])}')
        L += ["# HELP kingfisher_mount_chunks chunks the manifest declares",
              "# TYPE kingfisher_mount_chunks gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_chunks{{mount="{e["prefix"]}"}} '
                     f'{e["chunks"] or 0}')
        L += ["# HELP kingfisher_mount_resolutions H3 resolutions a mount publishes",
              "# TYPE kingfisher_mount_resolutions gauge"]
        for e in inv:
            L.append(f'kingfisher_mount_resolutions{{mount="{e["prefix"]}"}} '
                     f'{len(e["resolutions"])}')
        L += ["# HELP kingfisher_mount_readable 1 if the mount's path exists and is readable",
              "# TYPE kingfisher_mount_readable gauge"]
        for prefix, root in sorted(MOUNTS.items()):
            ok = 1 if (os.path.isdir(root) and os.access(root, os.R_OK)) else 0
            L.append(f'kingfisher_mount_readable{{mount="{prefix}"}} {ok}')
        rpc = s.get("rpc", {})
        L += ["# HELP kingfisher_rpc_requests_total RPC calls served, by method and outcome",
              "# TYPE kingfisher_rpc_requests_total counter"]
        for m, outs in sorted(rpc.items()):
            for o, n in sorted(outs.items()):
                L.append(f'kingfisher_rpc_requests_total{{method="{m}",outcome="{o}"}} {n}')
        L += ["# HELP kingfisher_rpc_duration_seconds RPC wall time, by method",
              "# TYPE kingfisher_rpc_duration_seconds counter"]
        for m, v in sorted(d.get("by_method", {}).items()):
            L.append(f'kingfisher_rpc_duration_seconds_sum{{method="{m}"}} {v["sum"]:.6f}')
            L.append(f'kingfisher_rpc_duration_seconds_count{{method="{m}"}} {v["count"]}')
        try:
            import providers as _prov
            with _prov._OUT_LOCK:
                out = {k: dict(v) for k, v in _prov.OUTBOUND.items()}
        except ImportError:
            out = {}
        L += ["# HELP kingfisher_provider_requests_total outbound requests to data providers",
              "# TYPE kingfisher_provider_requests_total counter"]
        for src, row in sorted(out.items()):
            for oc in ("ok", "error"):
                if oc in row:
                    L.append(f'kingfisher_provider_requests_total{{source="{src}",outcome="{oc}"}} {row[oc]}')
        L += ["# HELP kingfisher_provider_bytes_total bytes read from data providers",
              "# TYPE kingfisher_provider_bytes_total counter"]
        for src, row in sorted(out.items()):
            L.append(f'kingfisher_provider_bytes_total{{source="{src}"}} {row.get("bytes", 0)}')
        # Egress as net.py sees it. Distinct from the provider counters above,
        # which are labelled by SOURCE and count catalogue reads: these are
        # labelled by HOST and cover every outbound call the service makes --
        # providers, valhalla, flipr, the schema registry, postgrest.
        #
        # The retry counter is the one worth alerting on. A rising ok count with
        # a flat retry count is a healthy dependency; a flat ok count with a
        # rising retry count is a dependency degrading while still technically
        # working, which is the state that has no other symptom until it fails.
        try:
            import net as _net
            with _net._LOCK:
                ncalls = {k: dict(v) for k, v in _net.CALLS.items()}
                nretries = {k: dict(v) for k, v in _net.RETRIES.items()}
                ndur = {k: dict(v) for k, v in _net.DURATION.items()}
        except ImportError:
            ncalls = nretries = ndur = {}
        L += ["# HELP kingfisher_outbound_requests_total outbound calls through net.py, by host and outcome",
              "# TYPE kingfisher_outbound_requests_total counter"]
        for h, row in sorted(ncalls.items()):
            for oc, n3 in sorted(row.items()):
                L.append(f'kingfisher_outbound_requests_total{{host="{h}",outcome="{oc}"}} {n3}')
        L += ["# HELP kingfisher_outbound_retries_total retries taken, by host and reason",
              "# TYPE kingfisher_outbound_retries_total counter"]
        for h, row in sorted(nretries.items()):
            for rs, n3 in sorted(row.items()):
                L.append(f'kingfisher_outbound_retries_total{{host="{h}",reason="{rs}"}} {n3}')
        L += ["# HELP kingfisher_outbound_duration_seconds wall time per outbound call INCLUDING backoff sleeps",
              "# TYPE kingfisher_outbound_duration_seconds counter"]
        for h, v in sorted(ndur.items()):
            L.append(f'kingfisher_outbound_duration_seconds_sum{{host="{h}"}} {v["sum"]:.6f}')
            L.append(f'kingfisher_outbound_duration_seconds_count{{host="{h}"}} {v["count"]}')
        try:
            import heartbeat as _hb
            with _hb._BEATS_LOCK:
                beats = dict(_hb.BEATS)
        except ImportError:
            beats = {}
        L += ["# HELP kingfisher_heartbeat_total lease heartbeats emitted, by outcome",
              "# TYPE kingfisher_heartbeat_total counter"]
        for oc, n2 in sorted(beats.items()):
            L.append(f'kingfisher_heartbeat_total{{outcome="{oc}"}} {n2}')
        L += ["# HELP kingfisher_responses_total responses per status",
              "# TYPE kingfisher_responses_total counter"]
        for k, n in sorted(s["by_status"].items()):
            L.append(f'kingfisher_responses_total{{status="{k}"}} {n}')
        L += ["# HELP kingfisher_request_duration_seconds time to serve a request",
              "# TYPE kingfisher_request_duration_seconds histogram"]
        cum = 0
        for b in DUR_BUCKETS:
            cum += d["buckets"][b]
            L.append(f'kingfisher_request_duration_seconds_bucket{{le="{b}"}} {cum}')
        L.append(f'kingfisher_request_duration_seconds_bucket{{le="+Inf"}} {d["count"]}')
        L.append(f'kingfisher_request_duration_seconds_sum {d["sum"]:.6f}')
        L.append(f'kingfisher_request_duration_seconds_count {d["count"]}')
        L += ["# HELP kingfisher_kind_duration_seconds time per kind of thing served",
              "# TYPE kingfisher_kind_duration_seconds counter"]
        for k, v in sorted(d["by_kind"].items()):
            L.append(f'kingfisher_kind_duration_seconds_sum{{kind="{k}"}} {v["sum"]:.6f}')
            L.append(f'kingfisher_kind_duration_seconds_count{{kind="{k}"}} {v["count"]}')
        return ("\n".join(L) + "\n").encode()

    # ------------------------------------------------------------- landing
    def _landing(self):
        with STATS_LOCK:
            s = json.loads(json.dumps(STATS))
        rows, catalogues = [], []
        for e in inventory():
            series = e["series"] or []
            shown = ", ".join(html.escape(x) for x in series[:10])
            if len(series) > 10:
                shown += f" and {len(series) - 10} more"
            res = "r" + ", r".join(str(r) for r in e["resolutions"]) \
                if e["resolutions"] else "&mdash;"
            if e.get("manifests_skipped"):
                shown += (" &middot; not parsed, over "
                          + human(MANIFEST_PARSE_CAP) + ": "
                          + ", ".join(html.escape(x) for x in e["manifests_skipped"]))
            rows.append(
                f"<tr><td class='f'>{html.escape(e['prefix'])}</td>"
                f"<td class='f dim'>{html.escape(e['root'])}</td>"
                f"<td class='r'>{len(series) or '&mdash;'}</td>"
                f"<td class='r'>{e['chunks'] if e['chunks'] is not None else '&mdash;'}</td>"
                f"<td class='r'>{res}</td>"
                f"<td class='r'>{e['tiles']:,}</td>"
                f"<td class='r'>{e['files']:,}</td>"
                f"<td class='r'>{human(e['bytes'])}</td></tr>"
                f"<tr><td colspan='8' class='s'>{shown or 'no manifest found'}</td></tr>")
            if e.get("catalogue"):
                crows = "".join(
                    "<tr><td class='f'>" + html.escape(c["name"]) + "</td>"
                    "<td class='k'>" + html.escape(c["kind"]) + "</td>"
                    "<td class='f dim'>" + html.escape(c["unit"]) + "</td>"
                    "<td class='dim'>" + html.escape(c["frame"]) + "</td>"
                    "<td class='dim'>" + html.escape(c["vintage"]) + "</td>"
                    "<td class='r'>" + num(c["min"]) + " .. " + num(c["max"]) + "</td>"
                    "<td class='r'>" + num(c["p50"]) + "</td>"
                    "<td class='r'>" + num(c["n"]) + "</td></tr>"
                    for c in e["catalogue"])
                gen = (" &middot; generated " + html.escape(e["generated_at"])) \
                    if e.get("generated_at") else ""
                src = (" &middot; stats from res" + str(e["catalogue_res"])) \
                    if e.get("catalogue_res") else ""
                catalogues.append(
                    "<h2>" + html.escape(e["prefix"]) + " &mdash; "
                    + str(len(e["catalogue"])) + " series" + src + gen + "</h2>"
                    + "<p class='lede'>Range, median and cell counts are that "
                      "resolution's, not the finest one published &mdash; a "
                      "res-3 cell is a region and a res-8 cell is a "
                      "neighbourhood, so the same series has different "
                      "statistics at each.</p>"
                    "<table><tr><th>series</th><th>kind</th><th>unit</th>"
                    "<th>frame</th><th>vintage</th><th>range</th><th>median</th>"
                    "<th>cells</th></tr>" + crows + "</table>")
        errs = sorted(s.get("error_paths", {}).items(), key=lambda kv: -kv[1])[:12]
        errrows = "".join(
            f"<tr><td class='f'>{html.escape(k)}</td><td class='r'>{v:,}</td></tr>"
            for k, v in errs) or \
            "<tr><td colspan='2' class='s'>none</td></tr>"
        counters = "".join(
            f"<tr><td class='f'>{html.escape(k)}</td><td class='r'>{v:,}</td></tr>"
            for k, v in (("requests", s["requests_total"]),
                         ("tiles", s["tiles_total"]),
                         ("tile bytes", s["tile_bytes_total"]),
                         ("bytes", s["bytes_total"]),
                         ("errors", s["errors_total"])))
        bykind = "".join(
            f"<tr><td class='f'>{html.escape(k)}</td><td class='r'>{v:,}</td></tr>"
            for k, v in sorted(s["by_kind"].items())) or \
            "<tr><td colspan='2' class='s'>nothing served yet</td></tr>"
        # The shared sheet is inlined rather than linked: one request, and it
        # cannot half-load. It is re-read every time this page is served, which
        # is cheap next to the inventory walk already behind it.
        sheet, sheet_path, sheet_err = bench_css()
        if sheet is None:
            # DEGRADE, DO NOT DIE. Serving three of four datasets beats refusing
            # to start, and this page's job is to say what is wrong with the
            # service -- so it is the last thing that should go dark when
            # something is wrong. The note goes in the served CSS AND on the
            # page: unstyled and quiet looks exactly like a CSS bug in a page
            # nobody has touched, which is a mystery rather than a diagnosis.
            style = ("<style>/* bench-ui stylesheet NOT READ (" +
                     html.escape(sheet_err) + "). This page is unstyled. "
                     "Install it from janearc/bench-ui. */" +
                     KINGFISHER_CSS + "</style>")
            banner = ('<p class="nostyle">UNSTYLED. The shared bench-ui '
                      'stylesheet could not be read (' + html.escape(sheet_err) +
                      '). The page below is correct; only its appearance is '
                      'missing. Install it from janearc/bench-ui -- on a pod '
                      'this means mounting /var/mesh-ui.</p>')
        else:
            style = "<style>\n" + sheet + KINGFISHER_CSS + "</style>"
            banner = ""

        page = f"""<!doctype html><meta charset="utf-8"><title>kingfisher</title>
{style}
<div class="wrap">
{banner}
<p class="back"><a href="{html.escape(_dodo_url_for(self.headers.get('Host')))}">&larr; dodo</a></p>
<h1>kingfisher</h1>
<p class="lede">H3 map data over HTTP. A mount table over directories, read
strictly read-only, served on one origin so a viewer can fetch its catalogue at
runtime. Up {round(time.time() - STARTED)}s. The counters are monotonic on
purpose: a rate computed here would be wall-clock, so difference them
yourself.</p>

<h2>what it knows about</h2>
<table><tr><th>prefix</th><th>root</th><th>series</th><th>chunks</th>
<th>res</th><th>tiles</th><th>files</th><th>on disk</th></tr>
{''.join(rows) or '<tr><td colspan="8" class="s">no mounts declared</td></tr>'}</table>

<h2>served</h2>
<table><tr><th>counter</th><th>total</th></tr>{counters}</table>

<h2>by kind</h2>
<table><tr><th>kind</th><th>tiles</th></tr>{bykind}</table>

<h2>what failed</h2>
<table><tr><th>status and path</th><th>times</th></tr>{errrows}</table>

{''.join(catalogues)}
<h2>endpoints</h2>
<p class="lede"><code>/health</code> liveness &middot;
<code><a href="/stats">/stats</a></code> counters as JSON &middot;
<code><a href="/metrics">/metrics</a></code> the same as prometheus text</p>
</div>
"""
        return page.encode()

    # -------------------------------------------------------------- serving
    def _serve(self, body):
        hit, prefix = self._mounted(self.path)
        if hit is None:
            # not mounted: the static root handles it, and it is still counted
            self._sent = 0
            self._status = 200
            if body:
                super().do_GET()
            else:
                super().do_HEAD()
            record(self.path, None, self._status, self._sent)
            return
        if hit is False:
            return self._fail(403, f"path escapes its mount: {self.path}", prefix)
        if os.path.isdir(hit):
            # A JSON index rather than an HTML autoindex, and only at a mount
            # root. /layers/ and /rides/ carry no resN.json manifest, so without
            # this there was no way for a client to find out what they hold --
            # the landing page said "no manifest found" and this returned 403.
            # A catalogue you cannot read at runtime is not a catalogue.
            clean = urllib.parse.urlparse(self.path).path
            if clean.rstrip("/") + "/" == prefix:
                try:
                    names = sorted(n for n in os.listdir(hit)
                                   if not n.startswith("."))
                except OSError as e:
                    return self._fail(500, f"cannot list {prefix}: {e}", prefix)
                files, dirs = [], []
                for n in names:
                    # HALF-WRITTEN NAMES ARE NOT ON THE SHELF. ingestd builds
                    # <dataset>.tmp/ and renames it; gibsd writes manifest.json.tmp
                    # and renames it; the fetcher downloads to <id>.part. Each is
                    # correct for readers of the FINAL path, and each showed up
                    # in the listing while it existed (issue 80). A consumer
                    # walking the shelf should never see a name that will be
                    # gone, or different, by the time it asks for it.
                    if n.endswith((".tmp", ".part")):
                        continue
                    full = os.path.join(hit, n)
                    if os.path.isdir(full):
                        dirs.append(n + "/")
                    else:
                        files.append({"name": n, "bytes": os.path.getsize(full)})
                man = next((f["name"] for f in files
                            if f["name"].startswith("res")
                            and f["name"].endswith(".json")), None)
                payload = json.dumps({"mount": prefix, "manifest": man,
                                      "directories": dirs, "files": files},
                                     indent=1).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(payload)
                    except BrokenPipeError:
                        pass
                record(self.path, None, 200, len(payload))
                return
            return self._fail(403, "directory listing is off for mounted data", prefix)
        if not os.path.isfile(hit):
            return self._fail(404, f"no such file under mount: {self.path}", prefix)
        try:
            st = os.stat(hit)
            size = st.st_size
            # the generation is IN the tag, so a poke invalidates everything
            # without having to know which files changed
            etag = f'"{GENERATION["n"]}-{int(st.st_mtime_ns)}-{size}"'
            # If-None-Match wins when both are present, per RFC 9110: the entity
            # tag is the stronger validator and it is the one that carries the
            # generation.
            fresh = self.headers.get("If-None-Match") == etag
            if not fresh and self.headers.get("If-Modified-Since"):
                # We advertise last-modified, so we have to honour the request
                # header that goes with it. Emitting a validator and ignoring it
                # invites a well-behaved client to do the one thing that does not
                # work -- it asked politely, got a full 200, and re-shipped every
                # byte. Measured before fixing: If-None-Match 304, If-Modified-
                # Since 200 with the whole file, three times out of three.
                #
                # CAVEAT worth knowing: this tracks MTIME only. A /reload that
                # bumps the generation without any file changing will not reach
                # an If-Modified-Since client, because nothing it can see has
                # changed. In the case reload exists for -- a regeneration wrote
                # new files -- mtimes move and it works. ETag is still the
                # better validator and the one to prefer.
                try:
                    since = email.utils.parsedate_to_datetime(
                        self.headers["If-Modified-Since"])
                    if since is not None:
                        # HTTP dates have one-second resolution; the file's
                        # mtime almost never does, so compare truncated or a
                        # file saved mid-second looks perpetually newer.
                        fresh = int(st.st_mtime) <= int(since.timestamp())
                except (TypeError, ValueError):
                    fresh = False
            if fresh:
                # not modified: no body, and NOT counted as a tile, because no
                # tile was emitted. anything measuring throughput would
                # otherwise count the cheapest possible response as delivery.
                self.send_response(304)
                self.send_header("etag", etag)
                self.send_header("last-modified", self.date_time_string(st.st_mtime))
                self.send_header("cache-control", "public, max-age=60")
                self.end_headers()
                record(self.path, None, 304, 0)
                return
            # RANGE: one row of the ETA bake is 115KB inside a 954MB file,
            # and a dart is one row. Without this, the click that should cost
            # 115KB ships the whole table -- measured 200-with-954MB before
            # this existed. Single ascending range only (bytes=start-end,
            # bytes=start-, bytes=-suffix); multipart is complexity no caller
            # here wants. An unsatisfiable range answers 416 with the size,
            # per RFC 9110, so a client can recover instead of guessing.
            start, end = 0, size - 1
            partial = False
            rng = self.headers.get("Range", "")
            m = RANGE_RE.match(rng) if rng else None
            if m and not fresh:
                a, b = m.group(1), m.group(2)
                if a == "" and b != "":          # bytes=-suffix
                    start, end = max(0, size - int(b)), size - 1
                elif a != "":
                    start = int(a)
                    end = int(b) if b else size - 1
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("content-range", f"bytes */{size}")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    record(self.path, prefix, 416, 0)
                    return
                end = min(end, size - 1)
                partial = True
            nbytes = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("content-type", self.guess_type(hit))
            self.send_header("content-length", str(nbytes))
            if partial:
                self.send_header("content-range", f"bytes {start}-{end}/{size}")
            self.send_header("accept-ranges", "bytes")
            self.send_header("etag", etag)
            self.send_header("last-modified", self.date_time_string(st.st_mtime))
            self.send_header("cache-control", "public, max-age=60")
            self.end_headers()
            if body:
                with open(hit, "rb") as f:
                    f.seek(start)
                    remaining = nbytes
                    while remaining > 0:
                        buf = f.read(min(64 * 1024, remaining))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        remaining -= len(buf)
            record(self.path, prefix, 206 if partial else 200, nbytes)
        except BrokenPipeError:
            record(self.path, prefix, 200, 0)
        except Exception as e:
            self._fail(500, f"{self.path}: {e.__class__.__name__}: {e}", prefix)

    def _fail(self, code, msg, prefix=None):
        # loud and structured. a viewer showing an empty picker because a mount
        # is missing is worse than one that says the mount is missing.
        payload = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass
        record(self.path, prefix, code, len(payload))
        print(f"  {code}  {msg}", file=sys.stderr, flush=True)

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)
        # THE GENERATION ON EVERY RESPONSE, not only inside the ETag. A
        # cache that wants to know whether kingfisher has reloaded should
        # not have to parse a tile's ETag to find out; this is the one door
        # every response exits through, 404s included.
        self.send_header("x-kingfisher-generation", str(GENERATION["n"]))

    def _rpc(self, name):
        # protojson in, protojson out, GENERATED types in between: serve.py
        # never hand-builds an RPC response dict, so the wire cannot drift
        # from the contract.
        try:
            json_format, pb2, providers = _rpc_modules()
        except ImportError as e:
            return self._send(501, "application/json", json.dumps(
                {"error": f"rpc surface needs protobuf: {e}"}).encode())
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        import flipr_client
        try:
            if name == "ListSources":
                resp = pb2.ListSourcesResponse()
                for src in providers.list_sources():
                    m = resp.sources.add()
                    m.id = src["id"]; m.title = src["title"]
                    m.index_url = src["index_url"]; m.expensive = src["expensive"]
                    m.paid = src.get("paid", False)
                    m.country = src.get("country", "")
            elif name == "ListDatasets":
                req = json_format.Parse(raw, pb2.ListDatasetsRequest())
                # THE SAME THREE ANSWERS RefreshIndex GIVES. It used to answer
                # 200 with nothing for an unadapted source -- which is the
                # exact answer an adapted source gives before anything is
                # indexed, so a caller could not tell "nothing here yet" from
                # "nothing can ever be here" (issue 76). Unknown is 404,
                # unadapted is 501 {"unimplemented"}, and the empty list is
                # reserved for a source that could have datasets and has none.
                if req.source_id:
                    if req.source_id not in {src["id"] for src in providers.SOURCES}:
                        return self._send(404, "application/json", json.dumps(
                            {"error": f"unknown source {req.source_id}"}).encode())
                    if req.source_id not in providers.ADAPTED:
                        return self._send(501, "application/json", json.dumps(
                            {"unimplemented": req.source_id}).encode())
                resp = pb2.ListDatasetsResponse()
                for d in providers.list_datasets(req.source_id or None):
                    resp.datasets.append(_record_msg(pb2, json_format, d))
            elif name == "RefreshIndex":
                req = json_format.Parse(raw, pb2.RefreshIndexRequest())
                out = providers.refresh_index(
                    req.source_id, flipr_flags(),
                    bbox=tuple(req.bbox) if req.bbox else None,
                    limit=req.limit or 50, query=req.query)
                if "refused" in out:
                    return self._send(403, "application/json",
                                      json.dumps(out).encode())
                if "unimplemented" in out:
                    return self._send(501, "application/json",
                                      json.dumps(out).encode())
                resp = pb2.RefreshIndexResponse(indexed=out["indexed"])
            elif name == "Fetch":
                req = json_format.Parse(raw, pb2.FetchRequest())
                out = providers.start_fetch(
                    req.dataset_id, flipr_flags(),
                    os.environ.get("KINGFISHER_SPOOL", "spool"))
                if "refused" in out:
                    return self._send(403, "application/json",
                                      json.dumps(out).encode())
                if "error" in out:
                    return self._send(404, "application/json",
                                      json.dumps(out).encode())
                resp = pb2.FetchResponse(ticket=out["ticket"])
            elif name == "GetFetchStatus":
                req = json_format.Parse(raw, pb2.GetFetchStatusRequest())
                d = providers.ticket_status(req.ticket)
                if d is None:
                    return self._send(404, "application/json",
                                      b'{"error": "unknown ticket"}')
                resp = pb2.GetFetchStatusResponse(
                    record=_record_msg(pb2, json_format, d))
            elif name == "Tombstone":
                req = json_format.Parse(raw, pb2.TombstoneRequest())
                if not req.revive and not req.reason.strip():
                    return self._send(400, "application/json",
                                      b'{"error": "a tombstone needs a reason"}')
                out = providers.tombstone(req.dataset_id, req.reason.strip(), revive=req.revive)
                if "error" in out:
                    return self._send(404, "application/json",
                                      json.dumps(out).encode())
                resp = pb2.TombstoneResponse(record=_record_msg(pb2, json_format, out))
            else:
                return self._send(404, "application/json",
                                  json.dumps({"error": f"no rpc {name}"}).encode())
        except flipr_client.FliprDown as e:
            # the flag plane is down, so the expensive surface refuses --
            # fail-closed by design, and LOUD: 503 naming flipr, and now the
            # same degraded posture /health carries, rather than the posture it
            # was promised in a comment and never given.
            FLIPR_DOWN["at"] = time.time()
            FLIPR_DOWN["err"] = str(e)[:200]
            return self._send(503, "application/json", json.dumps(
                {"error": "flipr unreachable; fetch surface refuses",
                 "detail": str(e)}).encode())
        except flipr_client.FlagMissing as e:
            # an undeclared flag is a WIRING mistake, not an off switch
            return self._send(500, "application/json", json.dumps(
                {"error": "flag not declared in flipr; wiring defect",
                 "detail": str(e)}).encode())
        except Exception as e:
            # an upstream provider failing must be a LABELLED answer, not a
            # dropped connection: the first GIBS index died on a decode
            # error, the exception escaped, and the caller saw traefik's
            # anonymous Bad Gateway instead of what broke.
            log("warn", "discovery_upstream_failed", rpc=name, err=str(e)[:200])
            return self._send(502, "application/json", json.dumps(
                {"error": "provider upstream failed", "detail": str(e)[:300]}).encode())
        return self._send(200, "application/json",
                          json_format.MessageToJson(resp).encode())

    def _routing_rpc(self, name):
        # the ladder: valhalla when present AND permitted; the haversine
        # floor otherwise, ALWAYS labelled. Note what is deliberately absent:
        # flipr trouble here never 503s -- a routing answer flows whatever
        # the flag plane is doing, it just flows from a lower rung. The
        # expensive rung needs permission; the floor is arithmetic.
        try:
            json_format, _, _ = _rpc_modules()
            from kingfisher.routing.v1 import routing_pb2
            import routing
        except ImportError as e:
            return self._send(501, "application/json", json.dumps(
                {"error": f"rpc surface needs protobuf: {e}"}).encode())
        import flipr_client
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"

        def valhalla_permitted():
            if not routing.valhalla_url():
                return False
            try:
                return (flipr_flags().check("network.enabled")
                        and flipr_flags().check("routing.enabled")
                        and flipr_flags().check("routing.valhalla"))
            except (flipr_client.FliprDown, flipr_client.FlagMissing) as e:
                # no flag plane, no permission for the expensive rung --
                # fail toward the floor, not toward spending. LOGGED because
                # the e2e caught exactly this state and the logs could not
                # explain it: a rebuild wiped flipr's store, the flags were
                # gone, and every route silently floored with flags "on".
                log("warn", "valhalla_not_permitted", err=str(e)[:200])
                return False

        pt = lambda p: {"lat": p.lat, "lng": p.lng}
        if name == "GetCapability":
            resp = routing_pb2.GetCapabilityResponse()
            cap = resp.capability
            cap.modes.extend([routing_pb2.MODE_AUTO, routing_pb2.MODE_BICYCLE,
                              routing_pb2.MODE_PEDESTRIAN])
            cap.tile_vintage = os.environ.get("KINGFISHER_TILE_VINTAGE", "")
        elif name in ("Route", "Estimate"):
            req_cls = getattr(routing_pb2, name + "Request")
            req = json_format.Parse(raw, req_cls())
            mode_name = routing_pb2.Mode.Name(req.mode)
            resp = getattr(routing_pb2, name + "Response")()
            if name == "Route" and valhalla_permitted():
                try:
                    secs, meters, _shape = routing.valhalla_route(
                        mode_name, pt(req.origin), pt(req.destination))
                    resp.method = routing_pb2.METHOD_VALHALLA
                    resp.seconds, resp.meters = secs, meters
                    resp.tile_vintage = os.environ.get("KINGFISHER_TILE_VINTAGE", "")
                except Exception as e:
                    # the wire tells the CALLER (method=haversine); this line
                    # tells the OPERATOR why. Labelled degradation must be
                    # loud on both surfaces or the dashboard reads a healthy
                    # service quietly serving crow-flies guesses.
                    log("warn", "valhalla_step_down", rpc=name, err=str(e))
            if not resp.method:
                secs, meters = routing.floor_estimate(
                    mode_name, pt(req.origin), pt(req.destination))
                resp.method = routing_pb2.METHOD_HAVERSINE
                resp.seconds, resp.meters = secs, meters
        elif name == "Matrix":
            req = json_format.Parse(raw, routing_pb2.MatrixRequest())
            cells = len(req.sources) * len(req.targets)
            cap = int(os.environ.get("KINGFISHER_MATRIX_MAX", "2500"))
            if cells > cap:
                # the bulk shape is the expensive shape; the cap is not
                # advisory and applies to every caller including the bake
                return self._send(413, "application/json", json.dumps(
                    {"error": f"matrix of {cells} cells exceeds cap {cap}"}).encode())
            mode_name = routing_pb2.Mode.Name(req.mode)
            resp = routing_pb2.MatrixResponse()
            srcs = [pt(p) for p in req.sources]
            tgts = [pt(p) for p in req.targets]
            if valhalla_permitted():
                try:
                    resp.seconds.extend(routing.valhalla_matrix(mode_name, srcs, tgts))
                    resp.method = routing_pb2.METHOD_VALHALLA
                    resp.tile_vintage = os.environ.get("KINGFISHER_TILE_VINTAGE", "")
                except Exception as e:
                    log("warn", "valhalla_step_down", rpc=name, err=str(e))
            if not resp.method:
                resp.seconds.extend(routing.matrix_floor(mode_name, srcs, tgts))
                resp.method = routing_pb2.METHOD_HAVERSINE
        else:
            return self._send(404, "application/json",
                              json.dumps({"error": f"no rpc {name}"}).encode())
        return self._send(200, "application/json",
                          json_format.MessageToJson(resp).encode())

    @staticmethod
    def _copy(src, dst):
        while True:
            buf = src.read(64 * 1024)
            if not buf:
                break
            dst.write(buf)

    def copyfile(self, src, dst):
        # SimpleHTTPRequestHandler serves the static root through here, and it
        # is the only place those responses' byte counts are visible.
        n = 0
        try:
            while True:
                buf = src.read(64 * 1024)
                if not buf:
                    break
                dst.write(buf)
                n += len(buf)
        except BrokenPipeError:
            pass
        self._sent = getattr(self, "_sent", 0) + n

    def log_message(self, fmt, *args):
        # WHO ASKED. When kingfisher was OOM-killed on 2026-09-04 nothing said
        # which caller had asked for what: the doors were not logged at all,
        # and the lines that existed carried no peer. Every door request now
        # logs its peer, its user agent, its query and its status, so the next
        # kill names its caller. http.server calls this with (fmt, requestline,
        # status, size); status is args[1].
        path = self.path.split("?")[0]
        door = path in ("/cells", "/shelf", "/directory")
        if door or self.path.endswith(".html") or self._mounted(self.path)[0] is not None:
            fields = {"method": self.command, "path": path[:200],
                      "peer": self.client_address[0],
                      "ua": (self.headers.get("User-Agent") or "")[:80]}
            if door:
                fields["query"] = self.path.partition("?")[2][:200]
                fields["status"] = args[1] if len(args) > 1 else None
            log("info", "request", **fields)


# _shelf_mod imports shelf on first use; /metrics reads two gauges from it and
# the module is otherwise imported inside the doors that need it.
def _shelf_mod():
    import shelf
    return shelf


class Server(socketserver.ThreadingTCPServer):
    # the page fetches several chunks at once; single-threaded serialises them.
    allow_reuse_address = True
    daemon_threads = True
    # THE LISTEN BACKLOG. socketserver's default is 5. A browser map pane
    # opens six connections per host, so more than about six arriving
    # together had the excess dropped or reset; Linux retries a dropped SYN
    # after 1s then 3s, so the consumer saw tiles one to four seconds late or
    # missing, while /health said 200 throughout. Live counters inside the
    # serving pod after two days: ListenOverflows=102, ListenDrops=102
    # (issue 70). 128 is the conventional value and far above any pane.
    request_queue_size = 128

    # A CLIENT HANGING UP IS NOT AN ERROR. socketserver's default handle_error
    # prints a full traceback to stderr for any exception in a handler
    # thread, and under keep-alive the ordinary case -- a socket closed while
    # a reader waits on it -- raises exactly there. A bare traceback per
    # browser tab closed is not a log; one structured line is, and anything
    # that is NOT a connection error still gets the traceback it deserves.
    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
            log("info", "client_hung_up", peer=client_address[0],
                err=type(exc).__name__)
            return
        super().handle_error(request, client_address)




# ------------------------------------------------------------ fence store
# kingfisher's first read FROM the surreal store: the geofences, served as
# GeoJSON at /geofences.geojson so a browser tool (geojson.io, QGIS,
# interface's editor) opens the store's truth without anyone running
# anything. In-cluster, surreal is reached through the HOST'S prod traefik
# (host.k3d.internal) with the Host header set by hand -- the pod cannot
# resolve *.localhost, but it can say the name out loud.

def _fence_rows():
    """The fences, from postgrest.

    WAS: signin to surreal for a token, then POST SurrealQL to /sql, both with
    a hand-set Host header -- prod's surreal was ClusterIP-only and the sole
    road in was prod traefik, which routes on Host. A pod cannot resolve
    *.localhost but it can say the name out loud.

    That is all gone. postgrest is an ordinary in-cluster service reached by
    name, so there is no token dance, no Host header, and no credentials in
    this process at all: postgrest logs into postgres as a near-powerless role
    and postgres's own GRANTs decide what a request may see.

    The shape of a row is unchanged -- api.geofence projects the same name,
    tags, rings and owner the SurrealQL SELECT returned -- so fences_geojson
    below needed no edit.
    """
    import net as _net
    base = os.environ.get("KINGFISHER_API_URL", "http://postgrest.kingfisher:3000")
    # a GET against postgrest, in-cluster: retried freely. An empty geofence
    # list and a postgrest that did not answer are different facts, and letting
    # this raise keeps them different -- see the fail-LOUD rule.
    return _net.get_json(base + "/geofence?select=id,name,tags,rings,owner",
                         headers={"Accept": "application/json"},
                         policy=_net.INTERACTIVE.replace(timeout_s=10))


def fences_geojson(tag=""):
    feats = []
    for g in _fence_rows():
        if tag and tag not in (g.get("tags") or []):
            continue
        rings = []
        for ring in g.get("rings", []):
            pts = [[p["lng"], p["lat"]] for p in ring.get("points", [])]
            if pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            rings.append(pts)
        feats.append({"type": "Feature",
                      "properties": {"id": str(g.get("id", "")), "name": g.get("name", ""),
                                     "owner": g.get("owner", ""), "tags": g.get("tags", [])},
                      "geometry": {"type": "Polygon", "coordinates": rings}})
    return {"type": "FeatureCollection", "features": feats}


# --------------------------------------------------------------- rpc surface
# THIS SECTION STAYS ABOVE main(). It was once appended after the __main__
# block: pytest IMPORTS this module so everything loaded and the suite was
# green, while the container RUNS it as a script, blocked in serve_forever()
# mid-file, and every definition below was unreachable -- NameError on the
# first RPC, in prod shape only. The regression test runs serve.py the way
# the container does.
# protojson over plain http, the same wire flipr speaks. Marshaling is the
# GENERATED types' job -- serve.py never hand-builds a response dict for an
# RPC, so the wire cannot drift from the contract. The imports are lazy and
# the gen/ dir rides sys.path beside serve.py: tile serving keeps working on
# a box with no protobuf; the RPC surface says loudly what it needs.

def flipr_flags():
    # one client per process, built on first use. The namespace version is
    # the PROTO API VERSION: v1, moving only when
    # the contract moves -- NEVER the commit hash: a per-commit namespace
    # would reset operator-set values at every deploy.
    global _FLIPR
    if _FLIPR is None:
        import flipr_client
        _FLIPR = flipr_client.FliprClient(
            os.environ.get("KINGFISHER_FLIPR_URL", "http://flipr.test:9800"),
            "kingfisher", "v1")
    return _FLIPR


_FLIPR = None


def _rpc_modules():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "gen"))
    from google.protobuf import json_format
    from kingfisher.discovery.v1 import discovery_pb2
    import providers
    return json_format, discovery_pb2, providers


def _record_msg(pb2, json_format, d):
    from kingfisher.map.v1 import map_pb2
    m = pb2.DatasetRecord()
    m.id = d["id"]; m.source_id = d["source_id"]; m.title = d["title"]
    m.kind = map_pb2.LayerKind.Value(d.get("kind", "LAYER_KIND_UNSPECIFIED"))
    m.bytes_estimate = int(d.get("bytes_estimate", 0))
    m.state = pb2.FetchState.Value(d.get("state", "FETCH_STATE_UNSPECIFIED"))
    m.vintage_id = d.get("vintage_id", "")
    m.download_url = d.get("download_url", "")
    # the throbber's numerator and its failure line. Both default to the zero
    # value, which reads correctly: nothing downloaded, nothing went wrong.
    m.bytes_downloaded = int(d.get("bytes_downloaded", 0))
    m.last_error = d.get("last_error", "")
    m.tombstone_reason = d.get("tombstone_reason", "")
    m.tombstoned_at = d.get("tombstoned_at", "")
    return m



def _start_heartbeat():
    # the lease: hall-monitor judges our silence, so the beat starts with
    # the server and dies with it. Reads INFLIGHT under its own lock via
    # the closure; heartbeat failures are counted, never fatal.
    import heartbeat

    def inflight():
        with STATS_LOCK:
            return INFLIGHT["n"]
    heartbeat.start(STARTED, inflight)
    # declare our flags (routing.*) at startup, in the background so the
    # first request never waits on the flag plane
    import flags_decl
    threading.Thread(target=flags_decl.publish, args=(flipr_flags(),), daemon=True).start()


def main():
    global ROOT, DODO_URL
    ap = argparse.ArgumentParser(description="kingfisher -- serve H3 map data over HTTP")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("KINGFISHER_PORT", 15021)))
    ap.add_argument("--bind", default=os.environ.get("KINGFISHER_BIND", "127.0.0.1"))
    ap.add_argument("--root", default=os.environ.get("KINGFISHER_ROOT", os.getcwd()),
                    help="directory served at /")
    ap.add_argument("--mount", action="append", default=[], metavar="PREFIX=DIR",
                    help="data root under a url prefix; repeatable")
    ap.add_argument("--dodo-url", default=DODO_URL,
                    help="where the back link points")
    args = ap.parse_args()

    DODO_URL = args.dodo_url
    ROOT = os.path.realpath(os.path.expanduser(args.root))
    # KINGFISHER_MOUNTS lets a container declare its volumes without a bespoke
    # command line: "/econ/=/data/econ,/warn/=/data/warn"
    spec = list(args.mount)
    env_mounts = os.environ.get("KINGFISHER_MOUNTS", "").strip()
    if env_mounts:
        spec += [m for m in env_mounts.split(",") if m.strip()]
    for m in spec:
        if "=" not in m:
            sys.exit(f"--mount wants PREFIX=DIR, got {m!r}")
        prefix, d = m.split("=", 1)
        root = os.path.realpath(os.path.expanduser(d))
        prefix = prefix if prefix.endswith("/") else prefix + "/"
        if not os.path.isdir(root):
            print(f"mount      {prefix:9} {root}  MISSING -- requests will 404")
            continue
        MOUNTS[prefix] = root

    for e in inventory():
        n = len(e["series"] or [])
        print(f"mount      {e['prefix']:9} {e['root']}"
              + (f"  ({n} series, {e['chunks']} chunks)" if n else "")
              + f"  {e['files']:,} files, {human(e['bytes'])}")
    if not MOUNTS:
        print("mount      none declared")

    print(f"root       {ROOT}")
    # the librarian's memory, read back before the door opens. Without this the
    # index is whatever this process has been told since it started, which is
    # how 64 sources and zero datasets survived five restarts unnoticed.
    import providers as _providers
    n_idx = _providers.load_index()
    print(f"index      {n_idx} dataset(s) from {_providers.index_path()}")
    print(f"listening  http://{args.bind}:{args.port}/")
    _start_heartbeat()
    with Server((args.bind, args.port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
