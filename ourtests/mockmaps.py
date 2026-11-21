"""Handcrafted mock maps and synthetic /data for the selector unit tests.

Nothing here is derived from real sqlglot: the two source files under
``fixtures/mockrepo/``, the coverage intervals, symbols, usage, fixtures and
patches are all invented.  ``build_maps`` writes the exact MAPS_SPEC.md binary
layout (gzip header + offsets, little hand-rolled) so the production readers
are exercised, not stubs.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import struct
import tarfile
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "mockrepo"

SOURCE_FILES = (
    "sqlglot/__init__.py",
    "sqlglot/consts.py",
    "sqlglot/core.py",
    "sqlglot/dialects/duck.py",
    "sqlglot/dialects/gander.py",
    "sqlglot/mod.py",
    "sqlglot/other.py",
)

TESTS = [
    {
        "number": 0,
        "id": "tests/test_mod.py::test_alpha",
        "file": "tests/test_mod.py",
        "seconds": 0.2,
    },
    {
        "number": 1,
        "id": "tests/test_mod.py::test_beta",
        "file": "tests/test_mod.py",
        "seconds": 0.1,
    },
    {
        "number": 2,
        "id": "tests/test_mod.py::test_gamma",
        "file": "tests/test_mod.py",
        "seconds": 0.1,
    },
    {
        "number": 3,
        "id": "tests/test_other.py::test_metadata",
        "file": "tests/test_other.py",
        "seconds": 0.3,
    },
    {
        "number": 4,
        "id": "tests/test_other.py::test_extra",
        "file": "tests/test_other.py",
        "seconds": 0.05,
    },
    {
        "number": 5,
        "id": "tests/test_mod.py::test_delta",
        "file": "tests/test_mod.py",
        "seconds": 0.25,
    },
    {
        "number": 6,
        "id": "tests/test_other.py::test_gesundheit",
        "file": "tests/test_other.py",
        "seconds": 0.1,
    },
]

TEST_IDS = [test["id"] for test in TESTS]

# sqlglot/mod.py layout (pinned fixture):
#  1 import, 3 WEEK_OFFSET, 6-7 alpha, 10-18 Helper, 11 LIMIT,
#  13-14 beta, 17-18 gamma, 21-22 delta.
SYMBOLS = {
    "sqlglot/mod.py": [
        ["alpha", 6, 7, "function"],
        ["Helper", 10, 18, "class"],
        ["Helper.beta", 13, 14, "function"],
        ["Helper.gamma", 17, 18, "function"],
        ["delta", 21, 22, "function"],
    ],
    "sqlglot/other.py": [],
    # 1 TokenType import, 3-7 LIMITS, 9-12 OPTIONS, 14-17 SPARSE,
    # 19 BY_TOKEN, 22-23 read_limit, 26-30 class Registry.
    "sqlglot/consts.py": [["read_limit", 22, 23, "function"], ["Registry", 26, 30, "class"]],
    # 1 import, 4-17 class Duck, 16-17 Duck.emit.
    "sqlglot/dialects/duck.py": [
        ["Duck", 4, 17, "class"],
        ["Duck.emit", 16, 17, "function"],
    ],
    # 4-5 class Gander(Duck): derived dialect (inheritance-aware narrowing).
    "sqlglot/dialects/gander.py": [["Gander", 4, 5, "class"]],
    # 1 VERSION, 4-5 bump.
    "sqlglot/core.py": [["bump", 4, 5, "function"]],
    "sqlglot/__init__.py": [],
}

# Keyed by the file where NAME is defined (W1 layout): the bare line numbers
# are references inside that same file.
USAGE = {
    "sqlglot/mod.py": {
        "WEEK_OFFSET": [3, 7],
        "LIMIT": [11, 14],
    },
    "sqlglot/other.py": {
        "METADATA": [1, 7],
        "EXTRA": [7],
    },
    "sqlglot/consts.py": {
        "LIMITS": [23],
        "OPTIONS": [23],
        "SPARSE": [23],
        "BY_TOKEN": [23],
        "TABLE": [23],
        "SET_KEYWORDS": [23],
    },
    "sqlglot/dialects/duck.py": {
        "TOKENS": [5, 17],
        "OFFSET": [14],
    },
    "sqlglot/core.py": {"VERSION": [5]},
}

FIXTURES = {    "tests/fixtures/a.sql": ["tests/test_mod.py::test_beta"],
    "tests/fixtures/b.sql": ["tests/test_other.py::test_extra"],
    "__test_files__": {
        "tests/test_mod.py": [
            "tests/test_mod.py::test_alpha",
            "tests/test_mod.py::test_beta",
            "tests/test_mod.py::test_gamma",
            "tests/test_mod.py::test_delta",
        ],
        "tests/test_other.py": [
            "tests/test_other.py::test_metadata",
            "tests/test_other.py::test_extra",
        ],
    },
    "__shared__": ["tests/helpers.py"],
}

# W1 extension: cross-file references only.  Under the W1 layout usage.json.gz
# holds same-file reference lines keyed by the defining file.
USAGE_REFS = {
    "METADATA": {"sqlglot/mod.py": [7]},
    # A definition line of a *different* OPTIONS constant: reference maps list
    # it, but it must not contribute tests (it executes at import time).
    "OPTIONS": {"sqlglot/dialects/gander.py": [5]},
}

TEST_IDS_ORDINAL = [test["id"] for test in TESTS]

# v2 maps (handcrafted, same formats as tools/mapgen writes).
DIALECTS = {
    "exposure": {
        TEST_IDS_ORDINAL[0]: ["duck"],
        TEST_IDS_ORDINAL[1]: ["default"],
        TEST_IDS_ORDINAL[2]: ["default"],
        TEST_IDS_ORDINAL[3]: ["duck", "default"],
        TEST_IDS_ORDINAL[4]: ["duck"],
        TEST_IDS_ORDINAL[5]: ["default"],
        TEST_IDS_ORDINAL[6]: ["gander"],
    },
    "file_scope": {
        "sqlglot/dialects/duck.py": "duck",
        "sqlglot/dialects/gander.py": "gander",
    },
    "class_scope": {
        "sqlglot/dialects/duck.py": {"Duck": "duck"},
        "sqlglot/dialects/gander.py": {"Gander": "gander"},
    },
}

KEYREADS = {
    "exposure": {
        TEST_IDS_ORDINAL[0]: [
            ["LIMITS", "a"],
            ["SET_KEYWORDS", "one"],
            ["OPTIONS", "x"],
            ["SPARSE", "m"],
            ["Registry.TABLE", "p"],
        ],
        TEST_IDS_ORDINAL[1]: [
            ["LIMITS", "a"],
            ["SET_KEYWORDS", "one"],
            ["OPTIONS", "x"],
            ["SPARSE", "m"],
            ["Registry.TABLE", "p"],
        ],
        TEST_IDS_ORDINAL[2]: [
            ["LIMITS", "c"],
            ["SET_KEYWORDS", "one"],
            ["OPTIONS", "x"],
            ["Registry.TABLE", "p"],
        ],
        TEST_IDS_ORDINAL[3]: [
            ["LIMITS", "a"],
            ["SET_KEYWORDS", "one"],
            ["OPTIONS", "x"],
            ["Registry.TABLE", "p"],
        ],
        TEST_IDS_ORDINAL[4]: [
            ["LIMITS", "a"],
            ["SET_KEYWORDS", "one"],
            ["OPTIONS", "x"],
            ["Registry.TABLE", "p"],
        ],
        TEST_IDS_ORDINAL[5]: [],
    },
    "usage_kind": {
        "sqlglot/consts.py": {
            "LIMITS": "keyable",
            "OPTIONS": "opaque",
            "SPARSE": "keyable",
            "BY_TOKEN": "opaque",
            "Registry.TABLE": "keyable",
            "SET_KEYWORDS": "keyable",
        },
    },
}

TESTIMPORTS = {
    "tests/test_mod.py": ["sqlglot.core", "sqlglot.mod"],
    "tests/test_other.py": ["sqlglot.core"],
}

# Per file: [(test ordinal, [(start_line, run_length), ...]), ...]
COVERAGE = {
    "sqlglot/mod.py": [
        (0, [(6, 2)]),
        (1, [(13, 2)]),
        (2, [(17, 2)]),
        (5, [(21, 2)]),
    ],
    "sqlglot/other.py": [
        (3, [(1, 4)]),
        (4, [(7, 1)]),
    ],
    # Every test reads LIMITS (line 23); test_beta also touches the literal
    # lines, test_delta only the class body (the L3 visibility case).
    "sqlglot/consts.py": [
        (0, [(23, 1)]),
        (1, [(3, 5), (23, 1), (32, 4)]),
        (2, [(23, 1)]),
        (3, [(23, 1)]),
        (4, [(23, 1)]),
        (5, [(23, 1)]),
    ],
    "sqlglot/dialects/duck.py": [
        (0, [(16, 2)]),
        (2, [(17, 1)]),
        (3, [(5, 8)]),
        (4, [(14, 1)]),
        (6, [(17, 1)]),
    ],
    "sqlglot/core.py": [(0, [(4, 2)])],
    "sqlglot/dialects/gander.py": [(6, [(5, 1)])],
}

PATCHES = {
    "code-alpha": """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
