# Blast Radius — architecture contract (v1)

Shared contract for all workstreams. Read this before touching code.
Orchestrator owns this file; workers propose changes via review, never edit it directly.

## Mission recap

For each patch (unified diff against pinned sqlglot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`),
print `{"commit": id, "workers": [[test numbers...], ...]}`. Cost per commit =
`max(worker loads) + 2s × occupied workers`; a miss (any failing test not selected) = +300 s.
Recall dominates. Selector runs in ~1 s CPU total for the whole batch: it never runs sqlglot
tests; the scorer only simulates.

Baseline (starter, runs all 1620 tests on 3 workers): **1210.47 s, 0 misses**.

## Approach (what we build)

No patch→tests lookup tables (forbidden). Instead, precompute from the pinned commit:

1. **coverage map** — per test id: which sqlglot source lines it executes (per-test contexts).
2. **symbol table** — per source file: function/class ranges (AST line spans, 1-based inclusive).
3. **name usage map** — for every module-level / class-body assigned name (e.g.
   `WEEK_OFFSET`, `EXPRESSION_METADATA`): the source lines that reference that name anywhere
   in `sqlglot/`. Covers import-time/class-body edits, which per-test line coverage cannot see
   because module import happens before any test runs.
4. **fixture map** — every non-.py file under the pinned checkout → test ids that read it
   during their run (open()/os.open/Path trace, per-test; module-scope reads attributed to all
   tests of that test file).
5. **test-file map** — tests/*.py → test ids defined in it; special files (conftest.py,
   tests/helpers.py, anything imported by many test files) flagged.
6. **pinned source tarball** — the `sqlglot/` package .py files (NOT tests/), used at runtime
   for AST-equality comment-neutrality checks.
7. **dialect-exposure map** — per test id: the set of sqlglot dialects it runs code under
   (trace Dialect instantiation/get_or_raise/"dialect=" use during each test; the default
   dialect recorded as "default"). Plus a per-file/symbol dialect scope derived statically
   (sqlglot/dialects/snowflake.py, generators/clickhouse.py, class Snowflake(Dialect) →
   "snowflake"; non-dialect files → scope none).
8. **key-read map + usage-kind table** — for module-level and class-body dict/list/tuple/set
   constants: per test id, which (constant name, key/index) pairs it actually reads, captured
   by replacing those constants at import time with logging proxy subclasses. CRITICAL SAFETY
   CLASSIFICATION per constant name (from static AST of all its uses in sqlglot/): "keyable"
   iff every use is Subscript/.get()/.__contains__ style; "opaque" if any use expands/copies/
   iterates it (`{**X}`, `dict(X)`, `X.copy()`, `for k in X`, `X | Y`, `list(X)` …) — proxies
   cannot see protocol-level reads, so opaque constants must NEVER be narrowed on.
9. **test-file import map** — per tests/*.py: the sqlglot modules its top-level imports
   reach, statically (transitive through the sqlglot package's own imports).

Selection rules (v1, pure functions of the above maps — no patch-specific state):

- Changed files are partitioned: `SQLGLOT` (sqlglot/**.py), `TESTPY` (tests/**.py),
  `FIXTURE` (any other file under tests/), `OTHER` (docs, README, benchmarks, …).
- Commit is **neutral** iff every hunk in every file only adds/removes blank or `#` comment
  lines (verified: reconstruct the patched file in memory, `ast.parse` both pinned and patched,
  compare `ast.dump`; reject if a coding declaration `coding[:=]` appears in the first two
  lines) → output `[]`. Sample ground truth: comment-only commits break nothing.
- `FIXTURE` file → tests that read it (fixture map). The fixture map records ALL files under
  the checkout that tests read, including non-test .py files (e.g. tests/fixtures/**.py) — a
  tests/**.py that defines no numbered tests is treated as a fixture, not a test file.
  Unknown fixture → full suite (Tier E).
- `TESTPY` file → all its tests; conftest/helpers-style files → full suite.
- `SQLGLOT` hunk with old-line span S (old range of the hunk, incl. context lines):
  - select tests covering any line of every symbol (function/class) whose range intersects S;
  - **dialect narrowing** (applies to the symbol-level selection above when the changed
    symbol's file has a dialect scope D, and to L1 below): intersect with tests whose dialect
    exposure includes D. Tests not exposed to D cannot be affected by D-scoped code. If the
    exposure trace shows the default dialect only for a non-scoped file, no narrowing.
  - class-body or module-level lines in S (outside method/function bodies): the **L1/L2/L3
    ladder**:
      - L1 (always): tests covering the usage lines (name-reference map) of every name
        assigned on those lines, ∩ dialect narrowing when the file has a dialect scope.
      - L3 (optional, ONLY when ALL hold, evaluated in calibration before enabling): the
        changed line is a dict/tuple/list literal entry; the constant's usage-kind is
        "keyable"; the changed key/index was read by ZERO tests; the SAME constant has other
        keys read by ≥ 5 tests (proxy proven alive). Then drop L1 to just the tests reading
        any key of that constant (or to empty if none) — the mutation is unreachable.
  - a symbol spanning the whole file (module) → treat as module-level change.
  - changed old-lines that intersect top-level Import/ImportFrom statements, or the file is
    sqlglot/__init__.py, or the file is imported transitively by > 80% of test files AND the
    change is module-level → full suite (import-graph uncertainty is unbounded).
- Unknown .py under sqlglot/, or hunk old-lines beyond the pinned file length → full suite.
- If the commit has no changed old lines at all (pure additions incl. new files) → only the
  anchor-line rules apply; brand-new files select nothing.
- Always: union all files' selections; dedupe; drop tests not in tests.json.
- Worker split: for k in 1..8, LPT (longest-first, least-loaded) pack; cost = maxload + 2k;
  pick min cost, ties → smaller k. Empty selection → `[]`.

Fallback ladder (Tier E = full suite): unknown/risky shapes. It costs ~29 s; a miss costs 300 s.

## Data formats (committed under `maps/`, all readable by `nobody`, mode 644)

- `maps/tests_index.json` — `{"test_ids": ["tests/test_parser.py::TestParser::test_union", ...]}`
  Ordinal = position in this list. Built from the pinned commit's test list (same set as
  sample/tests.json ids; ids are the stable key, numbers are per-run).
- `maps/coverage.bin.gz` — gzip. Decompressed layout, all big-endian:
  `b"BRcov1"`, `u32 nfiles`, then nfiles × (`u16 namelen`, name utf-8, `u64 offset`), then per
  file at its offset: `u32 ntests`, then ntests × (`u32 ordinal`, `u32 nintervals`,
  nintervals × (`u32 start_line`, `u32 run_len`)) with 1-based inclusive runs, sorted by line.
- `maps/symbols.json.gz` — `{"sqlglot/...": [["qualname", start, end, "function"|"class"], ...]}`.
- `maps/usage.json.gz` — `{"sqlglot/...": {"NAME": [line, ...]}}` for every name assigned at
  module or class body level: lines anywhere in sqlglot/ referencing `NAME` (Name.id or
  Attribute.attr equal to NAME).
- `maps/fixtures.json.gz` — `{"tests/fixtures/...": ["test id", ...]}`,
  plus `"__test_files__": {"tests/test_x.py": ["test id", ...]}` and
  `"__shared__": ["tests/helpers.py", "conftest.py", ...]` (import-shared test support files).
- `maps/dialects.json.gz` — `{"exposure": {"test id": ["snowflake", ...]}, "file_scope":
  {"sqlglot/dialects/snowflake.py": "snowflake", ...}}` (missing file → no scope).
- `maps/keyreads.json.gz` — `{"exposure": {"test id": [["NAME", "KEY"], ...]},
  "usage_kind": {"sqlglot/...": {"NAME": "keyable"|"opaque"}}`.
- `maps/testimports.json.gz` — `{"tests/test_x.py": ["sqlglot.module.path", ...]}` — the
  transitive sqlglot modules a test file's top-level imports reach.
- `maps/source.tar.gz` — tarball of `sqlglot/**/*.py` at the pin (member names as repo-relative
  paths). Read whole into memory via `tarfile` only when a comment-neutrality check is needed.
- `maps/MANIFEST.json` — generation metadata: pinned sha, generator git state, per-file sha1s
  of sqlglot sources at the pin (integrity; selector verifies hunk context lines optionally).

Repo budget: `maps/` + source tarball together ≤ 8 MB. Whole repo ≤ 20 MB hard limit.

## File ownership (workers may create/edit ONLY their files)

- W1 mapgen: `tools/mapgen/**`, `maps/**` (outputs), `MAPS_SPEC` proposals via report.
- W2 selector: `selector.py`, `blastradius/**`, `ourtests/**` (its unit tests).
- W3 bench: `bench/**`.
- W4 build: `Dockerfile`, `.dockerignore`, `Makefile`, `README.md` (approach section), `scripts/**`.
- Orchestrator (me): git commits/pushes, `AGENTS.md`, `MAPS_SPEC.md`, scoring loop, submission.

Nobody edits: `sample/**`, `score.py`, `SCORING.md`, `AGENTS.md`, `.buildathon_session`
(never read, print, or commit that token).

## Environment parity for map generation (W1)

Docker `--platform linux/amd64` (host is arm64; expect emulation slowness). Base image:
`python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`
(same digest the scorer uses — its Python/SQLite decide which sqlglot tests run).
Install `pip install -r requirements.lock coverage` (PyPI allowed at build). Clone sqlglot at
the pin into the image (network allowed at build). Run tests exactly like the labelers:
`SKIP_INTEGRATION=0`, checkout on `PYTHONPATH`, cwd = checkout root. Per-test coverage via
coverage contexts; fixture reads via a conftest plugin that wraps open and records
per-test paths. Acceptance for W1 plumbing test: run the whole pipeline on ONE small test
file (e.g. `tests/dialects/test_dune.py`) and produce valid maps for it in minutes.

## Non-negotiables

- selector.py: stdlib only, stdout = answer lines only, all logs to stderr, never crash on a
  malformed patch (per-commit try/except → Tier E), < ~1 s CPU for 50 commits, work under
  user `nobody` + read-only disk + no network.
- No answer tables keyed by patches; maps derive only from the pinned commit.
- Every change validated by me with `make sample` before commit.
