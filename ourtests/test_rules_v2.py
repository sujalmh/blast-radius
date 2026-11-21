"""v2 rule refinement: dialect narrowing, L3 key reads, import-region Tier E.

Every scenario uses the handcrafted maps in ``mockmaps`` (nothing here is
derived from real sqlglot); the point is the *shape* of each rule, including its
gates and its degraded behaviour when a map file is missing.
"""

from __future__ import annotations

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


def _one_file(path, hunk):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunk}"


# -- maps_io readers -----------------------------------------------------


def test_dialect_readers(loaded):
    maps, _ = loaded
    assert maps.has_dialects()
    assert maps.dialect_file_scope("sqlglot/dialects/duck.py") == "duck"
    assert maps.dialect_file_scope("sqlglot/mod.py") is None
    assert maps.dialect_class_scope("sqlglot/dialects/duck.py", "Duck") == "duck"
    assert maps.dialect_class_scope("sqlglot/dialects/duck.py", "Duck.emit") == "duck"
    assert maps.dialect_class_scope("sqlglot/dialects/duck.py", "Other") is None
    exposed = maps.tests_exposed_to("duck")
    assert exposed is not None
    assert {"tests/test_mod.py::test_alpha", "tests/test_other.py::test_metadata"} <= exposed
    assert "tests/test_mod.py::test_gamma" not in exposed


def test_keyread_readers(loaded):
    maps, _ = loaded
    assert maps.has_keyreads()
    assert maps.name_usage_kind("sqlglot/consts.py", "LIMITS") == "keyable"
    assert maps.name_usage_kind("sqlglot/consts.py", "OPTIONS") == "opaque"
    assert maps.name_usage_kind("sqlglot/missing.py", "LIMITS") is None
    assert maps.name_is_fully_keyable("LIMITS")
    assert not maps.name_is_fully_keyable("OPTIONS")
    reads = maps.keyread_reads("LIMITS")
    assert set(reads) == {"a", "c"}
    assert reads["c"] == frozenset({"tests/test_mod.py::test_gamma"})


def test_literal_entry_lookup(loaded):
    maps, _ = loaded
    assert maps.literal_entry_at("sqlglot/consts.py", 5) == ("LIMITS", "dict", "b", None)
    # An enum key is not renderable: the L3 gate then refuses to narrow.
    assert maps.literal_entry_at("sqlglot/consts.py", 19) is None
    assert maps.literal_entry_at("sqlglot/mod.py", 7) is None


def test_testimport_shares(loaded):
    maps, _ = loaded
    assert maps.test_file_count() == 2
    assert maps.module_import_share("sqlglot.core") == 1.0
    assert maps.module_import_share("sqlglot.consts") == 0.0
    assert maps.module_import_share("sqlglot.missing") == 0.0


def test_dialect_family_includes_derived_dialects(loaded):
    maps, _ = loaded
    # gander.Gander inherits duck.Duck: a gander test exercises duck code.
    assert maps.dialect_family("duck") == frozenset({"duck", "gander"})
    assert maps.dialect_family("gander") == frozenset({"gander"})
    exposed = maps.tests_exposed_to_family("duck")
    assert exposed is not None
    assert "tests/test_other.py::test_gesundheit" in exposed
    assert "tests/test_mod.py::test_gamma" not in exposed


