#!/usr/bin/env python3
"""Parse one pytest run log into tools/mutantsweep/ground/<id>.json.

Recorded fields:
    id, patch, failing[]   ground-truth failing test node ids
    passed, failed, skipped, errors[] (node ids), error_count
    wall_s                 wall clock of the pytest process inside the image
    summary                the final pytest summary line (audit trail)

Node ids come from pytest's short test summary (``FAILED tests/...``), which
``-rfE`` guarantees is printed even under ``-q``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

COUNTS = [
    ("passed", re.compile(r"(\d+) passed")),
    ("failed", re.compile(r"(\d+) failed")),
    ("skipped", re.compile(r"(\d+) skipped")),
    ("error_count", re.compile(r"(\d+) errors?")),
]


def node_id(line: str) -> str:
    rest = line.split(" ", 1)[1]
    return rest.split(" - ", 1)[0].strip()


def parse(log_path: str) -> dict:
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    failing: list[str] = []
    errors: list[str] = []
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error_count": 0}
    summary = ""
    in_summary = False

    for line in lines:
        if line.startswith("=== short test summary info ===") or line.startswith(
            "=========================== short test summary info"
        ):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("FAILED "):
                nid = node_id(line)
                if nid not in failing:
                    failing.append(nid)
                continue
            if line.startswith("ERROR "):
                nid = node_id(line)
                if nid not in errors:
                    errors.append(nid)
                continue
            # end of summary section (progress/exit noise) -- keep scanning, the
            # final counts line is below it anyway.
        if line.startswith("FAILED "):
            nid = node_id(line)
            if nid not in failing:
                failing.append(nid)

    for line in reversed(lines):
        if " passed" in line or " failed" in line or " error" in line:
            if re.search(r"\d+ (passed|failed|error)", line):
                summary = line.strip()
                for key, rx in COUNTS:
                    m = rx.search(summary)
                    if m:
                        counts[key] = int(m.group(1))
                break

    counts["error_count"] = max(counts["error_count"], len(errors))
    return {"failing": failing, "errors": errors, "summary": summary, **counts}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--wall-ms", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--patch", default=None)
    args = ap.parse_args()

    parsed = parse(args.log)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    record = {
        "id": args.id,
        "patch": args.patch if args.patch is not None else (
            args.id if args.id == "baseline" else f"corpus/{args.id}.patch"
        ),
        "failing": parsed["failing"],
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "skipped": parsed["skipped"],
        "errors": parsed["errors"],
        "error_count": parsed["error_count"],
        "wall_s": round(args.wall_ms / 1000.0, 3),
        "summary": parsed["summary"],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    print(
        f"[run_one] {args.id}: {record['passed']} passed, {record['failed']} failed, "
        f"{record['skipped']} skipped, {len(record['failing'])} failing ids, "
        f"{record['wall_s']}s -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
