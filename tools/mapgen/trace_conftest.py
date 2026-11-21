"""Blast Radius map-generation pytest plugin: per-test file reads + per-test coverage.

This module is copied into the sqlglot checkout root at *runtime* by
``run_maps.sh`` (it is never baked into the pinned tree) and loaded with
``pytest -p trace_conftest``.  It does two things:

1. Wraps ``builtins.open``, ``io.open``, ``codecs.open``, ``os.open`` and
   ``pathlib.Path.{open,read_text,read_bytes}`` to record which files inside
   the checkout each test reads.  Reads that happen while a test is running are
   attributed to that test's node id.  Reads with no current test (module
   import, session fixtures) are attributed to the module bucket
   ``"module:<tests/... .py>"`` of the file being collected/imported, or to
   ``"__unknown__"`` when no owning file can be determined.

2. Drives coverage.py manually so every test gets its own context: coverage
   starts after collection (import time is deliberately *not* attributed to a
   test) and ``switch_context(nodeid)`` is called around every test protocol
   (setup + call + teardown).  ``gen_maps.py`` parses the resulting
   ``.coverage`` sqlite file.  No pytest-cov dependency is needed.

Environment:
  BR_CHECKOUT      checkout root (default: cwd)
  BR_TRACE_OUT     JSON output path (default: /work/trace_reads.json)
  BR_COVERAGE_FILE coverage data file (default: .coverage)
  BR_COVERAGE=0    disable the coverage driver (trace only)

Logs go to stderr.  Never reads ``.buildathon_session`` or anything outside
the checkout.
"""

from __future__ import annotations

import builtins
import codecs
import functools
import io
import json
import os
import pathlib
import sys

import pytest

ROOT = os.path.realpath(os.environ.get("BR_CHECKOUT", os.getcwd()))
TRACE_OUT = os.path.realpath(os.environ.get("BR_TRACE_OUT", "/work/trace_reads.json"))
COVERAGE_FILE = os.environ.get("BR_COVERAGE_FILE", ".coverage")
COVERAGE_ENABLED = os.environ.get("BR_COVERAGE", "1") != "0"
_SELF = os.path.realpath(__file__)

# Relative paths that are bookkeeping, never fixtures.
_IGNORE_PREFIXES = (
    "__pycache__/",
    ".pytest_cache/",
    ".git/",
    ".coverage",
    "trace_conftest.py",
)
_IGNORE_SUFFIXES = (".pyc", ".pyo")

_reads: dict[str, set[str]] = {}
_current_test: str | None = None
_current_file: str | None = None
_coverage = None
_written = False


# --------------------------------------------------------------------- tracing
def _relative(abspath: str) -> str | None:
    if not abspath.startswith(ROOT + os.sep):
        return None
    return abspath[len(ROOT) + 1 :].replace(os.sep, "/")


def _bucket() -> str:
    if _current_test is not None:
        return _current_test
    if _current_file is not None:
        return "module:" + _current_file
    return "__unknown__"


def _record(path) -> None:
    """Record one read of *path* if it lives inside the pinned checkout.

    Tracing must never change test behavior, so any failure here is swallowed.
    """
    try:
        _record_unchecked(path)
    except Exception:  # noqa: BLE001 - never break a test over tracing
        return


def _record_unchecked(path) -> None:
    if isinstance(path, int):  # already-open fd
        return
    raw = os.fspath(path)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return
    if not isinstance(raw, str) or not raw:
        return
    if "\x00" in raw:
        return
    abspath = raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
    abspath = os.path.normpath(abspath)
    rel = _relative(abspath)
    if rel is None:
        return
    if abspath == _SELF or abspath == TRACE_OUT:
        return
    for prefix in _IGNORE_PREFIXES:
        if rel == prefix or rel.startswith(prefix):
            return
    if rel.endswith(_IGNORE_SUFFIXES):
        return
    _reads.setdefault(_bucket(), set()).add(rel)


def _mode_is_read(mode) -> bool:
    if mode is None:
        return True
    try:
        text = str(mode)
    except Exception:  # noqa: BLE001
        return True
    return not any(flag in text for flag in ("w", "a", "x", "+"))


