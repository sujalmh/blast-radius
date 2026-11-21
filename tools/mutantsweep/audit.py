#!/usr/bin/env python3
"""Audit a selector output against the W5 observed ground truth.

The selector prints one JSON line per commit with worker lists of *test numbers*
(from /data/tests.json).  This tool maps the observed failing node ids in
ground/<id>.json to those numbers and reports, per mutant, what the selector
missed and what the simulated cost would be (same arithmetic as score.py:
worker load in whole milliseconds, +2000 ms per occupied worker, +300 s per
missed commit).

Usage:
    python3 audit.py --output ../../output.jsonl \
        --tests-json ../../sample/tests.json \
        [--ground ground] [--manifest corpus/manifest.json] [--only <id>]

Read-only: nothing under sample/, maps/, blastradius/ is modified.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="selector output.jsonl")
    ap.add_argument("--tests-json", required=True, help="tests.json with numbers/seconds")
    ap.add_argument("--ground", default=os.path.join(HERE, "ground"))
    ap.add_argument("--manifest", default=os.path.join(HERE, "corpus", "manifest.json"))
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    tests = load(args.tests_json)["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}

    selected_by_commit: dict[str, list[list[int]]] = {}
    with open(args.output, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            selected_by_commit[rec["commit"]] = rec["workers"]

    manifest = load(args.manifest)["mutants"]
    ids = [m["id"] for m in manifest]
    if args.only:
        ids = [i for i in ids if i == args.only]

    header = f"{'id':<28} {'sel':>4} {'fail':>4} {'miss':>4} {'cost_s':>7}  missed node ids"
    print(header)
    print("-" * len(header))
    total_cost = 0.0
    total_misses = 0
    unknown = 0
    broken_ground = 0
    for mid in ids:
        ground_path = os.path.join(args.ground, f"{mid}.json")
        if not os.path.isfile(ground_path):
            print(f"{mid:<28} {'--':>4} {'--':>4} {'--':>4} {'--':>7}  (no ground truth)")
            continue
        rec = load(ground_path)
        failing_ids = rec["failing"]
        mapped, unmapped = [], []
        for nid in failing_ids:
            if nid in number_of:
                mapped.append(number_of[nid])
            else:
                unmapped.append(nid)
        if unmapped:
            unknown += len(unmapped)
        if rec["failed"] and not failing_ids:
            broken_ground += 1

        workers = selected_by_commit.get(mid)
        if workers is None:
            print(f"{mid:<28} {'--':>4} {len(failing_ids):>4} {'--':>4} {'--':>7}  (no output line)")
            continue
        workers = [w for w in workers if w]
        selected = {n for w in workers for n in w}
        missed = [n for n in mapped if n not in selected]
        loads = [sum(round(seconds.get(n, 0.0) * 1000) for n in w) for w in workers]
        cost = (max(loads) / 1000.0 + 2.0 * len(workers)) if workers else 0.0
        if missed:
            cost += 300.0
        total_cost += cost
        total_misses += 1 if missed else 0

        missed_ids = [nid for nid in failing_ids if nid not in number_of or number_of[nid] not in selected]
        extra = f"  {', '.join(missed_ids[:4])}" + (" ..." if len(missed_ids) > 4 else "")
        print(
            f"{mid:<28} {len(selected):>4} {len(failing_ids):>4} {len(missed):>4} "
            f"{cost:>7.1f}{extra}"
        )

    print()
    print(f"commits audited:      {len(selected_by_commit)} selector lines, {len(ids)} ground-truth mutants")
    print(f"commits with a miss:  {total_misses}")
    print(f"simulated cost:       {total_cost:.1f}s (includes +300 s per missed commit)")
    if unknown:
        print(f"WARNING: {unknown} failing node ids are not in {args.tests_json}")
    if broken_ground:
        print(f"WARNING: {broken_ground} ground files report failed > 0 but have no failing ids")
    return 0


if __name__ == "__main__":
    sys.exit(main())
