"""Blast Radius map-generation pytest plugin.

Copied into the sqlglot checkout root at *runtime* by ``run_maps.sh`` (it is
never baked into the pinned tree) and loaded with ``pytest -p trace_conftest``.
It produces every *dynamic* input the map families need, in one pytest
session:

1. File reads (``maps/fixtures.json.gz`` input).  Wraps ``builtins.open``,
   ``io.open``, ``codecs.open``, ``os.open`` and
   ``pathlib.Path.{open,read_text,read_bytes}`` to record which files inside
   the checkout each test reads.  Reads that happen while a test is *running*
   (its call phase) are attributed to that test's node id.  Reads made while a
   test's ``setup`` / ``teardown`` phase runs -- ``setUpClass`` /
   ``setUp`` / fixture finalisers -- are class/module-scope work: they are
   attributed to the module bucket ``"module:<tests/... .py>"`` of the test's
   own file, which ``gen_maps.py`` expands to every test of that file.  A
   ``TestExecutor.setUpClass`` that loads 32 ``.csv.gz`` tables therefore maps
   those tables to *all* executor tests instead of charging them to whichever
   test happened to run first (W1k).  Reads with no current test (collection,
   module import, session fixtures) use the same module bucket of the file
   being collected/imported, or ``"__unknown__"`` when no owning file can be
   determined.

2. Coverage contexts (``maps/coverage.bin.gz`` input).  Drives coverage.py
   manually so every test gets its own context: coverage starts after
   collection (import time is deliberately *not* attributed to a test) and
   ``switch_context(nodeid)`` is called around every test protocol
   (setup + call + teardown).

3. Dialect exposure (``maps/dialects.json.gz`` input).  Patches the four
   chokepoints every dialect resolution goes through --
   ``Dialect.get_or_raise`` (the public resolver used by ``parse_one`` /
   ``sqlglot.transpile`` / ``expr.sql(dialect=...)``), ``Dialect.get`` (class
   lookup), ``Dialect.__getitem__`` on the ``_Dialect`` metaclass (the
   ``Dialect["duckdb"]`` subscript form) and ``Dialect.__init__`` (direct
   construction such as ``Snowflake(...)`` or the ``result(**kwargs)`` inside
   ``get_or_raise``) -- and records the resolved *registered* dialect key
   (``Dialect._classes`` reverse lookup, so ``TSQL`` -> ``"tsql"``) for the
   current test.  The base ``Dialect`` class is recorded as ``"default"``.
   Fixture-driven resolution (``# dialect: x`` headers) funnels through the
   same chokepoints (``Validator.parse_one`` -> ``parse_one(read=self.dialect)``).
   Resolutions outside a test -- and, since W1k, resolutions during a test's
   setup/teardown phase (class fixtures) -- go to the same
   ``module:<tests/file.py>`` buckets the file tracer uses, and
   ``gen_maps.py`` expands them to all tests of that file.

4. Key reads (``maps/keyreads.json.gz`` input).  Every module-level and
   class-body ``dict``/``list``/``tuple``/``set``/``frozenset`` value in
   ``sqlglot/`` is replaced, at module import time, by a transparent proxy
   subclass that logs ``(current test, NAME, key/index)`` on
   ``__getitem__`` / ``get`` / ``__contains__`` and delegates everything else
   to the builtin type.  No second pytest pass is needed: the import hook
   patches ``sqlglot`` modules lazily as tests import them, which preserves
   import timing (and therefore the coverage map) exactly.

   *Post-creation pass (W1g).*  ``_Dialect.__new__`` (the Dialect metaclass) and
   ``_TokenizerBase.__init_subclass__`` build **new plain** containers while a
   class is created -- merged copies (``UNESCAPED_SEQUENCES`` /
   ``ESCAPED_SEQUENCES`` / ``TIME_TRIE`` / ``INVERSE_*``), rebuilt tokenizer
   tables (``_QUOTES`` / ``_FORMAT_STRINGS`` / ``_KEYWORD_TRIE`` / ``_COMMENTS``)
   and freshly ``type()``-created ``tokenizer_class`` / ``jsonpath_tokenizer_class``
   / ``parser_class`` / ``generator_class`` objects.  ``_patch_module`` runs right
   after a module executes, so it sees the merged *values*, but it only descends
   into classes defined in that module; ``_repatch_class`` walks the class bodies
   again (following role attributes across modules, id-guarded) so those copies
   are proxied too.  It runs after every sqlglot module import -- including the
   lazy dialect modules ``Dialect._try_load`` imports inside a test -- plus one
   final sweep at collection end.  At the pin it proxies 496 further class
   containers on 61 classes and leaves **zero** plain ``dict``/``list``/``set``/
   ``tuple``/``frozenset`` attributes on any sqlglot class.

   *Coarse markers.*  A proxy cannot attribute a wholesale consumption to a key,
   so ``__iter__`` / ``keys`` / ``items`` / ``values`` / ``copy`` / ``update`` /
   set algebra / ``__repr__`` / mutation record the *name* in ``_bulk`` instead.
   Defining ``__iter__`` also makes ``{**X}`` and ``dict(X)`` go through
   ``keys()`` + ``__getitem__`` (CPython's ``PyDict_Merge`` fast path only
   applies to subtypes that keep the default iterator), so merges become visible
   *and* marked.  ``gen_maps.py`` demotes every name in that set to ``opaque``.

   Key reads raised while a sqlglot module is executing are routed to the sink
   (``_PatchLoader.exec_module``), so a dialect imported lazily inside a test
   never has its import-time merges attributed to that test.  Since W1k the
   *phase* rule applies here too: a constant read by a test's setup/teardown
   (class fixtures) goes to the ``module:<tests/file.py>`` bucket for that
   file, not to whichever test happened to run first.

   NAME is the bare module-level name, or ``Class.NAME`` for class bodies
   (``Qualname.NAME`` for nested classes, e.g. ``Dialect.Tokenizer.KEYWORDS``).
   ``gen_maps.py`` normalises the same way and only ever compares the emitted
   form.  ``BR_KEYREADS_NESTED=1`` additionally proxies sub-containers one level
   deep for "dict of small tables" constants (recorded as ``NAME[key]``); it is
   off by default because nothing can statically classify those names yet.

5. Expression construction (``maps/classes.json.gz`` input, L4).  The base
   ``__init__`` that every ``sqlglot.expressions.Expression`` subclass runs
   (``sqlglot.expressions.core.Expr.__init__`` -- see ``_install_expression_hook``
   for why patching that slot instead of ``Expression.__init__`` is the
   MRO-safe equivalent) is wrapped once, at import time of that module, and
   records the qualified name of the constructed class for the current test.
   The class' sqlglot ancestors are recorded too (a test that constructs a
   subclass does construct the base's code), which also resolves subclasses
   defined inside the test files themselves.  Spawned workers record it exactly
   like the dialect/key traces (``_br_task`` sets the current-test state).

6. Subprocess tracing (MAPS_SPEC "Mapgen v2", bug 1).  ``tests/test_optimizer.py``
   and ``tests/test_executor.py`` run every fixture-driven case in a
   ``ProcessPoolExecutor(mp_context=get_context("spawn"))``, so the parent's
   per-test coverage/dialect/key traces never see the real work.  Fix:
   ``ProcessPoolExecutor.submit`` is monkeypatched (parent side) so every task
   becomes ``submit(_br_task, current_test_id, fn, *args, **kwargs)``;
   ``_br_task`` runs in the worker, sets the current test, calls
   ``Coverage.current().switch_context(test_id)``, runs ``fn`` and restores
   state.  Workers get coverage from ``COVERAGE_PROCESS_START`` (started by the
   sitecustomize shim ``br_trace_boot.py`` installed in site-packages, which
   also imports *this* module at interpreter startup so the proxies and the
   dialect hooks exist in every child), and write their dynamic trace to
   ``<BR_DYNAMIC_OUT>.<pid>.child.json``.  ``run_maps.sh`` runs
   ``coverage combine`` and ``gen_maps.py`` merges the child JSON files.  Both
   the parent and the children save data in parallel mode so combine can merge
   them into one ``.coverage``.

Safety: nothing here may change test behaviour, so the proxies are exact
subclasses, copies/deepcopies degrade to plain containers (a copy is a
protocol-level read a proxy cannot observe, which is why every name whose
container is consumed wholesale is marked coarse and classified ``opaque``
statically), and every hook swallows its own errors.

Environment:
  BR_CHECKOUT       checkout root (default: cwd)
  BR_TRACE_OUT      fixture-read JSON path (default: /work/trace_reads.json)
  BR_DYNAMIC_OUT    dialect/key JSON path (default: /work/dynamic.json)
  BR_COVERAGE_FILE  coverage data file (default: .coverage)
  BR_COVERAGE=0     disable the coverage driver (trace only)
  BR_KEYREADS=0     disable the constant-key proxies
  BR_KEYREADS_NESTED=1  also proxy one level of sub-containers (off by default)
  BR_DIALECTS=0     disable the dialect-exposure trace
  BR_CLASSES=0      disable the expression-construction trace
  BR_KEYREADS_SCOPE all|core  which constants to proxy (default: all)
  BR_SUBPROCESS_TRACE=0  disable the ProcessPoolExecutor submit wrapper
  BR_CHILD_TRACE   set to 1 by this module for spawned children; the
                   sitecustomize shim (br_trace_boot.py) only instruments a
                   process when it is set, so the pytest parent never starts
                   coverage twice.  Never set it by hand.
  COVERAGE_PROCESS_START  .coveragerc consumed by coverage.process_startup()
                   in children (set by run_maps.sh).

Logs go to stderr.  Never reads ``.buildathon_session`` or anything outside
the checkout.
"""

