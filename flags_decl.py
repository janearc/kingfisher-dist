# the flags kingfisher declares in flipr, and the one call that publishes
# them. THE SERVICE IS THE SOURCE OF WHAT IT DECLARES: every kingfisher
# process publishes this list at startup, then reads. Publishing is
# idempotent and flipr never overwrites an operator's value, server-side, so
# a restart or a redeploy changes nothing an operator set. Before 2026-09-05
# the list lived in bin/publish-flags.sh, run once by the cluster's create script,
# and every deploy since rode on that one declaration; the fifth throwaway
# rebuild put kingfisher into a flipr with no kingfisher@v1 and the fetching
# daemons stayed not-ready, by design, until this module existed.
# bin/publish-flags.sh publishes the same list by hand, without a restart.
#
# The namespace version is the PROTO API VERSION (settled),
# never the commit hash: operator values must survive deploys.
#
# ONLY WIRED FLAGS ARE PUBLISHED. A flag the code does not consult is an
# off-switch that does nothing, and the emergency page projecting it would
# lie. serve.*, compose.* and routing.reduced_matrix arrive WITH the code
# paths that consult them.
import os

from log import log

SERVICE = "kingfisher"
VERSION = "v1"


def bflag(key, on, why, expensive):
    return {"key": key, "value": {"boolValue": on},
            "description": why, "expensive": expensive}

# fetch.enabled now defaults ON (was off; changed; the
# reasoning relayed by interface-3 after the opensky live-test: "the master
# should be the emergency brake, not the parking brake -- master defaults
# ON, per-source flags carry the real decisions"). A fresh deploy still
# spends nothing new: every fetch.<source> below still defaults False, so
# the real decision -- which source, if any -- stays per-source. What
# changes is a store wipe or rebuild no longer silently re-gates every
# already-approved source behind a second switch nobody remembers exists.
# routing rungs default on -- the ladder degrades to the floor, so "on"
# risks nothing when valhalla is absent.
#
# Descriptions follow flipr's uniform template (an interrupt
# work via mitigation-1: "off should stop behavior. that should be
# uniform... when you read the descriptions... it's not clear what they're
# doing"): "on: <what happens, cost named>. off: <what stops>." Plain
# words, no whimsy, and an enabled flag is never described as a "kill" --
# only off is the kill, everywhere, uniformly. Mechanically enforced now
# (flipr fc1a7f7): a non-conforming expensive publish is refused.
FLAGS = [
    # the big red switch: EVERY outbound request kingfisher makes -- provider
    # fetches and the valhalla rung -- stops when this is off. Deliberately
    # excluded: flipr itself (the flag plane cannot gate itself) and the
    # heartbeat (it is the LEASE; silencing it expires kingfisher from the
    # mesh, which is a bigger event than any fetch). Routing survives OFF by
    # collapsing to the haversine floor, labelled.
    bflag("network.enabled", True, "on: provider fetches and the valhalla routing rung are allowed to run. off: every outbound network request stops -- fetches refuse, routing floors to haversine.", True),
    bflag("fetch.enabled",  True, "on: external dataset fetching is allowed, gated further per-source by fetch.<source>. off: every dataset fetch refuses, regardless of any per-source flag -- the emergency brake, not the parking brake.", True),
    bflag("fetch.usgs",     False, "on: fetches USGS 3DEP / The National Map lidar index queries and downloads. off: no USGS requests are made.", True),
    bflag("fetch.noaa",     False, "on: fetches NOAA InPort bathymetry and fisheries datasets. off: no NOAA requests are made.", True),
    bflag("fetch.overture", False, "on: fetches Overture Maps buildings and places. off: no Overture requests are made.", True),
    bflag("fetch.nasa",     False, "on: fetches NASA Black Marble real night-lights data. off: no Black Marble requests are made.", True),
    bflag("fetch.carto",    False, "on: fetches Carto basemap tiles on a PAID api key. off: no Carto requests are made.", True),
    bflag("fetch.nasa_gibs", False, "on: fetches NASA GIBS/Worldview layer index and made-to-order snapshot imagery, no auth required. off: no GIBS/Worldview requests are made.", True),
    bflag("fetch.asf",      False, "on: fetches ASF SAR archive interferogram browse images, no auth required on browse. off: no ASF requests are made.", True),
    bflag("fetch.opensky",  False, "on: openskyd polls OpenSky Network's live ADS-B state vectors on an interval, no auth required. off: openskyd stops polling; the last snapshot keeps serving, unrefreshed, until this flips back on.", True),
    bflag("fetch.weather",  False, "on: weatherd polls Open-Meteo (primary) and NWS (fallback, tried only if Open-Meteo fails a point) for current conditions, no auth required either way. off: weatherd stops polling; the last snapshot keeps serving, unrefreshed, until this flips back on.", True),
    bflag("ingest.enabled",  True, "on: ingestd consumes the spool, processing queued items. off: ingestion pauses mid-queue -- the kill for a runaway pipeline.", True),
    bflag("routing.enabled",  True, "on: routing answers requests at all. off: every routing answer collapses to the haversine floor.", False),
    bflag("routing.valhalla", True, "on: routing answers use the live valhalla rung. off: routing steps down to the haversine floor, labelled.", True),
]

KEYS = frozenset(f["key"] for f in FLAGS)


# a flipr client for this process, addressed by KINGFISHER_FLIPR_URL
def client():
    import flipr_client
    return flipr_client.FliprClient(
        os.environ.get("KINGFISHER_FLIPR_URL", "http://flipr.test:9800"),
        SERVICE, VERSION)


# publish FLAGS through c at startup; returns flipr's count, or -1 after a
# warn line when flipr could not be reached. Never raises: a process whose
# flipr is down still starts, so /health can say so and the flag checks in
# its loop keep reporting the missing namespace until it appears.
def publish(c, log_fn=log):
    try:
        n = c.publish(FLAGS)
    except Exception as e:  # noqa: BLE001 - startup must not die on the flag plane
        log_fn("warn", "flags_publish_failed", namespace=f"{SERVICE}@{VERSION}", err=str(e)[:200])
        return -1
    log_fn("info", "flags_published", namespace=f"{SERVICE}@{VERSION}", count=n)
    return n
