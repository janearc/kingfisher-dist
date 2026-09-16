# using kingfisher

what it is and how it is laid out is `DESIGN.md`; running it in a cluster
is `OPERATION.md`. this is the rest: the surface it serves, the contract,
the flags, the configuration, the bench, the limits, the tests, the
targets, and where the data has to live.

## What it serves

| Path | What it is |
|---|---|
| `/` | what this instance is serving, for a person |
| `/health` | liveness, for a probe. Process is up -- **not** "mounts are populated" |
| `/api` | the contract: a buf-built `FileDescriptorSet`, byte-identical to `descriptor.binpb` |
| `/discovery` | the librarian's blob: every source and every dataset in the index, held or not |
| `/stats` | counters as JSON |
| `/metrics` | prometheus text; every RPC labelled by method and outcome |
| `/reload` | the poke: bump the cache generation, re-read the inventory |
| `/<mount>/` | a JSON index of that mount |
| `/kingfisher.discovery.v1.DiscoveryService/*` | the RPC surface: protojson over POST, marshalled through generated types |

## The contract

`proto/` holds five packages -- `map`, `routing`, `compose`, `discovery`,
`geofence`, all `v1` -- and they are the source of truth. `bin/gen.sh`
regenerates `gen/` and `descriptor.binpb` from them; a diff after running it
is a gen-freshness violation, and the suite asserts that the bytes `/api`
serves name exactly the proto files the generated types came from. Nothing
in `gen/` is ever edited by hand. The enum zero values follow one rule
throughout: pick the value that is safe to be wrong about.

## Flipr

Every expensive act -- anything that reaches somebody else's servers --
checks `fetch.enabled` AND `fetch.<source>` inline, and both default off.
The namespace is `kingfisher@v1`: the proto API version, which moves only
when the contract moves, so operator-set values survive every deploy. flipr
unreachable means the expensive surface refuses with a 503 naming flipr --
fail-closed and loud, never a quiet fallback to a cached yes.

## Configuration

Every flag has an environment variable behind it. The Dockerfile sets
defaults for the container; the target's `deployment-patch.yaml` overrides
the ones the cluster needs. `OPERATION.md` is the 03:20 view of all of this.

| Variable | Flag | Default (bare process) | In the cluster |
|---|---|---|---|
| `KINGFISHER_PORT` | `--port` | `15021` | `15021` |
| `KINGFISHER_BIND` | `--bind` | `127.0.0.1` | `0.0.0.0` |
| `KINGFISHER_ROOT` | `--root` | `$PWD` | `/srv/viewer` |
| `KINGFISHER_MOUNTS` | `--mount` | none | see below |
| `KINGFISHER_DODO_URL` | -- | `http://localhost:15000/` | the traefik name |
| `KINGFISHER_FLIPR_URL` | -- | `http://flipr.test:9800` | `http://flipr.flipr` |
| `KINGFISHER_SPOOL` | -- | `spool` | `/spool` |
| `KINGFISHER_KAFKA_BOOTSTRAP` | -- | unset (heartbeat disabled) | `kafka.<namespace>:9092` |
| `KINGFISHER_SCHEMA_REGISTRY` | -- | unset | `http://schema-registry.<namespace>:8081` |
| `KINGFISHER_COMMIT` | -- | `unknown` | the short sha, from `bin/build.sh` |

Locally that is just:

```
./serve.py --root <ui dir> --mount /econ/=<shelf>/econ
```

## The bench

`render` draws one dataset and exits. `bench` is the test rig for the
renderer itself: it puts every dataset you name onto ONE canvas and gives
you a viewport to move over it.

```
kingfisher bench --match GOLDENGATE --res 10
kingfisher bench --match "GOLDENGATE|SANFRANCOAST" --width 100 --height 40
kingfisher bench usgs-64390f69d34ee8d4ade0b214 --layer point_density
```

```
hjkl  pan          HJKL  pan a page      +/-  zoom
[ ]   H3 resolution   < >  layer         f    fit     a  ascii     q  quit
```

The two arguments that matter are the two resolutions a map-to-text pipeline
actually has: the terminal's (`--width`, `--height`) and the map's
(`--res`). Hold one still and move the other -- that is what the bench is
for. `--res` clamps to the resolution the data was folded at, because
nothing can be drawn finer than it was measured; the default is three levels
coarser, which is a whole-shelf view.

Rolling cells up a level is where `LAYER_KIND_EXTENSIVE` stops being
pedantry. `point_density` is returns per cell and its parent must be the
SUM; elevation is a property of the place and its parent is the mean.
Averaging a count up a level quietly divides it by the number of children
and draws a perfectly plausible, wrong map. The manifest already says which
is which, so the rollup reads it.

With no terminal -- a pipe, a test -- `bench` draws one frame and exits, so
it composes like everything else here.

The renderer lives in `textmap.py`, not in `bin/kingfisher`: `CellSource`
(cells, re-aggregated to any resolution), `Viewport` (centre, span, and the
clipping that makes panning mean something), `TextMap` (points plus a
viewport in, strings out). None of it knows what lidar is. It knows H3 cells
and floats, which is why anything in the estate that can emit cells is
already a picture.