from __future__ import annotations

import atexit
import builtins
import codecs
from collections import defaultdict
import functools
import importlib.abc
import io
import json
import os
import pathlib
import pickle
import sys

import pytest

ROOT = os.path.realpath(os.environ.get("BR_CHECKOUT", os.getcwd()))
TRACE_OUT = os.path.realpath(os.environ.get("BR_TRACE_OUT", "/work/trace_reads.json"))
DYNAMIC_OUT = os.path.realpath(os.environ.get("BR_DYNAMIC_OUT", "/work/dynamic.json"))
COVERAGE_FILE = os.environ.get("BR_COVERAGE_FILE", ".coverage")
COVERAGE_ENABLED = os.environ.get("BR_COVERAGE", "1") != "0"
KEYREADS_ENABLED = os.environ.get("BR_KEYREADS", "1") != "0"
DIALECTS_ENABLED = os.environ.get("BR_DIALECTS", "1") != "0"
CLASSES_ENABLED = os.environ.get("BR_CLASSES", "1") != "0"
KEYREADS_SCOPE = os.environ.get("BR_KEYREADS_SCOPE", "all")
# One level of nested proxying for "dict of small tables" constants (see
# ``_wrap``).  Off by default: nested proxies are recorded as ``NAME[key]``,
# which has no static definition in ``sqlglot/`` (so ``usage_kind`` can never
# call it ``keyable`` and the selector cannot narrow on it), and registering them
# costs ~19 KB gzipped of ``maps/keyreads.json.gz`` on a one-test plumbing run.
# The mechanism is implemented and validated: ``BR_KEYREADS_NESTED=1`` turns it on.
KEYREADS_NESTED = os.environ.get("BR_KEYREADS_NESTED", "0") != "0"
SUBPROCESS_TRACE = os.environ.get("BR_SUBPROCESS_TRACE", "1") != "0"
# True only inside a spawned worker (the sitecustomize shim imports this module
# there).  The parent sets the env var for its future children at import time.
CHILD_MODE = os.environ.get("BR_CHILD_TRACE") == "1"
CHILD_DYNAMIC_OUT = f"{DYNAMIC_OUT}.{os.getpid()}.child.json"
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
# Which pytest phase of the current test is running.  ``"call"`` means the
# test body itself (per-test attribution); anything else is setup/teardown,
# i.e. class- or module-scoped work that is attributed to the file bucket (W1k).
# The default is ``"call"`` so spawned workers -- which only ever run call-phase
# pool tasks -- keep per-test attribution without extra wiring.
_phase = "call"
_coverage = None
_written = False

# ------------------------------------------------------------------ new state
_dialects: dict[str, set[str]] = {}
_keyreads: dict[str, dict[str, set]] = {}
_names: dict[str, str] = {}
_skipped: set[str] = set()
_patched_modules: set[str] = set()

# Fast-path handles used by the proxy objects and the dialect wrappers.
# ``_CUR_KEYS`` is always a ``defaultdict(set)`` (NAME -> keys) so the hot path
# stays at two executed lines; reads outside a test land in ``_SINK`` and are
# dropped (they are import-time reads, not attributable to a test).
_SINK: "defaultdict[str, set]" = defaultdict(set)
_CUR_KEYS = _SINK


def _add_text(name: str, key) -> None:
    """Fallback for unhashable keys/items (membership checks on containers)."""
    try:
        _CUR_KEYS[name].add(repr(key))
    except Exception:  # noqa: BLE001 - never break a test over tracing
        pass
_CUR_DIA = None  # set: dialect names, or None outside a test

# Coarse (bulk / protocol / mutation) accesses.  Any name listed here had its
# entries consumed wholesale at least once -- iterated, copied, merged,
# rendered, unioned or mutated -- so per-key read data for it can no longer be
# complete (an iteration/copy also *moves* entries into another container whose
# reads are recorded under that other name).  ``gen_maps.py`` demotes every such
# name to ``opaque``: this is the runtime half of the safety classification.
# It is global (import time included) on purpose: a copy made at import time is
# exactly the propagation the static rules cannot see through an alias.
_bulk: set[str] = set()