index 1111111..2222222 100644
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,1 @@ def alpha(x):
-    return x + WEEK_OFFSET + METADATA["a"]
+    return x + WEEK_OFFSET + METADATA["b"]
""",
    "class-body": """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
index 1111111..3333333 100644
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -11,1 +11,1 @@ class Helper:
-    LIMIT = 3
+    LIMIT = 4
""",
    "module-dict": """\
diff --git a/sqlglot/other.py b/sqlglot/other.py
index 4444444..5555555 100644
--- a/sqlglot/other.py
+++ b/sqlglot/other.py
@@ -3,1 +3,1 @@
-    "b": 2,
+    "b": 3,
""",
    "fixture": """\
diff --git a/tests/fixtures/a.sql b/tests/fixtures/a.sql
index 6666666..7777777 100644
--- a/tests/fixtures/a.sql
+++ b/tests/fixtures/a.sql
@@ -1,1 +1,1 @@
-SELECT 1;
+SELECT 2;
""",
    "testpy": """\
diff --git a/tests/test_other.py b/tests/test_other.py
index 8888888..9999999 100644
--- a/tests/test_other.py
+++ b/tests/test_other.py
@@ -1,1 +1,1 @@
-def test_metadata():
+def test_metadata():  # touched
""",
    "malformed": """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
