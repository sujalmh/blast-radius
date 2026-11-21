#!/usr/bin/env python3
"""Parse one pytest run log into tools/mutantsweep/ground/<id>.json.

pytest 9 counts unittest subtests separately, so a single failing test method
can show up as many "failed" entries and is printed in the short test summary
as ``SUBFAILED[<context>] tests/...::Test::test_x`` (there is no top-level
``FAILED`` line in that case).  The ground truth we need is the set of failing
*test node ids*, so both plain and subtest lines are folded into ``failing``.

Recorded fields:
    id, patch, failing[]      ground-truth failing test node ids (deduped)
    passed, failed, skipped   pytest's own counters (subtests counted separately)
    errors[], error_count     collection/setup errors (node ids)
    wall_s                    pytest wall clock inside the image
    summary                   final pytest summary line (audit trail)
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

# "FAILED x", "SUBFAILED[ctx] x", "ERROR x", "SUBERROR[ctx] x", also the
# "SUBFAIL" spelling some pytest versions use.
HEAD = re.compile(r"^(SUBFAIL|SUBFAILED|SUBERROR|SUBERRORED|FAILED|ERRORED|ERROR)\b")
NODE = re.compile(r"(tests/[^\s]+)")


def node_id(line: str) -> str | None:
    m = NODE.search(line)
    return m.group(1) if m else None


def parse(log_path: str) -> dict:
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    failing: list[str] = []
    errors: list[str] = []
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error_count": 0}
    summary = ""

    # A SUBFAILED report embeds the subtest context, which sqlglot often fills
    # with multi-line SQL, so the node id can be several lines below the head.
    # Collect each report block (head line up to the next head/section) and
    # search the block for the node id.
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in lines:
        m = HEAD.match(line)
        if m:
            if current is not None:
                blocks.append(current)
            current = (m.group(1), [line])
            continue
        if current is None:
            continue
        if line.startswith("=") or line.startswith("---") or re.match(r"^\d+ (passed|failed)", line):
            blocks.append(current)
            current = None
            continue
        current[1].append(line)
    if current is not None:
        blocks.append(current)

    for kind, block_lines in blocks:
        nid = node_id("\n".join(block_lines))
        if nid is None:
            continue
        bucket = errors if kind.startswith("SUBERROR") or kind == "ERROR" or kind == "ERRORED" else failing
        if nid not in bucket:
            bucket.append(nid)

    for line in reversed(lines):
        if re.search(r"\d+ (passed|failed|error)", line):
            summary = line.strip()
            for key, rx in COUNTS:
                m2 = rx.search(summary)
                if m2:
                    counts[key] = int(m2.group(1))
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