def _mark_coarse(name: str) -> None:
    try:
        _bulk.add(name)
    except Exception:  # noqa: BLE001 - never break a test over tracing
        pass


# Expression construction (L4).  ``_CUR_CLASSES`` is always a *set*: reads
# outside a test land in the module-local sink and are dropped (import-time
# construction is not attributable to a test), mirroring ``_CUR_KEYS``.
_classes: dict[str, set[str]] = {}
_SINK_CLASSES: set[str] = set()
_CUR_CLASSES = _SINK_CLASSES
# type -> tuple of qualified names (the class plus its sqlglot ancestors).  The
# MRO scan runs once per class; the per-instantiation cost is one dict lookup
# plus one set.update, which is what keeps the hot path cheap.
_CLASS_QN: dict = {}
_EXPR_HOOK: dict = {"hooked": False}

_DIA: dict = {"hooked": False, "dialect": None, "reverse": {}}


# --------------------------------------------------------------------- tracing
def _relative(abspath: str) -> str | None:
    if not abspath.startswith(ROOT + os.sep):
        return None
    return abspath[len(ROOT) + 1 :].replace(os.sep, "/")


def _file_bucket(test_id: str) -> str:
    """``module:<test file>`` -- the class/module-scope bucket of a test id."""
    return "module:" + test_id.split("::", 1)[0]


def _bucket() -> str:
    if _current_test is not None:
        if _phase == "call":
            return _current_test
        # setup/teardown: class/module-scope work, expanded by gen_maps.py to
        # every test of the file (a sound superset; only over-selects).
        return _file_bucket(_current_test)
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


# --------------------------------------------------------------- key recording
class _MissingName:
    """Fallback for reconstructed proxies (copy/pickle) that lost the slot."""

    __slots__ = ()

    def __getattr__(self, attr):
        if attr == "_br_name":
            return ""
        raise AttributeError(attr)


class _ProxyDict(_MissingName, dict):
    __slots__ = ("_br_name",)

    def __getitem__(self, key):
        try:
            _CUR_KEYS[self._br_name].add(key)
        except TypeError:
            _add_text(self._br_name, key)
        except Exception:  # noqa: BLE001 - never break a test over tracing
            pass
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            _CUR_KEYS[self._br_name].add(key)
        except TypeError:
            _add_text(self._br_name, key)
        except Exception:  # noqa: BLE001 - never break a test over tracing
            pass
        return dict.get(self, key, default)

    def __contains__(self, key):
        try:
            _CUR_KEYS[self._br_name].add(key)
        except TypeError:
            _add_text(self._br_name, key)
        except Exception:  # noqa: BLE001 - never break a test over tracing
            pass
        return dict.__contains__(self, key)

    def __deepcopy__(self, memo):
        import copy as _copy

        return _copy.deepcopy(dict(self), memo)


class _ProxyList(_MissingName, list):
    __slots__ = ("_br_name",)

    def __getitem__(self, index):
        try:
            _CUR_KEYS[self._br_name].add(index)
        except TypeError:
            _add_text(self._br_name, index)
        except Exception:  # noqa: BLE001
            pass
        return list.__getitem__(self, index)

    def __contains__(self, item):
        try:
            _CUR_KEYS[self._br_name].add(item)
        except TypeError:
            _add_text(self._br_name, item)
        except Exception:  # noqa: BLE001
            pass
        return list.__contains__(self, item)

    def __deepcopy__(self, memo):
        import copy as _copy

        return _copy.deepcopy(list(self), memo)


class _ProxyTuple(_MissingName, tuple):
    # tuple subclasses cannot have non-empty __slots__; the name lives in the
    # instance __dict__ that a slot-less subclass gets automatically.
    def __new__(cls, value):
        return tuple.__new__(cls, value)

    def __getitem__(self, index):
        try:
            _CUR_KEYS[self._br_name].add(index)
        except TypeError:
            _add_text(self._br_name, index)
        except Exception:  # noqa: BLE001
            pass
        return tuple.__getitem__(self, index)

    def __contains__(self, item):
        try:
            _CUR_KEYS[self._br_name].add(item)
        except TypeError:
            _add_text(self._br_name, item)
        except Exception:  # noqa: BLE001
            pass
        return tuple.__contains__(self, item)

    def __deepcopy__(self, memo):
        import copy as _copy

        return _copy.deepcopy(tuple(self), memo)


class _ProxySet(_MissingName, set):
    __slots__ = ("_br_name",)

    def __contains__(self, item):
        try:
            _CUR_KEYS[self._br_name].add(item)
        except TypeError:
            _add_text(self._br_name, item)
        except Exception:  # noqa: BLE001
            pass
        return set.__contains__(self, item)

    def __deepcopy__(self, memo):
        import copy as _copy

        return _copy.deepcopy(set(self), memo)


class _ProxyFrozenSet(_MissingName, frozenset):
    # frozenset subclasses cannot have non-empty __slots__ (see _ProxyTuple).
    def __new__(cls, value):
        return frozenset.__new__(cls, value)

    def __contains__(self, item):
        try:
            _CUR_KEYS[self._br_name].add(item)
        except TypeError:
            _add_text(self._br_name, item)
        except Exception:  # noqa: BLE001
            pass
        return frozenset.__contains__(self, item)

    def __deepcopy__(self, memo):
        import copy as _copy

        return _copy.deepcopy(frozenset(self), memo)


_PROXY_CLASS = {
    dict: _ProxyDict,
    list: _ProxyList,
    tuple: _ProxyTuple,
    set: _ProxySet,
    frozenset: _ProxyFrozenSet,
}