## The ceiling

`/cells` will not build more than this process can hold. The budget a caller
names is a ceiling on cells delivered; the memory in the room is a second
one, computed per request from the pod's cgroup limit, the process's live
resident set, the row count `/shelf` prices, and a measured 600 bytes per
row. A request that fits is served as asked. One that would not fit is
served coarser and says so: `budget_served` is what it got, `clipped_by:
"memory"` is why. One that cannot fit even at res 0 is refused with 503 and
the two numbers, because the honest fix is a restart or a larger limit.
Above a million cells a request is refused with 413 before anything is
priced.

`/directory` publishes the limits, including `rows_that_fit_now`, so a
consumer can size its ask instead of discovering the answer. `/metrics`
carries `kingfisher_heap_bytes` and `kingfisher_memory_limit_bytes`.

The numbers came from `bin/kingfisher-stress.sh` against
`bin/serve-local.sh`, a kingfisher over the real shelf on a loopback port:
it ramps the bench's real requests, samples the process, and refuses a step
that `/shelf` prices past the limit. Its first version stopped on heap after
a step and killed the pod on 2026-09-04; read its header before pointing it
at the cluster.

## Tests

```
uv run --group dev pytest
```

These are integration tests, not unit tests with the thing under test mocked
out. Each case starts the real `Server` on a real socket -- on a
kernel-assigned port, so a suite running beside a live kingfisher cannot
collide with it -- and talks to it over real HTTP against a temporary tree
shaped like the map data. What gets asserted is what a client would see.

462 cases. The gate is a 90% coverage floor on `serve.py`, set in
`pyproject.toml`; it sits at 96.6% today. Treat that as a tripwire rather
than a target. The reason to add a test is a behaviour worth pinning, and
the day the number becomes the goal it stops measuring anything.

Two branches are excluded, both of them handlers for a client that hangs up
mid-body. Reproducing a broken pipe deterministically across platforms costs
more in flake than it buys in confidence, and both do the same thing:
swallow it and carry on.

**One test is a contract.** `test_metrics_emits_the_whole_contract` pins the
exact set of metric family names kingfisher publishes. It used to say the
consumer was a dashboard in another repository; that repository was turned down
in 2026-09 and the dashboard that binds these names now lives here, at
`kube/template/dashboard-kingfisher.json`, rendered and applied by the overlay.
A second test asserts every name that dashboard binds is in the contract, so the
two cannot drift. Renaming a metric fails both; the dashboard is the thing you
go and fix.

The stdlib-only runtime was retired ("stdlib-only is not a thing"). The
substrate takes dependencies as it needs them: protobuf for
the RPC surface now, h3 for hexing next. What survives from the old
discipline is the posture: every dependency is deliberate, locked, and
carries its reason.

## Targets: the overlay is generated

`kube/<target>/` is not edited by hand. It is the render of `kube/template/`
for that target, and `bin/render-overlay.py <target> --check` asserts that
the overlay on disk is that render, byte for byte. `roll.sh`
runs the check before it applies anything, so what is applied is what is
generated. This is the same rule kingfisher holds for protobuf.

