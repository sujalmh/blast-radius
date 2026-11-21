"""Per-commit dry run of the real selector over ``sample/`` (no Docker).

    python3 -m ourtests.sample_dryrun
    python3 -m ourtests.sample_dryrun --knob enum_neutral=off --knob line_bias=off

It calls ``blastradius.select.select_commit`` exactly like ``selector.py`` does,
splits with the shipped LPT chooser, and prices every commit with the scorer's
own arithmetic (``max(worker load) + 2000 ms per worker``, +300 s per miss).
The table is ordered by cost, biggest first, so the remaining fat is obvious.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blastradius.maps_io import Maps  # noqa: E402
from blastradius.patch import parse_patch  # noqa: E402
from blastradius.select import Knobs, TestIndex, select_commit  # noqa: E402
from blastradius.split import choose_split  # noqa: E402

WORKER_COST = 2000
MISS_PENALTY = 300_000


def _duration_ms(seconds: float) -> int:
    return int(seconds * 1000 + 0.5)


def _knobs(pairs: list[str]) -> Knobs:
    values: dict[str, object] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        values[key.strip()] = value.strip()
    return Knobs(**values).normalized()


def run(knobs: Knobs, sample: Path, maps_dir: Path) -> int:
    tests = json.loads((sample / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((sample / "answers.json").read_text("utf-8"))["commits"]
    duration = {int(t["number"]): _duration_ms(float(t["seconds"])) for t in tests}
    number_by_id = {str(t["id"]): int(t["number"]) for t in tests}
    tests_by_file: dict[str, list[str]] = {}
    for entry in tests:
        tests_by_file.setdefault(str(entry["file"]), []).append(str(entry["id"]))

    maps = Maps.load(maps_dir)
    index = TestIndex(maps.test_ids(), number_by_id, tests_by_file)
    rows: list[tuple[int, str, int, int, int, str]] = []
    total = 0
    misses = 0
    try:
        for patch_path in sorted((sample / "commits").glob("*.patch")):
            commit = patch_path.stem
            text = patch_path.read_text("utf-8", errors="replace")
            chosen = select_commit(parse_patch(text), maps, index, knobs)
            numbers = sorted(index.numbers_for_ids(chosen.tests))
            workers = choose_split([(n, duration[n]) for n in numbers])
            load = max(sum(duration[n] for n in worker) for worker in workers) if workers else 0
            cost = load + WORKER_COST * len(workers) if workers else 0
            failing = set(answers[commit]["failing"])
            missed = sorted(failing - set(numbers))
            if missed:
                misses += 1
            total += cost + (MISS_PENALTY if missed else 0)
            rules = ",".join(sorted({hit.rule for hit in chosen.rules})) or "-"
            rows.append((cost, commit, len(numbers), len(workers), len(missed), rules))
    finally:
        maps.close()

    width = max(len(row[1]) for row in rows)
    print(f"knobs: {knobs}")
    print(f"{'commit'.ljust(width)}  tests  work  cost       status  rules")
    for cost, commit, count, workers, missed, rules in sorted(rows, reverse=True):
        status = f"MISS {missed}" if missed else "ok"
        print(
            f"{commit.ljust(width)}  {count:5d}  {workers:4d}  {cost / 1000:8.3f}s  "
            f"{status:7s} {rules}"
        )
    print(f"TOTAL {total / 1000:.3f} s   misses={misses}")
    return 0 if misses == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knob", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--sample", type=Path, default=ROOT / "sample")
    parser.add_argument("--maps", type=Path, default=ROOT / "maps")
    options = parser.parse_args(argv)
    return run(_knobs(options.knob), options.sample, options.maps)


if __name__ == "__main__":
    raise SystemExit(main())
