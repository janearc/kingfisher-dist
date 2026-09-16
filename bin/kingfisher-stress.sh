#!/usr/bin/env bash
# Drive kingfisher's priced door with the bench's real requests, and watch what
# the process actually wants while it answers.
#
# NOT synthetic load. The requests are the ones `kingfisher bench` makes: a
# window and a budget against /cells, the way dodo and the terminal renderer
# ask. The windows are real places on the shelf. The question this answers is
# "at what budget does kingfisher's memory bend, and how hard" -- not "how
# fast is a fold", which the test suite's benchmarks answer.
#
# WHY THIS EXISTS. kingfisher was OOM-killed on 2026-09-01 by exactly this
# request shape at a world-sized window, and dodo was OOM-killed three times
# holding the answer. Both peaks were the kill point, not the want: a cgroup
# limit truncates the measurement. Nothing had measured what a /cells request
# costs, so the ceiling on `budget` was a guess. This is the ruler (bigbird
# docs/rings.md: a limit set from measured need).
#
# TWO TARGETS, AND WHY THE DEFAULT IS LOCAL. Against the cluster (TARGET set
# to kingfisher.test) it reads the pod's cgroup by exec. The first run of this
# harness, 2026-09-04 10:15Z, did that and OOM-killed the pod: the stop rule
# looked at heap AFTER each step, and the step from budget 256k to 512k on the
# Bay window crossed from res 10 to res 11, seven times the cells, inside one
# request. So the default target is a LOCAL serve.py over the same shelf on a
# loopback port (bin/serve-local.sh), where the process is sampled by pid and
# nothing in the cluster can die, and the stop is PREDICTIVE: before each
# request /shelf prices it, and the harness refuses a step whose predicted
# heap would cross the limit. Use the cluster target only to confirm a number
# already measured locally, with the same predictive stop in front of it.
#
# WHAT IT READS.
#   local    ps rss of the serve.py pid, sampled at 100ms through the request
#            so a transient peak is caught; the max is the request's cost
#   cluster  memory.stat anon (the heap; page cache is excluded because the
#            kernel reclaims it and it fills to the limit within minutes) and
#            /proc/1/status VmHWM (the process high-water mark, which catches a
#            peak that came and went inside one step), both by kubectl exec
#
# MODES. RAMP doubles the budget each step (the default). REPEAT=N asks the
# same budget N times and prints the heap after each, which is the test for a
# leak against a plateau: the 2026-09-04 run saw about 4Mi of retained heap
# per request whatever the answer, and a fresh pod idles at 44Mi where a
# pod that has served a day idles at 142Mi.
#
# Read-only against the cluster in either mode: HTTP GETs and, for the cluster
# target, exec to cat two files.
set -euo pipefail

TARGET="${TARGET:-http://127.0.0.1:15099}"
LIMIT_MIB="${LIMIT_MIB:-256}"           # the ceiling to predict against
STOP_FRACTION="${STOP_FRACTION:-0.65}"  # refuse a step predicted past this
BYTES_PER_ROW="${BYTES_PER_ROW:-700}"   # prediction constant; measured ~510 settled, more in flight
START_BUDGET="${START_BUDGET:-1000}"
MAX_BUDGET="${MAX_BUDGET:-4096000}"
MAX_SECONDS="${MAX_SECONDS:-120}"
REPEAT="${REPEAT:-0}"
WINDOW="${WINDOW:-bay}"
NS="${KINGFISHER_NAMESPACE:-kingfisher}"; CTX="${KINGFISHER_CONTEXT:-k3d-$NS}"

# --- the real windows ---------------------------------------------------------
case "$WINDOW" in
  bay)   BBOX="-123,37,-121,38.5";       WHY="the Bay Area: many datasets, mixed resolution, the bench's home" ;;
  ny)    BBOX="-74.1,40.6,-73.85,40.85";  WHY="lower Manhattan and Brooklyn: the New York lidar tiles at res 12" ;;
  world) BBOX="-180,-85,180,85";          WHY="the whole shelf: what killed kingfisher on 2026-09-01" ;;
  *)     BBOX="$WINDOW";                  WHY="a bbox given on the command line" ;;
esac

