# kingfisher

map data as a service: the catalogue of what we have, the index of what is
out there, and the machinery that puts either one where a viewer can draw
it. the viewers render; kingfisher is what they render from.

## what it does

a read-only mount table, so a viewer's page fetches map bytes from its own
origin. a page on one port cannot fetch another port without CORS headers,
and nothing here sends them.

an index of public data: what exists, where, under what licence, and a
state per dataset. a fetch brings a copy down to the spool.

a shelf: every ingested dataset, served as directories a viewer can walk.

## how it is laid out

    serve.py            the mux, the mounts, the shelf
    api.py              the rpc surface, one route per proto method
    providers.py        the catalogues it indexes, by provider
    shelf.py            what is on disk and servable
    pointcloud.py       lidar, and what a surface is made of
    hexify.py           h3, and the cells a viewer asks for
    textmap.py          the console renderer
    routing.py          travel times, behind a flag, not running here
    ingestd.py          the second process: spool to shelf
    gibsd.py            satellite imagery, on an interval
    weatherd.py         weather, on an interval
    openskyd.py         aircraft, on an interval
    heartbeat.py        what it tells the bus
    flipr_client.py     the flag client
    flags_decl.py       the flags it declares at boot
    net.py, log.py, errors.py, version.py
    proto/kingfisher/   map, routing, compose, discovery, geofence, all v1
    gen/                generated from proto/, never edited
    descriptor.binpb    what /api serves, built by bin/gen.sh
    bin/                gen.sh, build.sh, e2e.sh, install.sh, kingfisher
    kube/base/          deployment, service, the two routes
    kube/environments/  one env file per environment
    tests/              50 files, run by game check
    vendor/proto/       third-party protos, copied at a version
    art/                the header game paints after a build

## how it exists on disk

    kingfisher fetches  ->  <maps>/spool/<dataset> + <dataset>.meta.json
    ingestd polls       ->  reads the meta, runs the pipeline for its kind
                        ->  <maps>/ingested/<dataset>/: payload and
                            manifest.json, written tmp and renamed
    kingfisher serves   ->  /ingested/<dataset>/

| path | holds | written by | served |
|---|---|---|---|
| `<maps>/spool/` | downloads waiting to be ingested | kingfisher | no |
| `<maps>/ingested/<dataset>/` | one directory per dataset | ingestd | yes |
| `<data>/kingfisher/` | the index | kingfisher | no |

both paths are host paths, so they survive a pod restart. the meta file
beside a download carries what the pipeline needs. the dataset directory
existing is how ingestd knows the work is done, so re-ingesting means
deleting that directory first.

one pipeline is live: an asset, meaning imagery or a model pack, is placed
and given a manifest. every other kind logs that it has no pipeline yet and
leaves the file in the spool.

## how it runs

one process answers rpcs and serves the shelf. a second, ingestd, does the
cpu-bound work, because ingestion in the same process starves the server.

long work inside kingfisher runs on a background thread, one job at a time,
and the caller gets a ticket to ask about.

a tombstone is a state in the index, with a reason and a date, that fetch
honours before it reads a flag and a re-index preserves. reviving a dataset
means flipping that state.

every expensive act is behind a flipr flag in the namespace kingfisher@v1,
and a flag that cannot be read fails closed. travel times and the routing
rung are switched off; nothing here runs a routing engine.

## the metrics it adds

    kingfisher_bake_queue_depth          gauge
    kingfisher_bake_cells_total{state}   counter, done and failed
    kingfisher_bake_duration_seconds     histogram, per completed job
