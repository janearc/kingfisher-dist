# The image must ship every module the shipped code imports.
#
# Three times now a module has landed in git and not in the Dockerfile, and
# each time the failure looked the same from outside: the pod starts, or does
# not, and the log says "No module named X" once every ten seconds forever.
#
#   2026-09-01  hexify.py   ingestd went CrashLoopBackOff on deploy
#   2026-09-03  net.py      added by 559c3a9; flipr_client imports it, and seven
#                           modules import flipr_client -- every daemon at once
#   2026-09-03  errors.py   added by d2f6b36 for the dead-letter path
#
# The last two were found only because the merge that carried them was checked
# before it was built. An image built from 574570e alone would have failed on
# import in the whole fleet, and nobody knew, because the review read the
# source and the running image and nobody built HEAD.
#
# This is a static check and deliberately so: it needs no docker, runs in the
# unit suite on every change, and fails on the commit that introduces the gap
# rather than on the deploy that discovers it. The Dockerfile's own comment
# above pointcloud.py describes the shape of this bug; the comment was right
# and did not stop it happening twice more. A guard nothing runs is a comment.

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")

# COPY [--flags] <src> /app/...   -- the shape every module line here takes
_COPY = re.compile(r"^COPY\s+(?:--\S+\s+)*(\S+)\s+/app/", re.M)


def shipped():
    """Every source path the Dockerfile copies into /app."""
    with open(DOCKERFILE) as f:
        return set(_COPY.findall(f.read()))


def local_modules():
    """Top-level .py files in the repo root, by module name."""
    return {f[:-3] for f in os.listdir(ROOT)
            if f.endswith(".py") and os.path.isfile(os.path.join(ROOT, f))}


def imports_of(path):
    """Top-level names imported anywhere in a file -- INCLUDING inside
    functions, because lazy imports are exactly how this bug hides: the module
    starts fine and dies on the first request that reaches the import."""
    with open(path) as f:
        tree = ast.parse(f.read(), path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_every_local_module_the_image_imports_is_in_the_image():
    """Transitive closure over the shipped set: if A ships and imports B, then B
    must ship, and so must whatever B imports."""
    local = local_modules()
    ship = {p[:-3] for p in shipped() if p.endswith(".py")}
    seen, missing = set(), {}
    pending = sorted(ship & local)
    while pending:
        mod = pending.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for dep in sorted(imports_of(os.path.join(ROOT, mod + ".py")) & local):
            if dep not in ship:
                missing.setdefault(dep, set()).add(mod)
            elif dep not in seen:
                pending.append(dep)
    assert not missing, (
        "modules imported by shipped code but absent from the Dockerfile -- the "
        "image will fail on import:\n" + "\n".join(
            f"  {dep}.py   needed by {', '.join(sorted(by))}"
            for dep, by in sorted(missing.items())))


def test_the_known_daemons_are_all_shipped():
    # the five workloads kingfisher:dev runs; if one of these drops out of
    # the Dockerfile the transitive check above has nothing to start from
    ship = shipped()
    for entry in ("serve.py", "ingestd.py", "gibsd.py", "openskyd.py", "weatherd.py"):
        assert entry in ship, f"{entry} is a deployment and is not in the image"


@pytest.mark.parametrize("gap", ["net", "errors", "hexify", "shelf"])
def test_the_modules_that_have_already_bitten_are_shipped(gap):
    """Pinned by name as well as by closure, so the message names the history
    when one of them regresses."""
    assert f"{gap}.py" in shipped(), f"{gap}.py has shipped in git and not in the image before"