def _install_coarse_tracking() -> None:
    """Record wholesale consumption of a proxied container (see ``_bulk``).

    Only the operations whose *reads* a proxy cannot attribute to a key are
    wrapped: iteration, the three view methods, copies/merges, set algebra,
    rendering and mutation.  ``__len__``/truthiness is deliberately left alone
    (it reads no key, and it sits on the hottest path of all).
    """

    def install(proxy_class, base, names) -> None:
        for name in names:
            original = getattr(base, name, None)
            if original is None:  # pragma: no cover - version dependent
                continue

            def make(original):
                def method(self, *args, **kwargs):
                    _mark_coarse(self._br_name)
                    return original(self, *args, **kwargs)

                return method

            wrapper = make(original)
            wrapper.__name__ = name
            try:
                setattr(proxy_class, name, wrapper)
            except Exception:  # noqa: BLE001 - never break sqlglot
                continue

    dict_ops = (
        "__iter__",
        "keys",
        "items",
        "values",
        "copy",
        "update",
        "setdefault",
        "pop",
        "popitem",
        "__setitem__",
        "__delitem__",
        "__or__",
        "__ror__",
        "__ior__",
        "__repr__",
    )
    list_ops = (
        "__iter__",
        "copy",
        "__add__",
        "__radd__",
        "__iadd__",
        "__mul__",
        "__imul__",
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "sort",
        "reverse",
        "__setitem__",
        "__delitem__",
        "__repr__",
    )
    tuple_ops = ("__iter__", "__add__", "__mul__", "__repr__")
    set_ops = (
        "__iter__",
        "copy",
        "union",  # also covers ``|`` for a ``set`` subclass instance
        "intersection",
        "difference",
        "symmetric_difference",
        "update",
        "intersection_update",
        "difference_update",
        "symmetric_difference_update",
        "add",
        "discard",
        "remove",
        "pop",
        "clear",
        "__or__",
        "__and__",
        "__sub__",
        "__xor__",
        "__ior__",
        "__iand__",
        "__isub__",
        "__ixor__",
        "__repr__",
    )
    frozenset_ops = (
        "__iter__",
        "copy",
        "union",
        "intersection",
        "difference",
        "symmetric_difference",
        "__or__",
        "__and__",
        "__sub__",
        "__xor__",
        "__repr__",
    )
    install(_ProxyDict, dict, dict_ops)
    install(_ProxyList, list, list_ops)
    install(_ProxyTuple, tuple, tuple_ops)
    install(_ProxySet, set, set_ops)
    install(_ProxyFrozenSet, frozenset, frozenset_ops)


_install_coarse_tracking()

# Attributes of classes/interfaces we must never proxy (Python/Enum internals).
_SKIP_NAMES = frozenset({"__all__", "__builtins__", "__annotations__", "__dict__"})
_SKIP_PREFIX = ("__",)

# Modules that define the expression base class (single-module pins) or the
# module that holds it (package layout: ``expressions/core.py``).
_EXPRESSION_MODULES = frozenset({"sqlglot.expressions", "sqlglot.expressions.core"})

# High-value subset used when BR_KEYREADS_SCOPE=core is needed for speed.
_CORE_NAME_SUFFIXES = (
    "KEYWORDS",
    "TOKENIZERS",
    "PARSERS",
    "TRANSFORMS",
    "TYPE_MAPPING",
    "FUNC_TOKENS",
    "FUNCTIONS",
    "ARG_TYPES",
    "arg_types",
    "SETTINGS",
    "MAPPING",
    "_MAPPING",
)
_CORE_MODULES = (
    "sqlglot.tokens",
    "sqlglot.parser",
    "sqlglot.generator",
    "sqlglot.dialects.",
    "sqlglot.parsers.",
    "sqlglot.generators.",
    "sqlglot.jsonpath",
)

def _core_scope_ok(module_name: str, class_qualname: str | None, name: str) -> bool:
    if name.endswith(_CORE_NAME_SUFFIXES):
        return True
    if class_qualname:
        leaf = class_qualname.rsplit(".", 1)[-1]
        if leaf in {"Tokenizer", "Parser", "Generator"}:
            return True
    return module_name.startswith(_CORE_MODULES)

def _is_container(value) -> bool:
    return type(value) in _PROXY_CLASS


# Leaf types a nested (one level deep) proxied sub-container may hold.  The
# brief for W1g is explicit: recurse only into "plain containers of
# scalars/tuples", never into arbitrary object graphs.
_SCALAR_LEAVES = (str, bytes, bytearray, int, float, complex, bool, type(None))


def _is_scalar_leaf(value) -> bool:
    if isinstance(value, _SCALAR_LEAVES):
        return True
    if type(value) in (tuple, frozenset):
        return all(_is_scalar_leaf(item) for item in value)
    return False


def _is_scalar_container(value) -> bool:
    """A plain container whose elements are scalars (or tuples of scalars)."""
    kind = type(value)
    if kind is dict:
        return all(_is_scalar_leaf(key) for key in value.keys()) and all(
            _is_scalar_leaf(item) for item in value.values()
        )
    if kind in (list, tuple):
        return all(_is_scalar_leaf(item) for item in value)
    if kind in (set, frozenset):
        return all(_is_scalar_leaf(item) for item in value)
    return False


def _module_path(module_name: str) -> str:
    return module_name.replace(".", "/") + ".py"


def _registered_name(name: str, module_name: str, class_qualname: str | None) -> str:
    return f"{class_qualname}.{name}" if class_qualname else name


def _wrap(container, name: str, origin: str | None = None, nested: bool = True):
    """Return a logging proxy holding the same data as *container*.

    ``nested`` additionally proxies sub-containers one level deep, but only for
    a ``dict`` whose values are plain containers of scalars/tuples (the
    "dict of small tables" shape, e.g. ``EXPRESSION_METADATA``).  The inner
    proxies live inside the proxy, never in the original container, so the
    copy is ours alone; they are recorded as ``NAME[key]``.
    """
    proxy_class = _PROXY_CLASS[type(container)]
    proxy = proxy_class(container)
    proxy._br_name = name
    if origin is not None:
        _names.setdefault(name, origin)
    if nested and KEYREADS_NESTED and type(container) is dict and container:
        for key, value in list(dict.items(container)):
            if not _is_scalar_container(value):
                continue
            inner_name = f"{name}[{_key_text(key)}]"
            try:
                dict.__setitem__(proxy, key, _wrap(value, inner_name, origin, nested=False))
            except Exception:  # noqa: BLE001 - never break sqlglot
                continue
    return proxy


def _patch_scope(namespace, module_name: str, class_qualname: str | None, target) -> int:
    """Replace container values of *namespace* with logging proxies.

    ``target`` is the module/class the namespace belongs to; replacements are
    written back with ``setattr(target, name, proxy)``.  Returns the number of
    proxied constants.
    """
    count = 0
    for name, value in list(namespace.items()):
        if not isinstance(name, str) or name in _SKIP_NAMES or name.startswith(_SKIP_PREFIX):
            continue
        if _is_container(value):
            full = _registered_name(name, module_name, class_qualname)
            if KEYREADS_SCOPE == "core" and not _core_scope_ok(module_name, class_qualname, name):
                _skipped.add(f"{module_name}:{full} (scope)")
                continue
            try:
                setattr(
                    target,
                    name,
                    _wrap(value, full, f"{module_name}:{class_qualname or '<module>'}"),
                )
            except Exception as error:  # noqa: BLE001 - never break sqlglot
                _skipped.add(f"{module_name}:{full} ({type(error).__name__}: {error})")
                continue
            count += 1
            continue
        if isinstance(value, type) and getattr(value, "__module__", None) == module_name:
            count += _patch_class(value, module_name)
    return count


