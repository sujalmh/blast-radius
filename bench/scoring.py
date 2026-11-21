"""Bridge to the official scorer (`score.py`) plus table/provenance assembly.

The bench must never re-implement scoring: it imports `score.py` from the repo
root and calls `evaluate_run` per commit, so a calibration number is exactly the
number `make sample` would print.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MISS_PENALTY_MILLISECONDS = 300_000
WORKER_COST_MILLISECONDS = 2_000


def load_score_module(path: str | Path | None = None):
    score_path = Path(path) if path is not None else REPO_ROOT / "score.py"
    spec = importlib.util.spec_from_file_location("blast_radius_official_score", score_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import score.py from {score_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def format_seconds(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def commit_result(
    score,
    commit: str,
    failing: list[int],
    workers: list[list[int]],
    tests: list[dict],
) -> dict:
    """Score one commit with the official evaluator."""
    line = json.dumps({"commit": commit, "workers": workers}, separators=(",", ":"))
    evaluation = score.evaluate_run((line + "\n").encode("utf-8"), tests, {commit: {"failing": list(failing)}})
    if not evaluation.get("ok"):
        return {
            "ok": False,
            "code": evaluation.get("code"),
            "line": evaluation.get("line"),
            "simulated_ms": 0,
            "missed": False,
        }
    return {
        "ok": True,
        "code": None,
        "line": None,
        "simulated_ms": int(evaluation["simulated_milliseconds"]),
        "missed": bool(evaluation["missed_commits"]),
    }


@dataclass
class Row:
    commit: str
    kind: str
    selected: int
    expected: int
    covered: bool
    cost_ms: int
    tier_e: bool = False
    rules: str = ""
    rejected: str | None = None
    selected_ids: set[str] = field(default_factory=set)
    failing_ids: set[str] = field(default_factory=set)
    rule_map: dict[str, set[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "commit": self.commit,
            "kind": self.kind,
            "selected": self.selected,
            "expected": self.expected,
            "covered": self.covered,
            "cost_ms": self.cost_ms,
            "tier_e": self.tier_e,
            "rules": self.rules,
            "rejected": self.rejected,
        }


def totals(rows: list[Row]) -> dict:
    simulated = sum(row.cost_ms for row in rows)
    missed = [row.commit for row in rows if not row.covered]
    simulated += MISS_PENALTY_MILLISECONDS * len(missed)
    return {
        "simulated_ms": simulated,
        "misses": len(missed),
        "missed_commits": sorted(missed),
        "commits": len(rows),
        "selected_total": sum(row.selected for row in rows),
        "tier_e_commits": sum(1 for row in rows if row.tier_e),
    }


def rule_summary(rule_map: dict[str, set[str]], limit: int = 3) -> str:
    items = sorted(rule_map.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    if not items:
        return "-"
    parts = [f"{rule}:{len(tests)}" for rule, tests in items[:limit]]
    if len(items) > limit:
        parts.append(f"+{len(items) - limit}")
    return " ".join(parts)


def render_table(
    variant: str,
    *,
    engine_name: str = "",
    knobs_text: str = "",
    rows: list[Row],
    skipped: str | None = None,
    missing_ids: int = 0,
) -> str:
    header = f"== variant {variant}"
    if knobs_text:
        header += f"  [{knobs_text}]"
    if engine_name:
        header += f"  engine={engine_name}"
    lines = [header]
    if skipped:
        lines.append(f"   SKIPPED: {skipped}")
        return "\n".join(lines)

    widths = {"commit": 18, "kind": 9, "sel": 5, "fail": 5, "all": 4, "cost": 9}
    lines.append(
        f"{'commit':<{widths['commit']}} {'kind':<{widths['kind']}} "
        f"{'sel':>{widths['sel']}} {'fail':>{widths['fail']}} {'all':>{widths['all']}} "
        f"{'cost_s':>{widths['cost']}}  rules"
    )
    for row in rows:
        covered = "yes" if row.covered else "NO"
        if row.rejected:
            covered = "REJ"
        cost = format_seconds(row.cost_ms)
        lines.append(
            f"{row.commit:<{widths['commit']}} {row.kind:<{widths['kind']}} "
            f"{row.selected:>{widths['sel']}} {row.expected:>{widths['fail']}} "
            f"{covered:>{widths['all']}} {cost:>{widths['cost']}}  "
            f"{'tier-e ' if row.tier_e else ''}{row.rules}"
        )
    summary = totals(rows)
    lines.append(
        f"   TOTAL simulated {format_seconds(summary['simulated_ms'])} s "
        f"({summary['commits']} commits, {summary['selected_total']} test slots)"
    )
    lines.append(f"   MISSES {summary['misses']} (+{format_seconds(MISS_PENALTY_MILLISECONDS * summary['misses'])} s)")
    if summary["missed_commits"]:
        lines.append("   missed: " + ", ".join(summary["missed_commits"]))
    if missing_ids:
        lines.append(f"   note: {missing_ids} selected test ids were not in tests.json and were dropped")
    return "\n".join(lines)


def provenance_table(rows: list[Row]) -> str:
    """Which rule contributed the tests that caught each commit's failures."""
    stats: dict[str, dict] = {}
    for row in rows:
        for rule, tests in row.rule_map.items():
            entry = stats.setdefault(rule, {"selected": 0, "caught": 0, "commits": 0})
            entry["selected"] += len(tests)
            caught = tests & row.failing_ids
            entry["caught"] += len(caught)
            if caught:
                entry["commits"] += 1
    unattributed = [
        (row.commit, sorted(row.failing_ids - row.selected_ids))
        for row in rows
        if row.failing_ids - row.selected_ids
    ]
    lines = ["PROVENANCE (rule -> tests it contributed, failing tests it caught)"]
    if not stats:
        lines.append("   (no rules contributed tests)")
    for rule, entry in sorted(stats.items(), key=lambda item: (-item[1]["caught"], -item[1]["selected"], item[0])):
        lines.append(
            f"   {rule:<10} selected={entry['selected']:<6} "
            f"caught-failing={entry['caught']:<4} commits={entry['commits']}"
        )
    for commit, missing in unattributed:
        preview = missing[:6]
        suffix = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
        lines.append(f"   UNATTRIBUTED {commit}: failing tests selected by no rule: {preview}{suffix}")
    return "\n".join(lines)
