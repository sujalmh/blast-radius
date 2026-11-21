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
    "sqlglot/blank.py",
    "sqlglot/consts.py",
    "sqlglot/core.py",
    "sqlglot/dialects/beam.py",
    "sqlglot/dialects/crane.py",
    "sqlglot/dialects/duck.py",
    "sqlglot/dialects/gander.py",
    "sqlglot/enums.py",
    "sqlglot/expr.py",
    "sqlglot/lonely.py",
    "sqlglot/mod.py",
    "sqlglot/optimizer/simplify.py",
    "sqlglot/other.py",
    "sqlglot/pairs.py",
    "sqlglot/tokens.py",
    "sqlglot/wide.py",
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
    # L4 fixtures: Expression.validate reads self.ARG_TYPES (generic base-class
    # read, covered by every test); Trigger/TriggerChild are its subclasses.
    "sqlglot/expr.py": [
        ["Expression", 1, 8, "class"],
        ["Expression.validate", 6, 8, "function"],
        ["Trigger", 11, 18, "class"],
        ["Trigger.render", 17, 18, "function"],
        ["TriggerChild", 21, 22, "class"],
        ["build_trigger", 25, 26, "function"],
    ],
    # 1-5 class Lonely, 8-9 peek (the non-instance TAG read).
    "sqlglot/lonely.py": [
        ["Lonely", 1, 5, "class"],
        ["Lonely.show", 4, 5, "function"],
        ["peek", 8, 9, "function"],
    ],
    # 1-7 class Crane (dialect-scoped L4 case), 6-7 Crane.span.
    "sqlglot/dialects/crane.py": [
        ["Crane", 1, 7, "class"],
        ["Crane.span", 6, 7, "function"],
    ],
    # 1-5 class Beam(Dialect-analog, dialect-scoped L4 through class_scope),
    # 4-5 Beam.shift reads self.WEEK_OFFSET.
    "sqlglot/dialects/beam.py": [
        ["Beam", 1, 5, "class"],
        ["Beam.shift", 4, 5, "function"],
    ],
    # 1-3 TokenType, 6-10 class Tokenizer (K UNKNOWN L4 case), 9-10 escape.
    "sqlglot/tokens.py": [
        ["TokenType", 1, 3, "class"],
        ["Tokenizer", 6, 10, "class"],
        ["Tokenizer.escape", 9, 10, "function"],
    ],
    # 1-2 week_offset reads `dialect.WEEK_OFFSET`, 5-6 numeric_escape reads
    # `tokenizer.NUMERIC_ESCAPES` (both param receivers = instance_lines).
    "sqlglot/optimizer/simplify.py": [
        ["week_offset", 1, 2, "function"],
        ["numeric_escape", 5, 6, "function"],
    ],
    # W2f fixtures: enum-member comparisons (enum neutrality) and two
    # function bodies with deliberately sparse line coverage (line bias).
    "sqlglot/enums.py": [],
    "sqlglot/wide.py": [["wide", 6, 12, "function"]],
    "sqlglot/blank.py": [["lonely", 4, 11, "function"]],
    "sqlglot/pairs.py": [["pairs", 6, 10, "function"]],
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
    # Definition lines are included on purpose: the selector must drop them.
    "sqlglot/expr.py": {
        "ARG_TYPES": [2, 7, 12, 18, 22, 26],
    },
    "sqlglot/lonely.py": {"TAG": [2, 5]},
    "sqlglot/dialects/crane.py": {"SPANS": [2, 7]},
    # Definition line + the self-read inside Beam.shift.
    "sqlglot/dialects/beam.py": {"WEEK_OFFSET": [2, 5]},
    # Definition line + the self-read inside Tokenizer.escape.
    "sqlglot/tokens.py": {"NUMERIC_ESCAPES": [7, 10]},
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
    # Parameter-receiver reads (the 535z `dialect.WEEK_OFFSET` shape and the
    # Tokenizer mirror shape).
    "WEEK_OFFSET": {"sqlglot/optimizer/simplify.py": [2]},
    "NUMERIC_ESCAPES": {"sqlglot/optimizer/simplify.py": [6]},
}