def _patch_class(klass, module_name: str, depth: int = 0) -> int:
    import enum as _enum

    if depth > 6 or issubclass(klass, _enum.Enum) or getattr(klass, "__module__", None) != module_name:
        return 0
    qualname = getattr(klass, "__qualname__", klass.__name__)
    try:
        namespace = dict(vars(klass))
    except Exception:  # noqa: BLE001
        return 0
    count = 0
    for name, value in namespace.items():
        if not isinstance(name, str) or name in _SKIP_NAMES or name.startswith(_SKIP_PREFIX):
            continue
        if _is_container(value):
            if KEYREADS_SCOPE == "core" and not _core_scope_ok(module_name, qualname, name):
                _skipped.add(f"{module_name}:{qualname}.{name} (scope)")
                continue
            full = f"{qualname}.{name}"
            try:
                setattr(klass, name, _wrap(value, full, f"{module_name}:{qualname}"))
            except Exception as error:  # noqa: BLE001
                _skipped.add(f"{module_name}:{full} ({type(error).__name__}: {error})")
                continue
            _names.setdefault(full, f"{module_name}:{qualname}")
            count += 1
        elif isinstance(value, type) and getattr(value, "__module__", None) == module_name:
            count += _patch_class(value, module_name, depth + 1)
    return count


# ------------------------------------------------- post-creation re-proxying
# ``_Dialect.__new__`` (the Dialect metaclass) and ``_TokenizerBase.
# __init_subclass__`` build *new plain* class-body containers while a class is
# being created: ``UNESCAPED_SEQUENCES``/``ESCAPED_SEQUENCES``/``TIME_TRIE``/…
# are merged copies, ``_QUOTES``/``_KEYWORD_TRIE``/``_FORMAT_STRINGS``/… are
# rebuilt for every subclass, and ``tokenizer_class``/``jsonpath_tokenizer_class``
# /``parser_class``/``generator_class`` are freshly ``type()``-created classes.
# ``_patch_module`` runs right after a module executes, so it sees the merged
# *values* -- but it only descends into classes whose ``__module__`` is that
# module, which misses the metaclass-built classes and anything a later
# ``__init_subclass__`` rebound.  ``_repatch_class`` walks class bodies again
# (and follows role attributes across modules), so those copies are proxied too.
_PATCHED_CLASS_IDS: set[int] = set()
_REPATCHED = {"classes": 0, "proxied": 0, "rounds": 0}


def _class_origin(klass, fallback: str) -> str:
    module = getattr(klass, "__module__", "") or ""
    if module == "sqlglot" or module.startswith("sqlglot."):
        return module
    return fallback


def _repatch_class(klass, module_name: str, depth: int = 0) -> int:
    """(Re-)proxy the class-body containers of *klass* and its sub-classes."""
    import enum as _enum

    if depth > 4:
        return 0
    key = id(klass)
    if key in _PATCHED_CLASS_IDS:
        return 0
    try:
        if issubclass(klass, _enum.Enum):
            return 0
        namespace = list(vars(klass).items())
    except Exception:  # noqa: BLE001 - never break sqlglot
        return 0
    _PATCHED_CLASS_IDS.add(key)
    qualname = getattr(klass, "__qualname__", None) or getattr(klass, "__name__", "")
    origin_module = _class_origin(klass, module_name)
    count = 0
    for name, value in namespace:
        if not isinstance(name, str) or name in _SKIP_NAMES or name.startswith(_SKIP_PREFIX):
            continue
        if _is_container(value):
            if KEYREADS_SCOPE == "core" and not _core_scope_ok(origin_module, qualname, name):
                _skipped.add(f"{origin_module}:{qualname}.{name} (scope)")
                continue
            full = f"{qualname}.{name}"
            try:
                setattr(klass, name, _wrap(value, full, f"{origin_module}:{qualname}"))
            except Exception as error:  # noqa: BLE001
                _skipped.add(f"{origin_module}:{full} ({type(error).__name__}: {error})")
                continue
            count += 1
            continue
        if isinstance(value, type) and (
            getattr(value, "__module__", "") == "sqlglot"
            or str(getattr(value, "__module__", "")).startswith("sqlglot.")
        ):
            count += _repatch_class(value, module_name, depth + 1)
    if count:
        _REPATCHED["proxied"] += count
        _REPATCHED["classes"] += 1
    return count


def _repatch_module(module, module_name: str) -> int:
    """Re-proxy every sqlglot class reachable from *module*'s namespace."""
    total = 0
    try:
        values = list(vars(module).values())
    except Exception:  # noqa: BLE001
        return 0
    for value in values:
        if isinstance(value, type) and (
            getattr(value, "__module__", "") == "sqlglot"
            or str(getattr(value, "__module__", "")).startswith("sqlglot.")
        ):
            total += _repatch_class(value, module_name)
    if total:
        _REPATCHED["rounds"] += 1
    return total


def _repatch_loaded() -> None:
    """Final sweep: every sqlglot class in ``sys.modules`` (collection time)."""
    for module_name, module in list(sys.modules.items()):
        if not (module_name == "sqlglot" or module_name.startswith("sqlglot.")):
            continue
        if module is None or not hasattr(module, "__dict__"):
            continue
        if getattr(module, "__name__", "") != module_name:
            continue
        try:
            _repatch_module(module, module_name)
        except Exception as error:  # noqa: BLE001 - tracing must never break imports
            _skipped.add(f"{module_name} (repatch: {type(error).__name__})")


def _patch_module(module) -> None:
    name = getattr(module, "__name__", "")
    if not (name == "sqlglot" or name.startswith("sqlglot.")):
        return
    if name in _patched_modules:
        return
    _patched_modules.add(name)
    if KEYREADS_ENABLED:
        try:
            _patch_scope(vars(module), name, None, module)
        except Exception as error:  # noqa: BLE001 - a broken proxy is worse than none
            _skipped.add(f"{name} (module patch: {type(error).__name__})")
        # Post-creation pass (W1g): the metaclass/``__init_subclass__`` copies and
        # the ``type()``-built tokenizer/parser/generator classes only become
        # visible once the module has executed, and several of them live on
        # classes whose ``__module__`` is a *different* module.
        try:
            _repatch_module(module, name)
        except Exception as error:  # noqa: BLE001
            _skipped.add(f"{name} (module repatch: {type(error).__name__})")
    if DIALECTS_ENABLED and name == "sqlglot.dialects.dialect":
        _install_dialect_hooks(module)
    if CLASSES_ENABLED and name in _EXPRESSION_MODULES:
        try:
            _install_expression_hook(module)
        except Exception as error:  # noqa: BLE001 - tracing must not break imports
            _skipped.add(f"{name} (expression hook: {type(error).__name__})")

