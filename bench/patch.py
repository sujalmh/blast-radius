"""Unified-diff parsing and in-memory application.

The calibration harness needs three things from a patch that the selector also
needs, so this module is deliberately self-contained and stdlib-only:

* per-file hunks with their **old** spans (the spec's selection anchor),
* the set of changed lines (for comment-neutrality detection),
* an exact reconstruction of the patched file (for the AST-equality check).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = "\\ No newline at end of file"


@dataclass(frozen=True)
class Hunk:
    """One `@@` section of a unified diff (counts default to 1 when omitted)."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]

    def old_span(self) -> tuple[int, int] | None:
        """1-based inclusive old-line span, or None for a pure insertion hunk."""
        if self.old_count <= 0:
            return None
        return (self.old_start, self.old_start + self.old_count - 1)

    def padded_span(self, pad: int) -> tuple[int, int] | None:
        span = self.old_span()
        if span is None or pad <= 0:
            return span
        return (max(1, span[0] - pad), span[1] + pad)

    def anchor_line(self) -> int:
        """Line the change is glued to when there is no old-line span."""
        return max(1, self.old_start)

    def changed_lines(self) -> list[tuple[str, str]]:
        """(tag, text) for added/removed lines, tag in {'+', '-'}."""
        return [
            (line[:1], line[1:])
            for line in self.lines
            if line and line[0] in "+-"
        ]


@dataclass
class FileChange:
    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")

    def old_spans(self, pad: int = 0) -> list[tuple[int | None, int | None]]:
        """(start, end) per hunk; (None, None) for pure insertions."""
        return [h.padded_span(pad) for h in self.hunks]

    def all_changed_lines(self) -> list[tuple[str, str]]:
        return [pair for hunk in self.hunks for pair in hunk.changed_lines()]


def _clean_path(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    # `diff --git a/x b/x` style prefixes; `--- a/x` may carry a tab suffix.
    raw = raw.split("\t", 1)[0]
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    return raw


def parse_patch(text: str) -> list[FileChange]:
    """Parse a git unified diff. Never raises; malformed input yields what it can."""
    changes: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    hunk_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal hunk, hunk_lines
        if current is not None and hunk is not None:
            current.hunks.append(
                Hunk(
                    old_start=hunk.old_start,
                    old_count=hunk.old_count,
                    new_start=hunk.new_start,
                    new_count=hunk.new_count,
                    lines=tuple(hunk_lines),
                )
            )
        hunk = None
        hunk_lines = []

    for raw_line in text.splitlines():
        line = raw_line

        if line.startswith("diff --git "):
            flush_hunk()
            if current is not None:
                changes.append(current)
            parts = line[len("diff --git ") :].split(" ")
            paths = [p for p in (_clean_path(p) for p in parts) if p]
            current = FileChange(path=paths[-1] if paths else "", old_path=paths[0] if len(paths) > 1 else None)
            continue

        if current is None:
            # Some patch files arrive without `diff --git`; start on `---`.
            if line.startswith("--- "):
                current = FileChange(path="", old_path=_clean_path(line[4:]))
            else:
                continue

        if line.startswith("new file mode"):
            current.is_new = True
        elif line.startswith("deleted file mode"):
            current.is_deleted = True
        elif line.startswith("rename from "):
            current.old_path = _clean_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            current.path = _clean_path(line[len("rename to ") :]) or current.path
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.is_binary = True
        elif line.startswith("--- "):
            flush_hunk()
            old = _clean_path(line[4:])
            if old is None:
                current.is_new = True
            else:
                current.old_path = old
                if not current.path:
                    current.path = old
        elif line.startswith("+++ "):
            flush_hunk()
            new = _clean_path(line[4:])
            if new is None:
                current.is_deleted = True
            else:
                current.path = new
        elif line.startswith("@@"):
            flush_hunk()
            match = _HUNK_RE.match(line)
            if not match:
                hunk = None
                continue
            old_start, old_count, new_start, new_count = match.groups()
            hunk = Hunk(
                old_start=int(old_start),
                old_count=1 if old_count is None else int(old_count),
                new_start=int(new_start),
                new_count=1 if new_count is None else int(new_count),
                lines=(),
            )
        elif hunk is not None:
            if line == _NO_NEWLINE or line.startswith(_NO_NEWLINE):
                continue
            if line == "":
                # A context line whose single leading space was stripped by an editor.
                hunk_lines.append(" ")
            elif line[0] in " +-":
                hunk_lines.append(line)
            else:
                hunk_lines.append(" " + line)

    flush_hunk()
    if current is not None:
        changes.append(current)
    return [change for change in changes if change.path]


def apply_hunks(source: str, hunks: list[Hunk]) -> tuple[str, bool, str]:
    """Return (patched_text, ok, reason). On mismatch returns the source unchanged."""
    lines = source.splitlines()
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
        if start < cursor:
            return source, False, f"overlapping hunk at old line {hunk.old_start}"
        if start > len(lines):
            return source, False, f"hunk starts beyond EOF at old line {hunk.old_start}"
        out.extend(lines[cursor:start])
        cursor = start
        for line in hunk.lines:
            if not line:
                continue
            tag, text = line[0], line[1:]
            if tag in " -":
                if cursor >= len(lines) or lines[cursor].rstrip() != text.rstrip():
                    return source, False, f"context mismatch at old line {cursor + 1}"
                if tag == " ":
                    out.append(lines[cursor])
                cursor += 1
            elif tag == "+":
                out.append(text)
            # anything else (e.g. "\ No newline") is ignored
    out.extend(lines[cursor:])
    patched = "\n".join(out)
    if source.endswith("\n"):
        patched += "\n"
    return patched, True, "ok"


def apply_patch(source_by_path: dict[str, str], changes: list[FileChange]) -> tuple[dict[str, str], list[str]]:
    """Apply every change whose source is known; returns (patched, problems)."""
    patched: dict[str, str] = {}
    problems: list[str] = []
    for change in changes:
        source = source_by_path.get(change.path)
        if source is None:
            problems.append(f"no pinned source for {change.path}")
            continue
        text, ok, reason = apply_hunks(source, change.hunks)
        if not ok:
            problems.append(f"{change.path}: {reason}")
            continue
        patched[change.path] = text
    return patched, problems
