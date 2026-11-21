"""The synthetic generalization guard runs green and its expectations are sane."""

import json
from pathlib import Path

from bench import run_synthetic
from bench.engine import Knobs
from bench.patch import parse_patch

BENCH = Path(__file__).resolve().parents[1]
MOCK_MAPS = BENCH / "mockmaps"
MOCK_DATA = BENCH / "mockdata"
EXPECTATIONS = BENCH / "synthetic" / "expectations.json"


def test_expectations_reference_existing_patches_and_valid_keys():
    payload = json.loads(EXPECTATIONS.read_text("utf-8"))
    checks = payload["checks"]
    assert len(checks) == 10
    allowed = run_synthetic.ALLOWED_KEYS
    for case in checks:
        patch_path = BENCH / "synthetic" / case["patch"]
        assert patch_path.is_file(), case["patch"]
        changes = parse_patch(patch_path.read_text("utf-8"))
        assert changes, case["id"]
        assert case["checks"]
        for check in case["checks"]:
            Knobs().with_overrides(**check.get("knobs", {}))
            for key in ("expect", "mock", "real"):
                assert set(check.get(key, {})) <= allowed, (case["id"], key)


def test_synthetic_runner_is_green_on_mock_maps(capsys):
    code = run_synthetic.main(["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA)])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "0 failed" in captured.out
    assert "11 checks run" in captured.out
    assert "synthetic] ok" in captured.err


def test_synthetic_runner_verbose_lists_selections(capsys):
    code = run_synthetic.main(
        ["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA), "--cases", "fixture_known", "--verbose"]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "rules=fixture:2" in output


def test_synthetic_runner_reports_unknown_case(capsys):
    assert run_synthetic.main(["--maps", str(MOCK_MAPS), "--cases", "nope"]) == 2
    assert "unknown case ids" in capsys.readouterr().err


def test_runner_detects_failures_when_knobs_disabled(capsys):
    # with classbody/usage off, module_constant cannot be caught -> expectations fail
    payload = json.loads(EXPECTATIONS.read_text("utf-8"))
    for case in payload["checks"]:
        for check in case["checks"]:
            if case["id"] == "module_constant":
                check["knobs"] = {"classbody": False, "usage": False}
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        handle.write(json.dumps(payload))
        path = handle.name
    try:
        code = run_synthetic.main(
            ["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA), "--expectations", path]
        )
    finally:
        Path(path).unlink()
    assert code == 1
    assert "FAIL" in capsys.readouterr().out