# ------------------------------------------------------------------- import hook
class _PatchLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        # Module execution is import time even when it happens *inside* a test
        # (dialects are imported lazily by ``Dialect._try_load``): the dialect
        # class bodies run their metaclass merges there, and attributing those
        # copies to whichever test triggered the import would both explode the
        # key-read map and make it depend on test order/scheduling.  Route every
        # key read raised while a sqlglot module executes into the sink, exactly
        # like an import that happens before any test.
        global _CUR_KEYS
        saved = _CUR_KEYS
        _CUR_KEYS = _SINK
        try:
            self._loader.exec_module(module)
            _patch_module(module)
        finally:
            _CUR_KEYS = saved

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _PatchFinder(importlib.abc.MetaPathFinder):
    """Patch sqlglot modules as they are imported (preserves import timing)."""

    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        for finder in list(sys.meta_path):
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:  # noqa: BLE001
                spec = None
            if spec is not None:
                break
        else:
            return None
        loader = getattr(spec, "loader", None)
        if loader is not None and hasattr(loader, "exec_module"):
            spec.loader = _PatchLoader(loader)
        return spec


def _install_import_hook() -> None:
    if not (KEYREADS_ENABLED or DIALECTS_ENABLED or CLASSES_ENABLED):
        return
    try:
        sys.meta_path.insert(0, _PatchFinder())
    except Exception as error:  # noqa: BLE001
        print(f"[trace_conftest] import hook failed: {error}", file=sys.stderr)


def _instrument_loaded() -> None:
    for module_name, module in list(sys.modules.items()):
        if (
            (module_name == "sqlglot" or module_name.startswith("sqlglot."))
            and module is not None
            and hasattr(module, "__dict__")
            and getattr(module, "__name__", "") == module_name
        ):
            _patch_module(module)


# ------------------------------------------------------------ dialect exposure
def _install_dialect_hooks(dialect_module) -> None:
    if _DIA["hooked"]:
        return
    dialect = getattr(dialect_module, "Dialect", None)
    if not isinstance(dialect, type):
        return
    _DIA["hooked"] = True
    _DIA["dialect"] = dialect

    try:
        original_init = dialect.__dict__["__init__"]

        def patched_init(self, *args, **kwargs):
            _dia_record(type(self))
            original_init(self, *args, **kwargs)

        dialect.__init__ = patched_init  # type: ignore[assignment]
    except Exception as error:  # noqa: BLE001
        _skipped.add(f"dialect.__init__ ({type(error).__name__})")

    try:
        metaclass = type(dialect)
        original_get = metaclass.__dict__["get"]
        original_get_raise = dialect.__dict__["get_or_raise"]
        original_getitem = metaclass.__dict__["__getitem__"]

        def patched_get(cls, key, default=None):
            result = original_get.__func__(cls, key, default)
            if result is not None:
                _dia_record(result)
            return result

        def patched_get_or_raise(cls, dialect=None):
            result = original_get_raise.__func__(cls, dialect)
            _dia_record(type(result))
            return result

        # ``Dialect["duckdb"]`` is the other half of ``Dialect.get`` -- both are
        # the metaclass lookup that ``_classes`` exists for -- so it is a
        # resolution chokepoint too (W1k).  A special method must live on the
        # metaclass, and it stays a ``classmethod`` exactly like the original so
        # the subscript slot resolves it the same way.
        def patched_getitem(cls, key):
            result = original_getitem.__func__(cls, key)
            _dia_record(result)
            return result

        setattr(dialect, "get", classmethod(patched_get))
        setattr(dialect, "get_or_raise", classmethod(patched_get_or_raise))
        setattr(metaclass, "__getitem__", classmethod(patched_getitem))
    except Exception as error:  # noqa: BLE001
        _skipped.add(f"dialect.get/get_or_raise/__getitem__ ({type(error).__name__})")

    print(
        f"[trace_conftest] dialect hooks installed on {getattr(dialect, '__module__', '?')}",
        file=sys.stderr,
    )


def _dialect_key(klass) -> str:
    dialect = _DIA["dialect"]
    if dialect is None:
        return ""
    if klass is dialect:
        return "default"
    reverse = _DIA["reverse"]
    key = reverse.get(klass)
    if key is not None:
        return key
    key = ""
    try:
        classes = getattr(klass, "_classes", None) or {}
        for candidate, value in classes.items():
            if value is klass:
                key = candidate
                break
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        key = getattr(klass, "__name__", "").lower()
    reverse[klass] = key
    return key


def _dia_record(klass) -> None:
    bucket = _CUR_DIA
    if bucket is None:
        return
    try:
        key = _dialect_key(klass)
        if key:
            bucket.add(key)
    except Exception:  # noqa: BLE001 - never break a test
        return


# ------------------------------------------------- expression construction (L4)
def _class_names(ty) -> tuple[str, ...]:
    """Qualified names of *ty* and every sqlglot class it inherits from.

    ``sqlglot.expressions.Expression`` subclasses inherit ``__init__`` from
    ``Expr``; a test that constructs ``TriggerProperties`` therefore also
    constructs an ``Expression``.  Recording the whole sqlglot MRO (once per
    class) means the selector can expand construction evidence without knowing
    which spelling a test-local subclass used -- a subclass declared inside
    ``tests/`` maps onto the sqlglot classes whose ``__init__``/methods it runs.
    """
    names = _CLASS_QN.get(ty)
    if names is None:
        found: set[str] = set()
        try:
            mro = type.__getattribute__(ty, "__mro__")
        except Exception:  # noqa: BLE001 - never break a test
            mro = (ty,)
        for base in mro:
            module = getattr(base, "__module__", "") or ""
            if module == "sqlglot" or module.startswith("sqlglot."):
                found.add(module + "." + getattr(base, "__name__", ""))
        names = tuple(sorted(found))
        _CLASS_QN[ty] = names
    return names


def _install_expression_hook(expression_module) -> None:
    """Wrap the ``__init__`` every Expression subclass runs, once.

    MAPS_SPEC/W2c ask for ``sqlglot.expressions.Expression.__init__``.  In the
    pinned tree ``Expression`` does not define ``__init__`` itself: it inherits
    it from ``Expr`` (``sqlglot/expressions/core.py``), and classes such as
    ``_TimeUnit(Expression, TimeUnit)`` rely on the MRO picking *TimeUnit*'s
    ``__init__`` first.  ``setattr(Expression, "__init__", ...)`` would insert a
    new slot into ``Expression``, shadow ``TimeUnit.__init__`` for those
    subclasses and change behaviour, so the wrapper replaces the *defining*
    class' ``__init__`` instead: identical call graph, same orders, zero
    behavioural change.  Subclasses that override ``__init__`` and call
    ``super()`` are still caught (that call reaches the wrapper); the only
    construction this cannot see is an override that never calls ``super()``.
    Audit at the pin: 41 classes in ``sqlglot/`` define ``__init__``; the only
    ones on the Expression construction path are ``Expr`` (the wrapped slot
    itself), ``TimeUnit`` (a mixin in ``_TimeUnit``'s MRO) and ``DateTrunc`` (an
    Expression subclass), and both overriders call ``super().__init__(...)``.
    No Expression subclass overrides ``__init__`` without calling super, so one
    wrap is complete -- see the README's "``__init__`` override audit".
    """
    if _EXPR_HOOK["hooked"]:
        return
    expression = getattr(expression_module, "Expression", None)
    if not isinstance(expression, type):
        return
    try:
        mro = type.__getattribute__(expression, "__mro__")
    except Exception:  # noqa: BLE001
        return
    defining = None
    for klass in mro:
        if "__init__" in klass.__dict__:
            defining = klass
            break
    if defining is None:
        return
    original_init = defining.__dict__["__init__"]

    @functools.wraps(original_init)
    def patched_init(self, *args, **kwargs):
        if _CUR_CLASSES is not _SINK_CLASSES:
            try:
                try:
                    names = _CLASS_QN[type(self)]
                except KeyError:
                    names = _class_names(type(self))
                _CUR_CLASSES.update(names)
            except Exception:  # noqa: BLE001 - never break a test
                pass
        original_init(self, *args, **kwargs)

    try:
        defining.__init__ = patched_init  # type: ignore[assignment]
    except Exception as error:  # noqa: BLE001 - a missing trace beats broken code
        _skipped.add(f"{defining.__module__}.__init__ ({type(error).__name__})")
        return
    _EXPR_HOOK["hooked"] = True
    print(
        f"[trace_conftest] expression construction hook installed on "
        f"{defining.__module__}.{defining.__name__}.__init__",
        file=sys.stderr,
    )