# --- where the memory is read from ---------------------------------------------
if [[ "$TARGET" == *127.0.0.1* || "$TARGET" == *localhost* ]]; then
  MODE=local
  port="${TARGET##*:}"
  pid="${SERVE_PID:-$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1)}"
  [[ -n "$pid" ]] || { echo "==> nothing listening on $port; start bin/serve-local.sh first" >&2; exit 1; }
  # mem prints "heap_bytes hwm_bytes"; locally both are the process rss now
  mem() { local kb; kb="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"; echo "$(( ${kb:-0} * 1024 )) $(( ${kb:-0} * 1024 ))"; }
  limit=$(( LIMIT_MIB * 1048576 ))
  WHERE="local serve.py pid $pid, predicting against a ${LIMIT_MIB}Mi limit"
else
  MODE=cluster
  KC=(kubectl --context "$CTX" -n "$NS")
  pod="$("${KC[@]}" get pod -l app.kubernetes.io/name=kingfisher -o jsonpath='{.items[0].metadata.name}')"
  [[ -n "$pod" ]] || { echo "==> no kingfisher pod in $NS" >&2; exit 1; }
  mem() {
    "${KC[@]}" exec "$pod" -- sh -c \
      'a=$(awk "/^anon /{print \$2}" /sys/fs/cgroup/memory.stat); h=$(awk "/VmHWM/{print \$2}" /proc/1/status); echo "$a $((h*1024))"' 2>/dev/null \
      || echo "0 0"
  }
  limit="$("${KC[@]}" exec "$pod" -- cat /sys/fs/cgroup/memory.max 2>/dev/null || echo $(( LIMIT_MIB * 1048576 )))"
  WHERE="pod $pod, cgroup memory.max $(( limit / 1048576 ))Mi"
fi
mib() { awk -v b="$1" 'BEGIN{printf "%.0f", b/1048576}'; }

# sample_max runs in the background during a request and records the highest
# heap it saw, at 100ms locally (cheap) or 1s on the cluster (an exec each).
sample_max() {
  local f="$1" every; every=$([[ "$MODE" == local ]] && echo 0.1 || echo 1)
  local best=0 h
  while :; do read -r h _ <<<"$(mem)"; (( h > best )) && best=$h; echo "$best" > "$f"; sleep "$every"; done
}

# predict says what the NEXT request would cost, from /shelf's price, and is
# the reason this harness can be pointed at a pod without killing it.
predict() {  # budget -> "predicted_rows predicted_heap_bytes res"
  local q; q="$(curl -s --max-time 30 "$TARGET/shelf?bbox=$BBOX&budget=$1" || echo '{}')"
  python3 - "$q" "$BYTES_PER_ROW" "$2" <<'PYEOF'
import json, sys
try:
    p = json.loads(sys.argv[1])
except Exception:
    print("0 0 ?"); sys.exit()
rows = p.get("cells_estimate") or p.get("cells_native") or 0
# the accumulator may hold up to four times the budget before it folds, but
# never more than the cells actually read; take the smaller
rows = min(int(rows), 4 * int(p.get("budget") or 1)) if rows else 0
print(rows, int(rows) * int(sys.argv[2]) + int(sys.argv[3]), p.get("res", "?"))
PYEOF
}

out="$(mktemp -t kfstress)"; peakf="$out.peak"; rows_f="$out.rows"
trap 'rm -f "$out" "$peakf" "$rows_f"; [[ -n "${sampler:-}" ]] && kill "$sampler" 2>/dev/null || true' EXIT

echo "==> $WHERE"
echo "==> window $WINDOW = $BBOX ($WHY)"
read -r h0 _ <<<"$(mem)"
echo "==> before: heap $(mib "$h0")Mi; a step is refused when heap + rows*${BYTES_PER_ROW}B would pass $(awk -v l="$limit" -v f="$STOP_FRACTION" 'BEGIN{printf "%.0f", l*f/1048576}')Mi"
echo
printf '%-9s %8s %4s %9s %8s %9s %9s %7s %4s\n' budget cells res bytes seconds heap_after peak_seen cost_MB http
printf '%-9s %8s %4s %9s %8s %9s %9s %7s %4s\n' ------ ----- --- ----- ------- ---------- --------- ------- ----

