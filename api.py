# /api for the daemons: ingestd and openskyd serve no RPC of their own, so
# unlike serve.py's descriptor.binpb (what kingfisher SERVES), this one
# publishes what they CONSUME -- flipr's FliprService, vendored at
# vendor/proto/flipr/v1. "a daemon's contract is what it consumes"
# (mitigation-1, 2026-08-28, ruling that nobody gets to have no /api).
import os

DESCRIPTOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "descriptor-daemons.binpb")

_cache = None
_tried = False


def descriptor_bytes():
    global _cache, _tried
    if not _tried:
        _tried = True
        try:
            with open(DESCRIPTOR_PATH, "rb") as f:
                _cache = f.read()
        except OSError:
            _cache = None
    return _cache
