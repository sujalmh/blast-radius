"""Worker planning: must agree with score.py's arithmetic exactly."""

import json
import random

from bench.engine import duration_milliseconds, split_tests
from bench.scoring import load_score_module


def make_tests(seconds):
    return [
        {"number": index, "id": f"tests/t{index}.py::test_{index}", "file": f"tests/t{index}.py", "seconds": value}
        for index, value in enumerate(seconds)
    ]


def test_duration_rounding_matches_score_py():
    score = load_score_module()
    assert duration_milliseconds(0.0005) == score._duration_milliseconds(0.0005)
    assert duration_milliseconds(0.0014) == score._duration_milliseconds(0.0014)
    assert duration_milliseconds(0.031) == 31
    assert duration_milliseconds(0) == 0


def test_empty_selection():
    assert split_tests(make_tests([0.1, 0.2]), set()) == ([], 0)


def test_single_test_cost():
    tests = make_tests([0.5])
    workers, cost = split_tests(tests, {tests[0]["id"]})
    assert workers == [[0]]
    assert cost == 500 + 2000


def test_tie_prefers_fewer_workers():
    # two 2s tests: one worker = 4000+2000, two workers = 2000+4000 -> tie -> k=1
    tests = make_tests([2.0, 2.0])
    workers, cost = split_tests(tests, {test["id"] for test in tests})
    assert len(workers) == 1
    assert cost == 6000


def test_split_never_exceeds_max_workers():
    tests = make_tests([1.0] * 20)
    workers, _ = split_tests(tests, {test["id"] for test in tests}, max_workers=8)
    assert 1 <= len(workers) <= 8
    assert sum(len(worker) for worker in workers) == 20


def test_cost_matches_official_scorer_for_random_selections():
    score = load_score_module()
    random.seed(1234)
    for _ in range(40):
        tests = make_tests([round(random.uniform(0.001, 0.6), 3) for _ in range(random.randint(1, 25))])
        selected = {test["id"] for test in tests if random.random() < 0.6}
        if not selected:
            selected = {tests[0]["id"]}
        workers, split_cost = split_tests(tests, selected)
        line = json.dumps({"commit": "c", "workers": workers}) + "\n"
        evaluation = score.evaluate_run(line.encode(), tests, {"c": {"failing": []}})
        assert evaluation["ok"]
        assert evaluation["simulated_milliseconds"] == split_cost


def test_split_drops_unknown_ids():
    tests = make_tests([0.1, 0.2])
    workers, _ = split_tests(tests, {tests[0]["id"], "tests/ghost.py::test_ghost"})
    assert sum(len(worker) for worker in workers) == 1
