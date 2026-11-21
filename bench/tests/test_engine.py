"""The reference v1 rules and every knob, exercised on the mock maps."""

from bench.engine import Engine, Knobs, partition


def select(patch_text, maps, **overrides):
    return Engine(maps, Knobs().with_overrides(**overrides)).select(patch_text)


def test_partition():
    assert partition("sqlglot/parser.py") == "sqlglot"
    assert partition("sqlglot/parsers/bigquery.py") == "sqlglot"
    assert partition("tests/test_parser.py") == "testpy"
    assert partition("tests/fixtures/optimizer/x.sql") == "fixture"
    assert partition("README.md") == "other"


def test_symbol_rule_selects_covering_tests(maps):
    patch = (
        "diff --git a/sqlglot/parsers/bigquery.py b/sqlglot/parsers/bigquery.py\n"
        "--- a/sqlglot/parsers/bigquery.py\n"
        "+++ b/sqlglot/parsers/bigquery.py\n"
        "@@ -500,1 +500,1 @@\n"
        "-if rest and this:\n"
        "+if rest or this:\n"
    )
    selection = select(patch, maps)
    assert not selection.tier_e
    assert selection.tests
    assert "symbol" in selection.rules
    # a method-level change must not trigger the class-body expansion
    assert "classbody" not in selection.rules
    assert all("test_mock_bigquery" in test or "test_mock_parser" in test for test in selection.tests)


def test_line_mode_uses_exact_lines_only(maps):
    patch = (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "--- a/sqlglot/helper.py\n"
        "+++ b/sqlglot/helper.py\n"
        "@@ -466,1 +466,1 @@\n"
        "-DATE_UNITS = {\"day\"}\n"
        "+DATE_UNITS = {\"day\", \"decade\"}\n"
    )
    line = select(patch, maps, symbol_vs_line="line")
    assert line.tests == set()
    assert "line" in line.rules or not line.rules

    symbol = select(patch, maps)
    assert symbol.tests, "classbody/usage must recover the module-level change"
    assert {"classbody", "usage"} & set(symbol.rules)


def test_usage_rule_is_what_catches_uncovered_module_lines(maps):
    patch = (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "--- a/sqlglot/helper.py\n"
        "+++ b/sqlglot/helper.py\n"
        "@@ -464,5 +464,5 @@\n"
        " \n"
        " # Interval units that operate on date components\n"
        "-DATE_UNITS = {\"day\"}\n"
        "+DATE_UNITS = {\"day\", \"decade\"}\n"
        " \n"
        " \n"
    )
    used = select(patch, maps)
    without = select(patch, maps, usage=False)
    assert used.tests - without.tests, "the usage rule must contribute at least one unique test"
    assert not without.tests - used.tests


def test_classbody_knob_off(maps):
    patch = (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "--- a/sqlglot/helper.py\n"
        "+++ b/sqlglot/helper.py\n"
        "@@ -466,1 +466,1 @@\n"
        "-DATE_UNITS = {\"day\"}\n"
        "+DATE_UNITS = {\"day\", \"decade\"}\n"
    )
    selection = select(patch, maps, classbody=False)
    assert "classbody" not in selection.rules
    assert "usage" not in selection.rules


