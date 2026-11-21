# bench — calibration harness (W3)

Dev-only. Nothing here is imported by `selector.py`; the image never ships it.
It exists so rule variants can be A/B'd against `sample/answers.json` in
milliseconds instead of via `make sample` (a linux/amd64 Docker build), and so
map quality can be interrogated ad hoc.

```
bench/
  calibrate.py        A/B variants, per-commit table, totals, provenance
  probe.py            ad-hoc map queries (symbols / cover-lines / fixture / hunk / usage)
  run_synthetic.py    generalization guard over bench/synthetic/
  adapter.py          prefers W2's blastradius package, falls back to the reference engine
  engine.py           reference v1 rules + knobs + LPT worker split
  mapio.py            MAPS_SPEC format reader (own reader, independent of W2)
  patch.py            unified-diff parsing + in-memory application (neutrality check)
  scoring.py          bridge to score.py's evaluate_run + table/row assembly
  mockmaps/           MAPS_SPEC-formatted *mock* map set (hand-authored fiction)
  mockdata/           tiny patch set + tests.json + answers.json for the mock world
  synthetic/          10 patches against pinned sqlglot + qualitative expectations.json
  tests/              pytest for scoring math, table assembly, rules, adapter degradation
```

## Quickstart

```sh
# harness self-test before W1/W2 land (mock world, ~0.1 s)
python3 bench/calibrate.py --maps bench/mockmaps --data bench/mockdata --variants "A=v1; B=line+no-usage"

# the real loop once maps/ exists
python3 bench/calibrate.py --maps maps --data sample
python3 bench/calibrate.py --maps maps --data sample --variants "v1; line; no-usage; no-neutral; pad2; tier-e-empty"

# one knob at a time
python3 bench/calibrate.py --usage off
python3 bench/calibrate.py --pad 2 --tier-e empty

# synthetic guard (auto-uses maps/ if built, else bench/mockmaps)
python3 bench/run_synthetic.py -v

# unit tests
python3 -m pytest bench -q
```

`calibrate.py` prints one block per variant: a per-commit table (`selected`,
`expected` failing, covered-all-failing?, cost in simulated seconds, which rules
contributed), then totals (`simulated`, misses, missed commit ids), then the
provenance breakdown (`rule -> tests contributed / failing tests it caught`).
`--json FILE` writes the rows for scripting. Scoring is delegated to
`score.py`'s `evaluate_run` per commit, so the numbers match `make sample`
exactly (CPU seconds excluded).

## Variant grammar (`--variants`)

`;` separates variants; each is `name` or `name=spec`.

| spec | meaning |
| --- | --- |
| `v1`, `line`, `no-usage`, `no-classbody`, `no-neutral`, `tier-e-empty`, `pad2`, … | presets |
| `line+no-usage`, `pad2+tier-e-empty` | presets combined |
| `symbol,on,off,on,0,full` | positional: `symbol_vs_line, classbody, usage, neutral, pad, tier_e` |
| `{"pad": 2, "tier_e": "empty"}` | JSON overrides |

Flags (`--symbol-vs-line`, `--classbody`, `--usage`, `--neutral`, `--pad`,
`--tier-e`) set the base every variant starts from, so `--pad 2 --variants
"pad0; pad2"` A/Bs the base pad against 2.

