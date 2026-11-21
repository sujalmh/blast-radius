#!/usr/bin/env bash
# Run the pristine baseline and every corpus mutant, up to 3 docker runs at a
# time, writing ground/<id>.json for each.  Idempotent: ids that already have a
# ground file are skipped unless FORCE=1.
#
#   ./run_all.sh              # build image if missing, run everything missing
#   FORCE=1 ./run_all.sh      # rerun everything
#   JOBS=3 ./run_all.sh       # concurrency (default 3)
#
# Total budget: 1 baseline + 17 mutants at ~250 s emulated per run, 3-way
# parallel -> roughly 25 minutes on an Apple-silicon host.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${MUTANTSWEEP_IMAGE:-blast-mutantsweep}"
JOBS="${JOBS:-3}"
FORCE="${FORCE:-0}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[run_all] building $IMAGE (linux/amd64)" >&2
  docker build --platform linux/amd64 -t "$IMAGE" \
    -f "$HERE/runner.Dockerfile" "$HERE"
fi

mkdir -p "$HERE/ground"

# Baseline first: it validates the environment (must be 1620 passed / 1 skipped).
if [ ! -f "$HERE/ground/baseline.json" ] || [ "$FORCE" = "1" ]; then
  echo "[run_all] baseline" >&2
  MUTANTSWEEP_IMAGE="$IMAGE" "$HERE/run_one.sh" baseline
else
  echo "[run_all] baseline ground truth exists, skipping" >&2
fi

IDS=()
while IFS= read -r id; do
  if [ "$FORCE" = "1" ] || [ ! -f "$HERE/ground/$id.json" ]; then
    IDS+=("$id")
  fi
done < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
for entry in m["mutants"]:
    print(entry["id"])
' "$HERE/corpus/manifest.json")

if [ "${#IDS[@]}" -eq 0 ]; then
  echo "[run_all] all mutants already have ground truth" >&2
else
  echo "[run_all] running ${#IDS[@]} mutants with JOBS=$JOBS" >&2
  i=0
  while [ "$i" -lt "${#IDS[@]}" ]; do
    batch=("${IDS[@]:$i:$JOBS}")
    pids=()
    for id in "${batch[@]}"; do
      MUTANTSWEEP_IMAGE="$IMAGE" "$HERE/run_one.sh" "$id" &
      pids+=("$!")
    done
    status=0
    for pid in "${pids[@]}"; do
      wait "$pid" || status=1
    done
    if [ "$status" -ne 0 ]; then
      echo "[run_all] WARNING: a run in this batch failed; see output above" >&2
    fi
    i=$((i + JOBS))
  done
fi

python3 "$HERE/summarize.py"
