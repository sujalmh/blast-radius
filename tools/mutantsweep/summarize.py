#!/usr/bin/env python3
"""Summarise tools/mutantsweep ground truth (used by run_all.sh and reports).

Reads corpus/manifest.json plus ground/*.json and prints a per-mutant table
plus the validation checks from the W5 brief:
  * baseline is 1620 passed / 1 skipped
  * every mutant patch has a ground-truth file
  * how many mutants have at least one failing test
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PASSED = 1620
BASELINE_SKIPPED = 1


def load(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    manifest = load(os.path.join(HERE, "corpus", "manifest.json"))
    ground_dir = os.path.join(HERE, "ground")

    baseline_path = os.path.join(ground_dir, "baseline.json")
    if os.path.isfile(baseline_path):
        base = load(baseline_path)
        ok = base["passed"] == BASELINE_PASSED and base["skipped"] == BASELINE_SKIPPED
        print(
            f"baseline: {base['passed']} passed, {base['failed']} failed, "
            f"{base['skipped']} skipped, {base['wall_s']}s "
            f"({'OK' if ok else 'MISMATCH, expected 1620 passed / 1 skipped'})"
        )
        if not ok:
            print(f"  baseline summary: {base.get('summary', '')}")
    else:
        print("baseline: MISSING ground/baseline.json")

    print()
    header = f"{'id':<28} {'kind':<16} {'file':<46} {'fail':>4} {'wall_s':>7}"
    print(header)
    print("-" * len(header))
    breaking = 0
    missing = 0
    total_wall = 0.0
    for entry in manifest["mutants"]:
        path = os.path.join(ground_dir, f"{entry['id']}.json")
        if not os.path.isfile(path):
            missing += 1
            print(f"{entry['id']:<28} {entry['kind']:<16} {entry['file']:<46} {'--':>4} {'--':>7}")
            continue
        rec = load(path)
        n = len(rec["failing"])
        if n:
            breaking += 1
        total_wall += rec["wall_s"]
        print(
            f"{entry['id']:<28} {entry['kind']:<16} {entry['file']:<46} "
            f"{n:>4} {rec['wall_s']:>7.1f}"
        )

    print()
    print(f"mutants with >=1 failing test: {breaking} / {len(manifest['mutants'])}")
    print(f"ground truth files missing:   {missing}")
    print(f"sum of mutant wall time:      {total_wall:.1f}s")
    if os.path.isfile(baseline_path):
        print(f"wall time with baseline:      {total_wall + load(baseline_path)['wall_s']:.1f}s")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
