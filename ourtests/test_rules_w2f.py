"""W2f rules: enum-member neutrality, dialect-K intersection, line-biased symbols.

All maps are the handcrafted mock set (``ourtests/mockmaps.py``); the new mock
sources ``sqlglot/enums.py``, ``sqlglot/wide.py`` and ``sqlglot/blank.py`` exist
only for these cases.
"""

from __future__ import annotations

import gzip
import json

import pytest

from blastradius.bench_adapter import bench_adapter
from blastradius.enum_neutral import EnumIndex, maybe_enum_flip
from blastradius.maps_io import Maps
from blastradius.patch import parse_patch
from blastradius.select import Knobs, TestIndex, select_commit
from ourtests import mockmaps


def _index(maps):
    return TestIndex(maps.test_ids(), mockmaps.number_by_id(), mockmaps.tests_by_file())


@pytest.fixture()
def loaded(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    maps = Maps.load(maps_dir)
    yield maps, _index(maps)
    maps.close()


def _select(patch_name, maps, index, knobs=None):
    selection = select_commit(
        parse_patch(mockmaps.PATCHES[patch_name]), maps, index, knobs
    )
    return selection, index.numbers_for_ids(selection.tests)


def _rules(selection):
    return {hit.rule for hit in selection.rules}


def _enum_index():
    texts = {path: mockmaps.source_text(path) for path in mockmaps.SOURCE_FILES}
    return EnumIndex(list(texts), lambda path: texts.get(path))


def _is_enum_neutral(patch_name):
    files = parse_patch(mockmaps.PATCHES[patch_name])
    assert len(files) == 1
    file_patch = files[0]
    assert file_patch.path is not None
    pinned = mockmaps.source_text(file_patch.path)
    return _enum_index().is_neutral(pinned, file_patch, file_patch.path)


# -- enum-member neutrality ----------------------------------------------


def test_enum_flip_between_proven_members_is_neutral():
    # `self.selected` is assigned only from `self.strategy`, which is only ever
    # Strategy.UPPER: both operands are Strategy members (identity semantics).
    assert _is_enum_neutral("enum-neutral-flip")
    # The annotated parameter resolves to the same enum class.
    assert _is_enum_neutral("enum-annotated")


def test_enum_flip_with_unproven_operand_is_not_neutral():
    # An untyped parameter could be a plain string; `is` means identity.
    assert not _is_enum_neutral("enum-str-operand")


def test_rebound_parameter_is_not_treated_as_all_enum_sites():
    """`self.selected` also takes a plain parameter value: not provable."""
    assert not _is_enum_neutral("enum-rebound-param")


def test_tuple_target_sites_count_towards_the_assignment_proof():
    # `mode, extra = "x", "y"` is a non-enum site; ignoring it would be unsound.
    assert not _is_enum_neutral("enum-tuple-target")


def test_none_flip_is_not_neutral():
    # Expression.__eq__ is structural: `x is None` and `x == None` differ.
    assert not _is_enum_neutral("enum-none-flip")


def test_enum_with_custom_eq_is_not_neutral():
    # EqStrategy defines __eq__ in its own body: identity != equality.
    assert not _is_enum_neutral("enum-custom-eq")


def test_enum_neutral_commit_short_circuits_the_whole_commit(loaded):
    maps, index = loaded
    selection, numbers = _select("enum-neutral-flip", maps, index)
    assert selection.tier == "neutral"
    assert numbers == set()
    assert "neutral" in _rules(selection)


def test_enum_neutral_file_is_skipped_in_a_mixed_commit(loaded):
    maps, index = loaded
    # Enum-neutral enums.py + a real bigquery/mod.py edit: only the enum file is
    # skipped; the code hunk still selects.
    text = mockmaps.PATCHES["enum-neutral-flip"] + mockmaps.PATCHES["code-alpha"]
    selection = select_commit(parse_patch(text), maps, index)
    assert selection.tier != "neutral"
    assert "file_neutral(sqlglot/enums.py)" in {str(hit) for hit in selection.rules}
    assert index.numbers_for_ids(selection.tests) == {0}


def test_enum_neutral_and_comment_files_together_are_neutral(loaded):
    maps, index = loaded
    text = mockmaps.PATCHES["enum-neutral-flip"] + mockmaps.PATCHES["comment"]
    selection = select_commit(parse_patch(text), maps, index)
    assert selection.tier == "neutral"
    assert selection.tests == set()


def test_non_neutral_enum_flips_still_select(loaded):
    maps, index = loaded
    for name in (
        "enum-str-operand",
        "enum-none-flip",
        "enum-custom-eq",
        "enum-rebound-param",
        "enum-tuple-target",
    ):
        selection, _ = _select(name, maps, index)
        assert selection.tier != "neutral", name
        assert "neutral" not in _rules(selection), name


def test_enum_neutrality_knob_off_keeps_the_selection(loaded):
    maps, index = loaded
    selection, _ = _select("enum-neutral-flip", maps, index, Knobs(enum_neutral="off"))
    assert selection.tier != "neutral"
    assert "neutral" not in _rules(selection)


def test_patch_without_a_comparison_flip_never_touches_the_enum_index(
    loaded, monkeypatch
):
    """W2g: the hunk text gate keeps the whole pinned-source index unbuilt."""
    maps, index = loaded
    patch = mockmaps.PATCHES["code-alpha"]
    (file_patch,) = parse_patch(patch)
    assert not maybe_enum_flip(file_patch)  # no is/== pair at all

    def _unbuilt():
        raise AssertionError("enum index built for a patch with no is/== flip")

    monkeypatch.setattr(maps, "enum_index", _unbuilt)
    selection, numbers = _select("code-alpha", maps, index)
    assert numbers == {0}
    assert "neutral" not in _rules(selection)


# -- dialect-K refinement -------------------------------------------------


def _beam_exposed_non_reader(tmp_path):
    """Mock maps where test_gesundheit is beam-exposed but never reads/spans."""
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    path = maps_dir / "dialects.json.gz"
    document = json.loads(gzip.decompress(path.read_bytes()))
    document["exposure"]["tests/test_other.py::test_gesundheit"].append("beam")
    path.write_bytes(gzip.compress(json.dumps(document).encode("utf-8"), mtime=0))
    return maps_dir


def test_dialect_k_intersection_keeps_readers_and_drops_non_readers(tmp_path):
    maps_dir = _beam_exposed_non_reader(tmp_path)
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        union_ids = maps.classattr_tests(
            "sqlglot/dialects/beam.py", "Beam", "WEEK_OFFSET", dialect_k="union"
        )
        intersect_ids = maps.classattr_tests(
            "sqlglot/dialects/beam.py", "Beam", "WEEK_OFFSET", dialect_k="intersect"
        )
        assert union_ids is not None and intersect_ids is not None
        non_reader = "tests/test_other.py::test_gesundheit"
        reader = "tests/test_other.py::test_extra"  # instance_lines reader
        assert non_reader in union_ids
        assert non_reader not in intersect_ids
        # Soundness: the always-reading test survives the intersection.
        assert reader in union_ids and reader in intersect_ids

        selection, numbers = _select("l4-dialect-param", maps, index, Knobs(dialect_k="intersect"))
        assert numbers == {4}
        _, widened = _select("l4-dialect-param", maps, index, Knobs(dialect_k="union"))
        assert widened == {4, 6}
        assert "classattr" in _rules(selection)
    finally:
        maps.close()


def test_dialect_k_intersection_never_drops_a_reader(tmp_path):
    """Every test reading the attr through any receiver survives the intersection."""
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        ids = maps.classattr_tests(
            "sqlglot/dialects/beam.py", "Beam", "WEEK_OFFSET", dialect_k="intersect"
        )
        assert ids is not None
        # simplify.py:2 (`dialect.WEEK_OFFSET`, instance receiver) is read by
        # test_extra (4), which is beam-exposed; test_delta (5) reads it too but
        # is not beam-exposed, so it is not in K and not required here.
        assert "tests/test_other.py::test_extra" in ids
        readers = [
            index.ids_for_ordinals(
                maps.covering_tests(
                    file,
                    detail.get("instance_lines", [])
                    + detail.get("self_lines", [])
                    + detail.get("class_lines", []),
                )
            )
            for file, detail in maps.usage_style("WEEK_OFFSET").items()
        ]
        union_readers = set().union(*readers)
        exposed = maps.tests_exposed_to_family("beam")
        assert (union_readers & exposed) <= ids
    finally:
        maps.close()


# -- line-biased symbol selection -----------------------------------------


def test_line_bias_selects_the_changed_line_coverers(loaded):
    maps, index = loaded
    selection, numbers = _select("line-bias-subset", maps, index)
    assert numbers == {1, 2}
    assert "linebias" in _rules(selection)
    assert "coverage" not in _rules(selection)

    span_ids = index.ids_for_ordinals(maps.covering_tests("sqlglot/wide.py", [(6, 12)]))
    line_ids = index.ids_for_ordinals(maps.covering_tests("sqlglot/wide.py", [(10, 12)]))
    assert line_ids < span_ids  # a real strict subset
    assert numbers == index.numbers_for_ids(line_ids)

    _, plain = _select("line-bias-subset", maps, index, Knobs(line_bias="off"))
    assert plain == {0, 1, 2}


def test_line_bias_empty_line_coverage_falls_back_to_whole_symbol(loaded):
    maps, index = loaded
    selection, numbers = _select("line-bias-empty", maps, index)
    assert numbers == {1}
    assert "linebias" in _rules(selection)  # bias was attempted, then fell back
    line_ids = maps.covering_tests("sqlglot/blank.py", [(4, 6)])
    assert line_ids == set()


def test_two_hunks_in_one_symbol_keep_both_hunks_coverers(loaded):
    """Each hunk contributes its own line coverers; deduping would lose one."""
    maps, index = loaded
    selection, numbers = _select("line-bias-two-hunks", maps, index)
    assert numbers == {0, 1}
    assert "linebias" in _rules(selection)
    # hunk 1 (line 7) is only covered by test_alpha, hunk 2 (line 10) only by
    # test_beta: a per-symbol (instead of per-hunk) bias would select just one.
    assert index.ids_for_ordinals(maps.covering_tests("sqlglot/pairs.py", [(6, 8)])) == {
        "tests/test_mod.py::test_alpha"
    }
    assert index.ids_for_ordinals(maps.covering_tests("sqlglot/pairs.py", [(9, 11)])) == {
        "tests/test_mod.py::test_beta"
    }


def test_def_line_hunks_are_not_line_biased(loaded):
    maps, index = loaded
    selection, numbers = _select("line-bias-def", maps, index)
    assert numbers == {0, 1, 2}
    assert "linebias" not in _rules(selection)


def test_insertions_are_not_line_biased(loaded):
    maps, index = loaded
    selection, numbers = _select("line-bias-insertion", maps, index)
    assert numbers == {0, 1, 2}
    assert "linebias" not in _rules(selection)


def test_rewritten_statement_heads_stay_line_biased(loaded):
    """`if flag:` -> `if not flag:`: entering the body still runs this line."""
    maps, index = loaded
    selection, numbers = _select("line-bias-statement-head", maps, index)
    assert numbers == {1, 2}
    assert "linebias" in _rules(selection)


def test_deleted_multiline_statement_heads_fall_back(loaded):
    """Deleting `if flag:` + its body can move behaviour outside the old span."""
    maps, index = loaded
    selection, numbers = _select("line-bias-delete-head", maps, index)
    assert numbers == {0, 1, 2}
    assert "linebias" not in _rules(selection)


# -- adapter knobs --------------------------------------------------------


def test_adapter_exposes_the_w2f_knobs(tmp_path):
    maps_dir = _beam_exposed_non_reader(tmp_path)
    adapter = bench_adapter(maps_dir)
    try:
        base = adapter.select(mockmaps.PATCHES["l4-dialect-param"])
        wide = adapter.select(
            mockmaps.PATCHES["l4-dialect-param"], knobs={"dialect_k": "union"}
        )
        assert len(wide["tests"]) > len(base["tests"])

        neutral = adapter.select(mockmaps.PATCHES["enum-neutral-flip"])
        assert neutral["tier"] == "neutral"
        off = adapter.select(
            mockmaps.PATCHES["enum-neutral-flip"], knobs={"enum_neutral": "off"}
        )
        assert off["tier"] != "neutral"

        biased = adapter.select(mockmaps.PATCHES["line-bias-subset"])
        plain = adapter.select(
            mockmaps.PATCHES["line-bias-subset"], knobs={"line_bias": "off"}
        )
        assert len(biased["tests"]) == 2 and len(plain["tests"]) == 3
    finally:
        adapter.close()
