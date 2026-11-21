#!/usr/bin/env python3
"""Ad-hoc map queries for the orchestrator.

    python3 bench/probe.py symbols sqlglot/parser.py
    python3 bench/probe.py cover-lines sqlglot/parser.py 497 520
    python3 bench/probe.py fixture tests/fixtures/optimizer/pushdown_predicates.sql
    python3 bench/probe.py hunk sqlglot/parser.py 497 520 --pad 2
    python3 bench/probe.py usage DATE_UNITS --file sqlglot/helper.py
    python3 bench/probe.py describe

Every subcommand reads the map set from `--maps` (default `maps/`) and numbers
tests through `--data` (default `sample/tests.json`) when it is available. The
`hunk` subcommand runs the reference rule engine by default because it takes a
synthetic patch; use the knob flags to see what each rule would select.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # runnable as `python3 bench/probe.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "bench"

from bench.engine import Engine, Knobs, partition  # noqa: E402
from bench.mapio import MapSet  # noqa: E402


def _number_by_id(data_dir: Path) -> dict[str, int]:
    tests_path = data_dir / "tests.json"
    if not tests_path.is_file():
        return {}
    payload = json.loads(tests_path.read_text("utf-8"))
    return {test["id"]: int(test["number"]) for test in payload.get("tests", [])}


def _emit(ids, numbers: dict[str, int], *, show_lines=None, as_json: bool = False, extra=None):
    rows = []
    for test_id in sorted(ids):
        row = {"id": test_id, "number": numbers.get(test_id)}
        if show_lines is not None:
            row["lines"] = show_lines(test_id)
        rows.append(row)
    if as_json:
        print(json.dumps({"tests": rows, **(extra or {})}, indent=1))
        return
    for row in rows:
        number = "-" if row.get("number") is None else row["number"]
        line = f"{number:>5}  {row['id']}"
        if row.get("lines"):
            line += "  " + row["lines"]
        print(line)
    if not rows:
        print("(no tests)")
    if extra:
        for key, value in extra.items():
            print(f"{key}: {value}")


def _synthetic_hunk_patch(path: str, start: int, end: int) -> str:
    count = max(1, end - start + 1)
    body = "\n".join(" " + "context" for _ in range(count))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},{count} +{start},{count} @@\n"
        f"{body}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ad-hoc MAPS_SPEC map queries.")
    parser.add_argument("--maps", default="maps", help="map set directory")
    parser.add_argument("--data", default="sample", help="directory holding tests.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    symbols = sub.add_parser("symbols", help="dump the symbol table of a file")
    symbols.add_argument("file")

    cover = sub.add_parser("cover-lines", help="tests covering old lines START-END of FILE")
    cover.add_argument("file")
    cover.add_argument("start", type=int)
    cover.add_argument("end", type=int)

    fixture = sub.add_parser("fixture", help="tests that read a fixture file")
    fixture.add_argument("fixture")

    hunk = sub.add_parser("hunk", help="tests a hunk (FILE,START,END) would select")
    hunk.add_argument("file")
    hunk.add_argument("start", type=int)
    hunk.add_argument("end", type=int)
    hunk.add_argument("--symbol-vs-line", choices=("symbol", "line"), default="symbol")
    hunk.add_argument("--classbody", choices=("on", "off"), default="on")
    hunk.add_argument("--usage", choices=("on", "off"), default="on")
    hunk.add_argument("--neutral", choices=("on", "off"), default="off")
    hunk.add_argument("--pad", type=int, default=0)
    hunk.add_argument("--tier-e", choices=("full", "empty"), default="full")

    usage = sub.add_parser("usage", help="tests covering usages of NAME")
    usage.add_argument("name")
    usage.add_argument("--file", default=None, help="restrict to one source file")

    sub.add_parser("describe", help="map set summary (loaded/missing parts)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    maps = MapSet.load(args.maps)
    numbers = _number_by_id(Path(args.data))

    if args.command == "describe":
        if args.json:
            print(json.dumps(maps.describe(), indent=1))
        else:
            info = maps.describe()
            for key, value in info.items():
                print(f"{key}: {value}")
        return 0

    if args.command == "symbols":
        symbols = maps.symbol_list(args.file)
        if args.json:
            print(json.dumps([[s.qualname, s.start, s.end, s.kind] for s in symbols], indent=1))
            return 0 if symbols else 1
        for symbol in symbols:
            print(f"{symbol.start:>6}-{symbol.end:<6} {symbol.kind:<8} {symbol.qualname}")
        if not symbols:
            print(f"(no symbols mapped for {args.file})")
            return 1
        return 0

    if args.command == "cover-lines":
        if args.end < args.start:
            print("error: END must be >= START", file=sys.stderr)
            return 2
        ids = maps.tests_covering_ranges(args.file, [(args.start, args.end)])

        def lines_of(test_id: str) -> str:
            ordinal = maps.ordinal_of(test_id)
            if ordinal is None:
                return ""
            covered = maps.coverage.lines_covered_by(args.file, ordinal)
            inside = [
                f"{start}-{end}"
                for start, end in covered
                if start <= args.end and args.start <= end
            ]
            return ",".join(inside)

        _emit(ids, numbers, show_lines=lines_of, as_json=args.json)
        return 0 if ids else 1

    if args.command == "fixture":
        tests = maps.fixture_tests(args.fixture)
        if tests is None:
            print(f"{args.fixture}: NOT in fixture map -> the spec says Tier E (full suite)")
            return 1
        _emit(set(tests), numbers, as_json=args.json)
        return 0 if tests else 1

    if args.command == "usage":
        total: set[str] = set()
        per_file: dict[str, list[int]] = {}
        files = [args.file] if args.file else sorted(maps.usage)
        for path in files:
            lines = maps.usage_names(path).get(args.name)
            if not lines:
                continue
            per_file[path] = sorted(lines)
            total |= maps.tests_covering_ranges(path, [(line, line) for line in lines])
        if not per_file:
            print(f"no usage lines recorded for {args.name}")
            return 1
        if args.json:
            print(json.dumps({"name": args.name, "files": per_file, "tests": sorted(total)}, indent=1))
            return 0
        for path, lines in per_file.items():
            print(f"{path}: lines {', '.join(str(line) for line in lines)}")
        _emit(total, numbers)
        return 0 if total else 1

    if args.command == "hunk":
        kind = partition(args.file)
        knobs = Knobs(
            symbol_vs_line=args.symbol_vs_line,
            classbody=args.classbody == "on",
            usage=args.usage == "on",
            neutral=args.neutral == "on",
            pad=args.pad,
            tier_e=args.tier_e,
        )
        engine = Engine(maps, knobs)
        patch = _synthetic_hunk_patch(args.file, args.start, args.end)
        selection = engine.select(patch)
        extra = {
            "file": f"{args.file}:{args.start}-{args.end}",
            "partition": kind,
            "tier_e": selection.tier_e,
            "rules": {rule: len(tests) for rule, tests in sorted(selection.rules.items())},
            "notes": "; ".join(selection.notes),
        }
        _emit(selection.tests, numbers, as_json=args.json, extra=extra)
        return 0

    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
