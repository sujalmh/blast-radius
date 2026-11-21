#!/usr/bin/env python3
"""Fetch the pinned sqlglot tree from GitHub's codeload tarball (build time only).

The build may download sqlglot at the pinned commit (README rules); the tarball
is served by GitHub at that exact commit and avoids needing git in the slim
base image.  Usage: fetch_pin.py <sha> <dest-dir>
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import urllib.request

TARBALL = "https://codeload.github.com/tobymao/sqlglot/tar.gz/{sha}"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: fetch_pin.py <sha> <dest>", file=sys.stderr)
        return 2
    sha, dest = sys.argv[1], sys.argv[2]
    url = TARBALL.format(sha=sha)
    print(f"[fetch_pin] downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=600) as response:
        payload = response.read()
    print(f"[fetch_pin] {len(payload)} bytes", file=sys.stderr)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        names = tar.getnames()
        if not names:
            raise SystemExit("empty tarball")
        prefix = names[0].split("/", 1)[0]
        if not prefix.startswith("sqlglot-"):
            raise SystemExit(f"unexpected tarball prefix: {prefix}")
        staging = dest + ".staging"
        os.makedirs(staging, exist_ok=True)
        tar.extractall(staging, filter="data")

    os.rename(os.path.join(staging, prefix), dest)
    os.rmdir(staging)

    required = ("sqlglot/__init__.py", "tests/helpers.py", "pyproject.toml")
    for rel in required:
        if not os.path.isfile(os.path.join(dest, rel)):
            raise SystemExit(f"checkout is missing {rel}")
    py_files = 0
    for _dirpath, _dirnames, filenames in os.walk(os.path.join(dest, "sqlglot")):
        py_files += sum(1 for name in filenames if name.endswith(".py"))
    print(f"[fetch_pin] /work/sqlglot: {py_files} sqlglot/*.py files, sha={sha}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