def _install_open_wrappers() -> None:
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_codecs_open = codecs.open
    original_os_open = os.open

    @functools.wraps(original_builtin_open)
    def traced_open(file, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "r")
        if _mode_is_read(mode):
            _record(file)
        return original_builtin_open(file, *args, **kwargs)

    builtins.open = traced_open  # type: ignore[assignment]
    io.open = traced_open  # type: ignore[assignment]
    # importlib's io.open_code bypasses builtins.open; keep it untouched so
    # module imports are traced only for data files the module itself opens.

    @functools.wraps(original_codecs_open)
    def traced_codecs_open(filename, mode="r", *args, **kwargs):
        if _mode_is_read(mode):
            _record(filename)
        return original_codecs_open(filename, mode, *args, **kwargs)

    codecs.open = traced_codecs_open  # type: ignore[assignment]

    @functools.wraps(original_os_open)
    def traced_os_open(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if not (flags & write_flags):
            _record(path)
        return original_os_open(path, flags, *args, **kwargs)

    os.open = traced_os_open  # type: ignore[assignment]

    path_open = pathlib.Path.open
    path_read_text = pathlib.Path.read_text
    path_read_bytes = pathlib.Path.read_bytes

    @functools.wraps(path_open)
    def traced_path_open(self, mode="r", *args, **kwargs):
        if _mode_is_read(mode):
            _record(self)
        return path_open(self, mode, *args, **kwargs)

    @functools.wraps(path_read_text)
    def traced_read_text(self, *args, **kwargs):
        _record(self)
        return path_read_text(self, *args, **kwargs)

    @functools.wraps(path_read_bytes)
    def traced_read_bytes(self, *args, **kwargs):
        _record(self)
        return path_read_bytes(self, *args, **kwargs)

    pathlib.Path.open = traced_path_open  # type: ignore[method-assign]
    pathlib.Path.read_text = traced_read_text  # type: ignore[method-assign]
    pathlib.Path.read_bytes = traced_read_bytes  # type: ignore[method-assign]


# ---------------------------------------------------------------------- pytest
def pytest_configure(config):
    global _coverage
    _install_open_wrappers()
    if not COVERAGE_ENABLED:
        return
    try:
        import coverage
    except Exception as error:  # noqa: BLE001
        print(f"[trace_conftest] coverage unavailable: {error}", file=sys.stderr)
        return
    try:
        cov = coverage.Coverage(
            data_file=COVERAGE_FILE,
            source=["sqlglot"],
            branch=False,
            config_file=False,
        )
        config.pluginmanager.register(_CoverageStarter(cov), "blastradius-coverage")
        _coverage = cov
        print(f"[trace_conftest] coverage={COVERAGE_FILE} root={ROOT}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001
        print(f"[trace_conftest] could not create Coverage: {error}", file=sys.stderr)


class _CoverageStarter:
    """Tiny plugin object so coverage starts *after* collection."""

    def __init__(self, cov):
        self.cov = cov
        self.started = False

    def pytest_collection_finish(self, session):
        if self.started:
            return
        self.started = True
        try:
            self.cov.start()
        except Exception as error:  # noqa: BLE001
            print(f"[trace_conftest] coverage.start failed: {error}", file=sys.stderr)

    def pytest_unconfigure(self, config):
        # sessionfinish normally stops coverage; this is a safety net for
        # --collect-only or a hard interrupt.
        if self.started:
            try:
                self.cov.stop()
                self.cov.save()
            except Exception:  # noqa: BLE001 - best effort
                pass


def pytest_collect_file(file_path, parent):
    global _current_file
    try:
        text = str(file_path)
        if text.endswith(".py"):
            abspath = os.path.realpath(text)
            rel = _relative(abspath)
            if rel is not None:
                _current_file = rel
    except Exception:  # noqa: BLE001
        pass
    return None


def pytest_collection_finish(session):
    global _current_file
    _current_file = None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global _current_test
    _current_test = item.nodeid
    if _coverage is not None:
        try:
            _coverage.switch_context(item.nodeid)
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        _current_test = None
        if _coverage is not None:
            try:
                _coverage.switch_context("")
            except Exception:  # noqa: BLE001
                pass


def pytest_sessionfinish(session, exitstatus):
    global _written
    if _written:
        return
    _written = True
    if _coverage is not None:
        try:
            _coverage.stop()
            _coverage.save()
        except Exception as error:  # noqa: BLE001
            print(f"[trace_conftest] coverage save failed: {error}", file=sys.stderr)

    unknown = _reads.pop("__unknown__", set())
    payload = {key: sorted(paths) for key, paths in sorted(_reads.items())}
    tmp = TRACE_OUT + ".tmp"
    os.makedirs(os.path.dirname(TRACE_OUT) or ".", exist_ok=True)
    with builtins.open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, TRACE_OUT)
    buckets = sum(1 for key in payload if key.startswith("module:"))
    print(
        f"[trace_conftest] trace={TRACE_OUT} keys={len(payload)} "
        f"module_buckets={buckets} unknown_files={len(unknown)}",
        file=sys.stderr,
    )
    for path in sorted(unknown)[:20]:
        print(f"[trace_conftest] unknown-scope read: {path}", file=sys.stderr)
