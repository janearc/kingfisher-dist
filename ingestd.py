#!/usr/bin/env python3
# ingestd -- kingfisher's stomach. Fetches land in the spool; this daemon
# turns them into served datasets. A separate process because smoothing a
# point cloud inside the serving pod's 256Mi limit would starve the maps to
# feed the math -- see DESIGN.md, which carries the whole argument.
#
# Citizenship: /health reports ACTUAL state (spool readable, flag plane
# reachable, last error), /metrics counts the work, logs are structured,
# the heartbeat makes hm list this daemon as its own row, and
# ingest.enabled is the runtime kill -- checked between items, so a
# runaway ingest can be stopped mid-queue.

import http.server
import json
import os
import random
import shutil
import sys
import threading
import time
import flipr_client
import version

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log import log  # noqa: E402
from api import descriptor_bytes  # noqa: E402
from errors import Unprocessable  # noqa: E402

SPOOL = os.environ.get("KINGFISHER_SPOOL", "spool")

# Where items that can NEVER succeed go. A subdirectory of the spool on purpose:
# the scan filters on names ending .meta.json, so a directory is invisible to it,
# and the files stay on the same volume so the move is an atomic rename rather
# than a copy that can half-fail.
#
# MOVED, NEVER DELETED. kingfisher fetches these, so deleting one is a
# re-download waiting to happen, and the loop would resume. Moving breaks the
# loop AND keeps the evidence: if a dead-lettered id reappears in the spool that
# is a new arrival worth looking at, not an invisible retry.
DEAD = os.path.join(SPOOL, "dead")

# How long a TRANSIENT failure may keep failing before it is set aside anyway.
#
# "the government's websites are pretty flaky and a retry is important,
# but.. there's a limit." Both halves matter. USGS being down for an hour must
# not cost us the tile; a tile that has been failing for a day is not coming
# back on its own and is just the poison queue again in slower motion.
#
# AGE, NOT ATTEMPT COUNT, deliberately. Attempts burn at whatever rate the pass
# happens to run -- measured here, 4,463 per item per day -- so "10 attempts" is
# a hundred seconds and "1000 attempts" is four hours, and neither number means
# anything to the person reading it. A day of failing means the same thing
# whatever the pass interval, and survives someone changing that interval later.
GIVE_UP_AFTER_S = float(os.environ.get("INGESTD_GIVE_UP_AFTER_S", 24 * 3600))

# Per-item backoff, because a ceiling alone still lets a failing item be retried
# every single pass until it hits that ceiling.
#
# BACKOFF AND GIVE-UP FIX DIFFERENT HALVES. Backoff fixes the COST: 4,463
# attempts a day becomes roughly fifteen. Give-up fixes TERMINATION: without it
# the item is still here next year, quietly. Neither substitutes for the other.
#
# FULL JITTER -- sleep = random(0, min(cap, base * 2^attempts)) -- for the same
# reason the estate requires it everywhere: fixed backoff re-synchronises every
# failing item onto the same schedule and rebuilds the herd one beat later. With
# 119 items failing together against one flaky government endpoint, that matters
# here rather than theoretically.
BACKOFF_BASE_S = float(os.environ.get("INGESTD_BACKOFF_BASE_S", 30))
BACKOFF_MAX_S = float(os.environ.get("INGESTD_BACKOFF_MAX_S", 3600))
OUT = os.environ.get("INGESTD_OUT", "ingested")
PORT = int(os.environ.get("INGESTD_PORT", "15022"))
INTERVAL = float(os.environ.get("INGESTD_INTERVAL_S", "10"))

STATE_LOCK = threading.Lock()
STARTED = time.time()
# the stuck window: an error inside the first, no success inside the second
STUCK_ERROR_RECENT_S = float(os.environ.get("INGESTD_STUCK_ERROR_RECENT_S", "60"))
STUCK_AFTER_S = float(os.environ.get("INGESTD_STUCK_AFTER_S", "300"))
STATE = {"ingested": 0, "skipped_no_pipeline": 0, "errors": 0,
         "dead_lettered": 0, "paused_flag": 0, "last_error": "", "flipr_ok": True, "flag_missing": "",
         # DEPTHS, not counts. These go down as well as up, which is exactly why
         # they are the signal a counter cannot give: "119 failures" is
         # indistinguishable from a busy queue draining well, but "119 waiting
         # and nothing ingested" is stagnancy and can only be seen as a level.
         "spool_depth": 0, "dead_depth": 0, "backing_off": 0,
         # bounds recorded onto manifests that predate them; a one-time debt
         # paid down a few per pass behind ingestion
         "bounds_backfilled": 0,
         # WHEN, not how many. Issue 69: 2.1M failures and zero ingests looked
         # exactly like a busy queue from the counters, because a counter
         # cannot say "and nothing has succeeded for two days". These can.
         "last_ingested_at": 0.0, "last_error_at": 0.0}