# W1h committed enum-flip map: the pinned lines whose comparisons all pass the
# enum proof (both operands provably members of one sqlglot Enum that defines
# no __eq__/__ne__).  For the mock tree that is exactly the two neutral W2f
# cases -- `self.selected is/== Strategy.UPPER` (line 24, every assignment site
# of `selected` is a Strategy member) and the annotated parameter (line 27);
# the unproven/None/custom-__eq__ lines are deliberately absent.
ENUM_FLIPS = {"lines": {"sqlglot/enums.py": [24, 27]}}

TEST_IDS_ORDINAL = [test["id"] for test in TESTS]

# v2 maps (handcrafted, same formats as tools/mapgen writes).
DIALECTS = {
    "exposure": {
        TEST_IDS_ORDINAL[0]: ["duck", "crane"],
        TEST_IDS_ORDINAL[1]: ["default"],
        TEST_IDS_ORDINAL[2]: ["default", "crane"],
        TEST_IDS_ORDINAL[3]: ["duck", "default"],
        TEST_IDS_ORDINAL[4]: ["duck", "beam"],
        TEST_IDS_ORDINAL[5]: ["default"],
        TEST_IDS_ORDINAL[6]: ["gander"],
    },
    "file_scope": {
        "sqlglot/dialects/beam.py": "beam",
        "sqlglot/dialects/crane.py": "crane",
        "sqlglot/dialects/duck.py": "duck",
        "sqlglot/dialects/gander.py": "gander",
    },
    "class_scope": {
        "sqlglot/dialects/beam.py": {"Beam": "beam"},
        "sqlglot/dialects/crane.py": {"Crane": "crane"},
        "sqlglot/dialects/duck.py": {"Duck": "duck"},
        "sqlglot/dialects/gander.py": {"Gander": "gander"},
    },
}

# L4 maps (formats in MAPS_SPEC.md; the mock spells classes both ways on
# purpose: module-qualified in ``touched``, package-qualified in ``hierarchy``).
CLASSES = {
    "touched": {
        TEST_IDS_ORDINAL[0]: [
            "sqlglot.expr.Expression",
            "sqlglot.lonely.Lonely",
            "sqlglot.dialects.crane.Crane",
        ],
        TEST_IDS_ORDINAL[1]: ["sqlglot.expr.Trigger"],
        TEST_IDS_ORDINAL[2]: ["sqlglot.expr.Trigger"],
        TEST_IDS_ORDINAL[3]: ["sqlglot.expr.Expression"],
        TEST_IDS_ORDINAL[4]: [
            "sqlglot.expr.TriggerChild",
            "sqlglot.dialects.crane.Crane",
        ],
        TEST_IDS_ORDINAL[5]: ["sqlglot.expr.Expression", "sqlglot.lonely.Lonely"],
        TEST_IDS_ORDINAL[6]: ["sqlglot.TriggerChild"],
    },
    "hierarchy": {
        "sqlglot.expr.Expression": [],
        "sqlglot.expr.Trigger": ["sqlglot.expr.Expression"],
        "sqlglot.TriggerChild": ["sqlglot.expr.Trigger"],
        "sqlglot.lonely.Lonely": [],
        "sqlglot.dialects.crane.Crane": [],
        # A Tokenizer hierarchy leaf: the class graph knows it, but it is not an
        # expression class, so L4's universe stays UNKNOWN for it.
        "sqlglot.tokens.Tokenizer": [],
    },
}

