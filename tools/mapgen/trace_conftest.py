"""Blast Radius map-generation pytest plugin.

Copied into the sqlglot checkout root at *runtime* by ``run_maps.sh`` (it is
never baked into the pinned tree) and loaded with ``pytest -p trace_conftest``.
It produces every *dynamic* input the three map families need, in one pytest
session:

1. File reads (``maps/fixtures.json.gz`` input).  Wraps ``builtins.open``,
   ``io.open``, ``codecs.open``, ``os.open`` and
   ``pathlib.Path.{open,read_text,read_bytes}`` to record which files inside
   the checkout each test reads.  Reads that happen while a test is running are
   attributed to that test's node id.  Reads with no current test (module
   import, session fixtures) are attributed to the module bucket
   ``"module:<tests/... .py>"`` of the file being collected/imported, or to
   ``"__unknown__"`` when no owning file can be determined.

2. Coverage contexts (``maps/coverage.bin.gz`` input).  Drives coverage.py
   manually so every test gets its own context: coverage starts after
   collection (import time is deliberately *not* attributed to a test) and
   ``switch_context(nodeid)`` is called around every test protocol
   (setup + call + teardown).

3. Dialect exposure (``maps/dialects.json.gz`` input).  Patches the three
   chokepoints every dialect resolution goes through --
   ``Dialect.get_or_raise`` (the public resolver used by ``parse_one`` /
   ``sqlglot.transpile`` / ``expr.sql(dialect=...)``), ``Dialect.get`` (class
   lookup) and ``Dialect.__init__`` (direct construction such as
   ``Snowflake(...)`` or the ``result(**kwargs)`` inside ``get_or_raise``) --
   and records the resolved *registered* dialect key (``Dialect._classes``
   reverse lookup, so ``TSQL`` -> ``"tsql"``) for the current test.  The base
   ``Dialect`` class is recorded as ``"default"``.  Fixture-driven resolution
   (``# dialect: x`` headers) funnels through the same chokepoints
   (``Validator.parse_one`` -> ``parse_one(read=self.dialect)``).  Resolutions
   outside a test go to the same ``module:<tests/file.py>`` buckets the file
   tracer uses, and ``gen_maps.py`` expands them to all tests of that file.

4. Key reads (``maps/keyreads.json.gz`` input).  Every module-level and
   class-body ``dict``/``list``/``tuple``/``set``/``frozenset`` value in
   ``sqlglot/`` is replaced, at module import time, by a transparent proxy
   subclass that logs ``(current test, NAME, key/index)`` on
   ``__getitem__`` / ``get`` / ``__contains__`` and delegates everything else
   to the builtin type.  No second pytest pass is needed: the import hook
   patches ``sqlglot`` modules lazily as tests import them, which preserves
   import timing (and therefore the coverage map) exactly.

   NAME is the bare module-level name, or ``Class.NAME`` for class bodies
   (``Qualname.NAME`` for nested classes, e.g. ``Dialect.Tokenizer.KEYWORDS``).
   ``gen_maps.py`` normalises the same way and only ever compares the emitted
   form.

Safety: nothing here may change test behaviour, so the proxies are exact
subclasses, copies/deepcopies degrade to plain containers (a copy is a
protocol-level read a proxy cannot observe, so those names are classified
``opaque`` statically anyway), and every hook swallows its own errors.

Environment:
  BR_CHECKOUT       checkout root (default: cwd)
  BR_TRACE_OUT      fixture-read JSON path (default: /work/trace_reads.json)
  BR_DYNAMIC_OUT    dialect/key JSON path (default: /work/dynamic.json)
  BR_COVERAGE_FILE  coverage data file (default: .coverage)
  BR_COVERAGE=0     disable the coverage driver (trace only)
  BR_KEYREADS=0     disable the constant-key proxies
  BR_DIALECTS=0     disable the dialect-exposure trace
  BR_KEYREADS_SCOPE all|core  which constants to proxy (default: all)

Logs go to stderr.  Never reads ``.buildathon_session`` or anything outside
the checkout.
"""

from __future__ import annotations

import builtins
import codecs
from collections import defaultdict
import functools
import importlib.abc
import io
import json
import os
import pathlib
import sys

import pytest

ROOT = os.path.realpath(os.environ.get("BR_CHECKOUT", os.getcwd()))
TRACE_OUT = os.path.realpath(os.environ.get("BR_TRACE_OUT", "/work/trace_reads.json"))
DYNAMIC_OUT = os.path.realpath(os.environ.get("BR_DYNAMIC_OUT", "/work/dynamic.json"))
COVERAGE_FILE = os.environ.get("BR_COVERAGE_FILE", ".coverage")
COVERAGE_ENABLED = os.environ.get("BR_COVERAGE", "1") != "0"
KEYREADS_ENABLED = os.environ.get("BR_KEYREADS", "1") != "0"
DIALECTS_ENABLED = os.environ.get("BR_DIALECTS", "1") != "0"
KEYREADS_SCOPE = os.environ.get("BR_KEYREADS_SCOPE", "all")
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

