"""Unified-diff parsing for the blast-radius selector.

Only the old side matters for selection, so the parser records repo-relative
old/new paths plus every hunk's old span.  Hunk bodies are consumed strictly by
the counts in the hunk header: that is what makes a malformed patch (a hunk
whose body does not match its header) fail loudly instead of silently shifting
line numbers.

Anything we cannot parse confidently raises :class:`PatchError`; the caller
turns that into the Tier E fallback (full suite).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence


class PatchError(ValueError):
    """Raised when a patch cannot be parsed confidently."""


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))?"
    r" \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)

_GIT_PREFIX = "diff --git "


@dataclass(frozen=True)
class Hunk:
    """One unified-diff hunk (1-based line numbers, inclusive spans)."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: tuple[str, ...]

    @property
    def added(self) -> tuple[str, ...]:
        """Added lines with the leading ``+`` stripped (no ``\\ No newline``)."""
        return tuple(line[1:] for line in self.body if line.startswith("+"))

    @property
    def removed(self) -> tuple[str, ...]:
        """Removed lines with the leading ``-`` stripped."""
        return tuple(line[1:] for line in self.body if line.startswith("-"))

    def old_lines(self, file_len: int | None = None) -> Sequence[int]:
        """Old-side lines this hunk touches.

        ``old_count > 0`` is just the old span.  ``old_count == 0`` is a pure
        insertion: the spec says to treat the insertion point as covering the
        two neighbouring old lines when they exist.
        """
        if self.old_count > 0:
            return range(self.old_start, self.old_start + self.old_count)
        if self.old_start <= 0:
            anchors = [1]
        else:
            anchors = [self.old_start, self.old_start + 1]
        if file_len is not None:
            anchors = [line for line in anchors if 1 <= line <= file_len]
        else:
            anchors = [line for line in anchors if line >= 1]
        return anchors


@dataclass
class FilePatch:
    """All hunks for one file inside a patch."""

    old_path: str | None = None
    new_path: str | None = None
    hunks: list[Hunk] | None = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    is_rename: bool = False

    def __post_init__(self) -> None:
        if self.hunks is None:
            self.hunks = []

    @property
    def path(self) -> str | None:
        """Repo-relative path to reason about (old side when it exists)."""
        return self.old_path if self.old_path is not None else self.new_path


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # CRLF patches: strip one trailing CR so headers and bodies compare cleanly.
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _marker_path(rest: str) -> str | None:
    """``a/sqlglot/x.py`` -> ``sqlglot/x.py``; ``/dev/null`` -> ``None``."""
    path = rest.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _git_paths(rest: str) -> tuple[str | None, str | None]:
    """Best-effort old/new paths from a ``diff --git a/x b/x`` line."""
    if rest.startswith("a/"):
        separator = rest.find(" b/")
        if separator > 0:
            return rest[2:separator], rest[separator + 3 :]
    return None, None


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    match = _HUNK_HEADER.match(line)
    if match is None:
        raise PatchError(f"malformed hunk header: {line[:120]!r}")
    old_start = int(match.group("old_start"))
    new_start = int(match.group("new_start"))
    old_count = int(match.group("old_count")) if match.group("old_count") is not None else 1
    new_count = int(match.group("new_count")) if match.group("new_count") is not None else 1
    return old_start, old_count, new_start, new_count


