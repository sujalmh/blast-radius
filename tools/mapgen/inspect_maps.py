#!/usr/bin/env python3
"""Dump/inspect a generated maps/ bundle (plumbing-run proof helper).

Examples:
  inspect_maps.py --maps maps
  inspect_maps.py --maps /out --file sqlglot/dialects/dune.py \
      --test tests/dialects/test_dune.py::TestDune::test_dune
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gen_maps import _lines_to_runs, parse_coverage_payload  # noqa: E402


def read_gz_json(path: str):
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--maps", default="maps")
    parser.add_argument("--file", default=None)
    parser.add_argument("--test", default=None)
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()

    index = json.load(open(os.path.join(args.maps, "tests_index.json"), encoding="utf-8"))[
        "test_ids"
    ]
    coverage = parse_coverage_payload(
        gzip.open(os.path.join(args.maps, "coverage.bin.gz"), "rb").read()
    )
    symbols = read_gz_json(os.path.join(args.maps, "symbols.json.gz"))
    fixtures = read_gz_json(os.path.join(args.maps, "fixtures.json.gz"))
    manifest = json.load(open(os.path.join(args.maps, "MANIFEST.json"), encoding="utf-8"))

    print(f"pinned_sha: {manifest['pinned_sha']}")
    print(f"tests_index: {len(index)} ids")
    print(f"  head: {index[:2]}")
    print(f"  tail: {index[-2:]}")
    print(f"coverage files: {len(coverage)}  symbols files: {len(symbols)}")

    if args.file:
        path = args.file
        intervals = coverage.get(path, [])
        by_ordinal: dict[int, list[tuple[int, int]]] = {}
        for start, end, ordinal in intervals:
            by_ordinal.setdefault(ordinal, []).append((start, end))
        print(f"\ncoverage[{path}]: {len(intervals)} intervals from {len(by_ordinal)} tests")
        for ordinal in sorted(by_ordinal)[: args.limit]:
            lines = sorted(
                {line for start, end in by_ordinal[ordinal] for line in range(start, end + 1)}
            )
            print(f"  ordinal {ordinal} {index[ordinal] if ordinal < len(index) else '?'}")
            print(f"    lines {len(lines)} first={lines[:12]}{' ...' if len(lines) > 12 else ''}")

        entries = symbols.get(path, [])
        print(f"\nsymbols[{path}]: {len(entries)} entries")
        for entry in entries[: args.limit * 6]:
            print(f"  {entry}")

        target_ordinals = set(by_ordinal)
        if args.test:
            target_ordinals = {
                ordinal for ordinal, test_id in enumerate(index) if test_id == args.test
            }
            if not target_ordinals:
                print(f"  (test not found in index: {args.test})")
        overlap_found = False
        print(f"\noverlap check ({len(target_ordinals)} target test(s) vs symbols):")
        for qualname, start, end, kind in entries:
            hits = 0
            covered_lines = 0
            for ordinal in target_ordinals:
                for run_start, run_len in _lines_to_runs(
                    [
                        line
                        for s, e in by_ordinal.get(ordinal, [])
                        for line in range(s, e + 1)
                        if start <= line <= end
                    ]
                ):
                    covered_lines += run_len
                    hits += 1
            if covered_lines:
                overlap_found = True
                print(
                    f"  {kind} {qualname} [{start}-{end}]: covered_lines={covered_lines} "
                    f"runs={hits}"
                )
        print(f"  any_overlap={overlap_found}")

    if args.test:
        hits = sorted(path for path, tests in fixtures.items() if args.test in tests)
        print(f"\nfixtures read by {args.test}: {len(hits)}")
        for path in hits[: args.limit * 3]:
            print(f"  {path}")

    test_files = fixtures.get("__test_files__", {})
    print(f"\n__test_files__: {len(test_files)} files; __shared__: {fixtures.get('__shared__', [])}")
    try:
        import tarfile

        with tarfile.open(os.path.join(args.maps, "source.tar.gz"), "r:gz") as tar:
            names = [member.name for member in tar.getmembers() if member.isfile()]
        dune = [name for name in names if name.endswith("dialects/dune.py")]
        print(f"source.tar.gz: {len(names)} members (e.g. {dune})")
    except FileNotFoundError:
        print("source.tar.gz: missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