def test_partial_exposure_map_disables_narrowing(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    partial = dict(mockmaps.DIALECTS)
    partial["exposure"] = {
        key: value
        for key, value in mockmaps.DIALECTS["exposure"].items()
        if not key.endswith("test_delta")
    }
    mockmaps._write_gz_json(maps_dir / "dialects.json.gz", partial)
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert maps.tests_exposed_to("duck") is None
        _, numbers = _select("dialect-class-body", maps, index)
        assert numbers == {0, 2, 3, 6}  # unrefined: nothing dropped
        assert any("subset" in warning for warning in maps.warnings)
    finally:
        maps.close()


# -- (a) symbol hunks + dialect narrowing --------------------------------


def test_symbol_change_is_dialect_narrowed(loaded):
    maps, index = loaded
    _, numbers = _select("dialect-symbol", maps, index)
    # duck + gander are exposed; test_gamma is default-only and dropped.
    assert numbers == {0, 4, 6}


def test_symbol_change_without_dialect_knob_keeps_default_only_test(loaded):
    maps, index = loaded
    _, numbers = _select("dialect-symbol", maps, index, Knobs(dialect="off"))
    assert numbers == {0, 2, 4, 6}


def test_dialect_narrowing_never_drops_exposed_tests(loaded):
    maps, index = loaded
    _, narrowed = _select("dialect-class-body", maps, index)
    _, plain = _select("dialect-class-body", maps, index, Knobs(dialect="off"))
    family_exposed = {0, 3, 4, 6}  # duck + its derived dialect gander
    # Exactly the family-exposed part of the unrefined selection survives.
    assert narrowed == {number for number in plain if number in family_exposed}
    assert narrowed < plain
    assert 3 in narrowed  # the class-body anchor test is duck-exposed


# -- (b) class-body / module-level hunks ---------------------------------


def test_class_body_drops_all_file_expansion(loaded):
    maps, index = loaded
    selection, numbers = _select("dialect-class-body", maps, index, Knobs(dialect="off"))
    assert numbers == {0, 2, 3, 6}
    rules = {hit.rule for hit in selection.rules}
    assert "classbody_all" not in rules
    assert "anchor" in rules and "usage" in rules


def test_classbody_all_knob_restores_v1_expansion(loaded):
    maps, index = loaded
    selection, numbers = _select(
        "dialect-class-body", maps, index, Knobs(dialect="off", classbody="all")
    )
    assert "classbody_all" in {hit.rule for hit in selection.rules}
    assert {0, 2, 3, 4} <= numbers
    assert numbers != {0, 2, 3, 6}


def test_classbody_off_keeps_only_the_anchor_union(loaded):
    maps, index = loaded
    selection, numbers = _select(
        "dialect-class-body", maps, index, Knobs(dialect="off", classbody="off")
    )
    assert numbers == {3}
    assert {hit.rule for hit in selection.rules} == {"coverage", "anchor"} or {
        hit.rule for hit in selection.rules
    } == {"anchor"}


# -- (c) L3 key-read narrowing ------------------------------------------


def test_keyread_collapse_drops_unrelated_tests(loaded):
    maps, index = loaded
    selection, numbers = _select("keyread-collapse", maps, index)
    assert numbers == {0, 1, 2, 3, 4}
    assert "keyread" in {hit.rule for hit in selection.rules}
    # L1 alone (both maps present but the knob off) is the full six.
    _, plain = _select("keyread-collapse", maps, index, Knobs(keyreads="off"))
    assert plain == {0, 1, 2, 3, 4, 5}


def test_keyread_collapse_uses_the_class_qualified_name(loaded):
    maps, index = loaded
    selection, numbers = _select("keyread-class-qualified", maps, index)
    assert numbers == {0, 1, 2, 3, 4}
    assert "keyread" in {hit.rule for hit in selection.rules}


def test_keyread_collapse_supports_set_membership(loaded):
    maps, index = loaded
    # A set is membership-only, so the key is the element value (not an index).
    assert maps.literal_entry_at("sqlglot/consts.py", 34) == (
        "SET_KEYWORDS",
        "set",
        "two",
        None,
    )
    selection, numbers = _select("keyread-set", maps, index)
    assert numbers == {0, 1, 2, 3, 4}
    assert "keyread" in {hit.rule for hit in selection.rules}


@pytest.mark.parametrize(
    "patch_name",
    [
        "keyread-readers",  # the changed key has recorded readers
        "keyread-opaque",  # static usage kind is not "keyable"
        "keyread-sparse",  # fewer than 5 tests read other keys (proxy not alive)
        "keyread-ambiguous",  # the changed line does not give one constant key
    ],
)
def test_l3_gate_failure_keeps_l1(loaded, patch_name):
    maps, index = loaded
    selection, numbers = _select(patch_name, maps, index)
    assert numbers == {0, 1, 2, 3, 4, 5}  # L1 kept, nothing collapsed
    assert "keyread" not in {hit.rule for hit in selection.rules}
    assert "usage" in {hit.rule for hit in selection.rules}


def test_definition_lines_are_not_references(loaded):
    maps, index = loaded
    assert "OPTIONS" in maps.definition_names_at("sqlglot/dialects/gander.py", 5)
    assert "METADATA" in maps.definition_names_at("sqlglot/other.py", 1)
    assert "METADATA" not in maps.definition_names_at("sqlglot/other.py", 7)
    # The other file's OPTIONS definition line is in usage_refs; the selector
    # drops it, so gander's test is not dragged in.
    selection, numbers = _select("keyread-opaque", maps, index)
    assert numbers == {0, 1, 2, 3, 4, 5}
    assert "tests/test_other.py::test_gesundheit" not in selection.tests


def test_l3_unknown_name_keeps_l1(loaded):
    maps, index = loaded
    text = _one_file(
        "sqlglot/mod.py",
        "@@ -11,1 +11,1 @@ class Helper:\n-    LIMIT = 3\n+    LIMIT = 4\n",
    )
    selection = select_commit(parse_patch(text), maps, index)
    assert index.numbers_for_ids(selection.tests) == {1}
    assert "keyread" not in {hit.rule for hit in selection.rules}


# -- (d) import region ---------------------------------------------------


@pytest.mark.parametrize(
    "patch_name",
    ["import-line", "package-init", "shared-module", "new-conftest"],
)
def test_import_region_is_tier_e(loaded, patch_name):
    maps, index = loaded
    selection, numbers = _select(patch_name, maps, index)
    assert selection.tier == "tier_e"
    assert numbers == set(range(len(mockmaps.TESTS)))


def test_import_rule_needs_the_map(loaded, tmp_path):
    maps, index = loaded
    # A module level change in a non-shared module is *not* Tier E.
    text = _one_file(
        "sqlglot/consts.py",
        "@@ -23,1 +23,1 @@\n-    return LIMITS[key]\n+    return LIMITS[key] + 1\n",
    )
    selection = select_commit(parse_patch(text), maps, index)
    assert selection.tier != "tier_e"


def test_function_body_change_in_shared_module_is_not_tier_e(loaded):
    maps, index = loaded
    _, numbers = _select("core-function", maps, index)
    assert numbers == {0}


# -- degraded behaviour --------------------------------------------------


def test_missing_v2_maps_keep_the_unrefined_rules(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps", with_v2=False)
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        assert not maps.has_dialects()
        assert not maps.has_keyreads()
        assert maps.tests_exposed_to("duck") is None
        assert maps.module_import_share("sqlglot.core") is None
        # No narrowing, no collapse: the unrefined L1 selection.
        _, numbers = _select("dialect-class-body", maps, index)
        assert numbers == {0, 2, 3, 6}
        _, numbers = _select("keyread-collapse", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5}
        assert maps.warnings == []
    finally:
        maps.close()


def test_foreign_map_files_do_not_crash(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    (maps_dir / "dialects.json.gz").write_bytes(b"not gzip")
    (maps_dir / "keyreads.json.gz").write_bytes(b"{}")
    (maps_dir / "testimports.json.gz").write_text("[]", encoding="utf-8")
    maps = Maps.load(maps_dir)
    index = _index(maps)
    try:
        _, numbers = _select("dialect-class-body", maps, index)
        assert numbers == {0, 2, 3, 6}
        _, numbers = _select("keyread-collapse", maps, index)
        assert numbers == {0, 1, 2, 3, 4, 5}
    finally:
        maps.close()


# -- bench adapter -------------------------------------------------------


def test_adapter_returns_tests_rules_and_ab_knobs(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    durations = {
        number: 1000 for number in range(len(mockmaps.TESTS))
    }
    adapter = bench_adapter(maps_dir, durations_ms=durations)
    try:
        default = adapter.select(mockmaps.PATCHES["dialect-class-body"])
        assert default["tier_e"] is False
        assert default["rules"]
        assert default["notes"]
        assert default["workers"] is not None
        assert sorted(n for worker in default["workers"] for n in worker) == sorted(
            adapter.ordinal_by_id[test_id] for test_id in default["tests"]
        )

        off = adapter.select(
            mockmaps.PATCHES["dialect-class-body"], knobs={"dialect": "off"}
        )
        assert len(off["tests"]) > len(default["tests"])

        # Unknown knob keys are notes, never errors.
        odd = adapter.select(mockmaps.PATCHES["code-alpha"], knobs={"pad": 2})
        assert any("unknown knob" in note for note in odd["notes"])
        assert odd["tests"] == {"tests/test_mod.py::test_alpha"}

        tier_e = adapter.select(mockmaps.PATCHES["import-line"])
        assert tier_e["tier_e"] is True
        assert tier_e["tests"] == set(adapter.ids)
    finally:
        adapter.close()


def test_adapter_without_durations_returns_no_workers(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    adapter = bench_adapter(maps_dir)
    try:
        result = adapter.select(mockmaps.PATCHES["code-alpha"])
        assert result["workers"] is None
    finally:
        adapter.close()


def test_adapter_survives_a_malformed_patch(tmp_path):
    maps_dir = mockmaps.build_maps(tmp_path / "maps")
    adapter = bench_adapter(maps_dir)
    try:
        result = adapter.select(mockmaps.PATCHES["malformed"])
        assert result["tier_e"] is True
        assert result["tests"] == set(adapter.ids)
    finally:
        adapter.close()
