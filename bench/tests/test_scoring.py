"""Scoring bridge + table/provenance assembly (pure functions, easy to check)."""

import json

from bench.scoring import (
    Row,
    commit_result,
    format_seconds,
    load_score_module,
    provenance_table,
    render_table,
    rule_summary,
    totals,
)


def make_tests(seconds):
    return [
        {"number": index, "id": f"tests/t{index}.py::test_{index}", "file": f"tests/t{index}.py", "seconds": value}
        for index, value in enumerate(seconds)
    ]


def row(commit="c1", selected=2, expected=1, covered=True, cost=2500, **kwargs):
    return Row(
        commit=commit,
        kind=kwargs.pop("kind", "code"),
        selected=selected,
        expected=expected,
        covered=covered,
        cost_ms=cost,
        **kwargs,
    )


def test_format_seconds():
    assert format_seconds(0) == "0.000"
    assert format_seconds(1210470) == "1210.470"
    assert format_seconds(300000) == "300.000"


def test_commit_result_matches_score_py():
    score = load_score_module()
    tests = make_tests([0.25, 0.5, 0.75])
    result = commit_result(score, "c1", [0, 1], [[0, 2], [1]], tests)
    assert result["ok"] and not result["missed"]
    # loads: worker0 = 250+750 = 1000, worker1 = 500 -> 1000 + 2*2000
    assert result["simulated_ms"] == 5000

    missed = commit_result(score, "c1", [2], [[0, 1]], tests)
    assert missed["ok"] and missed["missed"]


def test_commit_result_detects_invalid_worker_output():
    score = load_score_module()
    tests = make_tests([0.1, 0.2])
    duplicate = commit_result(score, "c1", [], [[0, 0]], tests)
    assert not duplicate["ok"]
    assert duplicate["code"] == "repeated_test"
    unknown = commit_result(score, "c1", [], [[99]], tests)
    assert not unknown["ok"] and unknown["code"] == "unknown_test"


def test_totals_adds_miss_penalty():
    rows = [row("a", cost=1000), row("b", covered=False, cost=2000), row("c", cost=3000)]
    summary = totals(rows)
    assert summary["simulated_ms"] == 6000 + 300_000
    assert summary["misses"] == 1
    assert summary["missed_commits"] == ["b"]
    assert summary["selected_total"] == 6


def test_totals_empty():
    summary = totals([])
    assert summary == {
        "simulated_ms": 0,
        "misses": 0,
        "missed_commits": [],
        "commits": 0,
        "selected_total": 0,
        "tier_e_commits": 0,
    }


def test_rule_summary_orders_by_size():
    text = rule_summary({"usage": {"a"}, "symbol": {"a", "b", "c"}, "fixture": {"b", "c"}})
    assert text.split() == ["symbol:3", "fixture:2", "usage:1"]


def test_rule_summary_trucates():
    rules = {f"r{index}": {str(index)} for index in range(5)}
    assert rule_summary(rules, limit=2).endswith("+3")


def test_render_table_contains_rows_and_totals():
    rows = [
        row("a", selected=3, expected=2, cost=2500, tier_e=True, rules="symbol:3"),
        row("b", covered=False, cost=300000),
    ]
    text = render_table(
        "A", engine_name="reference", knobs_text="symbol coverage", rows=rows, missing_ids=2
    )
    assert "== variant A" in text
    assert "symbol coverage" in text
    assert "engine=reference" in text
    assert "a " in text and "tier-e symbol:3" in text
    assert "TOTAL simulated" in text
    assert "MISSES 1" in text
    assert "missed: b" in text
    assert "2 selected test ids were not in tests.json" in text


def test_render_table_skip_and_reject():
    skipped = render_table("B", rows=[], skipped="backend cannot run this knob")
    assert "SKIPPED: backend cannot run this knob" in skipped
    rejected = render_table("C", rows=[row(rejected="repeated_test")])
    assert "REJ" in rejected


def test_provenance_table_counts_rules():
    rows = [
        Row(
            commit="a",
            kind="code",
            selected=3,
            expected=2,
            covered=True,
            cost_ms=1000,
            failing_ids={"t0", "t2"},
            selected_ids={"t0", "t2", "t3"},
            rule_map={"symbol": {"t0", "t2"}, "usage": {"t3"}},
        ),
        Row(
            commit="b",
            kind="code",
            selected=1,
            expected=1,
            covered=True,
            cost_ms=1000,
            failing_ids={"t3"},
            selected_ids={"t3"},
            rule_map={"usage": {"t3"}},
        ),
    ]
    text = provenance_table(rows)
    assert "symbol" in text and "caught-failing=2" in text
    usage_line = next(line for line in text.splitlines() if "usage" in line)
    assert "selected=2" in usage_line
    assert "caught-failing=1" in usage_line
    assert "commits=1" in usage_line
    assert "UNATTRIBUTED" not in text


def test_provenance_reports_unattributed_failing_tests():
    rows = [
        Row(
            commit="z",
            kind="code",
            selected=1,
            expected=2,
            covered=False,
            cost_ms=300000,
            failing_ids={"t0", "t9"},
            selected_ids={"t0"},
            rule_map={"symbol": {"t0"}},
        )
    ]
    text = provenance_table(rows)
    assert "UNATTRIBUTED z" in text
    assert "t9" in text


def test_rows_serialize():
    data = row().as_dict()
    assert json.loads(json.dumps(data))["commit"] == "c1"
    assert set(data) == {"commit", "kind", "selected", "expected", "covered", "cost_ms", "tier_e", "rules", "rejected"}