index 1111111..2222222 100644
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,1 @@ def alpha(x):
-    return x + WEEK_OFFSET + METADATA["a"]
-    return x
+    return x
""",
    "comment": """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
index 1111111..2222222 100644
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,2 @@ def alpha(x):
     return x + WEEK_OFFSET + METADATA["a"]
+    # explain the expression
""",
    # -- v2 refinement scenarios ------------------------------------------
    "dialect-class-body": """\
diff --git a/sqlglot/dialects/duck.py b/sqlglot/dialects/duck.py
--- a/sqlglot/dialects/duck.py
+++ b/sqlglot/dialects/duck.py
@@ -5,5 +5,5 @@ class Duck:
     TOKENS = {
         "a": 1,
-        "b": 2,
+        "b": 3,
         "c": 3,
         "d": 4,
""",
    "dialect-symbol": """\
diff --git a/sqlglot/dialects/duck.py b/sqlglot/dialects/duck.py
--- a/sqlglot/dialects/duck.py
+++ b/sqlglot/dialects/duck.py
@@ -17,1 +17,1 @@ class Duck:
-        return x + self.TOKENS["a"]
+        return x + self.TOKENS["b"]
""",
    "keyread-collapse": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -4,3 +4,3 @@
     "a": 1,
-    "b": 2,
+    "b": 3,
     "c": 3,
""",
    "keyread-readers": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -4,3 +4,3 @@
-    "a": 1,
+    "a": 2,
     "b": 2,
     "c": 3,
""",
    "keyread-opaque": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -10,3 +10,3 @@
     "x": 1,
-    "y": 2,
+    "y": 3,
 }
""",
    "keyread-sparse": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -15,3 +15,3 @@
     "m": 1,
-    "n": 2,
+    "n": 3,
 }
""",
    "keyread-ambiguous": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -19,1 +19,1 @@
-BY_TOKEN = {TokenType.SELECT: 1, TokenType.INSERT: 2}
+BY_TOKEN = {TokenType.SELECT: 1, TokenType.INSERT: 3}
""",
    "keyread-set": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -33,3 +33,3 @@
     "one",
-    "two",
+    "three",
 }
""",
    "keyread-class-qualified": """\
diff --git a/sqlglot/consts.py b/sqlglot/consts.py
--- a/sqlglot/consts.py
+++ b/sqlglot/consts.py
@@ -28,3 +28,3 @@ class Registry:
         "p": 1,
-        "q": 2,
+        "q": 3,
     }
""",
    "import-line": """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -1,1 +1,1 @@
-from sqlglot.other import METADATA
+from sqlglot.other import METADATA, EXTRA
""",
    "package-init": """\
diff --git a/sqlglot/__init__.py b/sqlglot/__init__.py
--- a/sqlglot/__init__.py
+++ b/sqlglot/__init__.py
@@ -3,1 +3,1 @@
-from sqlglot.mod import WEEK_OFFSET
+from sqlglot.mod import WEEK_OFFSET, delta
""",
    "shared-module": """\
diff --git a/sqlglot/core.py b/sqlglot/core.py
--- a/sqlglot/core.py
+++ b/sqlglot/core.py
@@ -1,1 +1,1 @@
-VERSION = 1
+VERSION = 2
""",
    "core-function": """\
diff --git a/sqlglot/core.py b/sqlglot/core.py
--- a/sqlglot/core.py
+++ b/sqlglot/core.py
@@ -5,1 +5,1 @@ def bump(x):
-    return x + VERSION
+    return x + VERSION + 1
""",
    "new-conftest": """\
