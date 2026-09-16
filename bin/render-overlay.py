#!/usr/bin/env python3
# render-overlay.py: write kube/<target>/ from kube/template/ and a target's
# env file.
#
# a target is a name, the cluster and context to apply to, the namespace, the
# host port its edge listens on, and the roots it owns on the host. every
# in-cluster name follows the namespace, so two clusters differ only in paths
# and ports.
#
#   bin/render-overlay.py example --check     the overlay is the render
#   bin/render-overlay.py scratch --edge-port 9900
#                                             writes environments/scratch.env
#                                             if absent, renders kube/scratch/
#   bin/render-overlay.py scratch             re-renders from its env file
#
# stdlib only. the placeholders are ${ENV}, ${NAMESPACE}, ${DATA_ROOT},
# ${SHELF_ROOT} and ${UI_ROOT}, shell style, filled from the env file.
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(HERE, "kube", "template")
ENVIRONMENTS = os.path.join(HERE, "kube", "environments")
KEYS = ("ENV", "CLUSTER", "CTX", "NAMESPACE", "EDGE_PORT", "DATA_ROOT", "SHELF_ROOT", "UI_ROOT", "SHELF_READONLY")


# target returns the parameter set for an env, with the conventions filled
# in: data root /srv/<env>, shelf and ui under it, and the edge port. the
# edge port is the host port that reaches this cluster's edge; the host's own
# front door on 80 is a property of the host, not of an environment, so no
# environment says 80. used to create an environment file; once one exists,
# load_env reads it instead.
def target(name, data_root=None, shelf_root=None, ui_root=None, edge_port=None):
    data_root = data_root or os.path.join("/srv", name)
    return {
        "ENV": name,
        "CLUSTER": name,
        "CTX": f"k3d-{name}",
        # every in-cluster name follows the namespace, so two clusters differ
        # only in paths and ports; the namespace itself is one per machine.
        "NAMESPACE": os.environ.get("KINGFISHER_NAMESPACE", "kingfisher"),
        "EDGE_PORT": str(edge_port if edge_port is not None else 9800),
        "DATA_ROOT": data_root,
        "SHELF_ROOT": shelf_root or os.path.join(data_root, "maps"),
        "UI_ROOT": ui_root or os.path.join(data_root, "ui"),
        # a target owns its shelf unless its env file says otherwise
        "SHELF_READONLY": "false",
    }


# env_text is the file's content: KEY=value, one per line, in a fixed order.
def env_text(t):
    return "".join(f"{k}={t[k]}\n" for k in KEYS)


# load_env reads kube/environments/<env>.env, or None if there is none yet.
def load_env(name, path=None):
    path = path or os.path.join(ENVIRONMENTS, f"{name}.env")
    if not os.path.isfile(path):
        return None
    t = {}
    for line in open(path).read().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            t[k.strip()] = v.strip()
    missing = [k for k in KEYS if k not in t]
    if missing:
        raise SystemExit(f"render-overlay: {path} lacks {', '.join(missing)}")
    return t


# render fills every template for a target and returns {relative path: text}.
def render(t):
    out = {}
    for f in sorted(os.listdir(TEMPLATE)):
        s = open(os.path.join(TEMPLATE, f)).read()
        for k in ("ENV", "NAMESPACE", "DATA_ROOT", "SHELF_ROOT", "UI_ROOT"):
            s = s.replace("${%s}" % k, t[k])
        out[f] = s
    return out


# check compares a render against what is on disk and returns the drifted
# relative paths; empty means the committed overlay IS the render.
def check(t, outdir):
    drift = []
    for rel, text in render(t).items():
        p = os.path.join(outdir, rel)
        if not os.path.exists(p) or open(p).read() != text:
            drift.append(rel)
    rendered = set(render(t))
    for f in os.listdir(outdir) if os.path.isdir(outdir) else []:
        if f not in rendered:
            drift.append(f + " (not in the template)")
    return drift


def main():
    ap = argparse.ArgumentParser(description="render kube/<target>/ from kube/template/")
    ap.add_argument("name", help="the target: the cluster to render for, e.g. example")
    ap.add_argument("--data-root", help="the target's own root; default /srv/<name>")
    ap.add_argument("--shelf-root", help="the maps it reads; default <data-root>/maps")
    ap.add_argument("--ui-root", help="the ui files; default <data-root>/ui")
    ap.add_argument("--edge-port", type=int, help="host port of the target's edge; default 9800")
    ap.add_argument("--check", action="store_true", help="exit 1 if kube/<name>/ differs from the render")
    ap.add_argument("--out", help="write somewhere other than kube/<name>/")
    a = ap.parse_args()
    env_path = os.path.join(ENVIRONMENTS, f"{a.name}.env")
    t = load_env(a.name)
    if t is None:
        # first render of a new environment: write its file from the flags, then
        # it is the source of truth and the flags are ignored
        t = target(a.name, a.data_root, a.shelf_root, a.ui_root, a.edge_port)
        if not a.check:
            os.makedirs(ENVIRONMENTS, exist_ok=True)
            with open(env_path, "w") as f:
                f.write(env_text(t))
            print(f"wrote {os.path.relpath(env_path, HERE)}")
    outdir = a.out or os.path.join(HERE, "kube", a.name)
    if a.check:
        drift = check(t, outdir)
        if drift:
            print(f"render-overlay: kube/{a.name} differs from the render: {', '.join(drift)}", file=sys.stderr)
            print("  edit kube/template/ and re-render; the overlay is generated", file=sys.stderr)
            return 1
        print(f"kube/{a.name} is the render of kube/template for {t['CTX']}")
        return 0
    os.makedirs(outdir, exist_ok=True)
    for rel, text in render(t).items():
        with open(os.path.join(outdir, rel), "w") as f:
            f.write(text)
    print(f"rendered kube/{a.name}: {t['CTX']}, edge :{t['EDGE_PORT']}, data {t['DATA_ROOT']}, shelf {t['SHELF_ROOT']} ({'read-only' if t['SHELF_READONLY']=='true' else 'owned'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
