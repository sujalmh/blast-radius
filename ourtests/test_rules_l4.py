"""L4: class-attribute narrowing (the ktz3 over-selection fix).

The scenario the rule exists for: a class-body assignment such as
``TriggerProperties.arg_types = {...}`` is read instance-style in the *base*
class (``Expression.validate`` reads ``self.ARG_TYPES``), so the L1 usage rule
selects every test that runs any expression at all.  L4 replaces L1 for that
name with the class-family universe K, the static (``class_lines``) reads, and
the class's own symbol coverage.

Every map here is handcrafted (``ourtests/mockmaps.py``); fixture class bodies:

    sqlglot/expr.py       Expression.ARG_TYPES (11-18 Trigger, 21-22 TriggerChild)
    sqlglot/lonely.py     Lonely.TAG (TAG absent from usage_style: K UNKNOWN)
    sqlglot/dialects/crane.py   Crane.SPANS (dialect-scoped, class_scope)
    sqlglot/dialects/beam.py    Beam.WEEK_OFFSET (the 535z shape: reads it
                                through a *parameter* receiver only)
    sqlglot/tokens.py     Tokenizer.NUMERIC_ESCAPES (the mirror: K UNKNOWN)
"""

from __future__ import annotations

import gzip
import json
import time

import pytest

from blastradius.bench_adapter import bench_adapter
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
    selection = select_commit(parse_patch(mockmaps.PATCHES[patch_name]), maps, index, knobs)
    return selection, index.numbers_for_ids(selection.tests)


def _rules(selection):
    return {hit.rule for hit in selection.rules}


def _write_gz_json(path, document):
    path.write_bytes(gzip.compress(json.dumps(document).encode("utf-8"), mtime=0))


# -- reader level --------------------------------------------------------


def test_l4_maps_are_gated_and_loaded(loaded):
    maps, _ = loaded
    assert maps.has_classes()
    assert maps.usage_style("ARG_TYPES") == {
        "sqlglot/expr.py": {
            "self_lines": [7, 18],
            "class_lines": [26],
            "instance_lines": [],
        }
    }
    # A name the usage-style map does not know reads as "no static reads".
    assert maps.usage_style("TAG") == {}


def test_class_attr_readers_split_instance_and_static_reads(loaded):
    maps, _ = loaded
    # Trigger.ARG_TYPES: constructors of Trigger + TriggerChild (1, 2, 4) plus
    # the static Trigger.ARG_TYPES read (6); Expression's self.ARG_TYPES read
    # at line 7 (covered by every test) contributes nothing.
    assert maps.classattr_tests("sqlglot/expr.py", "Trigger", "ARG_TYPES") == {
        "tests/test_mod.py::test_beta",
        "tests/test_mod.py::test_gamma",
        "tests/test_other.py::test_extra",
        "tests/test_other.py::test_gesundheit",
    }


def test_class_spellings_are_folded(loaded):
    maps, _ = loaded
    # The mock records TriggerChild under the *package* spelling in `touched`
    # (test_gesundheit) and `hierarchy`, and the module spelling elsewhere;
    # both must reach the same class.
    ids = maps.classattr_tests("sqlglot/expr.py", "TriggerChild", "ARG_TYPES")
    assert ids == {
        "tests/test_other.py::test_extra",
        "tests/test_other.py::test_gesundheit",
    }


def test_foreign_class_is_unresolvable(loaded):
    maps, _ = loaded
    # Helper exists in the symbol table but appears nowhere in classes.json.gz.
    assert maps.classattr_tests("sqlglot/mod.py", "Helper", "LIMIT") is None
    assert maps.classattr_tests("sqlglot/expr.py", "Trigger", "LIMIT") not in (None, set())


# -- the rule ------------------------------------------------------------


def test_subclass_constructor_pulls_in_the_attr_edit(loaded):
    maps, index = loaded
    selection, numbers = _select("l4-subclass-attr", maps, index)
    assert numbers == mockmaps.EXPECTATIONS["l4-subclass-attr"] == {1, 2, 4, 6}
    assert "classattr" in _rules(selection)
    # L1 is every test (the generic base-class read); L4 drops exactly the
    # tests that neither build Trigger/TriggerChild nor read ARG_TYPES.
    _, plain = _select("l4-subclass-attr", maps, index, Knobs(l4="off"))
    assert plain == {0, 1, 2, 3, 4, 5, 6}
    assert plain - numbers == {0, 3, 5}
    # Those three cover `self.ARG_TYPES` (line 7) and nothing else: instance
    # reads must not drag them back in.
    assert 7 in maps.usage_style("ARG_TYPES")["sqlglot/expr.py"]["self_lines"]