def parse_patch(text: str) -> list[FilePatch]:
    """Parse unified diff text into :class:`FilePatch` objects."""
    lines = _split_lines(text)
    files: list[FilePatch] = []

    current: FilePatch | None = None
    git_style = False
    old_marker = False
    new_marker = False
    index = 0
    total = len(lines)

    def finish() -> None:
        nonlocal current
        if current is not None:
            if current.old_path is None and current.new_path is None:
                if current.hunks or current.is_binary or current.is_rename:
                    raise PatchError("file block without any path")
            else:
                files.append(current)
            current = None

    while index < total:
        line = lines[index]

        if line.startswith(_GIT_PREFIX):
            finish()
            old_path, new_path = _git_paths(line[len(_GIT_PREFIX) :])
            current = FilePatch(old_path=old_path, new_path=new_path)
            git_style = True
            old_marker = False
            new_marker = False
            index += 1
            continue

        if line.startswith("--- "):
            if current is None or old_marker or new_marker or current.hunks:
                if git_style and current is not None and current.hunks:
                    raise PatchError("unexpected '--- ' line after a hunk in a git patch")
                finish()
                current = FilePatch()
                git_style = False
                old_marker = False
                new_marker = False
            current.old_path = _marker_path(line[4:])
            if current.old_path is None:
                current.is_new = True
            old_marker = True
            index += 1
            continue

        if line.startswith("+++ "):
            if current is None:
                raise PatchError("'+++ ' line without a file header")
            current.new_path = _marker_path(line[4:])
            if current.new_path is None:
                current.is_deleted = True
            new_marker = True
            index += 1
            continue

        if line.startswith("@@"):
            if current is None:
                raise PatchError("hunk without a file header")
            old_start, old_count, new_start, new_count = _parse_hunk_header(line)
            body: list[str] = []
            old_seen = 0
            new_seen = 0
            index += 1
            while index < total and (old_seen < old_count or new_seen < new_count):
                body_line = lines[index]
                if body_line.startswith("\\"):
                    body.append(body_line)
                    index += 1
                    continue
                if body_line.startswith("+"):
                    new_seen += 1
                elif body_line.startswith("-"):
                    old_seen += 1
                elif body_line.startswith(" ") or body_line == "":
                    old_seen += 1
                    new_seen += 1
                else:
                    raise PatchError(
                        f"truncated hunk at line {index + 1}: {body_line[:80]!r}"
                    )
                if old_seen > old_count or new_seen > new_count:
                    raise PatchError(
                        f"hunk counts exceed header at line {index + 1} "
                        f"(-{old_start},{old_count} +{new_start},{new_count})"
                    )
                body.append(body_line)
                index += 1
            if old_seen != old_count or new_seen != new_count:
                raise PatchError(
                    f"hunk body does not match header "
                    f"(-{old_start},{old_count} +{new_start},{new_count})"
                )
            # "\ No newline at end of file" markers do not count toward either side.
            while index < total and lines[index].startswith("\\"):
                body.append(lines[index])
                index += 1
            current.hunks.append(
                Hunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    body=tuple(body),
                )
            )
            continue

        if line.startswith("new file mode"):
            if current is not None:
                current.is_new = True
            index += 1
            continue
        if line.startswith("deleted file mode"):
            if current is not None:
                current.is_deleted = True
            index += 1
            continue
        if line.startswith("rename from") or line.startswith("rename to"):
            if current is not None:
                current.is_rename = True
            index += 1
            continue
        if line.startswith("copy from") or line.startswith("copy to"):
            if current is not None:
                current.is_rename = True
            index += 1
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if current is not None:
                current.is_binary = True
            index += 1
            continue
        if line.startswith("index ") or line.startswith("old mode") or line.startswith("new mode"):
            index += 1
            continue
        if line.startswith("similarity index") or line.startswith("dissimilarity index"):
            index += 1
            continue

        # Anything else at file-block level that looks like diff body means a
        # hunk header lied about its counts.
        if current is not None and (
            line.startswith(" ") or line.startswith("+") or line.startswith("\\")
        ):
            raise PatchError(f"unexpected hunk body line outside a hunk: {line[:80]!r}")
        index += 1

    finish()
    return files


def touched_old_lines(files: Iterable[FilePatch], file_len: int | None = None) -> list[int]:
    """Convenience helper: sorted old-side lines for one parsed file list."""
    out: set[int] = set()
    for file_patch in files:
        for hunk in file_patch.hunks:
            out.update(hunk.old_lines(file_len))
    return sorted(out)
