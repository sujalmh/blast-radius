#!/usr/bin/env python3
"""Generate the mock map set + mock calibration data.

Everything here is hand-authored fiction: the coverage intervals, usage lines,
fixture map and test-file map are invented, and the test ids live in
`tests/test_mock_*.py`. Nothing is copied from real coverage/fixture data, and
no map says anything about which tests a real sqlglot patch breaks.

The one real ingredient is `source.tar.gz`: it holds the pinned files the
synthetic patches touch, so the comment-neutrality check (reconstruct the file,
compare `ast.dump`) and the hunk-beyond-EOF check can be exercised end to end
before W1's maps land. Regenerate it with `--pin /path/to/sqlglot@pin`; without
`--pin` the existing tarball is reused and only the JSON/binary maps are rebuilt.

    python3 bench/mockmaps/build.py
    python3 bench/mockmaps/build.py --pin /tmp/sqlglot-pin
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import struct
import tarfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
MOCK_DIR = BENCH_DIR / "mockmaps"
DATA_DIR = BENCH_DIR / "mockdata"

PINNED_SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

# Pinned files kept in source.tar.gz (small files; only those the synthetic
# patches anchor into). tests/ files are intentionally absent: the spec's
# source tarball is sqlglot/ only.
SOURCE_FILES = [
    "sqlglot/helper.py",
    "sqlglot/trie.py",
    "sqlglot/generators/sqlite.py",
    "sqlglot/parsers/bigquery.py",
]

MOCK_TESTS = [
    ("tests/test_mock_parser.py::TestMockParser::test_a", 0.120),
    ("tests/test_mock_parser.py::TestMockParser::test_b", 0.080),
    ("tests/test_mock_optimizer.py::TestMockOptimizer::test_a", 0.300),
    ("tests/test_mock_optimizer.py::TestMockOptimizer::test_b", 0.050),
    ("tests/test_mock_helper.py::TestMockHelper::test_dates", 0.200),
    ("tests/test_mock_helper.py::TestMockHelper::test_units", 0.070),
    ("tests/test_mock_sqlite.py::TestMockSQLite::test_generate", 0.150),
    ("tests/test_mock_sqlite.py::TestMockSQLite::test_roundtrip", 0.040),
    ("tests/test_mock_bigquery.py::TestMockBigQuery::test_parse", 0.110),
    ("tests/test_mock_bigquery.py::TestMockBigQuery::test_qualify", 0.060),
    ("tests/dialects/test_mock_dialect.py::TestMockDialect::test_x", 0.030),
    ("tests/dialects/test_mock_dialect.py::TestMockDialect::test_y", 0.025),
]

# Invented per-test coverage, keyed by file -> [(test index, start, end), ...].
# Deliberate property: the DATE_UNITS assignment line (466) is NOT covered by any
# test (module import happens before test coverage starts), while a nearby
# reference line (475) is covered -- that is what makes the usage rule matter.
MOCK_COVERAGE = {
    "sqlglot/helper.py": [(3, 54, 70), (5, 100, 130), (4, 471, 475)],
    "sqlglot/parsers/bigquery.py": [(8, 495, 515), (9, 171, 190), (0, 200, 260)],
    "sqlglot/generators/sqlite.py": [(6, 86, 130), (7, 23, 60)],
    "sqlglot/trie.py": [(1, 14, 41), (10, 1, 13)],
}

# Invented name-usage lines (a name -> lines that reference it).
MOCK_USAGE = {
    "sqlglot/helper.py": {"DATE_UNITS": [466, 470, 475], "PYTHON_VERSION": [39]},
    "sqlglot/generators/sqlite.py": {"TABLE_HINTS": [95, 130], "TYPE_MAPPING": [114]},
    "sqlglot/parsers/bigquery.py": {"_split_qualified_name": [482]},
    "sqlglot/trie.py": {},
}

MOCK_FIXTURE_MAP = {
    "tests/fixtures/optimizer/pushdown_predicates.sql": [
        "tests/test_mock_optimizer.py::TestMockOptimizer::test_a",
        "tests/test_mock_optimizer.py::TestMockOptimizer::test_b",
    ],
    "tests/fixtures/optimizer/merge_subqueries.sql": [
        "tests/test_mock_optimizer.py::TestMockOptimizer::test_b",
    ],
}
MOCK_TEST_FILES = {
    "tests/test_mock_parser.py": [
        "tests/test_mock_parser.py::TestMockParser::test_a",
        "tests/test_mock_parser.py::TestMockParser::test_b",
    ],
    "tests/test_mock_optimizer.py": [
        "tests/test_mock_optimizer.py::TestMockOptimizer::test_a",
        "tests/test_mock_optimizer.py::TestMockOptimizer::test_b",
    ],
    "tests/test_mock_helper.py": [
        "tests/test_mock_helper.py::TestMockHelper::test_dates",
        "tests/test_mock_helper.py::TestMockHelper::test_units",
    ],
    "tests/test_mock_sqlite.py": [
        "tests/test_mock_sqlite.py::TestMockSQLite::test_generate",
        "tests/test_mock_sqlite.py::TestMockSQLite::test_roundtrip",
    ],
    "tests/test_mock_bigquery.py": [
        "tests/test_mock_bigquery.py::TestMockBigQuery::test_parse",
        "tests/test_mock_bigquery.py::TestMockBigQuery::test_qualify",
    ],
    "tests/dialects/test_mock_dialect.py": [
        "tests/dialects/test_mock_dialect.py::TestMockDialect::test_x",
        "tests/dialects/test_mock_dialect.py::TestMockDialect::test_y",
    ],
    # synthetic patch 08 anchors into this real test file
    "tests/test_parser.py": [
        "tests/test_mock_parser.py::TestMockParser::test_a",
        "tests/test_mock_parser.py::TestMockParser::test_b",
    ],
}
MOCK_SHARED = ["tests/helpers.py"]

MOCK_ANSWERS = {
    "mm1_helper_const": {"failing": [4], "kind": "code"},
    "mm2_fixture_change": {"failing": [2], "kind": "fixture"},
    "mm3_test_file_edit": {"failing": [1], "kind": "code"},
    "mm4_unknown_module": {"failing": [3], "kind": "code"},
}


def build_sources(pin: Path | None) -> None:
    archive_path = MOCK_DIR / "source.tar.gz"
    if pin is None:
        if not archive_path.is_file():
            raise SystemExit("bench/mockmaps/source.tar.gz missing; re-run with --pin /path/to/sqlglot")
        return
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative in SOURCE_FILES:
            path = pin / relative
            if not path.is_file():
                raise SystemExit(f"{path} not found; is --pin the pinned sqlglot checkout?")
            data = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    archive_path.write_bytes(buffer.getvalue())
    print(f"[mockmaps] wrote {archive_path} ({len(buffer.getvalue())} bytes, {len(SOURCE_FILES)} files)")


def read_sources() -> dict[str, str]:
    archive_path = MOCK_DIR / "source.tar.gz"
    sources: dict[str, str] = {}
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                sources[member.name] = tar.extractfile(member).read().decode("utf-8")
    return sources


def symbols_from_sources(sources: dict[str, str]) -> dict[str, list[list]]:
    symbols: dict[str, list[list]] = {}
    for path, text in sorted(sources.items()):
        tree = ast.parse(text)
        entries: list[list] = []

        def visit(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(child, ast.ClassDef) else "function"
                    qualname = f"{prefix}{child.name}"
                    entries.append([qualname, child.lineno, child.end_lineno, kind])
                    visit(child, qualname + ".")
                else:
                    visit(child, prefix)

        visit(tree, "")
        entries.sort(key=lambda entry: (entry[1], entry[2], entry[0]))
        symbols[path] = entries
    return symbols


def write_coverage(path: Path, coverage: dict[str, list[tuple[int, int, int]]]) -> None:
    files = sorted(coverage)
    header_size = 6 + 4
    for name in files:
        header_size += 2 + len(name.encode("utf-8")) + 8
    blocks: list[bytes] = []
    offsets: list[tuple[str, int]] = []
    cursor = header_size
    for name in files:
        per_test: dict[int, list[tuple[int, int]]] = {}
        for ordinal, start, end in coverage[name]:
            per_test.setdefault(ordinal, []).append((start, end))
        block = bytearray(struct.pack(">I", len(per_test)))
        for ordinal in sorted(per_test):
            runs = sorted(per_test[ordinal])
            block += struct.pack(">II", ordinal, len(runs))
            for start, end in runs:
                block += struct.pack(">II", start, end - start + 1)
        offsets.append((name, cursor))
        blocks.append(bytes(block))
        cursor += len(block)

    out = bytearray(b"BRcov1" + struct.pack(">I", len(files)))
    for name, offset in offsets:
        encoded = name.encode("utf-8")
        out += struct.pack(">H", len(encoded)) + encoded + struct.pack(">Q", offset)
    for block in blocks:
        out += block
    path.write_bytes(gzip.compress(bytes(out), mtime=0))


def write_mockmaps(sources: dict[str, str]) -> None:
    tests_index = [test_id for test_id, _ in MOCK_TESTS]
    (MOCK_DIR / "tests_index.json").write_text(
        json.dumps({"test_ids": tests_index}, indent=1) + "\n", "utf-8"
    )

    write_coverage(
        MOCK_DIR / "coverage.bin.gz",
        {path: list(items) for path, items in MOCK_COVERAGE.items()},
    )

    symbols = symbols_from_sources(sources)
    (MOCK_DIR / "symbols.json.gz").write_bytes(
        gzip.compress(json.dumps(symbols, sort_keys=True).encode("utf-8"), mtime=0)
    )
    (MOCK_DIR / "usage.json.gz").write_bytes(
        gzip.compress(json.dumps(MOCK_USAGE, sort_keys=True).encode("utf-8"), mtime=0)
    )
    fixtures = dict(MOCK_FIXTURE_MAP)
    fixtures["__test_files__"] = MOCK_TEST_FILES
    fixtures["__shared__"] = MOCK_SHARED
    (MOCK_DIR / "fixtures.json.gz").write_bytes(
        gzip.compress(json.dumps(fixtures, sort_keys=True).encode("utf-8"), mtime=0)
    )

    manifest = {
        "source": "mock",
        "pinned_sha": PINNED_SHA,
        "generator": "bench/mockmaps/build.py",
        "note": (
            "Hand-authored fiction: coverage/usage/fixture/test-file maps are invented and "
            "describe no real patch. source.tar.gz holds pinned sqlglot files only so the "
            "neutrality and EOF checks can run before real maps land."
        ),
        "source_files": SOURCE_FILES,
        "source_sha1": {
            path: hashlib.sha1(sources[path].encode("utf-8")).hexdigest() for path in sorted(sources)
        },
    }
    (MOCK_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", "utf-8")
    print(f"[mockmaps] wrote tests_index.json, coverage.bin.gz, symbols.json.gz, usage.json.gz, fixtures.json.gz, MANIFEST.json")


def helper_patch(source: str) -> str:
    """One-hunk diff changing the pinned DATE_UNITS constant."""
    lines = source.splitlines()
    line_number = next(
        index for index, line in enumerate(lines, start=1) if line.startswith("DATE_UNITS = {")
    )
    old = lines[line_number - 1]
    new = old[:-1].rstrip() + ', "decade"}'
    start = line_number - 2
    body = [
        " " + lines[start - 1],
        " " + lines[start],
        "-" + old,
        "+" + new,
        " " + lines[start + 2],
        " " + lines[start + 3],
    ]
    old_count = sum(1 for line in body if line[0] in " -")
    new_count = sum(1 for line in body if line[0] in " +")
    return (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/sqlglot/helper.py\n"
        "+++ b/sqlglot/helper.py\n"
        f"@@ -{start},{old_count} +{start},{new_count} @@\n"
        + "\n".join(body)
        + "\n"
    )


def write_mockdata(sources: dict[str, str]) -> None:
    commits = DATA_DIR / "commits"
    commits.mkdir(parents=True, exist_ok=True)

    tests = []
    for number, (test_id, seconds) in enumerate(MOCK_TESTS):
        tests.append(
            {
                "number": number,
                "id": test_id,
                "file": test_id.split("::", 1)[0],
                "seconds": seconds,
            }
        )
    (DATA_DIR / "tests.json").write_text(
        json.dumps({"tests": tests, "skipped": []}, indent=1) + "\n", "utf-8"
    )
    (DATA_DIR / "answers.json").write_text(
        json.dumps({"commits": MOCK_ANSWERS}, indent=1) + "\n", "utf-8"
    )

    (commits / "mm1_helper_const.patch").write_text(helper_patch(sources["sqlglot/helper.py"]), "utf-8")
    (commits / "mm2_fixture_change.patch").write_text(
        "diff --git a/tests/fixtures/optimizer/pushdown_predicates.sql "
        "b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "index 0000000..2222222 100644\n"
        "--- a/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "+++ b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "@@ -140,3 +140,3 @@\n"
        " SELECT x.a FROM x\n"
        "-SELECT 1\n"
        "+SELECT 2\n"
        " SELECT 3\n",
        "utf-8",
    )
    (commits / "mm3_test_file_edit.patch").write_text(
        "diff --git a/tests/test_parser.py b/tests/test_parser.py\n"
        "index 0000000..3333333 100644\n"
        "--- a/tests/test_parser.py\n"
        "+++ b/tests/test_parser.py\n"
        "@@ -10,2 +10,3 @@ class TestParser(unittest.TestCase):\n"
        "     def test_union(self):\n"
        "         self.assertEqual(self.validate_identity(\"SELECT 1\"), \"SELECT 1\")\n"
        "+        self.assertTrue(True)\n",
        "utf-8",
    )
    (commits / "mm4_unknown_module.patch").write_text(
        "diff --git a/sqlglot/planner.py b/sqlglot/planner.py\n"
        "index 0000000..4444444 100644\n"
        "--- a/sqlglot/planner.py\n"
        "+++ b/sqlglot/planner.py\n"
        "@@ -13,2 +13,3 @@\n"
        " class Plan:\n"
        "     def __init__(self, expression):\n"
        "+        pass\n",
        "utf-8",
    )
    print(f"[mockdata] wrote {DATA_DIR}/tests.json, answers.json, commits/*.patch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", default=None, help="pinned sqlglot checkout (rebuilds source.tar.gz)")
    args = parser.parse_args(argv)
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    build_sources(Path(args.pin) if args.pin else None)
    sources = read_sources()
    write_mockmaps(sources)
    write_mockdata(sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