budget="$START_BUDGET"; n=0
while (( budget <= MAX_BUDGET )); do
  read -r h_before _ <<<"$(mem)"
  read -r prows pheap pres <<<"$(predict "$budget" "$h_before")"
  if awk -v p="$pheap" -v l="$limit" -v f="$STOP_FRACTION" 'BEGIN{exit !(p > l*f)}'; then
    echo; echo "==> refused budget $budget: /shelf prices it at ~$prows rows (res $pres), predicted heap $(mib "$pheap")Mi is past $STOP_FRACTION of $(mib "$limit")Mi"
    break
  fi
  : > "$peakf"; sample_max "$peakf" & sampler=$!
  t0=$(date +%s.%N)
  code="$(curl -s -o "$out" -w '%{http_code}' --max-time "$MAX_SECONDS" "$TARGET/cells?bbox=$BBOX&budget=$budget" || echo 000)"
  t1=$(date +%s.%N)
  kill "$sampler" 2>/dev/null || true; wait "$sampler" 2>/dev/null || true; sampler=""
  read -r h_after hwm_after <<<"$(mem)"
  peak="$(cat "$peakf" 2>/dev/null || echo 0)"; (( hwm_after > peak )) && peak=$hwm_after
  secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')
  bytes=$(wc -c < "$out" | tr -d ' ')
  if [[ "$code" == "200" ]]; then
    read -r cells res <<<"$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get("cells",{})), d.get("res"))' "$out" 2>/dev/null || echo "? ?")"
  else cells="-"; res="-"; fi
  # the request's cost: how far the peak rose above the heap before it
  cost=$(awk -v p="$peak" -v b="$h_before" 'BEGIN{d=p-b; if (d<0) d=0; printf "%.0f", d/1048576}')
  printf '%-9s %8s %4s %9s %8s %7sMi %7sMi %7s %4s\n' "$budget" "$cells" "$res" "$bytes" "$secs" "$(mib "$h_after")" "$(mib "$peak")" "$cost" "$code"
  echo "$budget $cells $bytes $secs $h_after $peak $code" >> "$rows_f"
  [[ "$code" == "200" ]] || { echo; echo "==> stopped: http $code at budget $budget"; break; }
  n=$((n+1))
  if (( REPEAT > 0 )); then (( n >= REPEAT )) && break; else budget=$((budget * 2)); fi
done

# --- report ------------------------------------------------------------------
echo
echo "==> what it wanted, from the rows above"
python3 - "$rows_f" "$limit" "$REPEAT" <<'PYEOF'
import sys
rows = [l.split() for l in open(sys.argv[1]).read().splitlines() if l.strip()]
limit = int(sys.argv[2]); repeat = int(sys.argv[3])
ok = [r for r in rows if r[6] == "200" and r[1] not in ("?", "-")]
if repeat and len(ok) >= 2:
    h0, h1 = int(ok[0][4]), int(ok[-1][4])
    print("  repeat: heap after went %d Mi -> %d Mi over %d identical requests (%.1f Mi each); a plateau flattens, a leak does not"
          % (h0 // 2**20, h1 // 2**20, len(ok), (h1 - h0) / 2**20 / max(len(ok) - 1, 1)))
elif len(ok) >= 2:
    pts = sorted({(int(r[1]), int(r[5])) for r in ok})
    (c0, p0), (c1, p1) = pts[0], pts[-1]
    if c1 > c0:
        print("  peak heap: %.0f bytes per delivered cell (smallest to largest answer)" % ((p1 - p0) / (c1 - c0)))
    y = sorted({(int(r[1]), int(r[2])) for r in ok}); (c0, y0), (c1, y1) = y[0], y[-1]
    if c1 > c0:
        print("  wire:      %.0f bytes per delivered cell" % ((y1 - y0) / (c1 - c0)))
    print("  highest peak seen: %d Mi against a %d Mi limit" % (max(int(r[5]) for r in ok) // 2**20, limit // 2**20))
    print("  the budget caps cells DELIVERED; cells READ (the input side) is each response's cells_read")
else:
    print("  not enough successful rows to fit anything")
PYEOF

if [[ "$MODE" == cluster ]]; then
  echo
  echo "==> restarts and last state (an OOMKill shows here):"
  "${KC[@]}" get pod "$pod" -o jsonpath='{range .status.containerStatuses[*]}  {.name}: restarts={.restartCount} {.lastState.terminated.reason}{"\n"}{end}' 2>/dev/null
fi
read -r h_end _ <<<"$(mem)"
echo "==> after: heap $(mib "$h_end")Mi, limit $(mib "$limit")Mi"