def test_classattr_is_distinct_provenance_from_usage(loaded):
    maps, index = loaded
    selection, _ = _select("l4-subclass-attr", maps, index)
    assert "classattr" in selection.by_rule
    assert "usage" not in _rules(selection)
    assert selection.by_rule["classattr"] == selection.tests


def test_base_class_edit_is_not_narrowed_below_l1(loaded):
    maps, index = loaded
    # Expression is constructed by every test in the mock: descendants are
    # widely constructed, so L4 must be at least L1 and identical here.
    selection, numbers = _select("l4-base-attr", maps, index)
    assert numbers == set(range(len(mockmaps.TESTS)))
    _, plain = _select("l4-base-attr", maps, index, Knobs(l4="off"))
    assert numbers >= plain
    constructing = set()
    for test_id, classes in mockmaps.CLASSES["touched"].items():
        if any(
            not name.endswith(".Trigger")
            for name in classes
            if isinstance(name, str)
        ):
            constructing.add(mockmaps.number_by_id()[test_id])
    assert constructing <= numbers


def test_name_absent_from_usage_style_uses_constructors_and_symbols(loaded):
    maps, index = loaded
    selection, numbers = _select("l4-missing-usage", maps, index)
    # TAG: constructors (0, 5) + Lonely's symbol span coverage (3).
    assert numbers == mockmaps.EXPECTATIONS["l4-missing-usage"] == {0, 3, 5}
    assert "classattr" in _rules(selection)
    # test_gamma covers `Lonely.TAG` statically but is neither a constructor
    # nor inside Lonely's span, and TAG has no static-read entry to add it.
    assert 2 not in numbers


def test_dialect_narrowing_applies_after_l4(loaded):
    maps, index = loaded
    selection, numbers = _select("l4-dialect", maps, index)
    # Crane.SPANS: constructors (0, 4) + symbol span coverage (2, 4).
    assert numbers == mockmaps.EXPECTATIONS["l4-dialect"] == {0, 2}
    assert "classattr" in _rules(selection) and "dialect" in _rules(selection)
    _, unnarrowed = _select("l4-dialect", maps, index, Knobs(dialect="off"))
    assert unnarrowed == {0, 2, 4}  # test_extra is not crane-exposed
    # L1 alone (narrowed) is just the tests covering `self.SPANS`: test_alpha,
    # which constructs Crane, is exactly what L4 adds back.
    _, plain = _select("l4-dialect", maps, index, Knobs(l4="off"))
    assert plain == {2}


def test_l4_off_keeps_l1(loaded):
    maps, index = loaded
    selection, numbers = _select("l4-subclass-attr", maps, index, Knobs(l4="off"))
    assert numbers == {0, 1, 2, 3, 4, 5, 6}
    assert "classattr" not in _rules(selection)
    assert "usage" in _rules(selection)


def test_classbody_off_disables_l4_too(loaded):
    maps, index = loaded
    selection, numbers = _select(
        "l4-subclass-attr", maps, index, Knobs(classbody="off")
    )
    assert numbers == set()  # only the (import-time) anchor lines remain
    assert "classattr" not in _rules(selection)


def test_module_level_change_is_never_classattr(loaded):
    maps, index = loaded
    selection, numbers = _select("module-dict", maps, index)
    assert numbers == mockmaps.EXPECTATIONS["module-dict"] == {0, 3, 4}
    assert "classattr" not in _rules(selection)


# -- class-family universe (3-way usage_style) ---------------------------


def test_class_families_are_detected(loaded):
    maps, _ = loaded
    # Expression path: the construction trace captures instances.
    assert maps.class_family("sqlglot/expr.py", "Trigger") == ("expression", None)
    assert maps.class_family("sqlglot/expr.py", "TriggerChild") == ("expression", None)
    # Dialect path: exact class_scope entry -> dialect key.
    assert maps.class_family("sqlglot/dialects/beam.py", "Beam") == ("dialect", "beam")
    assert maps.class_family("sqlglot/dialects/crane.py", "Crane") == (
        "dialect",
        "crane",
    )
    # Everything else: K is UNKNOWN.  (The mock's `touched` deliberately lists
    # non-expression classes too -- a real trace only records Expr
    # constructions -- so Lonely reads as expression through constructor
    # evidence; Tokenizer is only in the hierarchy, which is the shape a real
    # map has for a tokenizer body.)
    assert maps.class_family("sqlglot/tokens.py", "Tokenizer") == ("unknown", None)
    assert maps.class_family("sqlglot/lonely.py", "Lonely") == ("expression", None)