Engine selection: `--engine auto` (default) runs each variant on `blastradius`
when it can (W2's `select_commit` for the v1 defaults) and falls back to the
bench reference engine for knob variants, printing a note; `--engine reference`
forces the bench rules; `--engine blastradius` fails cleanly if W2 is not ready.
A variant that the backend cannot run (signature mismatch, unsupported knob,
exception) is printed as `SKIPPED: …` and never crashes the run.

`run_synthetic.py` defaults to `--engine reference` because its expectations
assert *which rule fired*; W2 currently exposes rule hits without per-rule test
ids, so those checks would skip under `--engine blastradius`.

## The mock world (honest version)

`bench/mockmaps/` is **fiction**: 12 fake test ids, invented coverage
intervals, invented name-usage lines, invented fixture/test-file maps, and a
mock `MANIFEST.json` (`"source": "mock"`). No real coverage data and no
patch→test knowledge is copied into it. It is deliberately shaped so each rule
matters: e.g. the module-level `DATE_UNITS` assignment (line 466) is covered by
no test while a nearby reference line (475) is, so `--usage off` produces a real
miss — that is the A/B demo.

The one real ingredient is `mockmaps/source.tar.gz`: the four pinned files the
synthetic patches touch, needed to exercise comment-neutrality (reconstruct the
patched file, compare `ast.dump`) and hunk-beyond-EOF. Rebuild everything with
`python3 bench/mockmaps/build.py --pin /path/to/sqlglot@pin`; without `--pin` the
existing tarball is reused and only the JSON/binary maps and mock data are
rewritten.

`bench/mockdata/` is the matching tiny calibration set (4 patches, fictional
answers). `bench/synthetic/checks` never assert exact failing-test sets; they
assert structure (Tier E or not, empty or not, which rule fired) and, only when
real maps are in use, that a parser-only change touches parser/bigquery tests.

## What W3 needs from W2 (`blastradius`)

Status: W2's package is detected and wired in two ways.

- **Native (works today).** `bench.adapter.BlastRadiusNativeBackend` drives
  `blastradius.patch.parse_patch`, `blastradius.maps_io.Maps` and
  `blastradius.select.select_commit(files, maps, TestIndex)`. `--engine auto`
  uses it for the v1-default variant, so calibrate numbers on real maps reflect
  the shipped selector. `bench/mockmaps` is spec-shaped and W2's `Maps` loads it,
  which is how the integration is tested before W1's maps exist.
- **Preferred (one small shim).** A knob-capable entry point and per-rule test
  ids would let every variant and the provenance table run on W2 itself:

```python
# blastradius/bench_adapter.py (proposal)
def bench_adapter(maps_dir):                     # -> adapter object
    ...
    def select(patch_text, knobs: dict):         # knobs: see Knobs.as_dict()
        return {
            "tests": set[str],
            "tier_e": bool,
            "rules": {rule: [test ids]},         # per-rule attribution, not just hits
            "notes": [str],
        }
    def split(tests, selected_ids, max_workers=8):
        return workers, cost_ms                  # optional; LPT is spec-fixed
```

Knobs are passed as
`{"symbol_vs_line": "symbol"|"line", "classbody": bool, "usage": bool,
"neutral": bool, "pad": int, "tier_e": "full"|"empty"}`. Today W2's
`select_commit` takes none, so knob variants print `SKIPPED` under
`--engine blastradius` (or silently switch to the reference engine under
`--engine auto`), and the provenance table says "unavailable" instead of
inventing attribution. Everything else degrades without crashing.

## Deviations / open questions (for the orchestrator)

- **Brand-new file vs unknown source file.** MAPS_SPEC says unknown `.py` under
  `sqlglot/` is Tier E *and* that brand-new files select nothing. The reference
  engine resolves this as: a file with `--- /dev/null` is a pure addition →
  nothing; an *existing* path absent from the maps → Tier E.
- **Deleted source/test files.** Not covered by v1; the reference engine treats
  a deleted `sqlglot/` or `tests/` `.py` file as Tier E (imports/collection can
  break far away) and a deleted fixture as "unknown fixture" → Tier E.
- **Pure insertion in an existing file** (`old_count == 0`): no old span exists,
  so the anchor line (`old_start`, clamped to EOF) selects the innermost symbol
  containing it, without class-body/usage expansion. This is W3's reading of
  "only the anchor-line rules apply".
- **Neutrality** is only *proven* when the pinned source is available; a
  comment-only change to a file outside `source.tar.gz` (e.g. `tests/*.py`) is
  reported `neutral=None` and goes through the normal rules (safe, not free).
  Non-Python, comment-only changes are accepted as neutral per the spec letter.
- **Fixture comment lines**: sqlglot fixtures use `--` comments, which are not
  "`#` comment lines", so fixture comment edits are not neutral; they go through
  the fixture map. Worth confirming with W1/W2 if that costs much.
- **`conftest.py`**: the pinned checkout has no `conftest.py`; the synthetic
  guard uses `tests/helpers.py` (a real `__shared__` member) for the Tier E case.
  A *new* `conftest.py` is currently "new test file" → Tier E.
- `--tier-e empty` exists only to quantify what Tier E buys; it is a calibration
  knob, never a recommended setting.
