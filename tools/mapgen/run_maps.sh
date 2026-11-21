#!/usr/bin/env bash
# One command: run the traced + coverage-instrumented sqlglot suite on the
# pinned checkout and write every MAPS_SPEC map into $OUT_DIR.
#
#   run_maps.sh [out-dir] [pytest targets...]
#
#   run_maps.sh /out                 # full suite (the committed maps)
#   run_maps.sh /out tests/dialects/test_dune.py   # plumbing run
#
# Everything verbose goes to /work/*.log inside the container; only a short
# summary is printed to stderr (stdout stays clean in case this is ever piped).
set -euo pipefail

OUT_DIR="${1:-/out}"
shift || true
if [ "$#" -gt 0 ]; then
  TARGETS=("$@")
else
  TARGETS=(tests)
fi

CHECKOUT=/work/sqlglot
COLLECT=/work/collect.txt
TRACE=/work/trace_reads.json
DYNAMIC=/work/dynamic.json
COVDATA=/work/.coverage
COVERAGERC=/work/br_coverage.coveragerc
PYTEST_LOG=/work/pytest.log

cd "$CHECKOUT"
export SKIP_INTEGRATION=0
export PYTHONPATH="$CHECKOUT${PYTHONPATH:+:$PYTHONPATH}"
export BR_CHECKOUT="$CHECKOUT"
export BR_COVERAGE_FILE="$COVDATA"
export BR_SUBPROCESS_TRACE=1
# Spawned test workers (tests/test_optimizer.py, tests/test_executor.py) inherit
# these: br_trace_boot.py calls coverage.process_startup() from this file and
# imports trace_conftest for the proxies/dialect hooks (MAPS_SPEC v2).
export COVERAGE_PROCESS_START="$COVERAGERC"

# Runtime copy: never baked into the pinned tree in the image.
cp /mapgen/trace_conftest.py "$CHECKOUT/trace_conftest.py"

mkdir -p "$OUT_DIR"
rm -f "$COLLECT" "$TRACE" "$DYNAMIC" "$COVDATA" /work/pytest.err /work/combine.log
rm -f "$DYNAMIC".children.log "$DYNAMIC".*.child.json
rm -f /work/.coverage.* "$CHECKOUT"/.coverage.* "$CHECKOUT"/dynamic.json.*.child.json

START_TS=$(date +%s)
echo "[run_maps] collecting: pytest ${TARGETS[*]}" >&2
python -m pytest --collect-only -q -p no:cacheprovider "${TARGETS[@]}" \
  > "$COLLECT" 2> /work/collect.err || true
COLLECTED=$(grep -c '::' "$COLLECT" || true)
COLLECT_TS=$(date +%s)
echo "[run_maps] collected lines: $COLLECTED in $((COLLECT_TS - START_TS))s" >&2

echo "[run_maps] running traced suite (coverage per test, fixture/dialect/key reads)" >&2
BR_TRACE_OUT="$TRACE" BR_DYNAMIC_OUT="$DYNAMIC" BR_COVERAGE_FILE="$COVDATA" \
  python -m pytest -p trace_conftest -q --tb=no -p no:cacheprovider "${TARGETS[@]}" \
  > "$PYTEST_LOG" 2> /work/pytest.err || true
RUN_TS=$(date +%s)
tail -n 3 "$PYTEST_LOG" >&2 || true
grep -E '^\[trace_conftest\]' /work/pytest.err >&2 || true
if [ -f "$DYNAMIC.children.log" ]; then
  echo "[run_maps] spawned worker trace flushes: $(wc -l < "$DYNAMIC.children.log" | tr -d ' ')" >&2
else
  echo "[run_maps] WARNING: no spawned worker traces (subprocess instrumentation inactive?)" >&2
fi

test -s "$TRACE" || { echo "[run_maps] ERROR: no trace file" >&2; exit 1; }
test -s "$DYNAMIC" || { echo "[run_maps] ERROR: no dynamic file (dialects/keyreads)" >&2; exit 1; }

# Merge the parent's and every spawned worker's parallel-mode data.  Failing
# loudly here is deliberate: silent data loss is how v1 shipped a coverage map
# with no fixture-driven lines (MAPS_SPEC v2, bug 1).
COVDATA_FILES=$(find /work "$CHECKOUT" -maxdepth 1 -name '.coverage.*' -type f 2>/dev/null | wc -l)
if [ "$COVDATA_FILES" -lt 1 ]; then
  echo "[run_maps] ERROR: coverage combine found no parallel data files" >&2
  exit 1
fi
echo "[run_maps] combining $COVDATA_FILES parallel coverage files" >&2
python -m coverage combine --data-file="$COVDATA" /work "$CHECKOUT" \
  >> /work/combine.log 2>&1 || {
    tail -n 20 /work/combine.log >&2 || true
    echo "[run_maps] ERROR: coverage combine failed" >&2
    exit 1
  }
test -s "$COVDATA" || { echo "[run_maps] ERROR: coverage combine produced no data" >&2; exit 1; }
COMBINED_CONTEXTS=$(python - "$COVDATA" <<'PY'
import sqlite3, sys
uri = "file:" + sys.argv[1] + "?mode=ro"
connection = sqlite3.connect(uri, uri=True)
try:
    print(connection.execute("SELECT COUNT(*) FROM context").fetchone()[0])
finally:
    connection.close()
PY
)
echo "[run_maps] combined contexts: $COMBINED_CONTEXTS (files: $COVDATA_FILES)" >&2
if [ "$COMBINED_CONTEXTS" -lt 1 ]; then
  echo "[run_maps] ERROR: combined coverage has no contexts" >&2
  exit 1
fi

TESTS_JSON_ARGS=()
if [ -n "${BR_TESTS_JSON:-}" ] && [ -f "${BR_TESTS_JSON}" ]; then
  TESTS_JSON_ARGS=(--tests-json "$BR_TESTS_JSON")
  echo "[run_maps] pinning test id set to $BR_TESTS_JSON" >&2
fi

echo "[run_maps] generating maps into $OUT_DIR" >&2
python /mapgen/gen_maps.py \
  --checkout "$CHECKOUT" \
  --coverage "$COVDATA" \
  --trace "$TRACE" \
  --dynamic "$DYNAMIC" \
  --collect "$COLLECT" \
  --out "$OUT_DIR" \
  "${TESTS_JSON_ARGS[@]}"

rm -f "$CHECKOUT/trace_conftest.py"
END_TS=$(date +%s)
echo "[run_maps] timings: collect=$((COLLECT_TS - START_TS))s run=$((RUN_TS - COLLECT_TS))s total=$((END_TS - START_TS))s (target=${TARGETS[*]})" >&2
echo "[run_maps] done" >&2
