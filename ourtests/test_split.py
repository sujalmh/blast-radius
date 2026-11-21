"""Worker split (LPT + 2 s per worker) tests."""

from __future__ import annotations

from blastradius.split import WORKER_COST_MILLISECONDS, choose_split, pack


def cost(workers, durations):
    if not workers:
        return 0
    return max(sum(durations[number] for number in worker) for worker in workers) + (
        WORKER_COST_MILLISECONDS * len(workers)
    )


def test_empty_selection():
    assert choose_split([]) == []


def test_single_test():
    assert choose_split([(7, 1234)]) == [[7]]


def test_every_test_exactly_once():
    items = [(number, 100 + 37 * number) for number in range(30)]
    workers = choose_split(items)
    flat = [number for worker in workers for number in worker]
    assert sorted(flat) == list(range(30))
    assert len(flat) == len(set(flat))


def test_tie_prefers_smaller_worker_count():
    # k=1: 4000 + 2000 = 6000.  k=2: 2000 + 4000 = 6000 -> tie, k=1 wins.
    workers = choose_split([(number, 1000) for number in range(4)])
    assert workers == [[0, 1, 2, 3]]


def test_eight_equal_tests_prefer_two_workers():
    # k=2: 4000 + 4000 = 8000; k=3: 3000 + 6000 = 9000; k=4: 2000 + 8000.
    workers = choose_split([(number, 1000) for number in range(8)])
    assert len(workers) == 2
    assert sorted(len(worker) for worker in workers) == [4, 4]


def test_mixed_durations_choose_two_workers():
    durations = {0: 5000, 1: 3000, 2: 2000, 3: 1000}
    workers = choose_split(list(durations.items()))
    assert cost(workers, durations) == 10000
    assert sorted(workers) == [[0, 3], [1, 2]]


def test_never_more_than_eight_workers():
    workers = choose_split([(number, 1) for number in range(1000)])
    assert 1 <= len(workers) <= 8
    flat = [number for worker in workers for number in worker]
    assert len(flat) == 1000


def test_choose_matches_minimum_over_k():
    items = [(0, 4200), (1, 1900), (2, 800), (3, 700), (4, 300), (5, 50)]
    durations = dict(items)
    workers = choose_split(items)
    chosen = cost(workers, durations)
    best = min(
        cost(pack(items, k)[0], durations) for k in range(1, min(8, len(items)) + 1)
    )
    assert chosen == best


def test_pack_returns_max_load():
    workers, max_load = pack([(0, 400), (1, 100), (2, 100)], 2)
    assert sorted(workers) == [[0], [1, 2]]
    assert max_load == 400
