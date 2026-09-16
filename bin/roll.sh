#!/bin/sh
# roll.sh -- build from HEAD, then roll all five workloads, then prove it.
#
#   bin/roll.sh            build + import + roll kingfisher, ingestd, gibsd, openskyd, weatherd
#   bin/roll.sh --wait     same, but wait for in-flight downloads instead of refusing
#
# THE GATE. The fetcher lands a payload before it writes the meta (issue 79),
# and ingestd treats a meta with no payload as broken and dead-letters it. A
# download in flight across a roll therefore looks, to the restarted ingestd,
# like exactly that. So this refuses to roll while /discovery shows any dataset
# QUEUED or FETCHING, and says which. It lives here rather than in a runbook
# because a runbook is a comment; the next roll has this whether or not anyone
# remembers.
set -eu
cd "$(dirname "$0")/.."
# THE TARGET IS A FILE, NOT A HABIT. kube/environments/<target>.env names the
# k3d context, the namespace, the edge port and the data root; it is the same
# shape flipr uses and deployd renders, and bin/render-overlay.py renders the
# overlay from it. Every kubectl below carries the
# context explicitly, so this script cannot roll whatever context happens to
# be current. The render check refuses to roll an overlay that
# drifted from the template: what is applied is what is generated.
# NO DEFAULT TARGET. A roll that assumes a cluster when nobody says otherwise is
# the 116 habit with an escape hatch; the environment is the first argument
# or the script refuses, as flipr's, starling's and dodo's do.
TARGET="${1:-}"
case "$TARGET" in
  ""|--*) echo "usage: bin/roll.sh <env> [--wait]   (env is a file under kube/environments/)" >&2; exit 2 ;;
esac
shift
[ -f "kube/environments/$TARGET.env" ] || { echo "roll.sh: no kube/environments/$TARGET.env; render it first" >&2; exit 1; }
. "kube/environments/$TARGET.env"
python3 bin/render-overlay.py "$TARGET" --check
# the edge port is the host port that reaches this cluster's edge; always named
PORT_SUFFIX=":$EDGE_PORT"
KF="${KINGFISHER_URL:-http://kingfisher.test$PORT_SUFFIX}"
NS="${KINGFISHER_NAMESPACE:-$NAMESPACE}"
KUBECTL="kubectl --context $CTX"
export TARGET
WORKLOADS="kingfisher kingfisher-ingestd kingfisher-gibsd kingfisher-openskyd kingfisher-weatherd"

bin/build.sh
sha=$(git rev-parse --short HEAD)

inflight() {
  python3 - "$KF" <<'PY'
import json, sys, urllib.request
try:
    d = json.load(urllib.request.urlopen(sys.argv[1] + "/discovery", timeout=20))
except Exception as e:  # a kingfisher that cannot answer is not a reason to roll blind
    print("cannot read /discovery: " + str(e)[:120]); sys.exit(2)
busy = [x["id"] for x in d.get("datasets", []) if x.get("state") in ("FETCH_STATE_QUEUED", "FETCH_STATE_FETCHING")]
for b in busy: print(b)
sys.exit(1 if busy else 0)
PY
}

if ! out=$(inflight); then
  echo "roll.sh: downloads in flight; a roll now would dead-letter them:" >&2
  echo "$out" | sed 's/^/  /' >&2
  if [ "${1:-}" != "--wait" ]; then
    echo "roll.sh: refusing. Wait for them, cancel them, or pass --wait." >&2
    exit 1
  fi
  while ! inflight >/dev/null; do sleep 10; done
fi

# THE MANIFESTS FIRST, THEN THE RESTART. A roll that only restarts is a restart
# with a better name: a change to kube/<target> or kube/base would render, pass
# review, and never reach the cluster. apply is idempotent -- on a tip whose
# manifests already describe the cluster it changes nothing and says so.
# the one token a render leaves open: the image tag. Filled here with the sha
# build.sh just tagged, so the cluster runs kingfisher:<sha>, named by hash.
kubectl kustomize "kube/$TARGET" | sed "s/\${COMMIT}/$sha/g" | $KUBECTL apply -f -
for d in $WORKLOADS; do $KUBECTL -n "$NS" rollout restart "deploy/$d"; done
for d in $WORKLOADS; do $KUBECTL -n "$NS" rollout status "deploy/$d" --timeout=240s; done

# PROVE IT: every /health must name the commit just built
sleep 5
python3 - "$sha" "$PORT_SUFFIX" <<'PY'
import json, sys, urllib.request
sha = sys.argv[1]; suffix = sys.argv[2]; bad = 0
for host in ("kingfisher.test", "ingestd.test", "gibsd.test", "openskyd.test", "weatherd.test"):
    try:
        b = json.load(urllib.request.urlopen(f"http://{host}{suffix}/health", timeout=15))
        got = b.get("commit"); ok = got == sha
    except Exception as e:
        got, ok = f"unreachable: {str(e)[:60]}", False
    print(f"  {host:16s} commit={got}  {'ok' if ok else 'MISMATCH'}"); bad += not ok
sys.exit(1 if bad else 0)
PY
echo "rolled $WORKLOADS to $sha in $CTX"
