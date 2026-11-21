"""Adapter degradation: missing blastradius pieces become skips, never crashes."""

import sys
import types

import pytest

from bench import adapter
from bench.engine import Knobs, Selection
from bench.mapio import MapSet


class FakePackage(types.ModuleType):
    def __init__(self, name="blastradius", **attributes):
        super().__init__(name)
        for key, value in attributes.items():
            setattr(self, key, value)


@pytest.fixture()
def no_blastradius(monkeypatch):
    monkeypatch.setitem(sys.modules, "blastradius", None)  # forces ImportError
    for name in [key for key in sys.modules if key.startswith("blastradius.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_auto_falls_back_to_reference_when_package_missing(maps, no_blastradius):
    warnings = []
    backend = adapter.load_backend("auto", maps, warn=warnings.append)
    assert backend.name == "reference"
    assert backend.has_provenance
    assert warnings and "reference engine" in warnings[0]
    assert "bench_adapter" in warnings[0]  # says exactly what W2 must expose


def test_explicit_blastradius_engine_raises_when_missing(maps, no_blastradius):
    with pytest.raises(adapter.BackendError):
        adapter.load_backend("blastradius", maps)


def test_reference_backend_handles_selection(maps):
    backend = adapter.load_backend("reference", maps)
    selection = backend.select(
        "diff --git a/tests/fixtures/optimizer/pushdown_predicates.sql "
        "b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "--- a/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "+++ b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "@@ -1,1 +1,1 @@\n"
        "-SELECT 1\n"
        "+SELECT 2\n",
        Knobs(),
    )
    assert selection.rules and "fixture" in selection.rules


def test_probe_uses_bench_adapter_shim(maps, monkeypatch):
    captured = {}

    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            captured["patch"] = patch_text
            captured["knobs"] = knobs
            return {"tests": {"tests/x.py::test_a"}, "tier_e": False, "rules": {"symbol": {"tests/x.py::test_a"}}}

    def bench_adapter(maps_dir):
        captured["maps_dir"] = maps_dir
        return FakeAdapter()

    monkeypatch.setitem(sys.modules, "blastradius", FakePackage(bench_adapter=bench_adapter))
    backend = adapter.load_backend("blastradius", maps)
    assert backend.name == "blastradius"
    selection = backend.select("patch-text", Knobs(pad=2))
    assert selection.tests == {"tests/x.py::test_a"}
    assert captured["knobs"]["pad"] == 2
    assert captured["maps_dir"] == maps.directory


def test_probe_without_usable_entry_point_degrades(maps, monkeypatch):
    monkeypatch.setitem(sys.modules, "blastradius", FakePackage())
    warnings = []
    backend = adapter.load_backend("auto", maps, warn=warnings.append)
    assert backend.name == "reference"
    capabilities = adapter.probe_blastradius(maps.directory)[1]
    assert not capabilities.available
    assert capabilities.missing


def test_select_signature_mismatch_becomes_unsupported(maps, monkeypatch):
    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            raise TypeError("unexpected keywords")

    monkeypatch.setitem(
        sys.modules, "blastradius", FakePackage(bench_adapter=lambda maps_dir: FakeAdapter())
    )
    backend = adapter.load_backend("blastradius", maps)
    with pytest.raises(adapter.BackendUnsupported) as error:
        backend.select("patch", Knobs())
    assert "call" in str(error.value)


def test_select_exception_becomes_unsupported(maps, monkeypatch):
    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            raise RuntimeError("boom")

    monkeypatch.setitem(
        sys.modules, "blastradius", FakePackage(bench_adapter=lambda maps_dir: FakeAdapter())
    )
    backend = adapter.load_backend("blastradius", maps)
    with pytest.raises(adapter.BackendUnsupported) as error:
        backend.select("patch", Knobs())
    assert "RuntimeError" in str(error.value)


def test_unsupported_return_shape_becomes_unsupported(maps, monkeypatch):
    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            return object()

    monkeypatch.setitem(
        sys.modules, "blastradius", FakePackage(bench_adapter=lambda maps_dir: FakeAdapter())
    )
    backend = adapter.load_backend("blastradius", maps)
    with pytest.raises(adapter.BackendUnsupported):
        backend.select("patch", Knobs())


def test_normalize_result_shapes():
    bare = adapter.normalize_result({"a"})
    assert bare.tests == {"a"} and bare.rules == {}
    assert adapter.normalize_result(["a", "b"]).tests == {"a", "b"}
    assert adapter.normalize_result({"tests": ["a"], "tier_e": True, "rules": {"x": ["a"]}}) == Selection(
        tests={"a"}, tier_e=True, rules={"x": {"a"}}
    )

    class Result:
        tests = {"a"}
        tier_e = True
        rules = {"symbol": {"a"}}
        notes = ["n"]
        neutral = True

    normalized = adapter.normalize_result(Result())
    assert normalized.tests == {"a"} and normalized.tier_e and normalized.neutral is True
    assert adapter.normalize_result(None) is None


def test_provenance_absence_is_detected(maps, monkeypatch):
    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            return ["tests/x.py::test_a"]

    warnings = []
    monkeypatch.setitem(
        sys.modules, "blastradius", FakePackage(bench_adapter=lambda maps_dir: FakeAdapter())
    )
    backend = adapter.load_backend("blastradius", maps, warn=warnings.append)
    backend.select("patch", Knobs())
    assert backend.has_provenance is False
    assert any("provenance" in message for message in warnings)


def test_split_is_shared_with_reference(maps):
    backend = adapter.load_backend("reference", maps)
    tests = [{"number": 0, "id": "tests/x.py::a", "seconds": 0.5, "file": "tests/x.py"}]
    workers, cost = backend.split(tests, {"tests/x.py::a"})
    assert workers == [[0]] and cost == 2500


def test_repo_root_is_put_on_sys_path():
    assert str(adapter.REPO_ROOT) in sys.path
    again = MapSet.load(adapter.REPO_ROOT / "bench" / "mockmaps")
    assert again.tests_index


def _native_or_skip(maps):
    found, capabilities = adapter.probe_blastradius(maps.directory)
    if found is None or not isinstance(found, adapter.BlastRadiusNativeBackend):
        pytest.skip(f"W2 native API not available: {capabilities.detail}")
    return found


def test_native_w2_backend_runs_v1_defaults(maps):
    backend = _native_or_skip(maps)
    assert backend.name == "blastradius"
    assert backend.has_provenance is False
    backend.check_knobs(Knobs())

    patch = (
        "diff --git a/tests/fixtures/optimizer/pushdown_predicates.sql "
        "b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "--- a/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "+++ b/tests/fixtures/optimizer/pushdown_predicates.sql\n"
        "@@ -1,1 +1,1 @@\n"
        "-SELECT 1\n"
        "+SELECT 2\n"
    )
    selection = backend.select(patch, Knobs())
    assert not selection.tier_e
    assert selection.tests


def test_native_w2_backend_rejects_knob_variants(maps):
    backend = _native_or_skip(maps)
    with pytest.raises(adapter.BackendUnsupported) as error:
        backend.check_knobs(Knobs(pad=2))
    assert "no knob arguments" in str(error.value)
