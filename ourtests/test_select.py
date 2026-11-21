"""Selection rules v1 against the handcrafted mock maps."""

from __future__ import annotations

import pytest

from blastradius.maps_io import Maps
from blastradius.patch import parse_patch
from blastradius.select import TestIndex, select_commit
from ourtests import mockmaps


@pytest.fixture()
def loaded(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    maps = Maps.load(maps_dir)
    index = TestIndex(
        maps.test_ids(), mockmaps.number_by_id(), mockmaps.tests_by_file()
    )
    yield maps, index
    maps.close()


def _select(text, maps, index):
    return select_commit(parse_patch(text), maps, index)


def _numbers(selection, index):
    return index.numbers_for_ids(selection.tests)


def _one_file(path, hunk):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"{hunk}"
    )


def test_maps_are_readable(loaded):
    maps, index = loaded
    assert maps.file_length("sqlglot/mod.py") == 22
    assert [symbol.qualname for symbol in maps.symbols_of("sqlglot/mod.py")] == [
        "alpha",
        "Helper",
        "Helper.beta",
        "Helper.gamma",
        "delta",
    ]
    assert maps.symbols_of("sqlglot/missing.py") is None
    references = {(reference.file, reference.line) for reference in maps.references_of("METADATA")}
    assert references == {("sqlglot/other.py", 1), ("sqlglot/other.py", 7), ("sqlglot/mod.py", 7)}


def test_module_and_class_level_assignments(loaded):
    maps, _ = loaded
    assert maps.assignments_on_lines("sqlglot/mod.py", {3}) == ("WEEK_OFFSET",)
    assert maps.assignments_on_lines("sqlglot/mod.py", {11}) == ("LIMIT",)
    assert maps.assignments_on_lines("sqlglot/mod.py", {1}) == ("METADATA",)
    assert maps.assignments_on_lines("sqlglot/mod.py", {7}) == ()
    assert maps.assignments_on_lines("sqlglot/other.py", {3}) == ("METADATA",)
    assert maps.assignments_on_lines("sqlglot/other.py", {7}) == ("EXTRA",)


def test_function_body_uses_innermost_symbol(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["code-alpha"], maps, index)
    assert selection.tier == "code"
    assert _numbers(selection, index) == {0}


def test_other_function_body(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -18,1 +18,1 @@\n-        return y - 1\n+        return y - 2\n",
    )
    assert _numbers(_select(text, maps, index), index) == {2}


def test_class_body_selects_all_file_symbols_plus_usage(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["class-body"], maps, index)
    assert _numbers(selection, index) == {0, 1, 2, 5}
    rules = {hit.rule for hit in selection.rules}
    assert "usage" in rules and "coverage" in rules


def test_module_level_dict_uses_usage_lines(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["module-dict"], maps, index)
    assert _numbers(selection, index) == {0, 3, 4}


def test_known_fixture_selects_readers(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["fixture"], maps, index)
    assert _numbers(selection, index) == {1}
    assert selection.rules[-1].rule == "fixture"


def test_fixture_comment_directive_still_selects_readers(loaded):
    maps, index = loaded
    text = _one_file(
        "tests/fixtures/a.sql",
        "@@ -1,1 +1,1 @@\n-# title: old\n+# title: new\n",
    )
    selection = _select(text, maps, index)
    assert _numbers(selection, index) == {1}
    assert selection.rules[-1].rule == "fixture"


