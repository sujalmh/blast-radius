"""W1h: the committed enum-flip map (``maps/enum_flips.json.gz``).

The runtime enum proof used to build an index over every pinned source file
(~1.7 s of CPU per process here, several seconds on the scoring fleet).  The
map carries the same proof computed once at map-generation time, so a hunk is
answered by a line-set lookup.

These tests pin the three properties that matter:

* with the map present the runtime index is **never** built (and
  ``source.tar.gz`` is never opened), even for the sample's 2haz;
* a pre-W1h bundle without the map still resolves the same flips through the
  lazy fallback, so nothing regresses;
* the proof stays sound: an operand edit or a re-indent on a *proven* line is
  not neutral, and the ``tools/mapgen`` port of the proof agrees with the
  runtime implementation line for line.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from blastradius.enum_neutral import EnumIndex, is_committed_flip, maybe_enum_flip
from blastradius.maps_io import Maps
from blastradius.patch import parse_patch
from blastradius.select import TestIndex, select_commit
from ourtests import mockmaps

ROOT = Path(__file__).resolve().parents[1]
REAL_MAPS = ROOT / "maps"
SAMPLE = ROOT / "sample"

# The proven line (24) with a real operand change: the pairing gate already
# rejects it, and the map must not rescue it.
PATCH_OPERAND_CHANGE = """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -24,1 +24,1 @@ class Chooser:
-        return self.selected is Strategy.UPPER
+        return self.selected == Strategy.LOWER
"""

# Operator-only flip plus a re-indent.  The cheap text gate ignores whitespace,
# so this reaches the line-set proof: it must not pair (the line moved).
PATCH_REINDENT = """\
diff --git a/sqlglot/enums.py b/sqlglot/enums.py
--- a/sqlglot/enums.py
+++ b/sqlglot/enums.py
@@ -24,1 +24,1 @@ class Chooser:
-        return self.selected is Strategy.UPPER
+            return self.selected == Strategy.UPPER
"""


def _index(maps):
    return TestIndex(maps.test_ids(), mockmaps.number_by_id(), mockmaps.tests_by_file())


@pytest.fixture()
def loaded(tmp_path):
    maps = Maps.load(mockmaps.build_maps(tmp_path / "maps"))
    yield maps, _index(maps)
    maps.close()


@pytest.fixture()
def legacy(tmp_path):
    """A pre-W1h bundle: no v2 maps at all, so no ``enum_flips.json.gz``."""
    maps = Maps.load(mockmaps.build_maps(tmp_path / "maps", with_v2=False))
    yield maps, _index(maps)
    maps.close()


def _select(text, maps, index):
    selection = select_commit(parse_patch(text), maps, index)
    return selection, index.numbers_for_ids(selection.tests)



def _real_index(tests_document):
    number_by_id = {str(entry["id"]): int(entry["number"]) for entry in tests_document}
    by_file: dict[str, list[str]] = {}
    for entry in tests_document:
        by_file.setdefault(str(entry["file"]), []).append(str(entry["id"]))
    return number_by_id, by_file


# -- map present: lookup only ---------------------------------------------
def test_committed_map_never_builds_the_runtime_index(loaded, monkeypatch):
    maps, index = loaded
    assert maps.enum_flip_lines() == {"sqlglot/enums.py": frozenset({24, 27})}

    def _unbuilt():
        raise AssertionError("runtime enum index built although the map is committed")

    monkeypatch.setattr(maps, "enum_index", _unbuilt)
    for name in ("enum-neutral-flip", "enum-annotated"):
        selection, numbers = _select(mockmaps.PATCHES[name], maps, index)
        assert selection.tier == "neutral", name
        assert numbers == set(), name
    # The proof never needed an index: not even the cheap constructor ran.
    assert maps._enum_index is None


def test_committed_map_keeps_every_unproven_flip_selected(loaded, monkeypatch):
    maps, index = loaded
    monkeypatch.setattr(
        maps,
        "enum_index",
        lambda: (_ for _ in ()).throw(AssertionError("index built")),
    )
    for name in (
        "enum-str-operand",
        "enum-none-flip",
        "enum-custom-eq",
        "enum-rebound-param",
        "enum-tuple-target",
    ):
        selection, _ = _select(mockmaps.PATCHES[name], maps, index)
        assert selection.tier != "neutral", name


def test_operand_change_on_a_proven_line_is_not_neutral(loaded):
    maps, index = loaded
    files = parse_patch(PATCH_OPERAND_CHANGE)
    (file_patch,) = files
    proven = maps.enum_flip_lines()
    assert proven is not None
    assert not is_committed_flip(proven, "sqlglot/enums.py", file_patch)
    assert select_commit(files, maps, index).tier != "neutral"


def test_reindent_never_pairs_as_an_operator_flip(loaded):
    maps, index = loaded
    files = parse_patch(PATCH_REINDENT)
    (file_patch,) = files
    # The W2g text gate is whitespace-blind on purpose: it only filters.
    assert maybe_enum_flip(file_patch)
    proven = maps.enum_flip_lines()
    assert proven is not None
    assert not is_committed_flip(proven, "sqlglot/enums.py", file_patch)
    assert select_commit(files, maps, index).tier != "neutral"


def test_missing_map_entry_for_a_file_is_not_neutral(loaded):
    maps, _index_ = loaded
    (file_patch,) = parse_patch(mockmaps.PATCHES["enum-neutral-flip"])
    assert not is_committed_flip({"lines": {}}, "sqlglot/enums.py", file_patch)
    assert not is_committed_flip(
        {"sqlglot/other.py": frozenset({24})}, "sqlglot/enums.py", file_patch
    )


# -- map absent: the v3 fallback ------------------------------------------
def test_bundle_without_the_map_still_resolves_the_flip(legacy):
    maps, index = legacy
    assert maps.enum_flip_lines() is None  # pre-W1h bundle
    built: list[int] = []
    real = Maps.enum_index

    def _spy(self):
        built.append(1)
        return real(self)

    maps.enum_index = _spy.__get__(maps, Maps)  # instance monkeypatch, no pytest fixture
    try:
        selection, numbers = _select(mockmaps.PATCHES["enum-neutral-flip"], maps, index)
    finally:
        del maps.enum_index
    assert selection.tier == "neutral"
    assert numbers == set()
    assert built == [1]
    # ... and the unproven cases stay selected with the fallback too.
    assert _select(mockmaps.PATCHES["enum-str-operand"], maps, index)[0].tier != "neutral"


# -- the mapgen port and the runtime proof must agree ---------------------
def _gen_maps():
    path = ROOT / "tools" / "mapgen" / "gen_maps.py"
    spec = importlib.util.spec_from_file_location("br_gen_maps_enum", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mapgen_port_matches_the_runtime_proof_on_the_mock_tree():
    texts = {path: mockmaps.source_text(path) for path in mockmaps.SOURCE_FILES}
    trees = {path: ast.parse(text) for path, text in texts.items()}
    payload, stats = _gen_maps().build_enum_flips(trees)
    runtime = EnumIndex(list(texts), lambda path: texts.get(path)).proven_lines()
    assert {rel: tuple(lines) for rel, lines in payload["lines"].items()} == runtime
    assert payload["lines"] == {"sqlglot/enums.py": [24, 27]}
    assert stats["files"] == 1 and stats["lines"] == 2


# -- the real sample: 2haz flips dialect.py:1280 --------------------------
@pytest.mark.skipif(
    not (REAL_MAPS / "enum_flips.json.gz").is_file()
    or not (SAMPLE / "commits" / "2haz.patch").is_file(),
    reason="committed enum map or the sample batch is not present",
)
def test_committed_map_covers_the_2haz_normalization_strategy_line(monkeypatch):
    maps = Maps.load(REAL_MAPS)
    try:
        proven = maps.enum_flip_lines()
        assert proven is not None
        lines = proven.get("sqlglot/dialects/dialect.py", frozenset())
        assert 1280 in lines
        pinned = maps.source_text("sqlglot/dialects/dialect.py")
        assert pinned is not None
        assert (
            pinned.splitlines()[1279].strip()
            == "if self.normalization_strategy is NormalizationStrategy.UPPERCASE"
        )
        tests = json.loads((SAMPLE / "tests.json").read_text("utf-8"))["tests"]
        number_by_id, by_file = _real_index(tests)
        index = TestIndex(maps.test_ids(), number_by_id, by_file)

        def _unbuilt():
            raise AssertionError("runtime enum index built for 2haz")

        monkeypatch.setattr(maps, "enum_index", _unbuilt)
        text = (SAMPLE / "commits" / "2haz.patch").read_text("utf-8")
        selection, numbers = _select(text, maps, index)
        assert selection.tier == "neutral"
        assert numbers == set()
    finally:
        maps.close()