def test_nested_class_in_a_dialect_file_is_not_a_dialect_family(loaded):
    """4d63: ``class Tokenizer`` inside ``class Duck(Dialect)`` is not a dialect.

    The *scope* walk (used for dialect narrowing) deliberately climbs outer
    classes, but the family test must not: a tokenizer class body's instance
    universe is not the dialect exposure map.
    """
    maps, _ = loaded
    assert maps.dialect_class_scope("sqlglot/dialects/beam.py", "Beam.Tokens") == "beam"
    assert maps.class_family("sqlglot/dialects/beam.py", "Beam.Tokens") == (
        "unknown",
        None,
    )


def test_dialect_family_keeps_a_parameter_receiver_reader(loaded):
    """535z soundness: the failing test reads WEEK_OFFSET via a param receiver.

    ``dialect.WEEK_OFFSET`` in optimizer/simplify.py is an ``instance_lines``
    read, so L4 must *not* rely on it -- the dialect-path universe K (tests
    exposed to ``beam``) is what keeps test_extra (4) selected, while test_delta
    (5), which covers the very same line but is not beam-exposed, is dropped.
    """
    maps, index = loaded
    attr_ids = maps.classattr_tests("sqlglot/dialects/beam.py", "Beam", "WEEK_OFFSET")
    assert attr_ids is not None
    assert "tests/test_other.py::test_extra" in attr_ids  # only via exposure
    assert "tests/test_mod.py::test_delta" not in attr_ids
    assert "beam" in mockmaps.DIALECTS["exposure"]["tests/test_other.py::test_extra"]
    assert "beam" not in mockmaps.DIALECTS["exposure"]["tests/test_mod.py::test_delta"]

    selection, numbers = _select("l4-dialect-param", maps, index)
    assert numbers == mockmaps.EXPECTATIONS["l4-dialect-param"] == {4}
    assert "classattr" in _rules(selection)
    assert any("dialect:beam" in hit.detail for hit in selection.rules)

    # Dialect narrowing off isolates the rule: K + Beam's span {0} survive,
    # the instance-only reader (5) does not.
    _, unnarrowed = _select("l4-dialect-param", maps, index, Knobs(dialect="off"))
    assert unnarrowed == {0, 4}
    # Without L4 the usage rule keeps both parameter readers.
    _, plain = _select(
        "l4-dialect-param", maps, index, Knobs(l4="off", dialect="off")
    )
    assert plain == {0, 4, 5}


def test_unknown_family_keeps_instance_line_readers(loaded):
    """Mirror of 535z: Tokenizer bodies are not traced, so instance reads stay.

    The same ``tokenizer.NUMERIC_ESCAPES`` parameter read that L4 drops for a
    dialect class must keep its test here.
    """
    maps, index = loaded
    attr_ids = maps.classattr_tests("sqlglot/tokens.py", "Tokenizer", "NUMERIC_ESCAPES")
    assert attr_ids is not None
    assert "tests/test_mod.py::test_delta" in attr_ids  # instance_lines reader
    selection, numbers = _select("l4-tokenizer-instance", maps, index)
    assert numbers == mockmaps.EXPECTATIONS["l4-tokenizer-instance"] == {3, 5}
    assert "classattr" in _rules(selection)
    assert any("unknown" in hit.detail for hit in selection.rules)
    # The same read *is* dropped for the dialect family above (see
    # test_dialect_family_keeps_a_parameter_receiver_reader): the split really
    # is receiver-kind + family driven.
    assert 5 in numbers


def test_legacy_two_way_usage_style_stays_conservative(tmp_path):
    """A pre-W1f map (``other_lines``) keeps every instance read in the union."""
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    legacy = {}
    for name, files in mockmaps.USAGE_STYLE.items():
        legacy[name] = {
            file: {
                "self_lines": detail["self_lines"],
                "other_lines": detail["class_lines"] + detail["instance_lines"],
            }
            for file, detail in files.items()
        }
    _write_gz_json(maps_dir / "usage_style.json.gz", legacy)
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert maps.has_classes()
        # class_lines falls back to other_lines, which contains the instance
        # read: both parameter readers stay.
        _, beam = _select("l4-dialect-param", maps, index, Knobs(dialect="off"))
        assert beam == {0, 4, 5}
        _, tokenizer = _select("l4-tokenizer-instance", maps, index)
        assert tokenizer == {3, 5}
    finally:
        maps.close()