def test_unknown_fixture_is_tier_e(loaded):
    maps, index = loaded
    text = _one_file(
        "tests/fixtures/optimizer/unknown.sql",
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    selection = _select(text, maps, index)
    assert selection.tier == "tier_e"
    assert _numbers(selection, index) == set(range(6))


def test_test_file_selects_its_tests(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["testpy"], maps, index)
    assert _numbers(selection, index) == {3, 4}


def test_shared_test_support_is_tier_e(loaded):
    maps, index = loaded
    text = _one_file(
        "tests/helpers.py",
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert _select(text, maps, index).tier == "tier_e"
    text = _one_file(
        "conftest.py",
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    assert _select(text, maps, index).tier == "tier_e"


def test_unknown_sqlglot_source_is_tier_e(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/ghost.py",
        "@@ -1,1 +1,1 @@\n-old\n+new\n",
    )
    selection = _select(text, maps, index)
    assert selection.tier == "tier_e"
    assert selection.rules[0].rule == "tier_e"


def test_hunk_past_pinned_file_length_is_tier_e(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -99,1 +99,1 @@\n-old\n+new\n",
    )
    assert _select(text, maps, index).tier == "tier_e"


def test_deleted_source_file_is_tier_e(loaded):
    maps, index = loaded
    text = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
deleted file mode 100644
--- a/sqlglot/mod.py
+++ /dev/null
@@ -1,1 +0,0 @@
-from sqlglot.other import METADATA
"""
    assert _select(text, maps, index).tier == "tier_e"


def test_binary_change_is_tier_e(loaded):
    maps, index = loaded
    text = """\
diff --git a/sqlglot/mod.py b/sqlglot/mod.py
Binary files a/sqlglot/mod.py and b/sqlglot/mod.py differ
"""
    assert _select(text, maps, index).tier == "tier_e"


def test_pure_insertion_uses_anchor_lines(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -7,0 +8,1 @@\n+    x = 1\n",
    )
    # Anchor lines 7 (alpha) and 8 (module level) -> broad module/class rule.
    assert _numbers(_select(text, maps, index), index) == {0, 1, 2, 5}


def test_brand_new_file_selects_nothing(loaded):
    maps, index = loaded
    text = """\
diff --git a/sqlglot/fresh.py b/sqlglot/fresh.py
new file mode 100644
--- /dev/null
+++ b/sqlglot/fresh.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""
    selection = _select(text, maps, index)
    assert selection.tests == set()
    assert any(hit.rule == "new_file" for hit in selection.rules)


def test_comment_only_commit_is_neutral(loaded):
    maps, index = loaded
    selection = _select(mockmaps.PATCHES["comment"], maps, index)
    assert selection.tier == "neutral"
    assert selection.tests == set()


def test_mixed_commit_skips_only_the_comment_file(loaded):
    maps, index = loaded
    comment_other = """\
diff --git a/sqlglot/other.py b/sqlglot/other.py
--- a/sqlglot/other.py
+++ b/sqlglot/other.py
@@ -7,1 +7,2 @@ EXTRA = METADATA["a"]
 EXTRA = METADATA["a"]
+# note for readers
"""
    text = mockmaps.PATCHES["code-alpha"] + comment_other
    selection = _select(text, maps, index)
    assert _numbers(selection, index) == {0}
    assert any(hit.rule == "file_neutral" for hit in selection.rules)


def test_docs_only_change_selects_nothing(loaded):
    maps, index = loaded
    text = _one_file(
        "README.md",
        "@@ -1,1 +1,1 @@\n-old text\n+new text\n",
    )
    selection = _select(text, maps, index)
    assert selection.tests == set()
    assert selection.tier == "code"


def test_context_lines_can_widen_to_module_level(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -5,5 +5,5 @@\n"
        " \n"
        " def alpha(x):\n"
        "-    return x + WEEK_OFFSET + METADATA[\"a\"]\n"
        "+    return x + WEEK_OFFSET + METADATA[\"b\"]\n"
        " \n"
        " \n",
    )
    assert _numbers(_select(text, maps, index), index) == {0, 1, 2, 5}


def test_selected_tests_missing_from_tests_json_are_dropped(loaded):
    maps, _ = loaded
    known = mockmaps.number_by_id()
    known.pop("tests/test_mod.py::test_delta")
    index = TestIndex(maps.test_ids(), known, mockmaps.tests_by_file())
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -21,1 +21,1 @@\n-def delta(x):\n+def delta(x, y):\n",
    )
    selection = _select(text, maps, index)
    assert selection.tests == set()
    assert selection.tier == "code"


def test_empty_patch_is_empty(loaded):
    maps, index = loaded
    selection = select_commit([], maps, index)
    assert selection.tier == "empty"
    assert selection.tests == set()
