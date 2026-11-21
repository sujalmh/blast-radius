"""calibrate.py: variant parsing, end-to-end mock run, skip/degrade paths."""

import json
import shutil
import sys
import types
from pathlib import Path

import pytest

from bench import calibrate
from bench.engine import Knobs

MOCK_MAPS = Path(__file__).resolve().parents[1] / "mockmaps"
MOCK_DATA = Path(__file__).resolve().parents[1] / "mockdata"


def run(argv):
    return calibrate.main(argv)


def test_parse_variants_default():
    assert calibrate.parse_variants(None, Knobs()) == [("base", Knobs())]


def test_parse_variants_presets_and_overrides():
    variants = dict(calibrate.parse_variants("A=v1; B=line+no-usage", Knobs()))
    assert variants["A"] == Knobs()
    assert variants["B"].symbol_vs_line == "line"
    assert variants["B"].usage is False


def test_parse_variants_positional_and_json():
    variants = dict(calibrate.parse_variants('C=symbol,on,off,on,0,empty; D={"pad": 3}', Knobs()))
    assert variants["C"].usage is False and variants["C"].tier_e == "empty"
    assert variants["D"].pad == 3


def test_parse_variants_rejects_unknown_preset():
    with pytest.raises(ValueError):
        calibrate.parse_variants("A=nope", Knobs())


def test_full_mock_run_scores_two_variants(capsys, tmp_path):
    json_path = tmp_path / "rows.json"
    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--engine",
            "reference",
            "--variants",
            "A=v1; B=line+no-usage",
            "--json",
            str(json_path),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "variant A" in output and "variant B" in output
    assert "engine=reference" in output
    assert "MISSES 0" in output.split("== variant B")[0]
    assert "MISSES 1" in output.split("== variant B")[1]
    assert "missed: mm1_helper_const" in output
    assert "PROVENANCE" in output
    assert "UNATTRIBUTED mm1_helper_const" in output

    payload = json.loads(json_path.read_text("utf-8"))
    assert set(payload) == {"A", "B"}
    assert len(payload["A"]) == 4
    assert all(row["covered"] for row in payload["A"])
    assert [row["commit"] for row in payload["B"] if not row["covered"]] == ["mm1_helper_const"]
    assert {row["commit"] for row in payload["A"]} == {
        "mm1_helper_const",
        "mm2_fixture_change",
        "mm3_test_file_edit",
        "mm4_unknown_module",
    }


def test_only_and_limit(capsys, tmp_path):
    json_path = tmp_path / "rows.json"
    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--engine",
            "reference",
            "--variants",
            "A=v1",
            "--only",
            "mm2_fixture_change,mm3_test_file_edit",
            "--json",
            str(json_path),
        ]
    )
    assert code == 0
    rows = json.loads(json_path.read_text("utf-8"))["A"]
    assert [row["commit"] for row in rows] == ["mm2_fixture_change", "mm3_test_file_edit"]

    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--engine",
            "reference",
            "--limit",
            "2",
            "--json",
            str(json_path),
        ]
    )
    assert code == 0
    assert len(json.loads(json_path.read_text("utf-8"))["base"]) == 2


def test_dropped_ids_are_reported(capsys, tmp_path):
    data_dir = tmp_path / "data"
    shutil.copytree(MOCK_DATA, data_dir)
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))
    kept = [test for test in tests["tests"] if test["id"] != "tests/test_mock_helper.py::TestMockHelper::test_dates"]
    (data_dir / "tests.json").write_text(json.dumps({"tests": kept, "skipped": []}), "utf-8")

    code = run(["--maps", str(MOCK_MAPS), "--data", str(data_dir), "--engine", "reference"])
    output = capsys.readouterr().out
    assert code == 0
    assert "not in tests.json" in output


def test_missing_blastradius_engine_reports_cleanly(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "blastradius", None)
    code = run(["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA), "--engine", "blastradius"])
    stderr = capsys.readouterr().err
    assert code == 2
    assert "no usable blastradius engine" in stderr
    assert "needed from W2" in stderr


def test_incompatible_backend_skips_variant_not_crash(capsys, monkeypatch):
    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            raise RuntimeError("W2 broke something")

    package = types.ModuleType("blastradius")
    package.bench_adapter = lambda maps_dir: FakeAdapter()
    monkeypatch.setitem(sys.modules, "blastradius", package)

    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--engine",
            "blastradius",
            "--variants",
            "A=v1",
        ]
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "SKIPPED" in output
    assert "RuntimeError" in output


def test_engine_auto_prefers_blastradius_when_compatible(capsys, monkeypatch):
    from bench.engine import Selection

    class FakeAdapter:
        def select(self, patch_text, knobs=None):
            return Selection(tests={"tests/test_mock_parser.py::TestMockParser::test_a"}, rules={"symbol": {"tests/test_mock_parser.py::TestMockParser::test_a"}})

    package = types.ModuleType("blastradius")
    package.bench_adapter = lambda maps_dir: FakeAdapter()
    monkeypatch.setitem(sys.modules, "blastradius", package)
    code = run(["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA), "--variants", "A=v1", "--limit", "1"])
    output = capsys.readouterr().out
    assert code == 0
    assert "engine=blastradius" in output
    assert "mixture" not in output


def test_missing_data_is_a_clean_error(capsys, tmp_path):
    with pytest.raises(SystemExit) as error:
        run(["--maps", str(MOCK_MAPS), "--data", str(tmp_path)])
    assert "tests.json" in str(error.value)


def _native_available():
    from bench import adapter

    found, _ = adapter.probe_blastradius(MOCK_MAPS)
    return found is not None


def test_auto_uses_native_for_v1_and_reference_for_knobs(capsys):
    if not _native_available():
        pytest.skip("W2 native API not available")
    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--variants",
            "A=v1; B=pad2",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "engine=blastradius" in captured.out
    assert "engine=reference" in captured.out
    assert "blastradius takes no knob arguments" in captured.err


def test_explicit_engine_blastradius_skips_knob_variant(capsys):
    if not _native_available():
        pytest.skip("W2 native API not available")
    code = run(
        [
            "--maps",
            str(MOCK_MAPS),
            "--data",
            str(MOCK_DATA),
            "--engine",
            "blastradius",
            "--variants",
            "A=pad2",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "SKIPPED" in captured.out
    assert "no knob arguments" in captured.out
