#!/usr/bin/env python3
# routing -- kingfisher answers travel questions, honestly labelled.
#
# The ladder: proxy valhalla when it is there and
# permitted; serve baked estimates when the memo has them; DEGRADE TO
# HAVERSINE when resources are absent. Every answer carries method, so a
# crow-flies guess can never masquerade as a routed path -- labelled
# degradation, never silent substitution. The zero value renders as an
# estimate: a forgotten field says "I am not sure", never "this is a road".
#
# Flipr gates the EXPENSIVE rungs only. valhalla is an upstream network call,
# so routing.enabled AND routing.valhalla guard it; the haversine floor is
# arithmetic and needs nobody's permission. Flipr being down therefore does
# not break routing -- it collapses it to the floor, labelled, which is
# exactly what "degrade if it doesn't have any resources" means.

import json
import math
import os
import urllib.request
import net

# one outbound door, injectable for the suite, same seam as providers.py
_urlopen = urllib.request.urlopen

# crude on purpose and stated: the floor divides crow-flies meters by a mode
# speed. It exists to keep an answer flowing when routing is down, not to be
# right. Anything reading seconds off a METHOD_HAVERSINE answer knows.
SPEED_MPS = {"MODE_AUTO": 13.0, "MODE_BICYCLE": 4.5, "MODE_PEDESTRIAN": 1.4}

EARTH_R = 6371000.0


def haversine_m(a_lat, a_lng, b_lat, b_lng):
    pa, pb = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(pa) * math.cos(pb) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def floor_estimate(mode_name, a, b):
    # (seconds, meters) by arithmetic alone. The floor never fails, which is
    # the property the whole ladder leans on.
    m = haversine_m(a["lat"], a["lng"], b["lat"], b["lng"])
    return m / SPEED_MPS.get(mode_name, 13.0), m


def valhalla_url():
    # a NAME, never an ip:port; unset means valhalla is not a resource we
    # have, and the ladder steps down without complaint.
    return os.environ.get("KINGFISHER_VALHALLA_URL", "")


_COSTING = {"MODE_AUTO": "auto", "MODE_BICYCLE": "bicycle",
            "MODE_PEDESTRIAN": "pedestrian"}


def valhalla_route(mode_name, a, b, timeout=10):
    # one leg through valhalla's /route. Any failure raises; the caller owns
    # the step down to the floor. Shape per the valhalla HTTP API.
    body = json.dumps({
        "locations": [{"lat": a["lat"], "lon": a["lng"]},
                      {"lat": b["lat"], "lon": b["lng"]}],
        "costing": _COSTING.get(mode_name, "auto"),
        "directions_options": {"units": "kilometers"},
    }).encode()
    # A POST that only READS: valhalla computes a route and changes nothing, so
    # it opts back in to retries. Worth having -- valhalla is in-cluster and a
    # pod restart mid-route is the ordinary case, and a dropped route is a
    # visibly missing line on the map rather than a logged warning.
    leg = net.get_json(valhalla_url() + "/route", data=body,
                       headers={"Content-Type": "application/json"},
                       method="POST", retry_non_idempotent=True,
                       policy=net.INTERACTIVE.replace(timeout_s=timeout),
                       )["trip"]["legs"][0]
    return (leg["summary"]["time"], leg["summary"]["length"] * 1000.0,
            leg.get("shape", ""))


def valhalla_matrix(mode_name, sources, targets, timeout=30):
    body = json.dumps({
        "sources": [{"lat": p["lat"], "lon": p["lng"]} for p in sources],
        "targets": [{"lat": p["lat"], "lon": p["lng"]} for p in targets],
        "costing": _COSTING.get(mode_name, "auto"),
    }).encode()
    # same shape as valhalla_route: a read carried over POST
    rows = net.get_json(valhalla_url() + "/sources_to_targets", data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST", retry_non_idempotent=True,
                        policy=net.INTERACTIVE.replace(timeout_s=timeout),
                        )["sources_to_targets"]
    # row-major seconds; valhalla marks unreachable with null time
    out = []
    for row in rows:
        for cell in row:
            t = cell.get("time")
            out.append(-1.0 if t is None else float(t))
    return out


def matrix_floor(mode_name, sources, targets):
    out = []
    for s_ in sources:
        for t_ in targets:
            secs, _ = floor_estimate(mode_name, s_, t_)
            out.append(secs)
    return out
