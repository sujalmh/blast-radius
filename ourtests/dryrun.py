"""50-commit dry run against the handcrafted mock maps (CPU frugality check).

    python3 -m ourtests.dryrun

Builds a synthetic /data with 50 patches (the 7 mock patterns repeated), then
times one cold selector run with ``time.time()`` / ``time.process_time()``.
"""

from __future__ import annotations

import io
import tempfile
import time
from pathlib import Path

import selector
from ourtests import mockmaps


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="blastradius-dryrun-"))
    data, maps = mockmaps.build_data(root)
    for existing in (data / "commits").glob("*.patch"):
        existing.unlink()
    names = list(mockmaps.PATCHES)
    for index in range(50):
        source = mockmaps.PATCHES[names[index % len(names)]]
        (data / "commits" / f"dry{index:03d}.patch").write_text(source, encoding="utf-8")

    out = io.StringIO()
    err = io.StringIO()

    wall_start = time.time()
    cpu_start = time.process_time()
    selector.run(data, maps, out, err)
    wall = time.time() - wall_start
    cpu = time.process_time() - cpu_start

    lines = out.getvalue().splitlines()
    print(f"commits={len(lines)} wall={wall:.4f}s cpu={cpu:.4f}s")
    print(f"sample: {lines[0]}")
    print(f"stderr tail: {err.getvalue().splitlines()[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
