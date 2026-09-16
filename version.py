# version.py -- the one place the running commit is known.
#
# hygiene.py's version check is the ROOT of its tree: without a commit there is
# no source to read, so every source-derived verdict is UNKNOWN. Until
# 2026-09-04 kingfisher's /health carried no commit at all, and the fleet ran an
# image nobody could name from git -- which is how a review read HEAD beside a
# live service that was a different program and had no way to tell.
#
# The value is a SHORT SHA, not a version string. hygiene treats it as a git
# ref and reads the source at that commit; "dev" or "0.1.0" fails that check
# by design. It arrives as a build arg (bin/build.sh passes git rev-parse
# --short HEAD), the Dockerfile turns it into an env, and every daemon reads
# it here. Read on every call rather than at import, so a test can set the
# environment and nothing is baked in by import order.

import os

UNKNOWN = "unknown"


def commit():
    """The short sha this process was built from, or "unknown"."""
    return os.environ.get("KINGFISHER_COMMIT", "").strip() or UNKNOWN


def server():
    """The Server: header value. One token, no Python version, so a consumer
    can tell a tile kingfisher served from one a fallback or a cache did."""
    return f"kingfisher/{commit()}"
