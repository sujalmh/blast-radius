# Map generation (`tools/mapgen`)

Reproducible builder for the committed `maps/` bundle described in
[MAPS_SPEC.md](../../MAPS_SPEC.md), pinned to sqlglot
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

Everything runs in the scorer's base image
(`python:3.13-slim@sha256:e48f59cd…`, linux/amd64) with the pinned
`requirements.lock`, so per-test coverage reflects the same interpreter/site
packages the labels were produced in. The image is **not** the submission
image: it only exists to regenerate `maps/`.

## One command for the full bundle

```sh
# 1. build (context is tools/mapgen/, see "Deviations" below)
docker build --platform linux/amd64 -t blast-mapgen \
  -f tools/mapgen/mapgen.Dockerfile tools/mapgen

# 2. run the whole suite once and write the committed maps
docker run --rm --platform linux/amd64 \
  -e BR_GENERATOR_GIT="$(git rev-parse HEAD)" \
  -e BR_TESTS_JSON=/data/tests.json \
  -v "$PWD/sample/tests.json:/data/tests.json:ro" \
  -v "$PWD/maps:/out" \
  blast-mapgen
```

`BR_GENERATOR_GIT` is recorded in `MANIFEST.json` (optional).

`BR_TESTS_JSON` is optional: without it `tests_index.json` is the raw
`pytest --collect-only` order (1621 ids, including the one environment-skipped
test); with it the index is filtered to the 1620 labeled ids, which is what we
commit. The mount is the test *list*, not the answers (`/data/tests.json` is
mounted at scoring time anyway).

The container takes roughly **6–10 minutes** under linux/amd64 emulation on a
macOS arm64 host (measured: see "Sizes and timing"). It writes to `/out` with
mode 0644, so the repo's `maps/` can be committed directly.

## Plumbing run (one test file, ~5 s)

```sh
mkdir -p /tmp/br-plumb
docker run --rm --platform linux/amd64 -v /tmp/br-plumb:/out \
  blast-mapgen /out tests/dialects/test_dune.py
python tools/mapgen/inspect_maps.py --maps /tmp/br-plumb \
  --file sqlglot/dialects/dune.py \
  --test tests/dialects/test_dune.py::TestDune::test_dune
```

`run_maps.sh [out-dir] [pytest targets…]` runs the same pipeline as the full
run, so plumbing and production differ only in the pytest target.

## Stages

1. **Build** – `fetch_pin.py` downloads `codeload.github.com/…/tar.gz/<sha>`
   (the README allows sqlglot at the pinned commit) into `/work/sqlglot`;
   `requirements.lock` + `coverage==7.17.4` are pip-installed.
2. **Collect** – `pytest --collect-only -q tests` → `/work/collect.txt`
   (test ids, deterministic file order).
3. **Trace + coverage** – `pytest -p trace_conftest` with `SKIP_INTEGRATION=0`,
   `PYTHONPATH=/work/sqlglot`, cwd `/work/sqlglot`:
   * `trace_conftest.py` is copied into the checkout root at run time (never
     baked) and wraps `builtins.open`, `io.open`, `codecs.open`, `os.open`,
     `pathlib.Path.open/read_text/read_bytes`. Reads are attributed to the
     running test's node id; reads outside a test go to
     `module:<tests/file.py>` (all test ids of that file) or `__unknown__`
     (logged). Write modes are ignored.
   * The same plugin starts `coverage.Coverage(source=["sqlglot"])` after
     collection and calls `switch_context(nodeid)` around each test protocol,
     i.e. `--cov-context=test` semantics without a pytest-cov dependency. Data
     is saved to `/work/.coverage` with one sqlite context per test node id.
4. **Generate** – `gen_maps.py` parses the coverage sqlite itself
   (`file`, `context`, `line_bits` + `coverage.numbits.numbits_to_nums`), walks
   the pinned ASTs, and writes every file below. Gzip is deterministic
   (`mtime=0`, no embedded filename) and JSON is sorted/compact.

## Outputs

