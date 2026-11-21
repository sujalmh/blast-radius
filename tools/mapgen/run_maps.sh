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
PYTEST_LOG=/work/pytest.log

cd "$CHECKOUT"
export SKIP_INTEGRATION=0
export PYTHONPATH="$CHECKOUT${PYTHONPATH:+:$PYTHONPATH}"
export BR_CHECKOUT="$CHECKOUT"

# Runtime copy: never baked into the pinned tree in the image.
cp /mapgen/trace_conftest.py "$CHECKOUT/trace_conftest.py"

mkdir -p "$OUT_DIR"
rm -f "$COLLECT" "$TRACE" "$DYNAMIC" "$COVDATA" /work/pytest.err

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

test -s "$TRACE" || { echo "[run_maps] ERROR: no trace file" >&2; exit 1; }
test -s "$DYNAMIC" || { echo "[run_maps] ERROR: no dynamic file (dialects/keyreads)" >&2; exit 1; }
test -s "$COVDATA" || { echo "[run_maps] ERROR: no coverage data" >&2; exit 1; }

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
