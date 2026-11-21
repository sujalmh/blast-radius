"""probe.py subcommands (the orchestrator's ad-hoc map queries)."""

import json
from pathlib import Path

from bench import probe

MOCK_MAPS = Path(__file__).resolve().parents[1] / "mockmaps"
MOCK_DATA = Path(__file__).resolve().parents[1] / "mockdata"


def run(*argv):
    return probe.main(["--maps", str(MOCK_MAPS), "--data", str(MOCK_DATA), *argv])


def test_describe(capsys):
    assert run("--json", "describe") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mock"] is True
    assert payload["tests"] == 12
    assert payload["missing"] == []


def test_symbols(capsys):
    assert run("symbols", "sqlglot/helper.py") == 0
    output = capsys.readouterr().out
    assert "class    AutoName" in output
    assert "function seq_get" in output
    assert run("symbols", "sqlglot/not_there.py") == 1


def test_cover_lines(capsys):
    assert run("cover-lines", "sqlglot/helper.py", "471", "476") == 0
    output = capsys.readouterr().out
    assert "tests/test_mock_helper.py::TestMockHelper::test_dates" in output
    assert "471-475" in output

    assert run("cover-lines", "sqlglot/helper.py", "1", "20") == 1
    assert "(no tests)" in capsys.readouterr().out
    assert run("cover-lines", "sqlglot/helper.py", "20", "10") == 2


def test_fixture(capsys):
    assert run("fixture", "tests/fixtures/optimizer/pushdown_predicates.sql") == 0
    output = capsys.readouterr().out
    assert "TestMockOptimizer::test_a" in output
    assert run("fixture", "tests/fixtures/identity.sql") == 1
    assert "Tier E" in capsys.readouterr().out


def test_usage(capsys):
    assert run("usage", "DATE_UNITS") == 0
    output = capsys.readouterr().out
    assert "sqlglot/helper.py: lines 466, 470, 475" in output
    assert "test_dates" in output
    assert run("usage", "DATE_UNITS", "--file", "sqlglot/trie.py") == 1
    assert run("usage", "NO_SUCH_NAME") == 1


def test_hunk(capsys):
    assert run("hunk", "sqlglot/helper.py", "464", "468") == 0
    output = capsys.readouterr().out
    assert "rules: {'classbody': 2, 'usage': 1}" in output
    assert "tier_e: False" in output

    capsys.readouterr()
    assert run("hunk", "sqlglot/helper.py", "464", "468", "--usage", "off") == 0
    assert "usage" not in capsys.readouterr().out

    capsys.readouterr()
    assert run("--json", "hunk", "sqlglot/planner.py", "13", "13") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tier_e"] is True
    assert len(payload["tests"]) == 12
