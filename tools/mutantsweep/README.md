# W5 — mutant sweep ground truth

Independent, really-executed ground truth for auditing the selector's recall:
17 event-style mutants against the pinned sqlglot commit
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), each with its unified diff and
the *observed* set of failing tests from a full `pytest tests` run in the same
linux/amd64 environment the event scores in (pinned base image + the committed
`requirements.lock`, no tracing, no coverage).

This directory only produces ground truth. It never touches the selector,
`maps/`, `blastradius/`, `bench/` or the sample data.

## Layout

```
tools/mutantsweep/
  fetch_pin.py          copy of the codeload fetcher (pin -> /pristine/sqlglot)
  requirements.lock     byte-identical copy of the repo-root lock
  runner.Dockerfile     python:3.13-slim@sha256:e48f... + lock + pinned tree
  run_in_container.sh   copy pristine -> apply patch -> full pytest -> ground json
  apply_patch.py        pure-python unified-diff applier (slim has no `patch`)
  collect.py            pytest log -> ground/<id>.json (failing node ids, counts)
  generate.py           AST-driven mutation operators -> corpus/<id>.patch
  run_one.sh            run one mutant (or baseline) through docker
  run_all.sh            baseline + all mutants, 3 concurrent, then summarize
  summarize.py          table of id/kind/file/#failing/wall_s
  corpus/<id>.patch     17 unified diffs against the pin (`a/`, `b/` labels)
  corpus/manifest.json  id / kind / group / file / line / summary / sha256
  ground/<id>.json      observed {failing[], passed, failed, skipped, wall_s}
```

## Rerun

```bash
# 1. build the runner image (network needed only here)
docker build --platform linux/amd64 -t blast-mutantsweep \
    -f tools/mutantsweep/runner.Dockerfile tools/mutantsweep

# 2. baseline + every mutant (baseline first, then 3 docker runs at a time)
cd tools/mutantsweep && ./run_all.sh        # FORCE=1 to redo, JOBS=3 default

# 3. one mutant / the baseline
./run_one.sh baseline
./run_one.sh andor_simplify
```

The baseline must report **1620 passed, 1 skipped**; `summarize.py` checks it.
A run is ~250 s emulated on Apple silicon; the whole sweep is ~25 min at 3-way
parallelism. `ground/<id>.json` is written from inside the container, so the
`wall_s` value is the pytest process wall clock, not host scheduling noise.

## Regenerate the corpus

The generator fetches nothing itself; point it at a pristine pinned checkout
(any copy made with `fetch_pin.py`, or `/work/sqlglot` inside `blast-mapgen`):

```bash
python3 tools/mutantsweep/fetch_pin.py eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/pin
python3 tools/mutantsweep/generate.py --checkout /tmp/pin \
    --out tools/mutantsweep/corpus --work /tmp/mutwork
```

Every mutant in `generate.py` is a `(file, line, operator)` site. The operator
locates the syntax node with `ast` (or the token stream) and rewrites exactly
one token; the generator then re-parses the result, asserts the site produced
exactly one candidate, writes the mutated file, produces the patch with
`diff -u --label a/<rel> --label b/<rel>`, and finally re-applies the patch to
a fresh copy of the pristine file and byte-compares. The comment control is
additionally checked with `ast.dump` equality.

## Corpus

| group | operator | mutants |
| --- | --- | --- |
| a | `and` <-> `or` in method-body conditions | `andor_simplify`, `andor_bq_parser` |
| b | `==` <-> `!=`, `is` <-> `is not` | `eqne_oracle_gen`, `isnone_tsql_gen` |
| c | class-body literal nudges (int / string / metadata attr) | `classlit_hive_regexp_group`, `classlit_duckdb_sample`, `classlit_typing_null` |
| d | arg_types / tokenizer / parser table entries | `argtypes_groupconcat`, `tokenizer_oracle_neq`, `parsertbl_presto_datediff` |
| e | optimizer module condition flips | `optcond_optimize_joins`, `optcond_pushdown_pos` |
| f | fixture `.sql` single-line edits | `fixture_eliminate_joins`, `fixture_qualify_columns`, `fixture_identity_cast` |
| g | controls (comment-only, enum `is` -> `==`) | `control_comment_oracle`, `control_enum_dialect` |

Files are spread over `sqlglot/{dialects,generators,parsers,optimizer,expressions,typing}`
plus three fixtures (`tests/fixtures/optimizer/{eliminate_joins,qualify_columns}.sql`,
`tests/fixtures/identity.sql`). A mutant that breaks nothing is a legitimate
data point; patches that fail to apply are bugs and must be regenerated.

## Consuming the ground truth

`ground/<id>.json` `failing` holds pytest node ids (the same ids as
`maps/tests_index.json` / `sample/tests.json`). Compare them with the
selector's `workers` for the same patch: any id in `failing` that is not
selected is a +300 s miss in the real scoring. `failing` is observed, not
derived, so it is safe to use as an audit oracle.
