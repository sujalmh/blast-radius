"""Patch parser edge cases."""

from __future__ import annotations

import pytest

from blastradius.patch import PatchError, parse_patch

SIMPLE = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
index 1111111..2222222 100644
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -7,3 +7,4 @@ def alpha(x):
 context before
-    return x
+    return y
+    # note
 context after
"""


def test_simple_patch_paths_hunks_and_lines():
    files = parse_patch(SIMPLE)
    assert len(files) == 1
    file_patch = files[0]
    assert file_patch.old_path == "sqlglot/mod.py"
    assert file_patch.new_path == "sqlglot/mod.py"
    assert file_patch.path == "sqlglot/mod.py"
    assert not file_patch.is_new and not file_patch.is_deleted

    (hunk,) = file_patch.hunks
    assert (hunk.old_start, hunk.old_count) == (7, 3)
    assert (hunk.new_start, hunk.new_count) == (7, 4)
    assert hunk.removed == ("    return x",)
    assert hunk.added == ("    return y", "    # note")
    assert list(hunk.old_lines()) == [7, 8, 9]


def test_new_file_from_dev_null():
    text = """\
diff --git a/sqlglot/new.py b/sqlglot/new.py
new file mode 100644
index 0000000..abcdef1
--- /dev/null
+++ b/sqlglot/new.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""
    (file_patch,) = parse_patch(text)
    assert file_patch.is_new
    assert file_patch.old_path is None
    assert file_patch.new_path == "sqlglot/new.py"
    assert file_patch.path == "sqlglot/new.py"
    (hunk,) = file_patch.hunks
    assert (hunk.old_start, hunk.old_count) == (0, 0)
    assert hunk.added == ("one", "two", "three")
    assert hunk.old_lines() == [1]


def test_deleted_file_to_dev_null():
    text = """\
diff --git a/sqlglot/gone.py b/sqlglot/gone.py
deleted file mode 100644
--- a/sqlglot/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
"""
    (file_patch,) = parse_patch(text)
    assert file_patch.is_deleted
    assert file_patch.old_path == "sqlglot/gone.py"
    assert file_patch.new_path is None
    assert file_patch.path == "sqlglot/gone.py"


def test_omitted_counts_mean_one():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -10 +10 @@
-old
+new
"""
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert (hunk.old_start, hunk.old_count) == (10, 1)
    assert (hunk.new_start, hunk.new_count) == (10, 1)
    assert list(hunk.old_lines()) == [10]


def test_zero_count_is_anchor_adjacency():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -5,0 +6,2 @@
+one
+two
"""
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert (hunk.old_start, hunk.old_count) == (5, 0)
    assert list(hunk.old_lines()) == [5, 6]
    assert list(hunk.old_lines(file_len=5)) == [5]
    assert list(hunk.old_lines(file_len=1)) == []


def test_zero_start_insertion_anchors_first_line():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -0,0 +1,1 @@
+first
"""
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert hunk.old_lines() == [1]


def test_crlf_patch():
    text = SIMPLE.replace("\n", "\r\n")
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert hunk.removed == ("    return x",)
    assert "return y" in hunk.added[0]


def test_empty_and_blank_patch():
    assert parse_patch("") == []
    assert parse_patch("\n\n") == []


def test_hunk_count_mismatch_raises():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
-a
-b
+c
"""
    with pytest.raises(PatchError):
        parse_patch(text)


def test_truncated_hunk_raises():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,1 @@
-a
"""
    with pytest.raises(PatchError):
        parse_patch(text)


def test_hunk_body_longer_than_header_raises():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
-a
+b
 c
"""
    with pytest.raises(PatchError):
        parse_patch(text)


def test_malformed_header_raises():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ not a hunk header @@
-a
+b
"""
    with pytest.raises(PatchError):
        parse_patch(text)


def test_multiple_files_git_style():
    text = SIMPLE + """\
diff --git a/tests/fixtures/a.sql b/tests/fixtures/a.sql
--- a/tests/fixtures/a.sql
+++ b/tests/fixtures/a.sql
@@ -1,1 +1,1 @@
-SELECT 1;
+SELECT 2;
"""
    files = parse_patch(text)
    assert [file_patch.path for file_patch in files] == [
        "sqlglot/mod.py",
        "tests/fixtures/a.sql",
    ]


def test_non_git_marker_style():
    text = """\
--- a/sqlglot/mod.py
+++ b/sqlglot/mod.py
@@ -1,1 +1,1 @@
-x
+y
--- a/tests/fixtures/a.sql
+++ b/tests/fixtures/a.sql
@@ -1,1 +1,1 @@
-a
+b
"""
    files = parse_patch(text)
    assert [file_patch.path for file_patch in files] == [
        "sqlglot/mod.py",
        "tests/fixtures/a.sql",
    ]
    assert all(not file_patch.is_new for file_patch in files)


def test_binary_and_rename_flags():
    binary = """\
diff --git a/sqlglot/x.py b/sqlglot/x.py
Binary files a/sqlglot/x.py and b/sqlglot/x.py differ
"""
    (file_patch,) = parse_patch(binary)
    assert file_patch.is_binary

    rename = """\
diff --git a/sqlglot/a.py b/sqlglot/b.py
similarity index 100%
rename from sqlglot/a.py
rename to sqlglot/b.py
"""
    (file_patch,) = parse_patch(rename)
    assert file_patch.is_rename
    assert file_patch.hunks == []


def test_mode_only_change_has_no_hunks():
    text = """\
diff --git a/scripts/run.sh b/scripts/run.sh
old mode 100644
new mode 100755
"""
    (file_patch,) = parse_patch(text)
    assert file_patch.hunks == []
    assert file_patch.path == "scripts/run.sh"


def test_no_newline_markers_are_not_lines():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
-x
\\ No newline at end of file
+y
\\ No newline at end of file
"""
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert hunk.removed == ("x",)
    assert hunk.added == ("y",)


def test_removed_line_looking_like_a_marker():
    text = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
--- whatever
+++ replacement
"""
    (file_patch,) = parse_patch(text)
    (hunk,) = file_patch.hunks
    assert hunk.removed == ("-- whatever",)
    assert hunk.added == ("++ replacement",)


def test_header_tab_timestamp():
    text = """\
--- a/sqlglot/x.py\t2020-01-01 00:00:00 +0000
+++ b/sqlglot/x.py\t2020-01-01 00:00:00 +0000
@@ -1,1 +1,1 @@
-a
+b
"""
    (file_patch,) = parse_patch(text)
    assert file_patch.path == "sqlglot/x.py"