def test_pad_widens_the_symbol_range(maps):
    patch = (
        "diff --git a/sqlglot/parsers/bigquery.py b/sqlglot/parsers/bigquery.py\n"
        "--- a/sqlglot/parsers/bigquery.py\n"
        "+++ b/sqlglot/parsers/bigquery.py\n"
        "@@ -192,1 +192,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    # line 192 sits in the BigQueryParser class body; padding to the method-free
    # zone changes nothing about class-body-ness, but the selection must be a superset.
    narrow = select(patch, maps)
    wide = select(patch, maps, pad=20)
    assert narrow.tests <= wide.tests
    assert wide.tests


def test_comment_only_is_neutral_when_proven(maps):
    patch = (
        "diff --git a/sqlglot/trie.py b/sqlglot/trie.py\n"
        "--- a/sqlglot/trie.py\n"
        "+++ b/sqlglot/trie.py\n"
        "@@ -32,3 +32,4 @@\n"
        "     trie = {} if trie is None else trie\n"
        " \n"
        "+    # only a comment\n"
        "     for key in keywords:\n"
    )
    neutral = select(patch, maps)
    assert neutral.neutral is True
    assert neutral.tests == set()
    assert "neutral" in neutral.rules

    not_neutral = select(patch, maps, neutral=False)
    assert not_neutral.neutral is None
    assert not_neutral.tests, "with --neutral off the comment hunk is treated as code"
    assert "symbol" in not_neutral.rules


def test_neutral_unknown_without_pinned_source(maps):
    # tests/*.py is intentionally absent from the spec's source tarball
    patch = (
        "diff --git a/tests/test_mock_parser.py b/tests/test_mock_parser.py\n"
        "--- a/tests/test_mock_parser.py\n"
        "+++ b/tests/test_mock_parser.py\n"
        "@@ -5,1 +5,2 @@\n"
        " import unittest\n"
        "+# comment only\n"
    )
    selection = select(patch, maps)
    assert selection.neutral is None
    assert selection.tests == set(maps.test_file_tests("tests/test_mock_parser.py"))


def test_new_source_file_selects_nothing(maps):
    selection = select(_read_synthetic("04_new_source_file.patch"), maps)
    assert not selection.tier_e
    assert selection.tests == set()


def _read_synthetic(name: str) -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "synthetic" / "patches" / name
    return path.read_text("utf-8")


def test_unknown_source_file_is_tier_e(maps):
    patch = (
        "diff --git a/sqlglot/planner.py b/sqlglot/planner.py\n"
        "--- a/sqlglot/planner.py\n"
        "+++ b/sqlglot/planner.py\n"
        "@@ -13,1 +13,1 @@\n"
        "-class Plan:\n"
        "+class Plan:  # probe\n"
    )
    selection = select(patch, maps)
    assert selection.tier_e
    assert selection.tests == set(maps.tests_index)
    assert selection.rules == {"tier_e": set(maps.tests_index)}

    empty = select(patch, maps, tier_e="empty")
    assert empty.tier_e
    assert empty.tests == set()


def test_hunk_beyond_eof_is_tier_e(maps):
    patch = (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "--- a/sqlglot/helper.py\n"
        "+++ b/sqlglot/helper.py\n"
        "@@ -4000,1 +4000,1 @@\n"
        "-x\n"
        "+y\n"
    )
    selection = select(patch, maps)
    assert selection.tier_e
    assert any("beyond EOF" in note for note in selection.notes)


def test_deleted_source_file_is_tier_e(maps):
    patch = (
        "diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n"
        "deleted file mode 100644\n"
        "--- a/sqlglot/helper.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-x = 1\n"
    )
    assert select(patch, maps).tier_e


def test_fixture_rule_and_unknown_fixture(maps):
    known = (
        "diff --git a/tests/fixtures/optimizer/pushdown_predicates.sql "
        "b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "--- a/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "+++ b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "@@ -140,1 +140,1 @@\n"
        "-SELECT 1\n"
        "+SELECT 2\n"
    )
    selection = select(known, maps)
    assert set(selection.rules) == {"fixture"}
    assert selection.tests == set(maps.fixture_tests("tests/fixtures/optimizer/pushdown_predicates.sql"))

    unknown = known.replace("pushdown_predicates.sql", "identity.sql")
    assert select(unknown, maps).tier_e


def test_test_file_rules(maps):
    edit = (
        "diff --git a/tests/test_mock_parser.py b/tests/test_mock_parser.py\n"
        "--- a/tests/test_mock_parser.py\n"
        "+++ b/tests/test_mock_parser.py\n"
        "@@ -5,1 +5,2 @@\n"
        " import unittest\n"
        "+X = 1\n"
    )
    selection = select(edit, maps)
    assert selection.rules == {"testpy": set(maps.test_file_tests("tests/test_mock_parser.py"))}

    shared = edit.replace("tests/test_mock_parser.py", "tests/helpers.py")
    assert select(shared, maps).tier_e

    unknown = edit.replace("tests/test_mock_parser.py", "tests/test_unknown.py")
    assert select(unknown, maps).tier_e


def test_other_files_select_nothing(maps):
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-old docs\n"
        "+new docs\n"
    )
    selection = select(patch, maps)
    assert selection.tests == set()
    assert not selection.tier_e
    assert any("non-code file" in note for note in selection.notes)


def test_anchor_rule_for_pure_insertion(maps):
    patch = (
        "diff --git a/sqlglot/trie.py b/sqlglot/trie.py\n"
        "--- a/sqlglot/trie.py\n"
        "+++ b/sqlglot/trie.py\n"
        "@@ -20,0 +21,1 @@\n"
        "+    EXTRA = 1\n"
    )
    selection = select(patch, maps)
    assert "anchor" in selection.rules
    assert selection.tests


def test_malformed_patch_does_not_crash(maps):
    selection = select("diff --git a/sqlglot/helper.py b/sqlglot/helper.py\n@@ nonsense @@\n", maps)
    assert selection.tier_e


def test_unparsable_patch_is_tier_e(maps):
    selection = select("this is not a patch at all", maps)
    assert selection.tier_e
    assert any("unparsable" in note for note in selection.notes)


def test_empty_patch_selects_nothing(maps):
    selection = select("", maps)
    assert selection.tests == set()
    assert not selection.tier_e


def test_knobs_validation():
    import pytest

    with pytest.raises(ValueError):
        Knobs(symbol_vs_line="symbols")
    with pytest.raises(ValueError):
        Knobs(tier_e="partial")
    with pytest.raises(ValueError):
        Knobs(pad=-1)
    assert "pad=2" in Knobs(pad=2).describe()