diff --git a/tests/conftest.py b/tests/conftest.py
new file mode 100644
--- /dev/null
+++ b/tests/conftest.py
@@ -0,0 +1,2 @@
+def helper():
+    return 1
""",
}

# Selected test numbers per synthetic commit (Tier E includes every test).
EXPECTATIONS = {
    "class-body": {1},
    "code-alpha": {0},
    "comment": set(),
    "fixture": {1},
    "malformed": {0, 1, 2, 3, 4, 5, 6},
    "module-dict": {0, 3, 4},
    "testpy": {3, 4, 6},
    # v2 refinements.  Dialect narrowing drops test_gamma (default-only).
    "dialect-class-body": {0, 3, 6},
    "dialect-symbol": {0, 4, 6},
    # L3: the changed key has no readers, so the collapse is the key-readers.
    "keyread-collapse": {0, 1, 2, 3, 4},
    # Gates that must keep L1 (all six tests read LIMITS).
    "keyread-readers": {0, 1, 2, 3, 4, 5},
    "keyread-opaque": {0, 1, 2, 3, 4, 5},
    "keyread-sparse": {0, 1, 2, 3, 4, 5},
    "keyread-ambiguous": {0, 1, 2, 3, 4, 5},
    "keyread-class-qualified": {0, 1, 2, 3, 4},
    "keyread-set": {0, 1, 2, 3, 4},
    # Import-region / package root / >80%-imported module / new conftest.
    "import-line": {0, 1, 2, 3, 4, 5, 6},
    "package-init": {0, 1, 2, 3, 4, 5, 6},
    "shared-module": {0, 1, 2, 3, 4, 5, 6},
    "core-function": {0},
    "new-conftest": {0, 1, 2, 3, 4, 5, 6},
}


def number_by_id() -> dict[str, int]:
    return {test["id"]: test["number"] for test in TESTS}


def tests_by_file() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for test in TESTS:
        grouped.setdefault(test["file"], []).append(test["id"])
    return grouped


def source_text(relative: str) -> str:
    return (FIXTURE_ROOT / relative).read_text(encoding="utf-8")


def _write_gz_json(path: Path, document: dict) -> None:
    path.write_bytes(gzip.compress(json.dumps(document).encode("utf-8"), mtime=0))


def _coverage_blob() -> bytes:
    names = sorted(COVERAGE)
    sections: list[bytes] = []
    for name in names:
        section = bytearray()
        entries = COVERAGE[name]
        section += struct.pack(">I", len(entries))
        for ordinal, intervals in entries:
            section += struct.pack(">II", ordinal, len(intervals))
            for start, length in intervals:
                section += struct.pack(">II", start, length)
        sections.append(bytes(section))

    header = bytearray(b"BRcov1")
    header += struct.pack(">I", len(names))
    offset = len(header) + sum(2 + len(name.encode("utf-8")) + 8 for name in names)
    for name, section in zip(names, sections):
        encoded = name.encode("utf-8")
        header += struct.pack(">H", len(encoded)) + encoded
        header += struct.pack(">Q", offset)
        offset += len(section)
    return bytes(header) + b"".join(sections)


def _write_source_tar(path: Path) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for relative in SOURCE_FILES:
            data = (FIXTURE_ROOT / relative).read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))


def build_maps(target: Path | str, with_v2: bool = True) -> Path:
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "tests_index.json").write_text(
        json.dumps({"test_ids": TEST_IDS}), encoding="utf-8"
    )
    _write_gz_json(target / "symbols.json.gz", SYMBOLS)
    _write_gz_json(target / "usage.json.gz", USAGE)
    _write_gz_json(target / "usage_refs.json.gz", USAGE_REFS)
    _write_gz_json(target / "fixtures.json.gz", FIXTURES)
    (target / "coverage.bin.gz").write_bytes(gzip.compress(_coverage_blob(), mtime=0))
    _write_source_tar(target / "source.tar.gz")
    if with_v2:
        _write_gz_json(target / "dialects.json.gz", DIALECTS)
        _write_gz_json(target / "keyreads.json.gz", KEYREADS)
        _write_gz_json(target / "testimports.json.gz", TESTIMPORTS)
    manifest = {
        "pin": "mock",
        "sqlglot_sha1": {
            relative: hashlib.sha1((FIXTURE_ROOT / relative).read_bytes()).hexdigest()
            for relative in SOURCE_FILES
        },
    }
    (target / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return target


def build_data(root: Path | str) -> tuple[Path, Path]:
    """Write a synthetic /data (tests.json + commits) and a maps dir."""
    root = Path(root)
    data = root / "data"
    maps = root / "maps"
    (data / "commits").mkdir(parents=True, exist_ok=True)
    (data / "tests.json").write_text(
        json.dumps({"tests": TESTS, "skipped": []}), encoding="utf-8"
    )
    for name, text in PATCHES.items():
        (data / "commits" / f"{name}.patch").write_text(text, encoding="utf-8")
    build_maps(maps)
    return data, maps
