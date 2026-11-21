#!/usr/bin/env python3
"""Blast Radius selector entry point.

Reads ``/data/tests.json`` and ``/data/commits/*.patch``, selects tests with
the committed dependency maps (``maps/`` next to this file) and prints one
JSON answer line per commit on stdout.  All logging goes to stderr.

Failure policy:
  * a single malformed/unmodelled commit -> Tier E (full suite), logged;
  * maps that cannot be loaded at all -> global fallback (full suite) for
    every commit, still valid output.

Overrides (used by the unit tests, harmless in the image):
  BLAST_RADIUS_DATA  - data directory (default /data)
  BLAST_RADIUS_MAPS  - maps directory (default <script dir>/maps)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blastradius import maps_io, patch as patch_parsing, select as selection, split as splitting

DEFAULT_DATA = Path("/data")
DEFAULT_MAPS = ROOT / "maps"


class Tests:
    """The run's tests.json, indexed the ways selection needs."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.number_by_id: dict[str, int] = {}
        self.durations_ms: dict[int, int] = {}
        self.ids_by_file: dict[str, list[str]] = {}
        numbers: list[int] = []
        for entry in entries:
            number = int(entry["number"])
            test_id = str(entry["id"])
            seconds = float(entry["seconds"])
            numbers.append(number)
            self.number_by_id[test_id] = number
            self.durations_ms[number] = math.floor(seconds * 1000 + 0.5)
            self.ids_by_file.setdefault(str(entry["file"]), []).append(test_id)
        self.numbers = sorted(numbers)

    def items(self) -> list[tuple[int, int]]:
        return [(number, self.durations_ms[number]) for number in self.numbers]


def _log(stream, message: str) -> None:
    stream.write(message + "\n")
    stream.flush()


def _load_tests(data_dir: Path) -> Tests:
    document = json.loads((data_dir / "tests.json").read_text(encoding="utf-8"))
    return Tests(document["tests"])


def run(data_dir: Path | str, maps_dir: Path | str, out, err) -> int:
    data_dir = Path(data_dir)
    maps_dir = Path(maps_dir)
    started = time.perf_counter()

    tests = _load_tests(data_dir)
    fallback_workers = splitting.choose_split(tests.items())

    maps = None
    index = None
    try:
        maps = maps_io.Maps.load(maps_dir)
        index = selection.TestIndex(maps.test_ids(), tests.number_by_id, tests.ids_by_file)
    except Exception:
        _log(err, f"maps unavailable under {maps_dir}; using global full-suite fallback")
        traceback.print_exc(file=err)
    if maps is not None:
        for warning in maps.warnings:
            _log(err, f"maps warning: {warning}")

    patch_paths = sorted((data_dir / "commits").glob("*.patch"))
    lines: list[str] = []
    tiers: dict[str, int] = {}

    for patch_path in patch_paths:
        commit = patch_path.stem
        if index is None or maps is None:
            workers = fallback_workers
            tier = "global_fallback"
            selected = len(tests.numbers)
            detail = "maps unavailable"
        else:
            try:
                text = patch_path.read_text(encoding="utf-8", errors="replace")
                files = patch_parsing.parse_patch(text)
                chosen = selection.select_commit(files, maps, index)
                if chosen.tier == "tier_e":
                    # Full suite is the same selection for every Tier E commit;
                    # reuse the split computed once at startup.
                    workers = fallback_workers
                    selected = len(tests.numbers)
                else:
                    numbers = index.numbers_for_ids(chosen.tests)
                    items = [(number, tests.durations_ms[number]) for number in numbers]
                    workers = splitting.choose_split(items)
                    selected = len(items)
                tier = chosen.tier
                detail = ", ".join(str(rule) for rule in chosen.rules[:6])
            except Exception:
                traceback.print_exc(file=err)
                workers = fallback_workers
                tier = "tier_e"
                selected = len(tests.numbers)
                detail = "exception -> full suite"
        tiers[tier] = tiers.get(tier, 0) + 1
        _log(
            err,
            f"{commit}: tier={tier} tests={selected} workers={len(workers)} [{detail}]",
        )
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))

    out.write("\n".join(lines))
    if lines:
        out.write("\n")
    out.flush()

    if maps is not None:
        maps.close()

    elapsed = time.perf_counter() - started
    summary = " ".join(f"{tier}={count}" for tier, count in sorted(tiers.items()))
    _log(err, f"selected {len(patch_paths)} commits in {elapsed:.3f}s ({summary})")
    return 0


def main() -> int:
    data_dir = Path(os.environ.get("BLAST_RADIUS_DATA", str(DEFAULT_DATA)))
    maps_dir = Path(os.environ.get("BLAST_RADIUS_MAPS", str(DEFAULT_MAPS)))
    return run(data_dir, maps_dir, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