def _flags():
    import flipr_client
    return flipr_client.FliprClient(
        os.environ.get("KINGFISHER_FLIPR_URL", "http://flipr.test:9800"),
        "kingfisher", "v1")


def ingest_asset(payload, meta, dest_dir):
    # imagery and model packs: the payload IS the product. Place it, write
    # the manifest, done -- atomically via tmp+rename so a crash mid-place
    # can never leave a half-dataset that looks whole.
    tmp = dest_dir + ".tmp"
    os.makedirs(tmp, exist_ok=True)
    dl = meta.get("download_url", "")
    ext = ".jpg" if "jpeg" in dl else (".png" if ".png" in dl.lower() else "")
    shutil.move(payload, os.path.join(tmp, "content" + ext))
    manifest = {
        "id": meta["id"], "title": meta.get("title", meta["id"]),
        # the layer's face: what a viewer SHOWS. title says what it is;
        # description says why anyone cares; content/content_type say what
        # to do with the bytes. Additive over the original shape.
        "description": meta.get("description", ""),
        "content": "content" + (".jpg" if meta.get("download_url", "").find("jpeg") >= 0
                                else (".png" if meta.get("download_url", "").lower().find("png") >= 0 else "")),
        "content_type": ("image/jpeg" if meta.get("download_url", "").find("jpeg") >= 0
                         else ("image/png" if meta.get("download_url", "").lower().find("png") >= 0 else "application/octet-stream")),
        "kind": meta.get("kind", "LAYER_KIND_ASSET"),
        "vintage_id": meta.get("vintage_id", ""),
        "source_id": meta.get("source_id", ""),
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provenance": meta.get("download_url", ""),
    }
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    os.rename(tmp, dest_dir)


def ingest_intensive(payload, meta, dest_dir):
    # lidar returns -> H3 cells. Imported lazily: pointcloud pulls laspy,
    # pyproj, numpy and h3, and a pod that only ever shelves imagery should
    # not pay their import cost (or fail to start if one is missing).
    from pointcloud import ingest_pointcloud
    stats = ingest_pointcloud(payload, meta, dest_dir)
    log("info", "hexified", dataset=meta["id"], cells=stats["cells"],
        points=stats["points_used"], res=stats["res"],
        epsg=stats["source_epsg"], sampled=stats["sampled"])


def backfill_bounds(limit=20, out=None):
    """Record where already-shelved datasets are, for manifests written before
    bounds existed.

    ONE DATASET AT A TIME, AND CAPPED PER PASS. The reason bounds exist at all
    is a consumer that was OOM-killed holding every cell on the shelf; a
    backfill that read the shelf into memory to fix that would be an unusually
    direct way to repeat it. So this opens one cells.json, takes four numbers,
    closes it, and does at most `limit` of them before yielding the loop back
    to ingestion -- which is the job that must not be starved.

    Idempotent by the presence of the key, so it costs one manifest read per
    dataset once the shelf is done and never runs again.
    """
    import h3

    from hexify import bounds_of

    root = out or OUT
    done = 0
    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        log("warn", "backfill_unreadable", err=str(e)[:120])
        return 0
    for name in names:
        if done >= limit:
            break
        d = os.path.join(root, name)
        mpath = os.path.join(d, "manifest.json")
        cpath = os.path.join(d, "cells.json")
        if not os.path.isfile(mpath) or not os.path.isfile(cpath):
            continue
        try:
            m = json.load(open(mpath))
        except Exception:
            continue
        pipe = m.get("pipeline")
        # only cell datasets have bounds, and only ones that lack them
        if not isinstance(pipe, dict) or pipe.get("bbox") is not None:
            continue
        try:
            cells = json.load(open(cpath)).get("cells") or {}
            bbox = bounds_of(cells, h3)
            del cells
        except Exception as e:
            log("warn", "backfill_failed", dataset=name, err=str(e)[:160])
            continue
        pipe["bbox"] = bbox
        # ATOMIC: tmp then replace, inside the dataset dir. A half-written
        # manifest is a dataset that reads as broken forever, and this touches
        # every dataset on the shelf.
        tmp = mpath + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(m, f, indent=1)
            os.replace(tmp, mpath)
        except OSError as e:
            log("warn", "backfill_unwritable", dataset=name, err=str(e)[:160])
            continue
        done += 1
        with STATE_LOCK:
            STATE["bounds_backfilled"] += 1
        log("info", "bounds_recorded", dataset=name, bbox=bbox)
    return done


