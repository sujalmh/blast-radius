"""Generate the synthetic /data + maps under ourtests/data and ourtests/data_maps.

Usage (from the repository root):

    python3 -m ourtests.build_data

The generated tree is disposable: every file is rebuilt from
``ourtests/mockmaps.py`` and ``ourtests/fixtures/mockrepo/``.
"""

from __future__ import annotations

from pathlib import Path

from ourtests import mockmaps


def main() -> int:
    root = Path(__file__).resolve().parent
    data, maps = mockmaps.build_data(root)
    print(f"synthetic data: {data}")
    print(f"mock maps:      {maps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
