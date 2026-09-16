# kingfisher -- operation

For whoever is looking at this at 03:20. What runs, where its data is, how to
tell whether it is well, and what to do when it is not. `README.md` says what
kingfisher is; DESIGN.md says why it is shaped this way; this says how to hold
it. Nothing here is an emergency, because nothing is an emergency.

## What runs

Five deployments in the target's namespace, one image, one commit.

| deployment | port | hostname | what it does |
|---|---|---|---|
| `kingfisher` | 15021 | `kingfisher.test` | serves the shelf and the RPC surface; fetches on request |
| `kingfisher-ingestd` | 15022 | `ingestd.test` | turns spooled downloads into datasets on the shelf |
| `kingfisher-openskyd` | 15023 | `openskyd.test` | polls live aircraft positions |
| `kingfisher-weatherd` | 15024 | `weatherd.test` | polls current conditions |
| `kingfisher-gibsd` | 15025 | `gibsd.test` | polls GOES-West cloud imagery |

They are addressed by name. `127.0.0.1:15021` is a laptop checkout, not the
service.

All five run `kingfisher:dev`, built by `bin/build.sh` from one commit. Every
one reports that commit on `/health` as `commit`, and every response from
`kingfisher` carries `Server: kingfisher/<sha>`. If those disagree across the
five, a roll is half done; if `commit` reads `unknown`, the image was built by
hand with a bare `docker build` and cannot be named from git.

## Is it well

```
kingfisher status        one line per workload: pods, ready, commit, health
kingfisher health        the raw /health of all five
```

`bin/install.sh` links `bin/kingfisher` into the operator's init directory
and their `bin`, so those verbs work from any shell whether or not an agent is
running. They need `kubectl` with the target's context and stdlib Python;
nothing else.

`/health` on every daemon is liveness plus a body. It is 200 whenever the
process is up, so a probe on it never restarts a pod for being degraded. The
body carries `ok`, `status`, `commit`, and a `degraded` list of reasons. Three
reasons can appear, and they mean three different things:

- `flipr unreachable` -- the flag plane is down. Every daemon fails closed and
  fetching stops. Fix flipr, not kingfisher.
