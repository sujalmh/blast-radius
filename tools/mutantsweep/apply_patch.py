#!/usr/bin/env python3
"""Tiny patch applier (the slim runner image has no ``patch``).

Two shapes are supported, both produced by tools/mutantsweep:

* ``diff -u`` output (``generate.py``): ``--- a/<path>`` / ``+++ b/<path>``
  headers and @@ hunks with context/removal/addition lines.  Hunks are matched
  exactly against the target file.
* ``git diff --binary`` output (``generate_gz.py``, for the gzip-compressed
  fixtures that no text diff can express): a ``diff --git a/<path> b/<path>``
  header followed by a ``GIT binary patch`` section whose first ``literal N``
  block is git's base85(zlib(content)) encoding.  The reverse ``literal`` block
  that git also emits is ignored, exactly as ``git apply`` would.

Any mismatch is an error, so a patch can never be applied partially.

Usage:
    python3 apply_patch.py <patch-file> <target-root>
"""

from __future__ import annotations

import os
import sys
import zlib

# git's base85 alphabet (base85.c); the first character of every binary-patch
# line is a length marker and is not part of the encoded stream.
_EN85 = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "!#$%&()*+-;<=>?@^_`{|}~"
)
_DE85 = {char: value for value, char in enumerate(_EN85)}


def decode_85(line: str) -> bytes:
    """Decode one ``GIT binary patch`` data line (marker byte dropped)."""
    payload = line[1:]
    out = bytearray()
    for start in range(0, len(payload), 5):
        acc = 0
        for char in payload[start : start + 5]:
            acc = acc * 85 + _DE85[char]
        out += acc.to_bytes(4, "big")
    return bytes(out)


def parse_binary(lines, start: int, rel: str):
    """Parse a ``GIT binary patch`` section; returns (entry, next index)."""
    i = start + 1  # skip the "GIT binary patch" line
    while i < len(lines):
        line = lines[i]
        if line.startswith("literal "):
            size = int(line.split()[1])
            i += 1
            blob = bytearray()
            while i < len(lines) and lines[i]:
                blob += decode_85(lines[i])
                i += 1
            data = zlib.decompress(bytes(blob))
            if len(data) != size:
                raise SystemExit(
                    f"apply_patch: {rel}: literal size {size} != decoded {len(data)}"
                )
            # The reverse ("applied in the other direction") literal block that
            # git appends is not needed to apply the patch forward.
            return {"path": rel, "binary": data}, _skip_binary_tail(lines, i)
        if line.startswith("delta "):
            raise SystemExit(f"apply_patch: {rel}: binary deltas are not supported")
        if not line:
            i += 1
            continue
        raise SystemExit(f"apply_patch: {rel}: unexpected binary patch line {line!r}")
    raise SystemExit(f"apply_patch: {rel}: truncated GIT binary patch")


def _skip_binary_tail(lines, i: int) -> int:
    while i < len(lines) and not lines[i].startswith("diff "):
        i += 1
    return i


def _git_path(line: str) -> str | None:
    """``diff --git a/x b/x`` -> ``x`` (paths in this corpus have no spaces)."""
    rest = line[len("diff --git ") :]
    marker = " b/"
    cut = rest.rfind(marker)
    if cut < 0:
        return None
    new = rest[cut + len(marker) :]
    old = rest[:cut]
    if old.startswith("a/") and old[2:] == new:
        return new
    return new or None


def parse_patch(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    files = []
    current = None
    git_path = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = None
            git_path = _git_path(line)
            i += 1
            continue
        if line == "GIT binary patch":
            if not git_path:
                raise SystemExit("apply_patch: binary patch without a diff header")
            entry, i = parse_binary(lines, i, git_path)
            files.append(entry)
            current = None
            continue
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
        if line.startswith("diff ") or line.startswith("index "):
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
        if "binary" in entry:
            with open(target, "wb") as fh:
                fh.write(entry["binary"])
            continue
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