| file | contents |
| --- | --- |
| `tests_index.json` | `{"test_ids": [...]}` in collection order; ordinal = position |
| `coverage.bin.gz` | `BRcov1` binary, per-file per-ordinal 1-based inclusive runs |
| `symbols.json.gz` | `{path: [[qualname, start, end, "function"\|"class"]]}` |
| `usage.json.gz` | `{path: {NAME: [same-file reference lines]}}` |
| `usage_refs.json.gz` | extension: `{NAME: {referencing file: [lines]}}` (see deviations) |
| `fixtures.json.gz` | `{non-.py file: [test ids]}` + `__test_files__` + `__shared__` |
| `source.tar.gz` | `sqlglot/**/*.py` only, repo-relative member names |
| `MANIFEST.json` | pinned sha, generator/version metadata, per-file sha1 |

`__shared__` = test-support files imported by ≥2 test files (e.g.
`tests/helpers.py`, `tests/dialects/test_dialect.py`) plus any `conftest.py`.

## Sizes and timing (measured on this host, linux/amd64 under Rosetta)

Plumbing run `tests/test_parser.py` (81 tests):

* coverage: 109 files, 855 (file, test) pairs, 33.6 KB gzip
* symbols 53 KB · usage 28 KB · usage_refs 68 KB · source 705 KB
* full test_parser map bundle: **907 KB total**

Extrapolated full suite (1620 tests): coverage ≈ **0.7 MB gzip**, fixtures
≈ 0.1–0.3 MB, everything else unchanged → **≈ 1.7–2.0 MB total**, well under
the 8 MB budget.

Timing measured in the mapgen image (same host):

| run | tests | wall |
| --- | --- | --- |
| `tests/dialects/test_dune.py`, full pipeline | 1 | ~5 s |
| `tests/test_parser.py`, plain pytest | 81 | 21.3 s |
| `tests/test_parser.py`, full pipeline | 81 | 95 s (pytest ≈ 91 s) |

`tests.json` records 103.05 s for all 1620 tests and 29.14 s for
`test_parser.py`; scaling the measured emulation factor (21.3/29.1) and
coverage factor (91/21.3 ≈ 4.3) gives **≈ 330 s** for the full traced suite,
plus ~2 s collection and ~2 s generation. Budget 6–10 minutes.

## Deviations / notes for the orchestrator

* **Build context is `tools/mapgen/`, not `.`** — the repo-root
  `.dockerignore` (W4, intentionally) excludes `tools/` and
  `requirements.lock`, so a root-context build cannot see them. The
  `requirements.lock` in this directory is a byte-identical copy of the root
  one (`cmp` verified). If W4 prefers root-context builds, add
  `!requirements.lock` and `!tools/mapgen/` to `.dockerignore`; the Dockerfile
  `COPY` lines are then the only change needed.
* **No pytest-cov** (not in `requirements.lock`): contexts are driven with the
  coverage API directly, which is what `--cov-context=test` does.
  `coverage==7.17.4` is pinned as a build ARG.
* **`usage.json.gz` is the only sound reading of the v1 format**: the spec keys
  by defining file but stores bare line numbers, so cross-file references
  cannot be represented. Values are therefore references *in that file*.
  `usage_refs.json.gz` carries the complete `NAME → {file: lines}` map for
  selectors that want cross-module uses (e.g. `TIMEZONES` in `time.py` used by
  `dialects/dialect.py`). It is additive; ignoring it degrades recall only for
  module/class-level edits whose only uses live in another file. A v2 spec
  entry could fold this into `usage.json.gz`.
* **No module pseudo-symbol** is emitted: the spec's symbol kinds are
  `function|class`, and the selector derives module-level lines from gaps.
* Coverage starts *after* collection, so import-time execution is not
  attributed to a test (that is what the usage map is for). Dialect modules
  imported lazily by the first test that uses them are attributed to it.
* Plumbing artifacts under `/tmp` are disposable; only `maps/` is committed.
  `maps/.gitkeep` is untouched.