# The kind is the contract's, and the dispatch is one dict on purpose: a new
# pipeline is a new entry, never a branch inside an existing one.
PIPELINES = {
    "LAYER_KIND_ASSET": ingest_asset,
    "LAYER_KIND_INTENSIVE": ingest_intensive,
}


def _backing_off(meta_path):
    # True if this item has a scheduled next attempt still in the future.
    # Unfailed items have no schedule and are always eligible, so the happy path
    # costs one absent dict key.
    try:
        meta = json.load(open(meta_path))
    except (OSError, ValueError):
        return False        # unreadable: let the normal path deal with it
    nxt = meta.get("_next_attempt_at")
    return nxt is not None and time.time() < float(nxt)


def _note_failure(meta_path):
    # Stamp the FIRST failure into the meta and return how long this item has
    # been failing. Persisted rather than held in memory on purpose: ingestd
    # restarts, and an in-memory counter would reset every time, which is
    # indistinguishable from no ceiling at all.
    now = time.time()
    try:
        meta = json.load(open(meta_path))
    except (OSError, ValueError):
        return 0.0          # unreadable meta is handled by the caller
    first = meta.get("_first_failed_at")
    if first is None:
        meta["_first_failed_at"] = first = now
        meta["_attempts"] = 0
    meta["_attempts"] = attempts = int(meta.get("_attempts", 0)) + 1
    ceiling = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (attempts - 1)))
    meta["_next_attempt_at"] = now + random.uniform(0, ceiling)
    tmp = meta_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, meta_path)       # atomic: never a half-written meta
    return max(0.0, now - float(first))


def _dead_letter(name, meta_path):
    # Move an item and its payload out of the spool. The meta moves LAST: while
    # it is still there the item is merely a failure to be retried, and only when
    # it is gone does the pass stop seeing it. Interrupted halfway, the worst case
    # is a payload in dead/ and a meta still in the spool -- which retries, fails
    # on the missing payload, and dead-letters again. Idempotent by construction.
    os.makedirs(DEAD, exist_ok=True)
    try:
        meta = json.load(open(meta_path))
        payload = os.path.join(SPOOL, meta["id"])
        if os.path.exists(payload):
            os.replace(payload, os.path.join(DEAD, meta["id"]))
    except (OSError, ValueError, KeyError):
        # An unreadable or malformed meta is itself unprocessable; move it
        # anyway rather than leaving it to be re-read forever.
        pass
    os.replace(meta_path, os.path.join(DEAD, name))