A target is a name (the k3d cluster and the namespace), the data root it
owns (spool, the daemons' output), the shelf root it reads, a UI root, and
the host port its edge listens on. The `.test` names never change between
targets: a second cluster answers `kingfisher.test` on a different port and
traefik matches the Host header whatever the port. The serving pod mounts
the shelf read-only in every target, and the daemons write only under the
target's own root, so a throwaway cluster reads another target's maps without
copying them and cannot write into them.

```
bin/render-overlay.py example --check
bin/render-overlay.py scratch --edge-port 9900 \
    --shelf-root <shelf> --ui-root <ui>
# writes kube/environments/scratch.env once; afterwards that file is the input
bin/roll.sh scratch
```

`kube/environments/<target>.env` is what the scripts read and the renderer
fills from: env, cluster, context, edge port, roots. `EDGE_PORT` is the host
port that reaches that cluster's edge (9800 by default); port 80 is the
host's front door and belongs to no environment. `roll.sh` takes the
environment as its first argument and has no default. It is the shape flipr
uses. The namespace is one per machine, `kingfisher` by default:
names never change between clusters, only paths and ports do. The commit is
never in a render: the overlay names `${COMMIT}` and `roll.sh` (or deployd)
fills it at apply with the sha it just built, so what runs is named by hash,
not by a tag. Every `kubectl` in `roll.sh` and `bin/kingfisher` names its
context explicitly, so neither can land on whatever cluster a shell was left
pointed at.

## Rebuilding into the cluster

Three commands, and **the middle one is not optional.** `docker build` puts
the image in the host daemon; the k3d node runs its own containerd and
cannot see it. Skip the import and the apply silently succeeds against the
*old* image, which is a genuinely miserable twenty minutes.

```
bin/build.sh                    # or bin/roll.sh, which does all of this and verifies
kubectl apply -k kube/<target>
kubectl -n kingfisher rollout status deploy/kingfisher
```

The overlay is `kube/<target>`, not `kube/`: `kube/` is the environment-neutral
base and applies to nothing on its own.

The daemons are separate deployments and each needs its own restart after an
import, which is easy to forget when only one of them changed:

```
kubectl -n kingfisher rollout restart deploy/kingfisher-ingestd
kubectl -n kingfisher rollout restart deploy/kingfisher-gibsd
kubectl -n kingfisher rollout restart deploy/kingfisher-weatherd
kubectl -n kingfisher rollout restart deploy/kingfisher-openskyd
```

### One warning before you apply

`kubectl kustomize` prints a deprecation notice suggesting you run
`kustomize edit fix` to replace `commonLabels`. **Do not.** For this
Kustomization the suggested migration drops a label out of the Deployment's
`spec.selector`, which Kubernetes will not allow you to change after
creation.

It fails safely -- the apply is refused and the pod keeps serving -- but it
fails *partially*, updating the Service before refusing the Deployment, and
the obvious way to force past `field is immutable` is to delete and recreate
the Deployment. That is an outage on both hostnames, taken to silence a
cosmetic warning. The full explanation, and the one migration form that is
actually safe, are in the comment block at the top of
`kube/base/kustomization.yaml`.

Validate the manifests with no cluster at all:

```
kubectl kustomize kube/<target>
```

Then check it:

```
curl -s -o /dev/null -w '%{http_code}\n' http://kingfisher.localhost:8800/health
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' http://<viewer host>:8800/econ/res8.json
curl -s http://kingfisher.localhost:8800/metrics | grep '^kingfisher_' | sed 's/[ {].*//' | sort -u | wc -l
```

Expect `200`; `200` with roughly 72 KB; and `28` distinct metric families.
(Grep the raw lines instead and you get 64, because several families carry
labels. Both numbers are fine. Only a *change* in them is interesting.)

## Where the data has to live

kingfisher serves what is on disk under the roots its target names, and
writes nothing outside them.

| root | holds |
|---|---|
| `${SHELF_ROOT}` | the shelf: one directory per ingested dataset, served |
| `${DATA_ROOT}/spool` | downloads waiting for ingestd, not served |
| `${DATA_ROOT}/kingfisher` | the index |
| `${UI_ROOT}` | the viewer's static files, if this target serves one |

Both roots are host paths in the target's env file, so a pod restart keeps
them. A cluster that mounts nothing of the host has nothing to serve: the
node has to carry those paths in, which is the cluster's business rather
than kingfisher's, and `kube/template/deployment-patch.yaml` is where a
target says which paths it mounts.

Data staged by hand goes under `${SHELF_ROOT}` as one directory per
dataset, with whatever `manifest.json` the viewer expects; anything
ingestd wrote is already in that shape.


## Couplings

**The viewer is a filesystem coupling.** Whatever builds the map page
publishes it into `${SHELF_ROOT}/viewer`, and kingfisher serves that at `/`.
No repository records the arrangement, so the directory is the contract:
deleting it while tidying takes the page down.

**Two hostnames, one Service.** The api name and the viewer's name both
land on this pod, because the viewer is a page rather than a daemon. The
second route is `kube/base/ingressroute-corvid.yaml`.

**Registered in dodo** at `~/etc/dodo/neighbors.d/kingfisher.json`, pointing
at `http://kingfisher.localhost:8800/`. Not affected by where this source
lives.

**The memory guard is what keeps the pod inside its limit.** It sits around
34 to 54 Mi against a 256 Mi limit. If it starts OOMKilling, do **not**
raise the limit -- find what is being parsed. `inventory()` used to
deserialize every `resN.json` at startup to read a list of key names, and
one layer's `res6.json` is 38 MB. That is fixed; the limit is the tripwire
that tells you it came back.

## Metrics

28 families, all prefixed `kingfisher_`, scraped via the pod annotations.

| Group | Families |
|---|---|
| traffic | `requests_total`, `responses_total`, `errors_total`, `bytes_total`, `inflight_requests` |
| latency | `request_duration_seconds_{bucket,count,sum}`, `kind_duration_seconds_{count,sum}` |
| tiles | `tiles_total`, `tile_bytes_total`, `tiles_by_kind_total`, `tiles_by_mount_total` |
| inventory | `mounts`, `mount_{files,bytes,chunks,series,tiles,tile_bytes,resolutions}` |
| process | `start_time_seconds`, `uptime_seconds`, `generation`, `heartbeat_total` |
| upstream | `provider_{requests,bytes}_total`, `outbound_{requests,retries}_total`, `outbound_duration_seconds`, `mount_readable` |
| rpc | `rpc_requests_total`, `rpc_duration_seconds_{count,sum}` |

**The dashboard is in this repo**, rendered into the target's overlay and
shipped to grafana by its configMapGenerator. It binds nine of these
families, and a test asserts each is in the contract, so renaming a metric
fails the suite rather than the dashboard.
