"""Worker splitting: LPT packing plus the 2 s-per-worker trade-off.

Cost model is exactly SCORING.md's: a worker's load is the sum of its tests'
durations in milliseconds; a commit costs the largest load plus 2000 ms for
each worker.  Durations must already be rounded with
``floor(seconds * 1000 + 0.5)`` by the caller.
"""

from __future__ import annotations

from typing import Iterable, Sequence

WORKER_COST_MILLISECONDS = 2000
MAX_WORKERS = 8

Item = tuple[int, int]  # (test number, duration milliseconds)


def _pack_ordered(ordered: Sequence[Item], worker_count: int) -> tuple[list[list[int]], int]:
    """LPT over already longest-first-ordered items."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for number, milliseconds in ordered:
        lightest = 0
        for candidate in range(1, worker_count):
            if loads[candidate] < loads[lightest]:
                lightest = candidate
        workers[lightest].append(number)
        loads[lightest] += milliseconds
    return workers, (max(loads) if loads else 0)


def pack(items: Sequence[Item], worker_count: int) -> tuple[list[list[int]], int]:
    """Longest-first, least-loaded (LPT).  Returns workers and the max load."""
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    ordered = sorted(items, key=lambda item: (-item[1], item[0]))
    return _pack_ordered(ordered, worker_count)


def choose_split(items: Iterable[Item]) -> list[list[int]]:
    """Pick the worker count minimizing ``max_load + 2000 * k`` (ties: smaller k)."""
    materialized = list(items)
    if not materialized:
        return []

    # Sorting once is enough; every k packs the same order.
    ordered = sorted(materialized, key=lambda item: (-item[1], item[0]))
    best_workers: list[list[int]] = []
    best_cost: int | None = None

    for worker_count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers, max_load = _pack_ordered(ordered, worker_count)
        cost = max_load + WORKER_COST_MILLISECONDS * worker_count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = workers

    return [worker for worker in best_workers if worker]