# --------------------------------------------------- subprocess (spawn) tracing
# Spawned pool workers are fresh interpreters: the proxies/dialect hooks only
# exist there because ``br_trace_boot`` (loaded from a .pth in site-packages)
# imports this module at startup, and coverage only exists because
# ``coverage.process_startup()`` reads COVERAGE_PROCESS_START.  The parent
# registers the *picklable* ``_br_task`` wrapper so every pool task carries its
# test id across the process boundary.
_CHILD_TASKS = 0
_CHILD_FLUSH_EVERY = 100


def _child_coverage():
    """The coverage instance started by ``coverage.process_startup()``."""
    try:
        import coverage  # type: ignore

        return coverage.Coverage.current()
    except Exception:  # noqa: BLE001 - no coverage in this process
        return None


def _br_task(test_id, fn, *args, **kwargs):
    """Worker-side wrapper: attribute the task to *test_id*.

    Set before ``fn`` runs so the dialect hooks and the container proxies that
    execute inside the worker land in the right test bucket, then switch the
    coverage context (parallel-mode data is saved at process exit).  A pool task
    is call-phase work for that test (there is no other test in the worker), so
    the phase flag is pinned to ``"call"`` for its duration.
    """
    global _current_test, _phase, _CUR_KEYS, _CUR_DIA, _CUR_CLASSES, _CHILD_TASKS
    previous = (_current_test, _phase, _CUR_KEYS, _CUR_DIA, _CUR_CLASSES)
    _phase = "call"
    if test_id:
        _current_test = test_id
        if KEYREADS_ENABLED:
            _CUR_KEYS = _keyreads.setdefault(test_id, defaultdict(set))
        if DIALECTS_ENABLED:
            _CUR_DIA = _dialects.setdefault(test_id, set())
        if CLASSES_ENABLED:
            _CUR_CLASSES = _classes.setdefault(test_id, set())
    coverage_instance = _child_coverage()
    if coverage_instance is not None and test_id:
        try:
            coverage_instance.switch_context(test_id)
        except Exception:  # noqa: BLE001 - tracing must not break the task
            pass
    try:
        result = fn(*args, **kwargs)
        # The pool pickles the result *after* this wrapper returns, i.e. outside
        # the switched coverage context; ``Expression.__reduce__`` runs
        # ``sqlglot.serde.dump`` there, which would lose serde.py coverage for
        # every pool-driven test.  Force that serialisation now, inside the
        # attributed context.  A result the pool cannot pickle is harmless here
        # (the pool would fail on it anyway); swallow and let it through.
        try:
            pickle.dumps(result)
        except Exception:  # noqa: BLE001 - never break the task over tracing
            pass
        return result
    finally:
        if coverage_instance is not None:
            try:
                coverage_instance.switch_context("")
            except Exception:  # noqa: BLE001
                pass
        _current_test, _phase, _CUR_KEYS, _CUR_DIA, _CUR_CLASSES = previous
        _CHILD_TASKS += 1
        if _CHILD_TASKS % _CHILD_FLUSH_EVERY == 0:
            _child_flush()


def _install_pool_wrapper() -> None:
    """Route every ProcessPoolExecutor task through ``_br_task``."""
    if not SUBPROCESS_TRACE:
        return
    try:
        from concurrent.futures import ProcessPoolExecutor

        original_submit = ProcessPoolExecutor.submit
        if getattr(original_submit, "_br_wrapped", False):
            return

        @functools.wraps(original_submit)
        def traced_submit(self, fn, /, *args, **kwargs):
            return original_submit(
                self, _br_task, _current_test or "", fn, *args, **kwargs
            )

        traced_submit._br_wrapped = True  # type: ignore[attr-defined]
        ProcessPoolExecutor.submit = traced_submit  # type: ignore[method-assign]
        print(
            "[trace_conftest] ProcessPoolExecutor.submit wrapped (subprocess tracing)",
            file=sys.stderr,
        )
    except Exception as error:  # noqa: BLE001 - tracing must never break the suite
        print(f"[trace_conftest] pool wrapper failed: {error}", file=sys.stderr)


def _child_flush() -> None:
    """Write this worker's dynamic trace (dialects/keys)."""
    global _CHILD_TASKS
    coverage_instance = _child_coverage()
    if coverage_instance is not None:
        try:
            coverage_instance.save()
        except Exception as error:  # noqa: BLE001
            print(f"[trace_conftest] child coverage save failed: {error}", file=sys.stderr)
    try:
        _write_json(CHILD_DYNAMIC_OUT, _dynamic_payload())
    except Exception as error:  # noqa: BLE001
        print(f"[trace_conftest] child dynamic write failed: {error}", file=sys.stderr)
    # pytest's fd-capture swallows worker stderr while tests run, so leave an
    # audit trail next to the trace files instead.
    try:
        with open(DYNAMIC_OUT + ".children.log", "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} tasks={_CHILD_TASKS}\n")
    except Exception:  # noqa: BLE001 - diagnostics only
        pass
    print(
        f"[trace_conftest] child flush pid={os.getpid()} tasks={_CHILD_TASKS} "
        f"file={CHILD_DYNAMIC_OUT}",
        file=sys.stderr,
    )


def _child_exit() -> None:
    _child_flush()


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
            # Parallel mode: the parent writes .coverage.<pid>.<rand> too, so
            # ``coverage combine`` merges the parent and every worker's data
            # into one file (MAPS_SPEC v2, subprocess coverage).
            data_suffix=True,
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
    global _current_file, _CUR_DIA
    try:
        text = str(file_path)
        if text.endswith(".py"):
            abspath = os.path.realpath(text)
            rel = _relative(abspath)
            if rel is not None:
                _current_file = rel
                if DIALECTS_ENABLED:
                    _CUR_DIA = _dialects.setdefault("module:" + rel, set())
    except Exception:  # noqa: BLE001
        pass
    return None


