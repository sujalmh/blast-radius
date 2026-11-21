"""Selector end-to-end: synthetic /data, JSON contract, fallbacks, CPU budget."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import selector
from ourtests import mockmaps


def _run(data: Path, maps: Path):
    out = io.StringIO()
    err = io.StringIO()
    started = time.perf_counter()
    selector.run(data, maps, out, err)
    elapsed = time.perf_counter() - started
    return out.getvalue(), err.getvalue(), elapsed


def test_end_to_end_selection(tmp_path):
    data, maps = mockmaps.build_data(tmp_path)
    stdout, stderr, _ = _run(data, maps)
    lines = stdout.splitlines()
    assert len(lines) == len(mockmaps.PATCHES)

    documents = [json.loads(line) for line in lines]
    assert {document["commit"] for document in documents} == set(mockmaps.EXPECTATIONS)

    for document in documents:
        workers = document["workers"]
        assert isinstance(workers, list) and len(workers) <= 8
        assert all(isinstance(worker, list) and worker for worker in workers)
        numbers = [number for worker in workers for number in worker]
        assert len(numbers) == len(set(numbers))
        assert set(numbers) == mockmaps.EXPECTATIONS[document["commit"]]

    # stdout is answer lines only; every log line is on stderr.
    assert all(line.startswith('{"commit":') for line in lines)
    assert "tier=neutral" in stderr
    assert "tier=tier_e" in stderr
    assert "malformed" in stderr


def test_comment_commit_runs_nothing(tmp_path):
    data, maps = mockmaps.build_data(tmp_path)
    stdout, _, _ = _run(data, maps)
    document = next(
        json.loads(line) for line in stdout.splitlines() if '"comment"' in line
    )
    assert document["workers"] == []


def test_missing_maps_falls_back_to_full_suite(tmp_path):
    data, _ = mockmaps.build_data(tmp_path)
    empty_maps = tmp_path / "no-maps"
    empty_maps.mkdir()
    stdout, stderr, _ = _run(data, empty_maps)
    lines = stdout.splitlines()
    assert len(lines) == len(mockmaps.PATCHES)
    all_numbers = set(range(len(mockmaps.TESTS)))
    for line in lines:
        workers = json.loads(line)["workers"]
        numbers = [number for worker in workers for number in worker]
        assert len(numbers) == len(set(numbers))
        assert set(numbers) == all_numbers
    assert "global_fallback" in stderr


def test_fifty_commits_stay_cheap(tmp_path):
    data, maps = mockmaps.build_data(tmp_path)
    for existing in (data / "commits").glob("*.patch"):
        existing.unlink()
    names = list(mockmaps.PATCHES)
    for index in range(50):
        source = mockmaps.PATCHES[names[index % len(names)]]
        (data / "commits" / f"dry{index:03d}.patch").write_text(source, encoding="utf-8")

    stdout, stderr, elapsed = _run(data, maps)
    lines = stdout.splitlines()
    assert len(lines) == 50
    for line in lines:
        assert set(json.loads(line)) == {"commit", "workers"}
    # Very loose bound: cold map load plus 50 commits is milliseconds.
    assert elapsed < 5.0, f"50-commit dry loop took {elapsed:.3f}s"
