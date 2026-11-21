"""Comment-only neutrality: changed-line test plus AST equality."""

from __future__ import annotations

import pytest

from blastradius import neutral
from blastradius.patch import parse_patch
from ourtests import mockmaps

MOD_SOURCE = mockmaps.source_text("sqlglot/mod.py")

COMMENT_PATCH = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,2 @@ def alpha(x):
     return x + WEEK_OFFSET + METADATA["a"]
+    # explain the expression
"""


def _patch(text):
    (file_patch,) = parse_patch(text)
    return file_patch


def test_real_comment_insertion_is_neutral():
    file_patch = _patch(COMMENT_PATCH)
    assert neutral.changed_lines_only_comments(file_patch)
    assert neutral.is_neutral_file(MOD_SOURCE, file_patch)


def test_comment_insertion_inside_triple_quoted_string_is_not_neutral():
    source = 'def f():\n    """Doc.\n\n    # not code\n    """\n    return 1\n'
    patch = """\
diff --git a/sqlglot/str.py b/sqlglot/str.py
--- a/sqlglot/str.py
+++ b/sqlglot/str.py
@@ -4,1 +4,2 @@
     # not code
+    # inserted into the docstring
"""
    file_patch = _patch(patch)
    assert neutral.changed_lines_only_comments(file_patch)
    assert not neutral.is_neutral_file(source, file_patch)


def test_comment_before_docstring_is_neutral():
    source = 'def f():\n    """Doc."""\n    return 1\n'
    patch = """\
diff --git a/sqlglot/str.py b/sqlglot/str.py
--- a/sqlglot/str.py
+++ b/sqlglot/str.py
@@ -1,1 +1,2 @@
 def f():
+    # leading comment
"""
    file_patch = _patch(patch)
    assert neutral.is_neutral_file(source, file_patch)


def test_coding_declaration_is_not_neutral():
    patch = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -0,0 +1,1 @@
+# -*- coding: latin-8 -*-
"""
    file_patch = _patch(patch)
    assert neutral.changed_lines_only_comments(file_patch)
    assert not neutral.is_neutral_file(MOD_SOURCE, file_patch)


def test_code_change_is_not_neutral():
    patch = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,1 @@ def alpha(x):
-    return x + WEEK_OFFSET + METADATA["a"]
+    return x + WEEK_OFFSET + METADATA["b"]
"""
    assert not neutral.changed_lines_only_comments(_patch(patch))


def test_hunk_that_does_not_apply_raises_and_is_not_neutral():
    patch = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,1 @@
-# not the pinned line
+# still a comment
"""
    file_patch = _patch(patch)
    assert neutral.changed_lines_only_comments(file_patch)
    with pytest.raises(neutral.NeutralityError):
        neutral.reconstruct(MOD_SOURCE, file_patch)
    assert not neutral.is_neutral_file(MOD_SOURCE, file_patch)


def test_commit_neutral_across_files():
    doc_patch = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-# Old heading
+# New heading
"""
    code_patch = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,1 +7,1 @@ def alpha(x):
-    return x + WEEK_OFFSET + METADATA["a"]
+    return x + WEEK_OFFSET + METADATA["b"]
"""
    source_of = lambda path: MOD_SOURCE.encode("utf-8")
    assert neutral.commit_is_neutral(parse_patch(COMMENT_PATCH), source_of)
    assert neutral.commit_is_neutral(parse_patch(doc_patch), source_of)
    assert neutral.commit_is_neutral([], source_of)
    assert not neutral.commit_is_neutral(
        parse_patch(COMMENT_PATCH + code_patch), source_of
    )


def test_fixture_hash_directive_is_not_neutral():
    patch = """\
diff --git a/tests/fixtures/optimizer/pushdown_predicates.sql b/tests/fixtures/optimizer/pushdown_predicates.sql
--- a/tests/fixtures/optimizer/pushdown_predicates.sql
+++ b/tests/fixtures/optimizer/pushdown_predicates.sql
@@ -1,1 +1,1 @@
-# title: old
+# title: new
"""
    assert not neutral.commit_is_neutral(parse_patch(patch), lambda path: None)


def test_commit_with_unknown_python_source_is_not_neutral():
    patch = """\
diff --git a/tests/test_mod.py b/tests/test_mod.py
--- a/tests/test_mod.py
+++ b/tests/test_mod.py
@@ -1,1 +1,2 @@
 def test_alpha():
+    # harmless looking
"""
    assert not neutral.commit_is_neutral(parse_patch(patch), lambda path: None)