def pytest_collection_finish(session):
    global _current_file, _CUR_DIA
    _current_file = None
    if DIALECTS_ENABLED:
        _CUR_DIA = None
    # Every sqlglot module is imported by now: one final (id-guarded) sweep so
    # classes created after their module was patched still get proxied.
    if KEYREADS_ENABLED:
        try:
            _repatch_loaded()
        except Exception as error:  # noqa: BLE001 - tracing must never break the run
            print(f"[trace_conftest] repatch sweep failed: {error}", file=sys.stderr)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global _current_test, _phase, _CUR_KEYS, _CUR_DIA, _CUR_CLASSES
    _current_test = item.nodeid
    # Setup/teardown work of a test (``setUpClass``, ``setUp``, fixtures,
    # finalisers) is class/module scope, so the dialect and constant-key traces
    # start in the *file* bucket; ``pytest_runtest_call`` switches both to the
    # test's own bucket for the body and back afterwards (W1k).  Coverage keeps
    # one context per test for the whole protocol, exactly as before.
    _phase = "setup"
    file_bucket = _file_bucket(item.nodeid)
    if KEYREADS_ENABLED:
        _CUR_KEYS = _keyreads.setdefault(file_bucket, defaultdict(set))
    if DIALECTS_ENABLED:
        _CUR_DIA = _dialects.setdefault(file_bucket, set())
    if CLASSES_ENABLED:
        _CUR_CLASSES = _classes.setdefault(item.nodeid, set())
    if _coverage is not None:
        try:
            _coverage.switch_context(item.nodeid)
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        _current_test = None
        _phase = "call"
        _CUR_KEYS = _SINK
        _CUR_DIA = None
        _CUR_CLASSES = _SINK_CLASSES
        if _coverage is not None:
            try:
                _coverage.switch_context("")
            except Exception:  # noqa: BLE001
                pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Switch the key/dialect sinks to the test bucket for the call phase."""
    global _phase, _CUR_KEYS, _CUR_DIA
    saved = (_phase, _CUR_KEYS, _CUR_DIA)
    _phase = "call"
    if _current_test is not None:
        if KEYREADS_ENABLED:
            _CUR_KEYS = _keyreads.setdefault(item.nodeid, defaultdict(set))
        if DIALECTS_ENABLED:
            _CUR_DIA = _dialects.setdefault(item.nodeid, set())
    try:
        yield
    finally:
        _phase, _CUR_KEYS, _CUR_DIA = saved


def _key_text(key) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "True" if key else "False"
    if isinstance(key, (int, float)):
        return repr(key)
    try:
        return repr(key)
    except Exception:  # noqa: BLE001
        return "<unrepr>"


def _write_json(path: str, payload) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with builtins.open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


def _dynamic_payload() -> dict:
    """Dialect/key-read payload; also written by every spawned worker."""
    keys_payload: dict[str, dict[str, list[str]]] = {}
    pair_count = 0
    for bucket, by_name in _keyreads.items():
        entry: dict[str, list[str]] = {}
        for name, keys in by_name.items():
            if not name:
                continue  # reconstructed copies lose the name; drop them
            text = sorted({_key_text(key) for key in keys})
            entry[name] = text
            pair_count += len(text)
        if entry:
            keys_payload[bucket] = entry
    dialects_payload = {
        bucket: sorted(names) for bucket, names in _dialects.items() if names
    }
    classes_payload = {
        bucket: sorted(names) for bucket, names in _classes.items() if names
    }
    return {
        "dialects": dialects_payload,
        "keys": keys_payload,
        "classes": classes_payload,
        "names": _names,
        # Names whose container was consumed wholesale (iteration/copy/merge/
        # render/mutation) at least once: per-key data cannot be complete.
        "bulk": sorted(_bulk),
        "skipped": sorted(_skipped),
        "stats": {
            "patched_modules": len(_patched_modules),
            "proxied_names": len(_names),
            "key_pairs": pair_count,
            "dialect_buckets": len(dialects_payload),
            "class_buckets": len(classes_payload),
            "class_pairs": sum(len(names) for names in classes_payload.values()),
            "coarse_names": len(_bulk),
            "repatched_classes": _REPATCHED["classes"],
            "repatched_rounds": _REPATCHED["rounds"],
        },
        "child": CHILD_MODE,
    }


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
    _write_json(TRACE_OUT, payload)
    buckets = sum(1 for key in payload if key.startswith("module:"))
    print(
        f"[trace_conftest] trace={TRACE_OUT} keys={len(payload)} "
        f"module_buckets={buckets} unknown_files={len(unknown)}",
        file=sys.stderr,
    )
    for path in sorted(unknown)[:20]:
        print(f"[trace_conftest] unknown-scope read: {path}", file=sys.stderr)

    dynamic = _dynamic_payload()
    _write_json(DYNAMIC_OUT, dynamic)
    keys_payload = dynamic["keys"]
    dialects_payload = dynamic["dialects"]
    pair_count = dynamic["stats"]["key_pairs"]
    print(
        f"[trace_conftest] dynamic={DYNAMIC_OUT} proxied_names={len(_names)} "
        f"key_buckets={len(keys_payload)} key_pairs={pair_count} "
        f"dialect_buckets={len(dialects_payload)} "
        f"class_buckets={len(dynamic['classes'])} "
        f"class_pairs={dynamic['stats']['class_pairs']} "
        # Sink counters are diagnostics only: they depend on import order across
        # spawned workers, so they are printed but never written into the maps
        # (that keeps two identical runs byte-identical).
        f"sink_pairs={sum(len(keys) for keys in _SINK.values())} "
        f"sink_classes={len(_SINK_CLASSES)} skipped={len(_skipped)}",
        file=sys.stderr,
    )
    for entry in sorted(_skipped)[:20]:
        print(f"[trace_conftest] skipped: {entry}", file=sys.stderr)


# Installed at plugin import time: pytest loads `-p trace_conftest` before it
# imports any test module, so every sqlglot import during collection/tests is
# seen.  Modules already imported (unusual) are patched immediately.
_install_import_hook()
_instrument_loaded()
_install_pool_wrapper()
if SUBPROCESS_TRACE and not CHILD_MODE:
    # Spawned workers inherit this process' environment: the sitecustomize shim
    # (br_trace_boot.py, loaded from a .pth) only instruments a process when the
    # flag is set, so workers get the same hooks while the pytest parent keeps
    # driving its own coverage instance.
    os.environ["BR_CHILD_TRACE"] = "1"
elif CHILD_MODE:
    atexit.register(_child_exit)
    print(
        f"[trace_conftest] subprocess worker instrumentation active pid={os.getpid()}",
        file=sys.stderr,
    )