def scan_once(flags=None):
    # one pass over the spool. Flag checked ONCE PER PASS (not per item --
    # the flag plane is not a hot loop's dependency); FliprDown pauses the
    # work and says so on /health rather than guessing.
    try:
        names = [n for n in os.listdir(SPOOL) if n.endswith(".meta.json")]
        with STATE_LOCK:
            STATE["spool_depth"] = len(names)
            try:
                STATE["dead_depth"] = len([n for n in os.listdir(DEAD)
                                           if n.endswith(".meta.json")])
            except OSError:
                STATE["dead_depth"] = 0   # no dead/ yet is a depth of zero
    except OSError as e:
        with STATE_LOCK:
            STATE["last_error"] = f"spool unreadable: {e}"
        return
    if not names:
        return
    try:
        f = flags or _flags()
        allowed = f.check("ingest.enabled")
        with STATE_LOCK:
            STATE["flipr_ok"] = True
            STATE["flag_missing"] = ""
    except flipr_client.FlagMissing as e:
        # flipr is THERE and has no ingest.enabled: an unpublished namespace,
        # not an outage. Saying "flipr unreachable" here sent someone to look
        # at a flipr that was fine.
        with STATE_LOCK:
            STATE["flag_missing"] = str(e)[:200]
            STATE["paused_flag"] += 1
        log("warn", "ingest_paused", reason="flag not declared in flipr", err=str(e)[:120])
        return
    except Exception as e:
        with STATE_LOCK:
            STATE["flipr_ok"] = False
            STATE["paused_flag"] += 1
        log("warn", "ingest_paused", reason="flag plane unreachable", err=str(e)[:120])
        return
    if not allowed:
        with STATE_LOCK:
            STATE["paused_flag"] += 1
        log("info", "ingest_paused", reason="ingest.enabled is off")
        return
    for name in names:
        meta_path = os.path.join(SPOOL, name)
        if _backing_off(meta_path):
            with STATE_LOCK:
                STATE["backing_off"] += 1
            continue
        try:
            meta = json.load(open(meta_path))
            payload = os.path.join(SPOOL, meta["id"])
            dest = os.path.join(OUT, meta["id"])
            if os.path.exists(dest):
                # idempotency: dataset-dir-exists means done. Re-ingest is a
                # deliberate deletion first, never a surprise overwrite.
                os.remove(meta_path)
                continue
            pipe = PIPELINES.get(meta.get("kind", ""))
            if pipe is None:
                with STATE_LOCK:
                    STATE["skipped_no_pipeline"] += 1
                log("info", "no_pipeline_yet", dataset=meta["id"],
                    kind=meta.get("kind", "?"))
                continue  # the file WAITS for its pipeline; never consumed blind
            if not os.path.exists(payload):
                # PERMANENT, not transient. The fetcher writes the payload
                # before the meta (providers._run_fetch), so a meta with no
                # payload cannot be a download in progress -- it is bytes that
                # were removed or never landed, and no number of passes will
                # bring them back. This used to raise FileNotFoundError and
                # back off for up to a day; issue 79.
                raise Unprocessable(
                    f"payload missing for {meta['id']}: nothing at {payload}")
            pipe(payload, meta, dest)
            # THE DATASET DIR IS THE PRODUCT (DESIGN.md: dataset-dir-exists is
            # the idempotency key) and the spool is runtime, not source
            # (078a6f0). Nothing reads a payload after pipe() returns, and
            # every one is a re-downloadable file. Leaving them was issue 115:
            # 8.2G of inputs beside 204M of product. The asset pipeline MOVES
            # its payload, so this is conditional.
            if os.path.exists(payload):
                os.remove(payload)
            os.remove(meta_path)
            with STATE_LOCK:
                STATE["ingested"] += 1
                STATE["last_ingested_at"] = time.time()
            log("info", "ingested", dataset=meta["id"], dest=dest)
        except Unprocessable as e:
            # Permanent by the refusal's own declaration -- see errors.py. Retrying
            # cannot help, so the item leaves the spool instead of being re-read
            # every pass forever. Measured before this existed: 119 items, 4,463
            # attempts each per day, 531,097 failures, nothing ingested.
            try:
                _dead_letter(name, meta_path)
                with STATE_LOCK:
                    STATE["dead_lettered"] += 1
                log("warn", "ingest_dead_lettered", item=name, err=str(e)[:200],
                    dead=DEAD)
            except OSError as move_err:
                # If the move fails the item stays and will be retried, which is
                # the safe direction: a poison queue is loud and recoverable.
                with STATE_LOCK:
                    STATE["errors"] += 1
                    STATE["last_error_at"] = time.time()
                    STATE["last_error"] = f"{name}: dead-letter failed: {move_err}"
                log("error", "dead_letter_failed", item=name, err=str(move_err)[:200])
        except Exception as e:
            # Transient by default -- retried, because provider outages are
            # ordinary and a tile should survive one. But not forever: an item
            # still failing after GIVE_UP_AFTER_S is not waiting on a blip.
            failing_for = _note_failure(meta_path)
            with STATE_LOCK:
                STATE["errors"] += 1
                STATE["last_error_at"] = time.time()
                STATE["last_error"] = f"{name}: {e}"
            if failing_for >= GIVE_UP_AFTER_S:
                try:
                    _dead_letter(name, meta_path)
                    with STATE_LOCK:
                        STATE["dead_lettered"] += 1
                    log("warn", "ingest_gave_up", item=name,
                        failing_for_s=round(failing_for), err=str(e)[:200], dead=DEAD)
                except OSError as move_err:
                    log("error", "dead_letter_failed", item=name,
                        err=str(move_err)[:200])
            else:
                log("warn", "ingest_failed", item=name,
                    failing_for_s=round(failing_for), err=str(e)[:200])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with STATE_LOCK:
            s = dict(STATE)
        if self.path == "/health":
            # ACTUAL health: degraded when the flag plane is gone or the
            # spool errored -- never an uncritical 200
            degraded = []
            if not s["flipr_ok"]:
                degraded.append("flipr unreachable")
            if s["flag_missing"]:
                degraded.append(f"flag not declared in flipr: {s['flag_missing']}")
            if s["last_error"].startswith("spool"):
                degraded.append(s["last_error"])
            # STUCK: work waiting, failing right now, and nothing has succeeded
            # for a long time. Issue 69 ran 54 hours at 11 failures a second
            # with ingested_total at zero and this endpoint said ok, because
            # the two things it checked were true. Each clause alone is
            # innocent -- an idle spool, a transient error, a slow download in
            # backoff -- and all three together is the loop.
            now = time.time()
            since_progress = now - max(s["last_ingested_at"], STARTED)
            if (s["spool_depth"] > 0 and (now - s["last_error_at"]) < STUCK_ERROR_RECENT_S
                    and since_progress > STUCK_AFTER_S):
                degraded.append(f"{s['spool_depth']} waiting, failing now, and nothing ingested "
                                f"for {int(since_progress)}s")
            ok = not degraded
            body = json.dumps({"ok": ok, "commit": version.commit(), "degraded": degraded,
                               **{k: v for k, v in s.items() if k != "flipr_ok"}}).encode()
            self._send(200 if ok else 503, "application/json", body)
        elif self.path == "/metrics":
            L = []
            for k in ("ingested", "skipped_no_pipeline", "errors", "dead_lettered", "backing_off", "paused_flag"):
                L.append(f"# TYPE ingestd_{k}_total counter")
                L.append(f"ingestd_{k}_total {s[k]}")
            # Gauges, deliberately not _total: a queue depth that only ever
            # rose would be a counter, and a counter cannot show stagnancy.
            # Stagnancy is depth ABOVE zero while ingested stays FLAT, which
            # needs a level and a rate read together.
            L.append("# HELP ingestd_spool_depth items waiting in the spool")
            L.append("# TYPE ingestd_spool_depth gauge")
            L.append(f"ingestd_spool_depth {s['spool_depth']}")
            L.append("# HELP ingestd_dead_depth items set aside as permanently unprocessable")
            L.append("# TYPE ingestd_dead_depth gauge")
            L.append(f"ingestd_dead_depth {s['dead_depth']}")
            L.append("# TYPE ingestd_flipr_reachable gauge")
            L.append(f"ingestd_flipr_reachable {1 if s['flipr_ok'] else 0}")
            self._send(200, "text/plain; version=0.0.4", "\n".join(L).encode() + b"\n")
        elif self.path == "/api":
            payload = descriptor_bytes()
            if payload is None:
                log("error", "descriptor-daemons.binpb not deployed beside the code; /api answering 503")
                self._send(503, "application/json", b'{"error": "descriptor-daemons.binpb not deployed beside the code"}')
                return
            self._send(200, "application/x-protobuf; messageType=google.protobuf.FileDescriptorSet", payload)
        else:
            self._send(404, "application/json", b'{"error": "health, metrics, or api"}')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # structured logging only; BaseHTTPRequestHandler prose is noise