# L4: reference lines of a name split by receiver kind (W1f format).
# ``TAG`` is deliberately absent (constructors + class spans only); ``SPANS``
# has no non-instance reads at all.  ``WEEK_OFFSET``/``NUMERIC_ESCAPES`` read
# through a *parameter* receiver in optimizer/simplify.py: instance_lines.
# Their real failing test is the one reading that line, and only the family
# universe decides whether it survives L4.
#
#   week_offset:     test_alpha covers Beam's span (0)      -> spans
#                    test_extra reads simplify.py:2 (4)     -> beam exposure
#                    test_delta reads simplify.py:2 (5)     -> NOT beam-exposed
#   numeric_escape:  test_metadata covers Tokenizer's span (3) -> spans
#                    test_delta reads simplify.py:6 (5)        -> instance_lines
USAGE_STYLE = {
    "ARG_TYPES": {
        "sqlglot/expr.py": {
            "self_lines": [7, 18],
            "class_lines": [26],
            "instance_lines": [],
        },
    },
    "SPANS": {
        "sqlglot/dialects/crane.py": {
            "self_lines": [7],
            "class_lines": [],
            "instance_lines": [],
        },
    },
    "WEEK_OFFSET": {
        "sqlglot/dialects/beam.py": {
            "self_lines": [5],
            "class_lines": [],
            "instance_lines": [],
        },
        "sqlglot/optimizer/simplify.py": {
            "self_lines": [],
            "class_lines": [],
            "instance_lines": [2],
        },
    },
    "NUMERIC_ESCAPES": {
        "sqlglot/tokens.py": {
            "self_lines": [10],
            "class_lines": [],
            "instance_lines": [],
        },
        "sqlglot/optimizer/simplify.py": {
            "self_lines": [],
            "class_lines": [],
            "instance_lines": [6],
        },
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
    # L4: every test executes Expression.validate (the generic self.ARG_TYPES
    # read); only test_beta/test_extra run Trigger.render; test_gesundheit runs
    # the module-level static read; test_gamma covers Crane.span.
    "sqlglot/expr.py": [
        (0, [(6, 3)]),
        (1, [(6, 3), (17, 2)]),
        (2, [(6, 3)]),
        (3, [(6, 3)]),
        (4, [(6, 3), (17, 2)]),
        (5, [(6, 3)]),
        (6, [(6, 3), (25, 2)]),
    ],
    # test_metadata runs Lonely.show; test_gamma reads Lonely.TAG statically.
    "sqlglot/lonely.py": [(2, [(9, 1)]), (3, [(4, 2)])],
    "sqlglot/dialects/crane.py": [(2, [(6, 2)]), (4, [(6, 2)])],
    # L4 universe cases: Beam's span is covered by test_alpha (0), while the
    # only evidence for test_extra (4) is the parameter read it covers.
    "sqlglot/dialects/beam.py": [(0, [(1, 5)])],
    # Tokenizer's span is covered by test_metadata (3); the parameter read of
    # NumericEscapes is covered only by test_delta (5).
    "sqlglot/tokens.py": [(3, [(6, 5)])],
    "sqlglot/optimizer/simplify.py": [(4, [(2, 1)]), (5, [(2, 1), (6, 1)])],
    # Line bias: `wide` spans 6-12; changed line 11 (block 2) is covered by
    # test_beta (12) and test_gamma (11), while test_alpha only reaches line 7
    # (block 1).  `lonely` spans 4-11 but only line 11 (block 2) is covered, so
    # a change on line 5 has empty line coverage and must fall back to the span.
    "sqlglot/wide.py": [(0, [(7, 1)]), (1, [(12, 1)]), (2, [(11, 1)])],
    "sqlglot/blank.py": [(1, [(11, 1)])],
    "sqlglot/pairs.py": [(0, [(7, 1)]), (1, [(10, 1)])],
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
    # -- L4 class-attribute scenarios --------------------------------------
    "l4-subclass-attr": """\
diff --git a/sqlglot/expr.py b/sqlglot/expr.py
--- a/sqlglot/expr.py
+++ b/sqlglot/expr.py
@@ -12,4 +12,4 @@ class Trigger(Expression):
     ARG_TYPES = {
         "timing": True,
-        "constraint": False,
+        "constraint": True,
     }
""",
    "l4-base-attr": """\
diff --git a/sqlglot/expr.py b/sqlglot/expr.py
--- a/sqlglot/expr.py
+++ b/sqlglot/expr.py
@@ -2,3 +2,3 @@ class Expression:
     ARG_TYPES = {
-        "this": True,
+        "this": False,
     }
""",
    "l4-missing-usage": """\
diff --git a/sqlglot/lonely.py b/sqlglot/lonely.py
--- a/sqlglot/lonely.py
+++ b/sqlglot/lonely.py
@@ -2,1 +2,1 @@ class Lonely:
-    TAG = {"a": 1}
+    TAG = {"a": 2}
""",
    "l4-dialect": """\
diff --git a/sqlglot/dialects/crane.py b/sqlglot/dialects/crane.py
--- a/sqlglot/dialects/crane.py
+++ b/sqlglot/dialects/crane.py
@@ -2,3 +2,3 @@
     SPANS = {
-        "a": 1,
+        "a": 2,
     }
""",
    # 535z-shaped: a dialect class-body attr whose real failing test reads it
    # through a *parameter* receiver (instance_lines).  Only the dialect-path
    # universe (= exposure of `beam`) can keep test_extra here.
    "l4-dialect-param": """\
diff --git a/sqlglot/dialects/beam.py b/sqlglot/dialects/beam.py
--- a/sqlglot/dialects/beam.py
+++ b/sqlglot/dialects/beam.py
@@ -2,1 +2,1 @@ class Beam:
-    WEEK_OFFSET = 1
+    WEEK_OFFSET = 2
""",
    # Mirror: a Tokenizer class-body attr.  K is UNKNOWN (instance creation is
    # not traced), so the instance_lines reader must stay in the union.
    "l4-tokenizer-instance": """\
diff --git a/sqlglot/tokens.py b/sqlglot/tokens.py
--- a/sqlglot/tokens.py
+++ b/sqlglot/tokens.py
@@ -7,1 +7,1 @@ class Tokenizer:
-    NUMERIC_ESCAPES = {"x": 1}
+    NUMERIC_ESCAPES = {"x": 2}
""",
    # -- W2f: enum-member neutrality --------------------------------------
    # Every operand is a proven member of `Strategy` (an Enum without its own
    # __eq__), so `is` -> `==` is a no-op: the commit must be neutral.
    "enum-neutral-flip": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -24,1 +24,1 @@ class Chooser:
-        return self.selected is Strategy.UPPER
+        return self.selected == Strategy.UPPER
""",
    # The other operand is a parameter annotated with the enum class: neutral.
    "enum-annotated": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -27,1 +27,1 @@ class Chooser:
-        return mode is Strategy.UPPER
+        return mode == Strategy.UPPER
""",
    # A parameter can be rebound to a plain string: the assignment proof only
    # sees enum-valued sites, so a parameter operand is never proven this way.
    "enum-rebound-param": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -41,1 +41,1 @@ class Chooser:
-        return self.rebound_selected is Strategy.UPPER
+        return self.rebound_selected == Strategy.UPPER
""",
    # Tuple targets are assignment sites too; this one binds plain strings.
    "enum-tuple-target": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -45,1 +45,1 @@ class Chooser:
-        return mode is Strategy.UPPER
+        return mode == Strategy.UPPER
""",
    # An untyped/str-capable operand is NOT provably a member: keep selecting.
    "enum-str-operand": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -30,1 +30,1 @@ class Chooser:
-        return other is Strategy.UPPER
+        return other == Strategy.UPPER
""",
    # `is None` vs `== None` is a real change (Expression.__eq__): not neutral.
    "enum-none-flip": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -33,1 +33,1 @@ class Chooser:
-        return self.selected is None
+        return self.selected == None
""",
    # An enum that defines __eq__ in its own body loses identity semantics.
    "enum-custom-eq": """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -36,1 +36,1 @@ class Chooser:
-        return self.selected is EqStrategy.A
+        return self.selected == EqStrategy.A
""",
    # -- W2f: line-biased symbol selection --------------------------------
    # `wide` (6-12): the changed line's coverers are a strict subset.
    "line-bias-subset": """\
diff --git a/sqlglot/wide.py b/sqlglot/wide.py
--- a/sqlglot/wide.py
+++ b/sqlglot/wide.py
@@ -11,1 +11,1 @@ def wide(flag):
-        return a
+        return c
""",
    # Empty line coverage on the changed line: fall back to the whole symbol.
    "line-bias-empty": """\
diff --git a/sqlglot/blank.py b/sqlglot/blank.py
--- a/sqlglot/blank.py
+++ b/sqlglot/blank.py
@@ -5,1 +5,1 @@ def lonely(x):
-    y = x + 1
+    y = x + 2
""",
    # A def/signature edit is never line-biased.
    "line-bias-def": """\
diff --git a/sqlglot/wide.py b/sqlglot/wide.py
--- a/sqlglot/wide.py
+++ b/sqlglot/wide.py
@@ -6,1 +6,1 @@
-def wide(flag):
+def wide(other):
""",
    # A pure insertion has no changed old line: whole symbol.
    "line-bias-insertion": """\
diff --git a/sqlglot/wide.py b/sqlglot/wide.py
--- a/sqlglot/wide.py
+++ b/sqlglot/wide.py
@@ -8,0 +9,1 @@
+    inserted = 0
""",
    # A *rewritten* statement head keeps its line bias: entering the body still
    # requires executing this very line.
    "line-bias-statement-head": """\
diff --git a/sqlglot/wide.py b/sqlglot/wide.py
--- a/sqlglot/wide.py
+++ b/sqlglot/wide.py
@@ -10,1 +10,1 @@
-    if flag:
+    if not flag:
""",
    # A *deleted* statement head can move the body out of the pre-patch
    # coverage, so it falls back to the whole symbol.
    "line-bias-delete-head": """\
diff --git a/sqlglot/wide.py b/sqlglot/wide.py
--- a/sqlglot/wide.py
+++ b/sqlglot/wide.py
@@ -10,2 +10,1 @@
-    if flag:
-        return a
+    return c
""",
    # Two hunks in one symbol must contribute their coverers independently:
    # hunk 1 is covered by test_alpha, hunk 2 by test_beta.
    "line-bias-two-hunks": """\
diff --git a/sqlglot/pairs.py b/sqlglot/pairs.py
--- a/sqlglot/pairs.py
+++ b/sqlglot/pairs.py
@@ -7,1 +7,1 @@ def pairs(flag):
-    a = 1
+    a = 2
@@ -10,1 +10,1 @@ def pairs(flag):
-    return a + b + c
+    return a + b + c + 1
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
    # v2 refinements.  Dialect narrowing drops test_gamma (default-only);
    # test_extra is duck-exposed through L4's family universe.
    "dialect-class-body": {0, 3, 4, 6},
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
    # L4: constructors of Trigger/TriggerChild (1, 2, 4) + the static
    # Trigger.ARG_TYPES read (6); the generic self.ARG_TYPES read is not enough.
    "l4-subclass-attr": {1, 2, 4, 6},
    # A base class every test instantiates: no wrong narrowing at all.
    "l4-base-attr": {0, 1, 2, 3, 4, 5, 6},
    # TAG is absent from usage_style: constructors + Lonely's symbol spans.
    "l4-missing-usage": {0, 3, 5},
    # L4 = {0, 2, 4}; only crane-exposed tests survive dialect narrowing.
    "l4-dialect": {0, 2},
    # 535z shape: L4 = exposure(beam) {4} + Beam's span {0}; test_delta (5)
    # reads the same parameter-receiver line but is not beam-exposed, so it is
    # dropped.  Dialect narrowing keeps only beam-exposed tests: {4}.
    "l4-dialect-param": {4},
    # Tokenizer mirror: L4 = span {3} + the instance_lines reader {5}.
    "l4-tokenizer-instance": {3, 5},
    # W2f: enum-member flips are neutral (both proven operands); the unproven /
    # None / custom-__eq__ variants are not, but the mock file has no numbered
    # tests and no coverage, so their selection is empty (they still must not
    # be reported as a neutral commit -- the unit tests assert that directly).
    "enum-neutral-flip": set(),
    "enum-annotated": set(),
    "enum-str-operand": set(),
    "enum-none-flip": set(),
    "enum-custom-eq": set(),
    "enum-rebound-param": set(),
    "enum-tuple-target": set(),
    # W2f line bias: changed line coverers ({1,2}) win over the whole span
    # ({0,1,2}); every fallback keeps the whole symbol.
    "line-bias-subset": {1, 2},
    "line-bias-empty": {1},
    "line-bias-def": {0, 1, 2},
    "line-bias-insertion": {0, 1, 2},
    "line-bias-statement-head": {1, 2},
    "line-bias-delete-head": {0, 1, 2},
    "line-bias-two-hunks": {0, 1},
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
        # ``with_v2=False`` is the pre-W1h bundle: no enum map either, so the
        # selector takes the lazy runtime-index fallback.
        _write_gz_json(target / "dialects.json.gz", DIALECTS)
        _write_gz_json(target / "keyreads.json.gz", KEYREADS)
        _write_gz_json(target / "testimports.json.gz", TESTIMPORTS)
        _write_gz_json(target / "classes.json.gz", CLASSES)
        _write_gz_json(target / "usage_style.json.gz", USAGE_STYLE)
        _write_gz_json(target / "enum_flips.json.gz", ENUM_FLIPS)
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