_DIA: dict = {"hooked": False, "dialect": None, "reverse": {}}


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

# Attributes of classes/interfaces we must never proxy (Python/Enum internals).
_SKIP_NAMES = frozenset({"__all__", "__builtins__", "__annotations__", "__dict__"})
_SKIP_PREFIX = ("__",)

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

def _module_path(module_name: str) -> str:
    return module_name.replace(".", "/") + ".py"

def _registered_name(name: str, module_name: str, class_qualname: str | None) -> str:
    return f"{class_qualname}.{name}" if class_qualname else name

def _wrap(container, name: str):
    """Return a logging proxy holding the same data as *container*."""
    proxy_class = _PROXY_CLASS[type(container)]
    proxy = proxy_class(container)
    proxy._br_name = name
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
                setattr(target, name, _wrap(value, full))
            except Exception as error:  # noqa: BLE001 - never break sqlglot
                _skipped.add(f"{module_name}:{full} ({type(error).__name__}: {error})")
                continue
            _names.setdefault(full, f"{module_name}:{class_qualname or '<module>'}")
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
                setattr(klass, name, _wrap(value, full))
            except Exception as error:  # noqa: BLE001
                _skipped.add(f"{module_name}:{full} ({type(error).__name__}: {error})")
                continue
            _names.setdefault(full, f"{module_name}:{qualname}")
            count += 1
        elif isinstance(value, type) and getattr(value, "__module__", None) == module_name:
            count += _patch_class(value, module_name, depth + 1)
    return count


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
    if DIALECTS_ENABLED and name == "sqlglot.dialects.dialect":
        _install_dialect_hooks(module)

# ------------------------------------------------------------------- import hook
class _PatchLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _patch_module(module)

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
    if not (KEYREADS_ENABLED or DIALECTS_ENABLED):
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

        def patched_get(cls, key, default=None):
            result = original_get.__func__(cls, key, default)
            if result is not None:
                _dia_record(result)
            return result

        def patched_get_or_raise(cls, dialect=None):
            result = original_get_raise.__func__(cls, dialect)
            _dia_record(type(result))
            return result

        setattr(dialect, "get", classmethod(patched_get))
        setattr(dialect, "get_or_raise", classmethod(patched_get_or_raise))
    except Exception as error:  # noqa: BLE001
        _skipped.add(f"dialect.get/get_or_raise ({type(error).__name__})")

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


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global _current_test, _CUR_KEYS, _CUR_DIA
    _current_test = item.nodeid
    if KEYREADS_ENABLED:
        _CUR_KEYS = _keyreads.setdefault(item.nodeid, defaultdict(set))
    if DIALECTS_ENABLED:
        _CUR_DIA = _dialects.setdefault(item.nodeid, set())
    if _coverage is not None:
        try:
            _coverage.switch_context(item.nodeid)
        except Exception:  # noqa: BLE001
            pass
    try:
        yield
    finally:
        _current_test = None
        _CUR_KEYS = _SINK
        _CUR_DIA = None
        if _coverage is not None:
            try:
                _coverage.switch_context("")
            except Exception:  # noqa: BLE001
                pass


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
    dynamic = {
        "dialects": dialects_payload,
        "keys": keys_payload,
        "names": _names,
        "skipped": sorted(_skipped),
        "stats": {
            "patched_modules": len(_patched_modules),
            "proxied_names": len(_names),
            "key_pairs": pair_count,
            "sink_pairs": sum(len(keys) for keys in _SINK.values()),
            "dialect_buckets": len(dialects_payload),
        },
    }
    _write_json(DYNAMIC_OUT, dynamic)
    print(
        f"[trace_conftest] dynamic={DYNAMIC_OUT} proxied_names={len(_names)} "
        f"key_buckets={len(keys_payload)} key_pairs={pair_count} "
        f"dialect_buckets={len(dialects_payload)} skipped={len(_skipped)}",
        file=sys.stderr,
    )
    for entry in sorted(_skipped)[:20]:
        print(f"[trace_conftest] skipped: {entry}", file=sys.stderr)


# Installed at plugin import time: pytest loads `-p trace_conftest` before it
# imports any test module, so every sqlglot import during collection/tests is
# seen.  Modules already imported (unusual) are patched immediately.
_install_import_hook()
_instrument_loaded()
