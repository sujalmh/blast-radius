"""Stable A/B hook for the calibration harness (W3).

The harness calls this through the shipped code so knob variants measure exactly
what the submission image runs::

    from blastradius.bench_adapter import bench_adapter
    adapter = bench_adapter(maps_dir)                 # optional durations_ms=...
    result = adapter.select(patch_text, knobs={"dialect": "off"})
    result["tests"]   # -> set of test ids
    result["rules"]   # -> {rule name: [test ids]}
    result["tier_e"]  # -> bool (full suite)
    result["workers"] # -> [[numbers]] when durations were supplied, else None

Knob values are the four documented axes (``symbol``, ``classbody``,
``dialect``, ``keyreads``); unknown keys are ignored and reported in ``notes``
so a harness bug never becomes a crash.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import patch as patch_parsing
from . import select as selection
from . import split as splitting
from .maps_io import Maps

KNOB_NAMES = frozenset(item.name for item in fields(selection.Knobs))


def _coerce_knobs(knobs: Mapping[str, Any] | selection.Knobs | None) -> tuple[selection.Knobs, list[str]]:
    if knobs is None:
        return selection.DEFAULT_KNOBS, []
    if isinstance(knobs, selection.Knobs):
        return knobs.normalized(), []
    if not isinstance(knobs, Mapping):
        return selection.DEFAULT_KNOBS, [f"knobs of type {type(knobs).__name__} ignored"]
    accepted: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in knobs.items():
        if key in KNOB_NAMES:
            accepted[key] = value
        else:
            notes.append(f"unknown knob {key!r} ignored")
    return selection.Knobs(**accepted).normalized(), notes


class BenchAdapter:
    """Runs the shipped selection over an arbitrary patch text."""

    def __init__(
        self,
        maps_dir: str | Path,
        durations_ms: Mapping[int, int] | None = None,
        max_workers: int = splitting.MAX_WORKERS,
    ) -> None:
        self.maps = Maps.load(maps_dir)
        ids = list(self.maps.test_ids())
        self.ids = ids
        self.ordinal_by_id = {test_id: ordinal for ordinal, test_id in enumerate(ids)}
        self.index = selection.TestIndex(
            ids, {test_id: ordinal for ordinal, test_id in enumerate(ids)}
        )
        self.durations_ms = dict(durations_ms) if durations_ms else None
        self.max_workers = max_workers

    def close(self) -> None:
        self.maps.close()

    # -- selection -------------------------------------------------------

    def _workers(self, test_ids: Iterable[str]) -> list[list[int]] | None:
        if not self.durations_ms:
            return None
        items = [
            (number, self.durations_ms[number])
            for number in (self.ordinal_by_id[test_id] for test_id in test_ids)
            if number in self.durations_ms
        ]
        return splitting.choose_split(items)

    def _result(
        self,
        chosen: selection.Selection,
        notes: list[str],
    ) -> dict[str, Any]:
        rules = {
            rule: sorted(ids & self.index.all_ids)
            for rule, ids in chosen.by_rule.items()
        }
        tests = set(chosen.tests) & self.index.all_ids
        return {
            "tests": tests,
            "tier_e": chosen.tier == "tier_e",
            "rules": rules,
            "notes": notes + [str(hit) for hit in chosen.rules],
            "workers": self._workers(tests),
            "tier": chosen.tier,
        }

    def select(
        self,
        patch_text: str,
        knobs: Mapping[str, Any] | selection.Knobs | None = None,
    ) -> dict[str, Any]:
        knobs_obj, notes = _coerce_knobs(knobs)
        try:
            files = patch_parsing.parse_patch(patch_text)
            chosen = selection.select_commit(files, self.maps, self.index, knobs_obj)
        except Exception as error:  # noqa: BLE001 - a harness must never crash
            notes.append(f"{type(error).__name__}: {error}")
            full = selection.Selection(
                set(self.index.all_ids),
                [selection.RuleHit("tier_e", detail="selection failed")],
                "tier_e",
                {"tier_e": set(self.index.all_ids)},
            )
            return self._result(full, notes)
        return self._result(chosen, notes)


def bench_adapter(
    maps_dir: str | Path,
    durations_ms: Mapping[int, int] | None = None,
    max_workers: int = splitting.MAX_WORKERS,
) -> BenchAdapter:
    """Factory used by the W3 calibration harness."""
    return BenchAdapter(maps_dir, durations_ms=durations_ms, max_workers=max_workers)
