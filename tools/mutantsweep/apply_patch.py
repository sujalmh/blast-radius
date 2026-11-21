#!/usr/bin/env python3
"""Tiny unified-diff applier (the slim runner image has no ``patch``).

Only the shapes produced by ``diff -u`` in tools/mutantsweep/generate.py are
supported: ``--- a/<path>`` / ``+++ b/<path>`` headers and @@ hunks with
context/removal/addition lines.  Hunks are matched exactly against the target
file, and any mismatch is an error, so a patch can never be applied partially.

Usage:
    python3 apply_patch.py <patch-file> <target-root>
"""

from __future__ import annotations

import os
import sys


def parse_patch(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    files = []
    current = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old = line[4:].split("\t")[0].strip()
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise SystemExit(f"apply_patch: bad header after {line!r}")
            new = lines[i + 1][4:].split("\t")[0].strip()
            rel = new[2:] if new.startswith("b/") else new
            if old.startswith("a/"):
                if old[2:] != rel:
                    raise SystemExit(f"apply_patch: old/new path mismatch: {old} vs {new}")
            current = {"path": rel, "hunks": []}
            files.append(current)
            i += 2
            continue
        if line.startswith("@@ "):
            if current is None:
                raise SystemExit("apply_patch: hunk before file header")
            header = line
            body = []
            i += 1
            while i < len(lines):
                hline = lines[i]
                if hline.startswith("@@ ") or hline.startswith("--- ") or hline.startswith("diff "):
                    break
                body.append(hline)
                i += 1
            # trailing empty string from split("\n") is not a hunk line
            if body and body[-1] == "":
                body.pop()
            current["hunks"].append({"header": header, "body": body})
            continue
        if line.startswith("diff "):
            current = None
        i += 1
    return files


def apply_hunks(path: str, hunks) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Split preserving no trailing-newline information via "\ No newline" markers.
    lines = text.split("\n")
    pos = 0
    for hunk in hunks:
        header = hunk["header"]
        try:
            old_range = header.split("@@")[1].strip().split(" ")[0]
        except IndexError:
            raise SystemExit(f"apply_patch: bad hunk header {header!r}")
        start = int(old_range[1:].split(",")[0])
        old_block, new_block = [], []
        for body_line in hunk["body"]:
            if body_line.startswith("\\"):
                continue
            tag, content = body_line[:1], body_line[1:]
            if tag == " ":
                old_block.append(content)
                new_block.append(content)
            elif tag == "-":
                old_block.append(content)
            elif tag == "+":
                new_block.append(content)
            elif tag == "":
                # blank context line (diff emits " " for an empty context line)
                old_block.append("")
                new_block.append("")
            else:
                raise SystemExit(f"apply_patch: bad hunk line {body_line!r}")
        wanted = start - 1
        idx = _find(lines, old_block, max(0, wanted - 8), len(lines))
        if idx is None:
            idx = _find(lines, old_block, 0, len(lines))
        if idx is None:
            raise SystemExit(f"apply_patch: {path}: hunk at {header!r} does not match")
        if idx < pos:
            raise SystemExit(f"apply_patch: {path}: overlapping hunks at {header!r}")
        lines[idx : idx + len(old_block)] = new_block
        pos = idx + len(new_block)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _find(lines, block, lo, hi):
    n = len(block)
    for j in range(lo, hi - n + 1):
        if lines[j : j + n] == block:
            return j
    return None


def apply_patch(patch_path: str, root: str) -> None:
    files = parse_patch(patch_path)
    if not files:
        raise SystemExit(f"apply_patch: {patch_path} contains no file sections")
    for entry in files:
        target = os.path.join(root, entry["path"])
        if not os.path.isdir(os.path.dirname(target)):
            os.makedirs(os.path.dirname(target), exist_ok=True)
        if not os.path.isfile(target):
            raise SystemExit(f"apply_patch: missing target {target}")
        apply_hunks(target, entry["hunks"])


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: apply_patch.py <patch> <target-root>", file=sys.stderr)
        return 2
    apply_patch(sys.argv[1], sys.argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main())