# -- degraded behaviour --------------------------------------------------


def test_missing_classes_map_keeps_l1_and_warns(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    (maps_dir / "classes.json.gz").unlink()
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert any("classes.json.gz" in w and "L4" in w for w in maps.warnings)
        assert not maps.has_classes()
        _, numbers = _select("l4-subclass-attr", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5, 6}  # L1 kept
    finally:
        maps.close()


def test_missing_usage_style_map_keeps_l1_and_warns(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    (maps_dir / "usage_style.json.gz").unlink()
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert any("usage_style.json.gz" in w and "L4" in w for w in maps.warnings)
        assert not maps.has_classes()
        _, numbers = _select("l4-subclass-attr", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5, 6}
    finally:
        maps.close()


def test_partial_construction_trace_disables_l4(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    partial = dict(mockmaps.CLASSES)
    # 6 of 7 tests traced: below the 90% gate.
    partial["touched"] = {
        key: value
        for key, value in mockmaps.CLASSES["touched"].items()
        if not key.endswith("test_gesundheit")
    }
    _write_gz_json(maps_dir / "classes.json.gz", partial)
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert not maps.has_classes()
        assert any("L4" in warning for warning in maps.warnings)
        _, numbers = _select("l4-subclass-attr", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5, 6}
    finally:
        maps.close()


def test_garbage_class_maps_do_not_crash(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    (maps_dir / "classes.json.gz").write_bytes(b"not gzip at all")
    (maps_dir / "usage_style.json.gz").write_text("[]", encoding="utf-8")
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        _, numbers = _select("l4-subclass-attr", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5, 6}
        assert not maps.has_classes()
    finally:
        maps.close()


def test_class_maps_without_symbols_still_work(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    # Constructors only: an empty hierarchy and a class whose symbol entry is
    # missing still select its constructors instead of crashing.
    _write_gz_json(
        maps_dir / "classes.json.gz",
        {
            "touched": {
                test_id: [name for name in classes if "Trigger" in name]
                for test_id, classes in mockmaps.CLASSES["touched"].items()
            },
            "hierarchy": {},
        },
    )
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert maps.has_classes()
        # Trigger's own constructors survive; TriggerChild is no longer a known
        # descendant (test_extra stays only through Trigger.render's span).
        ids = maps.classattr_tests("sqlglot/expr.py", "Trigger", "ARG_TYPES")
        assert ids is not None
        assert {
            "tests/test_mod.py::test_beta",
            "tests/test_mod.py::test_gamma",
            "tests/test_other.py::test_gesundheit",
        } <= ids
        selection, numbers = _select("l4-subclass-attr", maps, index)
        assert numbers == {1, 2, 4, 6}
        assert "classattr" in _rules(selection)
    finally:
        maps.close()


# -- knobs / adapter -----------------------------------------------------


def test_adapter_exposes_the_l4_knob(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    adapter = bench_adapter(maps_dir)
    try:
        on = adapter.select(mockmaps.PATCHES["l4-subclass-attr"])
        off = adapter.select(mockmaps.PATCHES["l4-subclass-attr"], knobs={"l4": "off"})
        assert "classattr" in on["rules"]
        assert "classattr" not in off["rules"]
        assert len(off["tests"]) > len(on["tests"])
        assert on["tier_e"] is False
    finally:
        adapter.close()


def test_l4_selection_is_cheap(tmp_path):
    """50 commits with L4 active must stay well under the per-batch CPU budget."""
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    adapter = bench_adapter(maps_dir)
    patches = [
        mockmaps.PATCHES[name]
        for name in ("l4-subclass-attr", "l4-dialect", "l4-missing-usage")
    ] * 17
    try:
        started = time.process_time()
        for patch in patches[:50]:
            adapter.select(patch)
        elapsed = time.process_time() - started
        assert elapsed < 2.0, f"L4 selection took {elapsed:.3f}s CPU for 50 commits"
    finally:
        adapter.close()
