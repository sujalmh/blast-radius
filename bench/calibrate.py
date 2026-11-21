#!/usr/bin/env python3
"""A/B selection-rule variants against a real (or mock) data set, no Docker.

Examples
--------
    # default v1 rules on the sample commits
    python3 bench/calibrate.py

    # two variants A/B'd: spec rules vs exact-line coverage without the usage rule
    python3 bench/calibrate.py --variants "A=v1; B=line+no-usage"

    # vary one knob
    python3 bench/calibrate.py --pad 2 --variants "pad0; pad2"

    # smoke-test the harness before W1/W2 land
    python3 bench/calibrate.py --maps bench/mockmaps --data bench/mockdata --variants "A=v1; B=line+no-usage"

Scoring uses `score.py`'s own `evaluate_run`, so the totals are exactly what
`make sample` would print. All harness warnings go to stderr; the tables are on
stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # runnable as `python3 bench/calibrate.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "bench"

from bench.adapter import (  # noqa: E402
    REQUIRED_FROM_W2,
    BackendUnsupported,
    ReferenceBackend,
    probe_blastradius,
)
from bench.engine import Knobs  # noqa: E402
from bench.mapio import MapSet  # noqa: E402
from bench.scoring import (  # noqa: E402
    Row,
    commit_result,
    format_seconds,
    load_score_module,
    provenance_table,
    render_table,
    rule_summary,
)

PRESETS: dict[str, dict] = {
    "v1": {},
    "symbol": {"symbol_vs_line": "symbol"},
    "line": {"symbol_vs_line": "line"},
    "classbody": {"classbody": True},
    "no-classbody": {"classbody": False},
    "usage": {"usage": True},
    "no-usage": {"usage": False},
    "neutral": {"neutral": True},
    "no-neutral": {"neutral": False},
    "tier-e-empty": {"tier_e": "empty"},
    "tier-e-full": {"tier_e": "full"},
    "pad0": {"pad": 0},
    "pad1": {"pad": 1},
    "pad2": {"pad": 2},
    "pad4": {"pad": 4},
}

POSITIONAL_FIELDS = ("symbol_vs_line", "classbody", "usage", "neutral", "pad", "tier_e")


def _on_off(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("on", "true", "1", "yes"):
        return True
    if lowered in ("off", "false", "0", "no"):
        return False
    raise ValueError(f"expected on/off, got {value!r}")


def parse_variants(spec: str | None, base: Knobs) -> list[tuple[str, Knobs]]:
    """`--variants` grammar:

    * `A=v1; B=line+no-usage`          presets, combined with `+`
    * `A=symbol,on,on,on,0,full`       positional: symbol_vs_line,classbody,usage,neutral,pad,tier_e
    * `A={"pad": 2}`                   JSON overrides
    * omitted                          a single variant named `base` from the flags
    """
    if not spec:
        return [("base", base)]
    variants: list[tuple[str, Knobs]] = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, body = chunk.partition("=")
        name = name.strip()
        body = body.strip() or name
        overrides: dict = {}
        if body.startswith("{"):
            overrides = json.loads(body)
            if not isinstance(overrides, dict):
                raise ValueError(f"variant {name}: JSON overrides must be an object")
        elif "," in body:
            fields = [part.strip() for part in body.split(",")]
            if len(fields) != len(POSITIONAL_FIELDS):
                raise ValueError(
                    f"variant {name}: expected {len(POSITIONAL_FIELDS)} positional fields "
                    f"({','.join(POSITIONAL_FIELDS)}), got {len(fields)}"
                )
            for field, value in zip(POSITIONAL_FIELDS, fields):
                if field in ("classbody", "usage", "neutral"):
                    overrides[field] = _on_off(value)
                elif field == "pad":
                    overrides[field] = int(value)
                else:
                    overrides[field] = value
        else:
            for token in body.split("+"):
                token = token.strip()
                if token not in PRESETS:
                    raise ValueError(f"variant {name}: unknown preset {token!r} (known: {', '.join(sorted(PRESETS))})")
                overrides.update(PRESETS[token])
        variants.append((name or "unnamed", base.with_overrides(**overrides)))
    return variants


@dataclass
class DataSet:
    tests: list[dict]
    answers: dict[str, dict]
    patches: dict[str, str]

    @property
    def commits(self) -> list[str]:
        return sorted(self.patches)


def load_data(data_dir: Path, answers_path: Path | None = None, commits_dir: Path | None = None) -> DataSet:
    tests_path = data_dir / "tests.json"
    if not tests_path.is_file():
        raise SystemExit(f"bench/calibrate: {tests_path} not found (--data DIR must hold tests.json)")
    tests = json.loads(tests_path.read_text("utf-8"))["tests"]

    answers_file = answers_path or (data_dir / "answers.json")
    if not answers_file.is_file():
        raise SystemExit(
            f"bench/calibrate: {answers_file} not found; answers are required to score "
            "(pass --answers, or use bench/mockdata for the mock world)"
        )
    answers = json.loads(answers_file.read_text("utf-8"))["commits"]

    patches_dir = commits_dir or (data_dir / "commits")
    if not patches_dir.is_dir():
        raise SystemExit(f"bench/calibrate: {patches_dir} not found (expected <data>/commits/*.patch)")
    patches = {
        path.stem: path.read_text("utf-8", "replace") for path in sorted(patches_dir.glob("*.patch"))
    }
    if not patches:
        raise SystemExit(f"bench/calibrate: no *.patch files under {patches_dir}")

    unknown_answers = sorted(set(answers) - set(patches))
    if unknown_answers:
        print(f"[calibrate] answers without a patch: {unknown_answers}", file=sys.stderr)
    missing_answers = sorted(set(patches) - set(answers))
    if missing_answers:
        print(f"[calibrate] patches without an answer (skipped): {missing_answers}", file=sys.stderr)
    return DataSet(tests=tests, answers=answers, patches=patches)


def run_variant(
    name: str,
    knobs: Knobs,
    *,
    backend,
    data: DataSet,
    score,
    only: set[str] | None = None,
    limit: int | None = None,
    verbose: bool = False,
) -> tuple[list[Row], int]:
    number_by_id = {test["id"]: int(test["number"]) for test in data.tests}
    id_by_number = {int(test["number"]): test["id"] for test in data.tests}
    commits = [commit for commit in data.commits if commit in data.answers]
    if only:
        commits = [commit for commit in commits if commit in only]
    if limit:
        commits = commits[:limit]

    rows: list[Row] = []
    dropped_ids = 0
    for commit in commits:
        selection = backend.select(data.patches[commit], knobs)
        selected_numbers: list[int] = []
        for test_id in sorted(selection.tests):
            number = number_by_id.get(test_id)
            if number is None:
                dropped_ids += 1
                continue
            selected_numbers.append(number)

        failing_numbers = [int(number) for number in data.answers[commit]["failing"]]
        failing_ids = {id_by_number[number] for number in failing_numbers if number in id_by_number}
        workers, _split_cost = backend.split(data.tests, set(selection.tests), max_workers=8)
        result = commit_result(score, commit, failing_numbers, workers, data.tests)
        covered = bool(result["ok"]) and not result["missed"]
        if not result["ok"]:
            print(f"[calibrate] {name}/{commit}: official scorer rejected the line: {result['code']}", file=sys.stderr)

        row = Row(
            commit=commit,
            kind=str(data.answers[commit].get("kind", "?")),
            selected=len(selected_numbers),
            expected=len(failing_numbers),
            covered=covered,
            cost_ms=int(result["simulated_ms"]),
            tier_e=selection.tier_e,
            rules=rule_summary(selection.rules),
            rejected=str(result["code"]) if not result["ok"] else None,
            selected_ids=set(selection.tests),
            failing_ids=failing_ids,
            rule_map=selection.rule_pairs(),
            notes=list(selection.notes),
        )
        rows.append(row)
        if verbose:
            print(
                f"[calibrate] {name}/{commit}: selected={row.selected} failing={row.expected} "
                f"tier_e={row.tier_e} cost={format_seconds(row.cost_ms)}s notes={'; '.join(row.notes) or '-'}",
                file=sys.stderr,
            )
    return rows, dropped_ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A/B blast-radius selection-rule variants and score them with score.py.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--maps", default="maps", help="map set directory (MAPS_SPEC formats)")
    parser.add_argument("--data", default="sample", help="directory holding tests.json, answers.json, commits/")
    parser.add_argument("--answers", default=None, help="override answers.json path")
    parser.add_argument("--commits", default=None, help="override commits/ directory")
    parser.add_argument("--engine", choices=("auto", "reference", "blastradius"), default="auto")
    parser.add_argument("--variants", default=None, help="variant list, e.g. 'A=v1; B=line+no-usage'")
    parser.add_argument("--only", default=None, help="comma-separated commit ids")
    parser.add_argument("--limit", type=int, default=None, help="only the first N commits")
    parser.add_argument("--verbose", "-v", action="store_true", help="per-commit notes on stderr")
    parser.add_argument("--json", dest="json_path", default=None, help="write per-commit rows as JSON")

    # rule knobs (the base variant; --variants can override per variant)
    parser.add_argument("--symbol-vs-line", choices=("symbol", "line"), default="symbol")
    parser.add_argument("--classbody", type=_on_off, default=True)
    parser.add_argument("--usage", type=_on_off, default=True)
    parser.add_argument("--neutral", type=_on_off, default=True)
    parser.add_argument("--pad", type=int, default=0, help="extra context lines around hunk old spans")
    parser.add_argument("--tier-e", choices=("full", "empty"), default="full", dest="tier_e")
    args = parser.parse_args(argv)

    base = Knobs(
        symbol_vs_line=args.symbol_vs_line,
        classbody=args.classbody,
        usage=args.usage,
        neutral=args.neutral,
        pad=args.pad,
        tier_e=args.tier_e,
    )
    try:
        variants = parse_variants(args.variants, base)
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2

    data = load_data(Path(args.data), Path(args.answers) if args.answers else None,
                     Path(args.commits) if args.commits else None)
    maps = MapSet.load(args.maps)
    info = maps.describe()
    print(
        f"[calibrate] maps={info['directory']} tests={info['tests']} files={info['files']} "
        f"mock={info['mock']} loaded={info['loaded']} missing={info['missing']}",
        file=sys.stderr,
    )
    if info["missing"]:
        print(
            "[calibrate] WARNING: incomplete map set; Tier E ('full') becomes an empty selection "
            "where tests_index is missing",
            file=sys.stderr,
        )
    print(
        f"[calibrate] data={args.data} commits={len(data.patches)} answers={len(data.answers)} "
        f"tests={len(data.tests)}",
        file=sys.stderr,
    )

    warn = lambda message: print(f"[calibrate] {message}", file=sys.stderr)

    native = None
    native_caps = None
    if args.engine in ("auto", "blastradius"):
        native, native_caps = probe_blastradius(maps.directory, warn=warn)
        if native is not None:
            print(f"[calibrate] blastradius available: {native_caps.detail}", file=sys.stderr)
        elif args.engine == "blastradius":
            print(f"[calibrate] no usable blastradius engine: {native_caps.detail}", file=sys.stderr)
            print(f"[calibrate] needed from W2: {REQUIRED_FROM_W2}", file=sys.stderr)
            return 2

    reference = ReferenceBackend(maps, warn=warn)
    if native is None:
        print(
            f"[calibrate] using reference engine ({native_caps.detail if native_caps else 'skipped'}); "
            f"needed from W2 for native runs: {REQUIRED_FROM_W2}",
            file=sys.stderr,
        )
    knob_fallback_warned = False

    score = load_score_module()
    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else None
    json_payload: dict[str, list[dict]] = {}
    exit_code = 0
    printed_any = False

    for name, knobs in variants:
        if printed_any:
            print()
        printed_any = True
        if native is not None and knobs == Knobs():
            backend = native
        elif native is not None and args.engine == "auto":
            backend = reference
            if not knob_fallback_warned:
                knob_fallback_warned = True
                print(
                    f"[calibrate] variant {name}: blastradius takes no knob arguments; "
                    "running this variant on the reference engine",
                    file=sys.stderr,
                )
        else:
            backend = native if native is not None else reference
        skipped: str | None = None
        try:
            backend.check_knobs(knobs)
        except BackendUnsupported as error:
            skipped = str(error)
        rows: list[Row] = []
        dropped = 0
        if skipped is None:
            try:
                rows, dropped = run_variant(
                    name,
                    knobs,
                    backend=backend,
                    data=data,
                    score=score,
                    only=only,
                    limit=args.limit,
                    verbose=args.verbose,
                )
            except BackendUnsupported as error:
                skipped = str(error)
            except Exception as error:  # noqa: BLE001 - W2 code is a moving target
                skipped = f"{type(error).__name__}: {error}"
                if args.verbose:
                    traceback.print_exc()

        print(
            render_table(
                name,
                engine_name=backend.name,
                knobs_text=knobs.describe(),
                rows=rows,
                skipped=skipped,
                missing_ids=dropped,
            )
        )
        json_payload[name] = [row.as_dict() for row in rows]

        if skipped is not None:
            exit_code = 1
            continue
        if backend.has_provenance is False:
            print("PROVENANCE unavailable from this backend (it exposes rule hits, not per-rule test ids)")
            print(
                "[calibrate] provenance needs per-rule test-id sets from W2, or --engine reference",
                file=sys.stderr,
            )
        else:
            print(provenance_table(rows))

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(json_payload, indent=1, sort_keys=True) + "\n", "utf-8")
        print(f"[calibrate] wrote {args.json_path}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
