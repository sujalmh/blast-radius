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

That single pytest session produces **all** maps: coverage, symbols, usage,
fixtures, source tarball **and** the three dynamic maps
(`dialects.json.gz`, `keyreads.json.gz`, `testimports.json.gz`). There is no
second pytest pass. Inside `run_maps.sh` the session is: collect → traced
pytest (parent + spawned workers) → `coverage combine` → `gen_maps.py`.

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

`inspect_maps.py` validates the bundle (gzip/JSON readable, ids in the index,
paths in `source.tar.gz`, `usage_kind` kinds, non-empty module lists) and exits
non-zero on any error.

`run_maps.sh [out-dir] [pytest targets…]` runs the same pipeline as the full
run, so plumbing and production differ only in the pytest target.

Pool-proof plumbing run (spawned workers, ~13 s): the fixture-driven optimizer
tests execute their cases in a `ProcessPoolExecutor`, so before v2 the map only
had import-time `def` lines:

```sh
mkdir -p /tmp/br1c
docker run --rm --platform linux/amd64 -v /tmp/br1c:/out blast-mapgen /out \
  tests/test_optimizer.py::TestOptimizer::test_eliminate_joins
python3 bench/probe.py --maps /tmp/br1c cover-lines \
  sqlglot/optimizer/eliminate_joins.py 62 74     # -> 61-65,67,69-74
```

## Stages

1. **Build** – `fetch_pin.py` downloads `codeload.github.com/…/tar.gz/<sha>`
   (the README allows sqlglot at the pinned commit) into `/work/sqlglot`;
   `requirements.lock` + `coverage==7.17.4` are pip-installed.
   `br_trace_boot.py` + `zz_blastradius_trace.pth` are installed into
   site-packages (inert unless `BR_CHILD_TRACE=1`) and
   `br_coverage.coveragerc` is copied to `/work/`.
2. **Collect** – `pytest --collect-only -q tests` → `/work/collect.txt`
   (test ids, deterministic file order).
