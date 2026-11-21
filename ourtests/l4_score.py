"""Score the real selector on the 30 sample commits with fabricated L4 maps.

    python3 -m ourtests.l4_score [MAPS_DIR]

``maps/`` ships without ``classes.json.gz`` / ``usage_style.json.gz`` until W1f
lands them, so this diagnostic stages a copy of the map set in a temp directory
(never writing ``maps/``), adds the same stand-in L4 maps ``ourtests.l4_probe``
uses -- restricted to the expression tree, which is exactly what the
``Expr.__init__`` construction trace covers -- and runs the real entry point
(``selector.run``) + the real scorer (``score.py``).

The point is negative: adding L4 must not introduce a single miss.  The costs
are upper bounds, not predictions (the stand-in ``touched`` is a span-coverage
approximation of "the test constructs the class").
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import selector  # noqa: E402
from ourtests import l4_probe  # noqa: E402

SAMPLES = ("ktz3", "4d63", "535z", "6ze8")


def stage(maps_dir: Path) -> Path:
    temp = Path(tempfile.mkdtemp(prefix="blastradius-l4score-"))
    staged = temp / "maps"
    shutil.copytree(maps_dir, staged)
    from blastradius.maps_io import Maps

    maps = Maps.load(staged)
    ids = list(maps.test_ids())
    try:
        texts = l4_probe._sources(staged)
        symbols = json.loads(gzip.decompress((staged / "symbols.json.gz").read_bytes()))
        hierarchy = l4_probe.expression_hierarchy(
            l4_probe._hierarchy(texts, symbols), symbols
        )
        touched = l4_probe.expression_touched(maps, symbols, ids)
        (staged / "classes.json.gz").write_bytes(
            gzip.compress(
                json.dumps({"touched": touched, "hierarchy": hierarchy}).encode("utf-8"),
                mtime=0,
            )
        )
        (staged / "usage_style.json.gz").write_bytes(
            gzip.compress(
                json.dumps(l4_probe._usage_style(texts, l4_probe.NAMES)).encode(
                    "utf-8"
                ),
                mtime=0,
            )
        )
        print(
            f"[l4_score] stand-in classes: {len(touched)}/{len(ids)} tests, "
            f"{len(hierarchy)} expression classes"
        )
    finally:
        maps.close()
    return staged


def main(argv: list[str]) -> int:
    maps_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "maps"
    staged = stage(maps_dir)
    data = ROOT / "sample"

    out, err = io.StringIO(), io.StringIO()
    selector.run(data, staged, out, err)
    for line in err.getvalue().splitlines():
        if "warning" in line or "L4" in line:
            print(f"[l4_score] {line.strip()}")

    import score

    tests = json.loads((data / "tests.json").read_text(encoding="utf-8"))["tests"]
    answers = json.loads((data / "answers.json").read_text(encoding="utf-8"))["commits"]
    stdout = out.getvalue().encode("utf-8")
    result = score.evaluate_run(stdout, tests, answers)
    if not result["ok"]:
        print(f"[l4_score] REJECTED: {result}")
        return 1
    missed = set(result["missed_commits"])

    print(f"{'commit':<10} {'tests':>6} {'failing':>8}  verdict")
    for line in out.getvalue().splitlines():
        document = json.loads(line)
        commit = document["commit"]
        if commit not in SAMPLES:
            continue
        numbers = {number for worker in document["workers"] for number in worker}
        failing = answers[commit]["failing"]
        print(
            f"{commit:<10} {len(numbers):>6} {len(failing):>8}  "
            f"{'MISS' if commit in missed else 'ok'}"
        )
    print(
        f"TOTAL simulated {result['simulated_milliseconds'] / 1000:.3f} s  "
        f"misses {len(missed)}  missed: {','.join(sorted(missed))}"
    )
    shutil.rmtree(staged.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