def main():
    os.makedirs(OUT, exist_ok=True)
    import heartbeat

    def inflight():
        with STATE_LOCK:
            return 0
    heartbeat.start(time.time(), inflight)
    # declare our flags before the first check: the service is the source of
    # what it declares, and a fresh flipr has no kingfisher@v1 until we say so
    import flags_decl
    flags_decl.publish(_flags())
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("info", "ingestd_up", spool=SPOOL, out=OUT, port=PORT)
    # INGESTION FIRST, BACKFILL WITH WHAT IS LEFT. The backfill is a one-time
    # debt against a shelf written before bounds existed; new arrivals record
    # their own. Doing it after scan_once, capped per pass, means the debt is
    # paid down steadily without ever making a dataset wait behind it.
    backfill = int(os.environ.get("INGESTD_BACKFILL_PER_PASS", "20"))
    while True:
        scan_once()
        if backfill:
            # A MAINTENANCE PASS MUST NOT BE ABLE TO KILL THE DAEMON. This ran
            # bare on 2026-09-01, hexify.py was missing from the image, and the
            # ImportError came straight out of main() -- so a backfill that had
            # nothing to do with ingestion took ingestion down with it and the
            # pod sat in CrashLoopBackOff. Ingestion is the job; bounds are a
            # convenience. Log it and carry on.
            try:
                backfill_bounds(limit=backfill)
            except Exception as e:
                with STATE_LOCK:
                    STATE["last_error"] = f"backfill: {e}"
                log("warn", "backfill_pass_failed", err=str(e)[:200])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