3. **Trace + coverage + dynamic maps** – `pytest -p trace_conftest` with
   `SKIP_INTEGRATION=0`, `PYTHONPATH=/work/sqlglot`, cwd `/work/sqlglot`:
   * `trace_conftest.py` is copied into the checkout root at run time (never
     baked) and wraps `builtins.open`, `io.open`, `codecs.open`, `os.open`,
     `pathlib.Path.open/read_text/read_bytes`. Reads are attributed to the
     running test's node id; reads outside a test go to
     `module:<tests/file.py>` (all test ids of that file) or `__unknown__`
     (logged). Write modes are ignored.
   * The same plugin starts `coverage.Coverage(source=["sqlglot"],
     data_suffix=True)` after collection and calls `switch_context(nodeid)`
     around each test protocol, i.e. `--cov-context=test` semantics without a
     pytest-cov dependency. Data is written in **parallel mode**
     (`/work/.coverage.<pid>.<rand>`), one sqlite context per test node id.
   * **Dialect exposure**: `Dialect.get_or_raise` (the resolver behind
     `parse_one` / `transpile` / `expr.sql(dialect=…)`), `Dialect.get` and
     `Dialect.__init__` are wrapped; the resolved *registered* key comes from
     the `Dialect._classes` reverse map (`TSQL` → `"tsql"`), the base class is
     `"default"`. Fixture `# dialect: x` headers go through the same path
     (`Validator.parse_one(read=self.dialect)`). Resolutions outside a test
     land in the same `module:<tests/file.py>` buckets as fixture reads.
   * **Key reads**: a `sys.meta_path` hook wraps `sqlglot.*` module loaders and,
     right after each module executes, replaces module-level and class-body
     `dict`/`list`/`tuple`/`set`/`frozenset` values with transparent proxy
     subclasses that log `(test, NAME, key)` on `__getitem__` / `get` /
     `__contains__`. Because patching happens at import time, import timing
     (and therefore the coverage map) is unchanged. Copies/deepcopies degrade
     to plain containers. `BR_KEYREADS=0` / `BR_DIALECTS=0` disable either
     trace; `BR_KEYREADS_SCOPE=core` proxies only the highest-value tables
     (kept as a speed escape hatch; not needed, see timing below).
   * Both traces are written to `/work/dynamic.json` (`/work/trace_reads.json`
     keeps the fixture file reads). `gen_maps.py` post-processes them.
   * **Subprocess coverage (v2, bug 1)**: `tests/test_optimizer.py` and
     `tests/test_executor.py` run every fixture-driven case in a
     `ProcessPoolExecutor(mp_context=get_context("spawn"))`. The plugin
     monkeypatches `ProcessPoolExecutor.submit` so every task becomes
     `submit(_br_task, current_test_id, fn, …)`; `_br_task` (module-level, so
     spawn can pickle it by qualname) sets the worker's current test, calls
     `Coverage.current().switch_context(test_id)`, runs `fn`, `pickle.dumps` the
     result inside that context (so `Expression.__reduce__` → `serde.dump` is
     covered; the pool's own later pickling is outside it), and the worker
     writes its dialect/key trace to `/work/dynamic.json.<pid>.child.json`
     (plus a line in `/work/dynamic.json.children.log`). Workers are
     instrumented from interpreter startup by `br_trace_boot.py`, installed
     into site-packages with a `zz_blastradius_trace.pth`; it is a no-op unless
     `BR_CHILD_TRACE=1` (set by `trace_conftest` in the pytest parent, inherited
     by workers), in which case it also calls `coverage.process_startup()` using
     `COVERAGE_PROCESS_START=/work/br_coverage.coveragerc` (`parallel = true`,
     `source = sqlglot`, `data_file = /work/.coverage`).
   * `run_maps.sh` then runs `coverage combine --data-file=/work/.coverage
     /work /work/sqlglot` (**fails loudly** if there are no parallel data files
     or no contexts) before generation, so parent + workers end up in one
     `.coverage` with per-test contexts. `gen_maps.py` merges
     `dynamic.json` with every `dynamic.json.*.child.json` automatically.
4. **Generate** – `gen_maps.py` parses the coverage sqlite itself
   (`file`, `context`, `line_bits` + `coverage.numbits.numbits_to_nums`), walks
   the pinned ASTs, and writes every file below. Gzip is deterministic
   (`mtime=0`, no embedded filename) and JSON is sorted/compact; set-union
   merging of worker traces is order-independent, so two runs are byte-identical
   (verified by sha256 on the plumbing target).

## Outputs

| file | contents |
| --- | --- |
| `tests_index.json` | `{"test_ids": [...]}` in collection order; ordinal = position |
| `coverage.bin.gz` | `BRcov1` binary, per-file per-ordinal 1-based inclusive runs |
| `symbols.json.gz` | `{path: [[qualname, start, end, "function"\|"class"]]}` |
| `usage.json.gz` | `{path: {NAME: [same-file reference lines]}}` — `Name.id` **and** `Attribute.attr` matches, definition lines excluded |
| `usage_refs.json.gz` | `{NAME: {referencing file: [lines]}}` — same v2 reference rule, all files (see deviations) |
| `fixtures.json.gz` | `{non-.py file: [test ids]}` + `__test_files__` + `__shared__` |
| `dialects.json.gz` | `{"exposure": {test: [dialect keys]}, "file_scope": {path: key}, "class_scope": {path: {Class: key}}}` |
| `keyreads.json.gz` | `{"exposure": {test: [[NAME, KEY], …]}, "usage_kind": {path: {NAME: "keyable"\|"opaque"}}}` |
| `testimports.json.gz` | `{tests/test_x.py: [sqlglot.module, …]}`, static transitive top-level imports |
| `source.tar.gz` | `sqlglot/**/*.py` only, repo-relative member names |
| `MANIFEST.json` | pinned sha, generator/version metadata, per-file sha1, `keyreads` stats |

`__shared__` = test-support files imported by ≥2 test files (e.g.
`tests/helpers.py`, `tests/dialects/test_dialect.py`) plus any `conftest.py`.

### dialects.json.gz

`exposure[test]` is the set of dialect keys the test resolved, plus `"default"`
for the base dialect; a test that resolved nothing at all also gets
`["default"]` (conservative: it can then never be narrowed away by a
dialect-scoped change). `file_scope` is static: `sqlglot/dialects/<name>.py`,
`sqlglot/generators/<name>.py`, `sqlglot/parsers/<name>.py` → the registered
key from `sqlglot/dialects/__init__.py:DIALECTS` (so `python.py`, `base.py`,
`dialect.py`, `__init__.py` are absent). `class_scope` is an additive
extension: the dialect classes defined in that file (`{"Snowflake":
"snowflake"}`) so a symbol-level selector can scope a class even when the file
also contains helpers.

### keyreads.json.gz

`exposure[test]` lists `[NAME, KEY]` pairs the test actually read. `NAME` is
the bare module-level name, or `Class.NAME` for class bodies (`Dialect.
Tokenizer.KEYWORDS` for nested classes); `KEY` is `str` for string keys,
`repr`-style for anything else (`"1"` for `1`, `"slice(1, 2, None)"` for a
slice, `"<DType.INT: …>"` for enum members). Reads with no current test
(import time) are dropped — they are not attributable to a test.

`usage_kind[path][NAME]` is the safety table. A name is `keyable` only when
**every** reference to it anywhere in `sqlglot/` is one of `X[k]` (load),
`X.get(k)` / `X.__contains__(k)` (called), or `k in X`; any other use
(iteration, `{**X}`, `dict(X)`, `X.copy()`, `.items()/.keys()/.values()`,
`len(X)`, truthiness, comparison, being passed to a call, `.pop/.setdefault`,
assignment aliasing, deletion) makes it `opaque`. Names observed at runtime but
not statically located (e.g. `Class.required_args`, computed by
`__init_subclass__`) are `opaque`. Only `keyable` names may ever be narrowed on.

### testimports.json.gz

Static closure over **module-level** imports only: `tests/x.py` top-level
`import`/`from` statements → sqlglot modules, then each sqlglot module's own
module-level imports (relative imports resolved), stopping at the sqlglot
package boundary. Imports inside function bodies (lazy/`if TYPE_CHECKING:`
style deferred imports inside functions) are not followed.


## Sizes and timing (measured on this host, linux/amd64 under Rosetta)

Plumbing run `tests/test_parser.py` (81 tests):

* coverage: 109 files, 855 (file, test) pairs, 33.6 KB gzip
* symbols 53 KB · usage 28 KB · usage_refs 68 KB · source 705 KB
* keyreads: 26 799 (test, NAME, KEY) pairs, ≈ 120 KB gzip
* full test_parser map bundle: **≈ 1.0 MB total**

Mixed plumbing run
`tests/dialects/test_dune.py tests/dialects/test_snowflake.py tests/test_jsonpath.py`
(123 tests):

* coverage 55 KB · symbols 53 KB · usage 28 KB · refs 68 KB · fixtures 1 KB
* dialects 2.4 KB · keyreads 207 KB (**49 577 pairs**) · testimports 0.4 KB
* total 1.14 MB

Extrapolated full suite (1620 tests): coverage ≈ **0.7 MB gzip**, keyreads ≈
**2.5–3 MB**, fixtures ≈ 0.1–0.3 MB → **≈ 4–4.5 MB total**, inside the 8 MB
budget (the repo limit is 20 MB).

Timing measured in the mapgen image (same host, linux/amd64 under Rosetta, in
one container so docker start-up does not dominate):

| run | plain | traced (v1: parent only) | traced (v2: + subprocess) |
| --- | --- | --- | --- |
| 123-test mixed set | 3.65 s | — | 4.39 s |
| `tests/test_optimizer.py::…::test_eliminate_joins` | 3.52 s | 3.6 s | 4.08 s |
| `tests/test_optimizer.py` (167 tests, 6 082 subtests, pool-driven) | 38.75 s | — | 44.65 s |
| `tests/test_executor.py` (60 tests, 426 subtests, pool per test) | 34.08 s | — | 48.33 s |

So subprocess tracing costs **1.15×** on the big optimizer file, **1.42×** on
`test_executor.py` (it builds a fresh 10-worker pool per test, so every worker
pays the pytest+coverage import), and **~1.16–1.20×** on small sets. In absolute
terms the two pool files add ≈ **20 s** to the suite. Proxying *all* constants
stays under the 1.5× threshold, so nothing is skipped by default
(`MANIFEST.json: keyreads.skipped == []`); `BR_KEYREADS_SCOPE=core` remains an
escape hatch (proxies only Tokenizer/Parser/Generator/`*_MAPPING`/`arg_types`-style
tables and records what it skipped). Full traced suite therefore still ≈
**8–10 minutes** (v1 measured 6–10), plus ~2 s collection, ~1–2 s combine and
~2 s generation.

Note: the parent-side coverage startup and `coverage combine` are cheap; the
real v2 cost is the extra spawn workers' instrumentation, which only exists for
`tests/test_optimizer.py` and `tests/test_executor.py`.

## Deviations / notes for the orchestrator

* **Fixture reads stay parent-side**: `check_file`/`_mp_execute` enumerate
  fixture pairs in the parent and only the per-case work is submitted, so
  `fixtures.json.gz` is complete. Workers do not install the `open()` wrappers
  (a hypothetical future test that reads fixtures inside a task would be
  invisible to the fixture map; none does today).
* **`bench/probe.py usage NAME` reads only `usage.json.gz`**, i.e. same-file
  references. Cross-file proof for a name (e.g. `WEEK_OFFSET`) must be read from
  `maps/usage_refs.json.gz` (`{"NAME": {"file": [lines]}}`); the selector's
  `references_of()` already unions both maps, so selection is unaffected. The
  v2 reference rule (Name **and** Attribute matches, definition lines excluded)
  applies identically to both files.
* **Definition lines are excluded from reference sets** (v2, bug 2): a
  module/class-body assignment of `NAME` is not a *use* of `NAME`; keeping it
  only added first-importer noise (`WEEK_OFFSET = 0` at `dialects/tsql.py:17`).
  The definition lines remain in `symbols`/coverage data as usual.
* **Coverage data is parallel-mode** (`.coverage.<pid>.<rand>`) and only exists
  as a single file after `coverage combine`; `run_maps.sh` fails if combine sees
  no data files or produces no contexts. Child import-time execution lands in
  the empty context and is dropped by `gen_maps.py` (`import_time=` counter),
  exactly like the parent's.
* **`<dynamic>.children.log`** lists every worker that flushed a trace
  (`<pid> tasks=<n>`); pytest's fd-capture hides worker stderr while tests run,
  so this file is the audit trail. `gen_maps.py` merges
  `<dynamic>.*.child.json` and records `MANIFEST.capture` (`version: 2`,
  `dynamic_parents`, `dynamic_children`).
* **`inspect_maps.py` also validates the v2 usage rule** for bundles whose
  manifest says `capture.version >= 2`: it re-parses `source.tar.gz` and fails
  if any `usage`/`usage_refs` entry contains one of the name's own definition
  lines (`--name WEEK_OFFSET` prints that name's cross-file references).
* **Build context is `tools/mapgen/`, not `.`** — the repo-root
  `.dockerignore` (W4, intentionally) excludes `tools/` and
  `requirements.lock`, so a root-context build cannot see them. The
  `requirements.lock` in this directory is a byte-identical copy of the root
  one (`cmp` verified). If W4 prefers root-context builds, add
  `!requirements.lock` and `!tools/mapgen/` to `.dockerignore`; the Dockerfile
  `COPY` lines are then the only change needed.
* **No pytest-cov** (not in `requirements.lock`): contexts are driven with the
  coverage API directly, which is what `--cov-context=test` does.
  `coverage==7.17.4` is pinned as a build ARG. The `.pth`/`br_trace_boot.py`
  shim lives in site-packages; it is inert unless `BR_CHILD_TRACE=1`.
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
* **Coverage starts *after* collection**, so import-time execution is not
  attributed to a test (that is what the usage map is for). Dialect modules
  imported lazily by the first test that uses them are attributed to it.
* **Dialect exposure is per test, not per phase**: `exposure` is a set, so a
  test that needs `"default"` for one assertion and `"duckdb"` for another
  contains both; narrowing tests on either is therefore sound (union).
  Module-scope dialect resolutions (e.g. a test module that parses at import
  time) are expanded to all tests of that file, mirroring the fixture rules.
* **`class_scope` is an additive key** in `dialects.json.gz` (MAPS_SPEC lists
  only `exposure` + `file_scope`). `file_scope` alone cannot express a file
  that defines more than one dialect class; `class_scope` can. Ignoring it is
  safe (it only adds precision).
* **`"default"` fallback**: a test with no recorded resolution gets
  `["default"]`. This keeps dialect narrowing from ever dropping a test that
  simply did not touch a dialect.
* **keyreads NAME convention**: module level → `NAME`; class body →
  `Class.NAME` (`Outer.Inner.NAME` for nested classes). The module is *not*
  in the name, so two classes with the same qualname in different modules share
  a name; `usage_kind` is still per file, and the selector must treat a name as
  keyable only if **every** file that defines it says `keyable`.
* **keyread keys are strings**: ints/None/enum members/slices are `repr`ed
  (`"1"`, `"None"`, `"<TokenType.SELECT: 'SELECT'>"`, `"slice(1, 2, None)"`).
  The same textual form is used for both sides of the L3 comparison.
* **key reads outside a test are dropped** (a sink buckets them and they are
  never written). Import-time reads are not attributable to a test, and
  attributing them to whichever test module happened to be collecting would
  only add false positives. Dialect resolutions *are* kept in module buckets
  because the spec asks for fixture-like expansion there.
* **Opaque-by-construction names**: proxy-created copies (`copy`/`deepcopy`
  return plain containers) and runtime-computed class attributes such as
  `Expr.required_args` cannot be followed statically; any such name that shows
  up in the trace is classified `opaque` in `gen_maps.py` and counted as
  `unclassified` in the log/MANIFEST.
* Plumbing artifacts under `/tmp` are disposable; only `maps/` is committed.
  `maps/.gitkeep` is untouched.
