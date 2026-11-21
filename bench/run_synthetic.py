#!/usr/bin/env python3
"""Generalization guard: run hand-written patches and check qualitative expectations.

The sample answers are the tuning set; these ten patches are the guard against
tuning only to it. Expectations are structural (Tier E or not, empty or not,
which rule fired), never exact test sets -- exact sets would be answers worked
out ahead of time.

    python3 bench/run_synthetic.py                     # mock maps (works before W1 lands)
    python3 bench/run_synthetic.py --maps maps         # real maps
    python3 bench/run_synthetic.py --verbose           # show every selection
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # runnable as `python3 bench/run_synthetic.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "bench"

from bench.adapter import BackendError, BackendUnsupported, load_backend  # noqa: E402
from bench.engine import Knobs  # noqa: E402
from bench.mapio import MapSet  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
DEFAULT_EXPECTATIONS = BENCH_DIR / "synthetic" / "expectations.json"
ALLOWED_KEYS = {
    "tier_e",
    "empty",
    "min_selected",
    "max_selected",
    "rules_include",
    "rules_exclude",
    "rules_exactly",
    "patterns",
    "min_pattern_matches",
}


def _numbers(data_dir: Path) -> dict[str, int]:
    path = data_dir / "tests.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text("utf-8"))
    return {test["id"]: int(test["number"]) for test in payload.get("tests", [])}


def merge_expectation(check: dict, is_mock: bool) -> dict:
    merged = dict(check.get("expect", {}))
    merged.update(check.get("mock", {}) if is_mock else check.get("real", {}))
    unknown = set(merged) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown expectation keys {sorted(unknown)}")
    return merged


def contributing_rules(selection) -> set[str]:
    return {rule for rule, tests in selection.rules.items() if tests}


def evaluate(selection, expect: dict, numbers: dict[str, int]) -> list[str]:
    """Return a list of failure reasons (empty == pass)."""
    failures: list[str] = []
    selected = set(selection.tests)
    if "tier_e" in expect and expect["tier_e"] is not None:
        if bool(selection.tier_e) != bool(expect["tier_e"]):
            failures.append(f"tier_e={selection.tier_e} expected {expect['tier_e']}")
    if "empty" in expect and expect["empty"] is not None:
        if (not selected) != bool(expect["empty"]):
            failures.append(f"empty={not selected} expected {expect['empty']}")
    if expect.get("min_selected") is not None and len(selected) < expect["min_selected"]:
        failures.append(f"selected {len(selected)} < min {expect['min_selected']}")
    if expect.get("max_selected") is not None and len(selected) > expect["max_selected"]:
        failures.append(f"selected {len(selected)} > max {expect['max_selected']}")
    rules = contributing_rules(selection)
    if expect.get("rules_include"):
        if not (rules & set(expect["rules_include"])):
            failures.append(f"none of {expect['rules_include']} fired (fired: {sorted(rules)})")
    if expect.get("rules_exclude"):
        hit = rules & set(expect["rules_exclude"])
        if hit:
            failures.append(f"forbidden rules fired: {sorted(hit)}")
    if expect.get("rules_exactly") is not None and rules != set(expect["rules_exactly"]):
        failures.append(f"rules {sorted(rules)} != expected {sorted(expect['rules_exactly'])}")
    patterns = expect.get("patterns") or []
    if patterns:
        needed = int(expect.get("min_pattern_matches", 1))
        matched = [
            pattern
            for pattern in patterns
            if any(re.fullmatch(pattern, test_id) or re.search(pattern, test_id) for test_id in selected)
        ]
        if len(matched) < needed:
            failures.append(
                f"matched {len(matched)}/{len(patterns)} patterns (need {needed}): {matched}"
            )
    return failures


def _detail(selection, numbers: dict[str, int], limit: int = 4) -> str:
    preview = [f"{numbers.get(test_id, '-')}:{test_id}" for test_id in sorted(selection.tests)[:limit]]
    more = "" if len(selection.tests) <= limit else f" +{len(selection.tests) - limit}"
    rules = ",".join(f"{rule}:{len(tests)}" for rule, tests in sorted(selection.rules.items()) if tests) or "-"
    return f"selected={len(selection.tests)} tier_e={selection.tier_e} rules={rules} [{'; '.join(preview)}{more}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic generalization guard.")
    parser.add_argument("--maps", default="auto", help="'auto' = maps/ if built, else bench/mockmaps")
    parser.add_argument("--data", default="auto", help="tests.json dir for number display ('auto')")
    parser.add_argument("--expectations", default=str(DEFAULT_EXPECTATIONS))
    parser.add_argument(
        "--engine",
        choices=("auto", "reference", "blastradius"),
        default="reference",
        help="reference = rule-capable engine (default); blastradius lacks knobs/provenance so rule checks skip",
    )
    parser.add_argument("--cases", default=None, help="comma-separated synthetic case ids")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    maps_dir = args.maps
    if maps_dir == "auto":
        real = REPO_ROOT / "maps"
        maps_dir = str(real if (real / "tests_index.json").is_file() else BENCH_DIR / "mockmaps")
        print(f"[synthetic] --maps auto -> {maps_dir}", file=sys.stderr)
    maps = MapSet.load(maps_dir)
    is_mock = maps.is_mock()
    data_dir = Path(args.data)
    if args.data == "auto":
        data_dir = BENCH_DIR / "mockdata" if is_mock else REPO_ROOT / "sample"
    numbers = _numbers(data_dir)

    expectations = json.loads(Path(args.expectations).read_text("utf-8"))
    cases = expectations["checks"]
    if args.cases:
        wanted = {item.strip() for item in args.cases.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            print(f"[synthetic] unknown case ids: {sorted(missing)}", file=sys.stderr)
            return 2

    try:
        backend = load_backend(args.engine, maps)
    except BackendError as error:
        print(f"[synthetic] no usable engine: {error}", file=sys.stderr)
        return 2
    capabilities = backend.capabilities()
    print(
        f"[synthetic] maps={maps.directory} mock={is_mock} engine={backend.name} ({capabilities.detail}) "
        f"tests_index={len(maps.tests_index)}",
        file=sys.stderr,
    )
    if maps.missing_parts:
        print(f"[synthetic] map parts missing: {maps.missing_parts}", file=sys.stderr)

    failures = 0
    skipped = 0
    checks_run = 0
    print(f"{'case':<26} {'knobs':<46} {'result':<7} detail")
    print("-" * 120)
    for case in cases:
        patch_path = BENCH_DIR / "synthetic" / case["patch"]
        patch_text = patch_path.read_text("utf-8")
        for index, check in enumerate(case["checks"]):
            knobs = Knobs().with_overrides(**check.get("knobs", {}))
            expect = merge_expectation(check, is_mock)
            label = case["id"] if index == 0 else f"{case['id']}#{index + 1}"
            knob_text = (
                f"{knobs.symbol_vs_line}/cb={'on' if knobs.classbody else 'off'}/"
                f"use={'on' if knobs.usage else 'off'}/neu={'on' if knobs.neutral else 'off'}/"
                f"pad={knobs.pad}/te={knobs.tier_e}"
            )
            try:
                selection = backend.select(patch_text, knobs)
            except BackendUnsupported as error:
                skipped += 1
                print(f"{label:<26} {knob_text:<46} {'SKIP':<7} {error}")
                continue
            except Exception as error:  # noqa: BLE001 - W2 is a moving target
                failures += 1
                print(f"{label:<26} {knob_text:<46} {'FAIL':<7} {type(error).__name__}: {error}")
                continue

            checks_run += 1
            checks = evaluate(selection, expect, numbers)
            provenance_rules = {
                "rules_include",
                "rules_exclude",
                "rules_exactly",
            } & set(expect)
            if provenance_rules and backend.has_provenance is False:
                skipped += 1
                print(f"{label:<26} {knob_text:<46} {'SKIP':<7} backend exposes no provenance")
                continue
            if checks:
                failures += 1
                print(f"{label:<26} {knob_text:<46} {'FAIL':<7} {'; '.join(checks)}  {_detail(selection, numbers)}")
            else:
                detail = _detail(selection, numbers) if args.verbose else ""
                print(f"{label:<26} {knob_text:<46} {'PASS':<7} {detail}")

    print("-" * 120)
    print(f"{checks_run} checks run, {failures} failed, {skipped} skipped")
    if failures:
        print("[synthetic] FAILED", file=sys.stderr)
        return 1
    print("[synthetic] ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