- A dataset that must never be fetched again gets a tombstone, not a deletion:
  `bin/kingfisher tombstone <id> --reason "..."` (ids on stdin with `-`; every
  `.meta.json` in a directory with `--from-dead spool/dead`, which also rewrites
  the meta's state in place). Fetch then refuses it, naming the reason; `ls`
  shows `FETCH_STATE_TOMBSTONED`; `--revive` returns it to `INDEXED`. The 119
  CA_ALAMEDACO_2006 tiles carry one from 2026-09-05 (no CRS in the LAZ).
- `flag not declared in flipr: ...` -- flipr is up and the `kingfisher@v1`
  namespace has no such flag. A wiped or replayed-empty store. Every
  kingfisher process republishes the namespace at startup, so a restart
  clears it; `bin/publish-flags.sh` republishes by hand without one. The
  reason clears on the next check.
- `N waiting, failing now, and nothing ingested for Ns` -- ingestd only. Work
  is queued, it is erroring right now, and nothing has succeeded for five
  minutes. This is the loop that ran for 54 hours in September reporting ok.
  Read the log for the failure, then `spool/dead` for what was refused.

A declared `false` flag is not degradation. The daemon pauses, counts
`paused_flag`, and stays `ok`. An operator turned it off.

`kingfisher` also answers `/ready`, which is 503 when a configured mount is
empty -- the hostPath resolved to nothing inside the node and the pod would
serve 404 forever. As of this writing the manifests still probe `/health` for
readiness, so that pod stays in rotation. Moving the readinessProbe to
`/ready` is a decision that has not been taken; do not assume it has.

## Where the data is

The node mounts the data roots from the host. Everything kingfisher touches is
under the target's data root:

| host path | in the pod | who writes it |
|---|---|---|
| `${SHELF_ROOT}/<dataset>` | `/data/<dataset>` | staged datasets, read-only here |
| `${SHELF_ROOT}/viewer` | `/srv/viewer` | the map page's build, served at `/` |
| `${SHELF_ROOT}/ingested` | `/data/ingested` (kingfisher), `/maps/ingested` (ingestd) | ingestd, one dataset directory per item |
| `${SHELF_ROOT}/flights` `weather` `clouds` | `/data/<name>` | openskyd, weatherd, gibsd |
| `${DATA_ROOT}/spool` | `/spool` | kingfisher writes downloads; ingestd consumes them |
| `${DATA_ROOT}/spool/dead` | `/spool/dead` | ingestd, for items that can never succeed |
| `${UI_ROOT}` | `/var/mesh-ui` | the viewer's static files, placed by `install.sh` |

A dataset on the shelf is a directory holding `manifest.json` and its payload
(`cells.json` for hexed data, `content.jpg` for imagery). The directory
existing is the idempotency key: ingestd will not redo it, and deleting it is
how you ask for a re-ingest.

The spool is runtime, not source. Every payload in it is a re-downloadable
file. Until 2026-09-04 a successful ingest left its payload behind, so the
spool holds about 8 GB of inputs beside about 200 MB of product. Deleting
those is a deliberate step that has not been taken; the
code now removes payloads on success so it does not grow further.

One dataset on the shelf, `gibs-VIIRS_Black_Marble-r4`, was made by hand with
`bin/hexify-asset` and no pipeline rebuilds it. A clean machine will not have
it until that changes.

## Flags

Namespace `kingfisher@v1`. Everything that reaches somebody else's servers
checks `fetch.enabled` and `fetch.<source>`; the daemons check
`network.enabled`; ingestd checks `ingest.enabled`. All default off. Flipr down
means the expensive surface answers 503 naming flipr; that is by design.

`kingfisher flags` is not a verb here yet; use flipr's own tooling.

## Deploying

The target is a file. `bin/roll.sh` reads `kube/environments/$TARGET.env`
(no default), refuses if `kube/$TARGET/` is not the render of
`kube/template/`, and names the k3d context on every `kubectl`. To roll a
second cluster: render its overlay once, then `bin/roll.sh <name>`. There is
no default: `bin/roll.sh <target>` names the cluster it rolls.

```
bin/build.sh      build kingfisher:dev from HEAD, stamped with the short sha, and import it
bin/roll.sh <env> build, apply kube/<env>, roll all five deployments, verify the commit they report
```

`bin/build.sh` refuses a dirty tree: an image labelled with a commit has to
contain that commit. `bin/roll.sh` applies the overlay before it restarts
anything, so a manifest change and an image change deploy the same way.

`bin/roll.sh` refuses to roll while a dataset is `QUEUED` or `FETCHING`. The
fetcher writes a payload before its meta, so an ingestd restarted mid
download would read a meta with no payload and dead-letter a transfer in
flight. Wait for the downloads or cancel them; the script names the datasets
it is waiting on.

The middle step of a build is `k3d image import`, and it is not optional. The
node runs its own containerd and cannot see the host daemon's images; skip it
and the roll silently restarts the old image. `bin/build.sh` does it for you.

`kubectl kustomize kube/<target>` renders the manifests with no cluster. Do not
run `kustomize edit fix` when it suggests it; see the comment at the top of
`kube/base/kustomization.yaml` for why the migration it proposes is an outage.

## Starting and stopping

```
kingfisher stop          scale all five to zero
kingfisher start         scale them back to one
kingfisher restart       rollout restart, same image
kingfisher logs [name]   follow one deployment's log (default kingfisher)
```

Stopping loses nothing: fetch tickets are process-local and forgotten, but the
work behind them is in the spool and the shelf, and a re-issued fetch resumes
from what is held. Restart is not deploy. New code is `bin/roll.sh`.

## Logs and metrics

Logs are structured JSON, one event per line, collected by vector. The events
worth grepping for: `ingested`, `ingest_dead_lettered`, `ingest_failed`,
`fetch_held`, `fetch_failed`, `heartbeat_dropped`, `client_hung_up`,
`backfill_pass_failed`.

`/metrics` on every daemon is Prometheus text. `kingfisher` publishes 28
families, all prefixed `kingfisher_`; `tests/test_endpoints.py` pins the set,
and the dashboard that binds nine of them is in this repo at
the target's `dashboard-kingfisher.json`, applied by the overlay. Renaming a
metric fails the test and breaks that dashboard; nothing outside this repo
consumes these names any more.

## Things that will bite

- A hostPath that resolves to nothing inside the node produces a pod that is
  healthy and serves 404 forever. `/ready` says so; the probes do not yet ask.
- The dead-letter directory moves bytes, it does not free them. Counters going
  up in `dead_lettered_total` mean nothing about disk.
- `kingfisher:dev` is one tag for whatever was built last. Until deployd tags
  by sha, the commit on `/health` is the only thing that names
  what is running. Trust it over the tag.
- The viewer's name and kingfisher's name both land on this pod. The viewer
  is a page served from the viewer mount, not a daemon.
- A `/cells` answer that comes back coarser than asked, with `clipped_by:
  "memory"`, is the ceiling working: the process did not have the room for
  what was asked and said so instead of dying. A 503 with `heap_bytes` and
  `memory_limit_bytes` means the heap has plateaued too close to the limit
  for any fold; the fix is a restart (`kingfisher restart kingfisher`) or a
  larger limit in `kube/template/deployment-patch.yaml`, not a smaller window.
- `kingfisher_heap_bytes` climbing toward `kingfisher_memory_limit_bytes` on
  the board is the warning the two OOM kills never gave. A fresh pod idles
  near 44Mi and plateaus around 140Mi after a day of requests; that is
  retention, measured as a plateau and not a leak (15 identical requests,
  -0.5Mi each), and it is why the ceiling reads the live heap rather than a
  constant.
