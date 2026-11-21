#!/usr/bin/env python3
"""Re-derive ground/*.json from the raw logs/ (same parser as collect.py).

Useful when the pytest-log parser is fixed after a sweep: the logs are kept
under logs/<id>.log, so the ground truth can be rebuilt without re-running the
suite.  ``wall_s`` (measured inside the container) is preserved.

    python3 reparse.py            # rewrite every ground/<id>.json
    python3 reparse.py andor_simplify ...

Prints which failing ids were added/removed per mutant so a parser change is
auditable.
"""

from __future__ import annotations

import json
import os
import sys

from collect import parse

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    log_dir = os.path.join(HERE, "logs")
    ground_dir = os.path.join(HERE, "ground")
    only = set(sys.argv[1:]) if len(sys.argv) > 1 else None
    ids = sorted(
        os.path.splitext(name)[0] for name in os.listdir(log_dir) if name.endswith(".log")
    )
    if only:
        ids = [i for i in ids if i in only]

    changed = 0
    for mid in ids:
        ground_path = os.path.join(ground_dir, f"{mid}.json")
        if not os.path.isfile(ground_path):
            print(f"[reparse] {mid}: no existing ground file, skipping", file=sys.stderr)
            continue
        with open(ground_path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
        parsed = parse(os.path.join(log_dir, f"{mid}.log"))
        before = set(record["failing"])
        after = set(parsed["failing"])
        record.update(
            {
                "failing": parsed["failing"],
                "passed": parsed["passed"],
                "failed": parsed["failed"],
                "skipped": parsed["skipped"],
                "errors": parsed["errors"],
                "error_count": parsed["error_count"],
                "summary": parsed["summary"],
            }
        )
        with open(ground_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        if before != after:
            changed += 1
            print(
                f"[reparse] {mid}: +{len(after - before)} -{len(before - after)} ids "
                f"({len(after)} total)",
                file=sys.stderr,
            )

    print(f"[reparse] {len(ids)} logs, {changed} ground files updated", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
