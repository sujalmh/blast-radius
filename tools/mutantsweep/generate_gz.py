#!/usr/bin/env python3
"""Generate the one *binary* mutant of the corpus (mutant 18, W1k).

``tests/fixtures/optimizer/tpc-h/lineitem.csv.gz`` is gzip-compressed test data,
so a change to it can never be a unified text diff.  The corpus therefore also
supports ``git diff --binary`` patches (``GIT binary patch`` / ``literal``
sections, decoded by ``apply_patch.py``), which is the format a real commit that
touches a ``.gz`` fixture produces.

The mutation itself is one field on one line: the first data row's
``l_extendedprice`` value changes from a number to a number with a comma
(``11763.84`` -> ``11763,84``).  ``TestExecutor.setUpClass`` infers column types
from that first row with ``ast.literal_eval``, so the column becomes ``str`` for
the python executor while duckdb's typed ``READ_CSV`` view raises a
``ConversionException`` -- exactly the "one corrupted fixture line" shape whose
reader set the fixture map has to cover.

Usage:
    python3 tools/mutantsweep/generate_gz.py --checkout /tmp/pin
    # rewrites corpus/gz_fixture_corrupt.patch and its manifest.json entry

Nothing else in the corpus is touched; ``generate.py`` regenerates the 17 text
mutants and this file appends/updates mutant 18's entry.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HERE = os.path.dirname(os.path.abspath(__file__))
REL = "tests/fixtures/optimizer/tpc-h/lineitem.csv.gz"
MUTANT = {
    "id": "gz_fixture_corrupt",
    "kind": "fixture",
    "group": "f",
    "file": REL,
    "line": 1,
    "summary": (
        "first lineitem data row: l_extendedprice `11763.84` -> `11763,84` "
        "(binary .gz rewrite; python infers str, duckdb raises ConversionException)"
    ),
}
DATA_ROW = 0  # 0-based data row (file line 2)
COLUMN = "l_extendedprice"


def mutate_bytes(original: bytes) -> bytes:
    """Rewrite the .gz with one field changed (deterministic recompression)."""
    text = gzip.decompress(original).decode("utf-8")
    lines = text.splitlines(keepends=True)
    header = lines[0].rstrip("\r\n").split("|")
    index = header.index(COLUMN)
    fields = lines[1 + DATA_ROW].rstrip("\r\n").split("|")
    if fields[index] == "11763,84":  # pragma: no cover - already mutated
        return original
    fields[index] = "11763,84"
    lines[1 + DATA_ROW] = "|".join(fields) + "\n"
    payload = "".join(lines).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as raw:
        raw.write(payload)
    return buffer.getvalue()


def build_patch(pristine: bytes, mutated: bytes, rel: str, work: str) -> str:
    """`git diff --no-index --binary`, with the corpus' a/<rel>, b/<rel> labels."""
    shutil.rmtree(work, ignore_errors=True)
    old = os.path.join(work, "old", rel)
    new = os.path.join(work, "new", rel)
    for path, data in ((old, pristine), (new, mutated)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    proc = subprocess.run(
        ["git", "diff", "--no-index", "--binary", "--", old, new],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work,
        check=False,
    )
    if proc.returncode not in (0, 1) or not proc.stdout:
        raise SystemExit(f"generate_gz: git diff failed: {proc.stderr.decode()}")
    out = []
    for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("diff --git "):
            line = f"diff --git a/{rel} b/{rel}"
        elif line.startswith("--- "):
            line = f"--- a/{rel}"
        elif line.startswith("+++ "):
            line = f"+++ b/{rel}"
        out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", required=True, help="pristine pinned sqlglot tree")
    ap.add_argument("--out", default=os.path.join(HERE, "corpus"))
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "mutantsweep-gz-work"))
    args = ap.parse_args()

    source = os.path.join(args.checkout, REL)
    if not os.path.isfile(source):
        raise SystemExit(f"not a pinned sqlglot checkout: {source} missing")
    with open(source, "rb") as fh:
        pristine = fh.read()
    mutated = mutate_bytes(pristine)

    patch_body = build_patch(pristine, mutated, REL, args.work)
    patch_path = os.path.join(args.out, f"{MUTANT['id']}.patch")
    with open(patch_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(patch_body)

    sys.path.insert(0, HERE)
    from apply_patch import apply_patch  # noqa: E402  (local sibling module)

    check = os.path.join(args.work, "check")
    shutil.rmtree(check, ignore_errors=True)
    target = os.path.join(check, REL)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(pristine)
    apply_patch(patch_path, check)
    with open(target, "rb") as fh:
        if fh.read() != mutated:
            raise SystemExit("generate_gz: patch round-trip mismatch")

    entry = dict(MUTANT)
    entry["patch"] = os.path.basename(patch_path)
    entry["sha256"] = hashlib.sha256(mutated).hexdigest()
    entry["diff_lines"] = patch_body.count("\n")

    manifest_path = os.path.join(args.out, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    mutants = [item for item in manifest["mutants"] if item.get("id") != entry["id"]]
    mutants.append(entry)
    manifest["mutants"] = mutants
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(
        f"[generate_gz] {entry['id']}: {REL}:{entry['line']} "
        f"{len(pristine)} -> {len(mutated)} bytes, {entry['diff_lines']} patch lines -> {patch_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
