#!/usr/bin/env bash
# Run the full pinned sqlglot suite once: either pristine (MUTANT_ID=baseline)
# or with corpus/<MUTANT_ID>.patch applied.  Writes /data/ground/<id>.json.
set -euo pipefail

ID="${MUTANT_ID:-baseline}"
DATA="${DATA_DIR:-/data}"
DEST=/work/m
LOG=/work/pytest.log

rm -rf "$DEST"
cp -a /pristine/sqlglot "$DEST"
cd "$DEST"

if [ "$ID" != "baseline" ]; then
  PATCH="$DATA/corpus/$ID.patch"
  test -f "$PATCH" || { echo "[run_one] missing patch: $PATCH" >&2; exit 2; }
  python /runner/apply_patch.py "$PATCH" "$DEST"
fi

export PYTHONPATH="$DEST"
export SKIP_INTEGRATION=0

START=$(date +%s%N)
# Full suite, quiet, no traceback; -rfE prints the short summary of failures
# and errors, which is the ground-truth list.
timeout 1500 python -m pytest tests -q -rfE --tb=no -p no:cacheprovider \
  > "$LOG" 2>&1 || true
END=$(date +%s%N)
WALL_MS=$(( (END - START) / 1000000 ))

mkdir -p "$DATA/logs"
cp "$LOG" "$DATA/logs/$ID.log"

python /runner/collect.py \
  --id "$ID" \
  --log "$LOG" \
  --wall-ms "$WALL_MS" \
  --out "$DATA/ground/$ID.json"
