# kingfisher -- H3 map data over HTTP.
#
# NOTHING DATA-SHAPED GOES IN THIS IMAGE. The map data is large, slow to
# rebuild, and kingfisher's own charge (ruled 2026-08-28: maps are
# kingfisher's job). Baking a copy in would make the image stale at the next
# regeneration and would rebuild gigabytes to ship a code change.
#
# So the image is the server and nothing else, and every data root arrives as a
# read-only bind mount named in KINGFISHER_MOUNTS. That is the same mount table
# the laptop uses, which is the point -- one code path, two deployments.
#
# The stdlib-only image was retired 2026-08-28 with the runtime invariant
# ("stdlib-only is not a thing"): the RPC surface ships generated
# types and their protobuf runtime; hexing brings h3 next. Dependencies are
# deliberate and locked, and each COPY below says why it is here.

FROM python:3.12-slim

# a non-root user, because this process only ever reads
RUN useradd --create-home --uid 10001 kingfisher
USER kingfisher
WORKDIR /app

# the RPC surface's one dependency: generated types need their runtime.
# Pinned; pure-python wheels exist for arm64, so no cross-compile.
# brotli: worldview's CDN sends Content-Encoding: br UNCONDITIONALLY --
# measured against Accept-Encoding: identity, still br. stdlib cannot
# decompress it; this dependency exists because NASA's CDN does not take
# no for an answer.
RUN pip install --no-cache-dir protobuf==7.36.0 kafka-python==3.0.11 brotli==1.1.0

# The point-cloud pipeline's four, and "hexing brings h3 next" above finally
# coming true. Each one is here because a lidar tile cannot be read without it:
#   h3      the cells. The service is NAMED for this and did not depend on it.
#   numpy   a million-point tile is array work or it is nothing
#   laspy   LAS/LAZ is not a struct.unpack format; lazrs is the rust backend,
#           chosen over laszip's C bindings so this image needs no compiler
#   pyproj  NEVER guess a projection. The USGS tiles arrive in a projected CRS
#           measured in US survey feet, and reading them as metres puts
#           California in the Atlantic -- silently, as a map.
RUN pip install --no-cache-dir h3==4.5.0 numpy==2.5.2 'laspy[lazrs]==2.7.0' pyproj==3.7.2

COPY --chown=kingfisher:kingfisher serve.py /app/serve.py
COPY --chown=kingfisher:kingfisher log.py /app/log.py
# the one place the running commit is known; every daemon's /health reads it
COPY --chown=kingfisher:kingfisher version.py /app/version.py
# the contract /api serves. Absent = /api answers 503 naming the defect.
COPY --chown=kingfisher:kingfisher descriptor.binpb /app/descriptor.binpb
# the daemons' /api: what ingestd/openskyd consume, not what this pod serves
COPY --chown=kingfisher:kingfisher descriptor-daemons.binpb /app/descriptor-daemons.binpb
COPY --chown=kingfisher:kingfisher api.py /app/api.py
# the librarian and the routing ladder
COPY --chown=kingfisher:kingfisher providers.py /app/providers.py
COPY --chown=kingfisher:kingfisher routing.py /app/routing.py
# the stomach: ingestd runs from this same image as its own deployment
COPY --chown=kingfisher:kingfisher ingestd.py /app/ingestd.py
# the LAYER_KIND_INTENSIVE pipeline ingestd dispatches to. Imported lazily by
# ingestd, so a missing file does not stop the pod -- it just makes every
# lidar tile fail with "No module named 'pointcloud'" once every ten seconds
# forever, which is how this line came to be written.
COPY --chown=kingfisher:kingfisher pointcloud.py /app/pointcloud.py
# Unprocessable and the rest of the failure vocabulary the dead-letter path
# speaks. d2f6b36 added it for ingestd and pointcloud without adding this
# line; see the note above net.py, same defect, same day.
COPY --chown=kingfisher:kingfisher errors.py /app/errors.py
# raster-to-H3, and the geography every other module borrows: bounds_of and
# bbox_overlaps live here. pointcloud takes a dataset's bounds from it at
# ingest and ingestd's backfill takes them for the shelf that predates that.
# The comment above about a missing file failing once every ten seconds
# forever came true again on 2026-09-01, in a worse way -- the backfill ran
# from the main loop, so the import error did not fail a tile, it killed the
# daemon. Both halves are fixed: this line, and the loop no longer lets a
# maintenance pass take the process down.
COPY --chown=kingfisher:kingfisher hexify.py /app/hexify.py
# the priced door: serve.py imports this to answer /shelf and /cells. Without
# it kingfisher starts clean, reports healthy, and 500s on the two endpoints
# that exist to stop a consumer being handed more than it can hold.
COPY --chown=kingfisher:kingfisher shelf.py /app/shelf.py
# the sky, polled: openskyd runs from this same image as its own deployment
COPY --chown=kingfisher:kingfisher openskyd.py /app/openskyd.py
# the weather, polled with a fallback: weatherd runs from this same image
COPY --chown=kingfisher:kingfisher weatherd.py /app/weatherd.py
# the clouds, live from GOES-West via GIBS: gibsd, same image, own pod
COPY --chown=kingfisher:kingfisher gibsd.py /app/gibsd.py
# the lease: heartbeat.py emits the standard ServiceHealthHeartbeat; hm
# judges our silence. gen/observability is vendored from blm @ d045b19.
COPY --chown=kingfisher:kingfisher heartbeat.py /app/heartbeat.py
# the vendored flipr client (stdlib): the flag plane's reference semantics
COPY --chown=kingfisher:kingfisher flipr_client.py /app/flipr_client.py
# flags_decl.py: the one list of flags kingfisher declares in flipr; every
# process publishes it at startup (2026-09-05), so the image must carry it
COPY --chown=kingfisher:kingfisher flags_decl.py /app/flags_decl.py
# the shared retry policy: backoff with jitter, circuit-break, the lot. Seven
# modules import it, flipr_client first among them, and 559c3a9 added it
# without adding this line -- so an image built from 574570e would have died
# on import in every daemon at once, and nobody knew because nobody built it.
# The third time a module has shipped in git and not in the image; the test
# beside this file now checks the two agree.
COPY --chown=kingfisher:kingfisher net.py /app/net.py
# generated types -- the contract, importable. Never hand-edited.
COPY --chown=kingfisher:kingfisher gen /app/gen

# defaults matching the /data/<name> layout kube/deployment.yaml mounts. The
# Deployment overrides KINGFISHER_MOUNTS to add /rides/ and /base/.
# the commit this image was built from, as a SHORT SHA. bin/build.sh passes
# it; a bare `docker build` gets "unknown", which hygiene.py fails on
# purpose -- an image nobody can name from git is the condition that let a
# review read HEAD beside a live service running a different program.
ARG KINGFISHER_COMMIT=unknown
ENV KINGFISHER_COMMIT=$KINGFISHER_COMMIT
ENV KINGFISHER_PORT=15021 \
    KINGFISHER_BIND=0.0.0.0 \
    KINGFISHER_ROOT=/srv/viewer \
    KINGFISHER_MOUNTS=/econ/=/data/econ,/layers/=/data/layers

EXPOSE 15021

# /health is liveness only: it answers whether the process is up, not whether a
# mount is populated. A mount that is missing is visible on / and in the startup
# log, and is deliberately NOT fatal -- serving three of four datasets beats
# refusing to start.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('KINGFISHER_PORT','15021')+'/health', timeout=2).status==200 else 1)"

ENTRYPOINT ["python3", "/app/serve.py"]
