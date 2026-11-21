"""Thin adapter: prefer W2's `blastradius` package, fall back to the reference engine.

W2 is building `blastradius/` concurrently, so the API is probed rather than
assumed. Anything that cannot be called is reported as "variant skipped" with a
message naming exactly what is missing; the harness never crashes because of it.

Integration contract (what W3 needs from W2, in order of preference):

    # 1. explicit shim (best; no guessing)
    import blastradius
    adapter = blastradius.bench_adapter(maps_dir)      # -> object
    adapter.select(patch_text, knobs=dict)             # -> Selection-like
    adapter.split(tests, selected_ids, max_workers=8)  # -> (workers, cost_ms)

    # 2. module-level pieces
    from blastradius.select import select              # select(patch_text, maps, knobs=dict)
    from blastradius.maps import load                  # load(maps_dir) -> maps object

`select` may return a set of test ids, a dict with keys in
{"tests", "tier_e", "rules"} (or "selected"/"provenance"), or an object with
those attributes. Provenance is optional; without it calibrate.py says so and
prints the provenance section from the reference engine only.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .engine import Knobs, Selection, split_tests
from .mapio import MapSet

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FROM_W2 = (
    "for knob A/B: blastradius.bench_adapter(maps_dir) -> object with "
    ".select(patch_text, knobs=dict); for provenance: per-rule test-id sets "
    "(rules: {rule: [test ids]}), not just rule hits"
)


class BackendError(RuntimeError):
    """No backend can run at all (e.g. --engine blastradius but package absent)."""


class BackendUnsupported(RuntimeError):
    """This backend cannot run a particular variant; the caller skips it."""


@dataclass
class Capabilities:
    available: bool
    detail: str
    missing: list[str]


class BlastRadiusNativeBackend:
    """Adapter over W2's concrete `blastradius` API (as landed).

    W2 exposes `select_commit(files, maps, index)` with no knob arguments, so
    only the v1 defaults run here; every other variant is skipped with a message
    (the reference engine is the knob-capable path). Provenance exists as rule
    *hits*, not as per-rule test sets, so `has_provenance` is False.
    """

    name = "blastradius"
    has_provenance = False

    def __init__(self, maps_dir: str | Path, warn=None):
        from blastradius import patch as blast_patch
        from blastradius import select as blast_select
        from blastradius.maps_io import Maps
        from blastradius.select import TestIndex

        self.warn = warn or (lambda message: None)
        self._patch = blast_patch
        self._select = blast_select
        self.maps = Maps.load(maps_dir)
        self.ids = list(self.maps.test_ids())
        self.index = TestIndex(self.ids, {test_id: number for number, test_id in enumerate(self.ids)})

    def capabilities(self) -> Capabilities:
        warnings = getattr(self.maps, "warnings", [])
        return Capabilities(
            available=True,
            detail="blastradius.select.select_commit (W2 native API)",
            missing=list(warnings),
        )

    def check_knobs(self, knobs: Knobs) -> None:
        if knobs != Knobs():
            raise BackendUnsupported(
                "blastradius.select_commit accepts no knob arguments; only v1 defaults run. "
                "Use --engine reference to A/B --symbol-vs-line/--classbody/--usage/--neutral/--pad/--tier-e."
            )

    def select(self, patch_text: str, knobs: Knobs) -> Selection:
        self.check_knobs(knobs)
        try:
            files = self._patch.parse_patch(patch_text)
        except Exception as error:  # noqa: BLE001 - W2 parser is not ours
            raise BackendUnsupported(
                f"blastradius.patch.parse_patch raised {type(error).__name__}: {error}"
            ) from error
        try:
            selection = self._select.select_commit(files, self.maps, self.index)
        except Exception as error:  # noqa: BLE001
            raise BackendUnsupported(
                f"blastradius.select.select_commit raised {type(error).__name__}: {error}"
            ) from error
        tier = str(getattr(selection, "tier", ""))
        notes = [str(hit) for hit in getattr(selection, "rules", [])]
        rules = {"tier_e": set()} if tier == "tier_e" else {}
        if tier == "neutral":
            rules["neutral"] = set()
        return Selection(
            tests={str(test_id) for test_id in selection.tests},
            tier_e=tier == "tier_e",
            rules=rules,
            neutral=True if tier == "neutral" else None,
            notes=notes,
        )

    def split(self, tests: list[dict], selected_ids: set[str], max_workers: int = 8):
        return split_tests(tests, selected_ids, max_workers)


class ReferenceBackend:
    """The bench's own v1 rule implementation (bench/engine.py)."""

    name = "reference"
    has_provenance = True

    def __init__(self, maps: MapSet, warn=None, valid_ids: set[str] | None = None):
        self.maps = maps
        self.warn = warn or (lambda message: None)
        self.valid_ids = valid_ids
        self._engines: dict[tuple, object] = {}

    def capabilities(self) -> Capabilities:
        missing = list(self.maps.missing_parts)
        return Capabilities(
            available=True,
            detail="bench reference engine (" + ("mock maps" if self.maps.is_mock() else "maps") + ")",
            missing=missing,
        )

    def check_knobs(self, knobs: Knobs) -> None:
        return None

    def select(self, patch_text: str, knobs: Knobs) -> Selection:
        from .engine import Engine

        key = tuple(sorted(knobs.as_dict().items(), key=lambda item: item[0]))
        engine = self._engines.get(key)
        if engine is None:
            engine = Engine(self.maps, knobs, valid_ids=self.valid_ids)
            self._engines[key] = engine
        return engine.select(patch_text)

    def split(self, tests: list[dict], selected_ids: set[str], max_workers: int = 8):
        return split_tests(tests, selected_ids, max_workers)


class BlastRadiusBackend:
    """Adapter over whatever shape W2 exposes today."""

    name = "blastradius"

    def __init__(self, select_function, maps_object, detail: str, warn=None):
        self.select_function = select_function
        self.maps_object = maps_object
        self.detail = detail
        self.warn = warn or (lambda message: None)
        self.has_provenance: bool | None = None
        self._provenance_missing_warned = False

    def capabilities(self) -> Capabilities:
        return Capabilities(available=True, detail=self.detail, missing=[])

    def check_knobs(self, knobs: Knobs) -> None:
        return None

    def select(self, patch_text: str, knobs: Knobs) -> Selection:
        calls = (
            lambda: self.select_function(patch_text, self.maps_object, knobs=knobs.as_dict()),
            lambda: self.select_function(patch_text, self.maps_object, knobs),
            lambda: self.select_function(patch_text, knobs=knobs.as_dict()),
            lambda: self.select_function(patch_text),
        )
        last_error: Exception | None = None
        for call in calls:
            try:
                result = call()
            except TypeError as error:
                last_error = error
                continue
            except Exception as error:  # noqa: BLE001 - W2 code is untrusted here
                raise BackendUnsupported(f"blastradius.select raised {type(error).__name__}: {error}") from error
            selection = normalize_result(result)
            if selection is None:
                raise BackendUnsupported(
                    "blastradius.select returned an unsupported shape; expected a set of ids or "
                    "an object/dict with tests/tier_e/rules"
                )
            if selection.rules:
                self.has_provenance = True
            elif self.has_provenance is None and not self._provenance_missing_warned:
                self.has_provenance = False
                self._provenance_missing_warned = True
                self.warn(
                    "blastradius.select does not expose provenance (rules); "
                    "calibrate will print the reference engine's provenance instead"
                )
            return selection
        raise BackendUnsupported(
            "blastradius.select is not callable with the bench's knobs signature "
            f"(tried 4 call shapes; last TypeError: {last_error})"
        )

    def split(self, tests: list[dict], selected_ids: set[str], max_workers: int = 8):
        # No split API in W2's contract initially; LPT is spec-fixed, share it.
        return split_tests(tests, selected_ids, max_workers)


def normalize_result(result) -> Selection | None:
    """Map W2's return value onto bench.engine.Selection, or None if unusable."""
    if isinstance(result, Selection):
        return result
    if isinstance(result, (set, frozenset, list, tuple)):
        tests = {str(item) for item in result}
        return Selection(tests=tests)

    if isinstance(result, dict):
        raw_tests = result.get("tests", result.get("selected", result.get("selection")))
        raw_rules = result.get("rules", result.get("provenance"))
        tier_e = bool(result.get("tier_e", result.get("tierE", False)))
        notes = [str(note) for note in result.get("notes", [])]
        neutral = result.get("neutral")
    else:
        raw_tests = getattr(result, "tests", getattr(result, "selected", None))
        raw_rules = getattr(result, "rules", getattr(result, "provenance", None))
        tier_e = bool(getattr(result, "tier_e", getattr(result, "tierE", False)))
        notes = [str(note) for note in getattr(result, "notes", [])]
        neutral = getattr(result, "neutral", None)

    if raw_tests is None:
        return None
    tests = {str(item) for item in raw_tests}
    rules: dict[str, set[str]] = {}
    if isinstance(raw_rules, dict):
        for rule, items in raw_rules.items():
            rules[str(rule)] = {str(item) for item in items}
    elif isinstance(raw_rules, (list, tuple, set)):
        rules["blastradius"] = {str(item) for item in raw_rules}
    return Selection(
        tests=tests,
        tier_e=tier_e,
        rules=rules,
        neutral=neutral if isinstance(neutral, bool) else None,
        notes=notes,
    )


def probe_blastradius(maps_dir: str | Path, warn=None) -> tuple[object | None, Capabilities]:
    """Find a usable entry point in `blastradius` without crashing on partial APIs."""
    warn = warn or (lambda message: None)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        package = importlib.import_module("blastradius")
    except ImportError as error:
        return None, Capabilities(
            available=False,
            detail=f"blastradius not importable ({error})",
            missing=[REQUIRED_FROM_W2],
        )

    # 1. explicit shim: blastradius.bench_adapter(maps_dir) -> object with .select
    shim = getattr(package, "bench_adapter", None)
    if callable(shim):
        try:
            adapter_object = shim(maps_dir)
        except Exception as error:  # noqa: BLE001
            warn(f"blastradius.bench_adapter failed: {error}")
            adapter_object = None
        select_function = getattr(adapter_object, "select", None)
        if callable(select_function):
            return (
                BlastRadiusBackend(
                    select_function, None, "blastradius.bench_adapter(maps_dir).select", warn=warn
                ),
                Capabilities(available=True, detail="blastradius.bench_adapter", missing=[]),
            )

    # 2. W2's concrete API as landed: maps_io.Maps + select.select_commit
    try:
        native = BlastRadiusNativeBackend(maps_dir, warn=warn)
    except ImportError as error:
        native_error = f"native API unavailable ({error})"
    except Exception as error:  # noqa: BLE001 - e.g. empty maps directory
        native_error = f"blastradius.Maps.load failed ({type(error).__name__}: {error})"
    else:
        return native, native.capabilities()

    # 3. generic: a loader plus a module-level select(patch, maps, knobs=dict)
    maps_object = None
    loader_error = None
    for module_name, attribute in (
        ("blastradius", "load_maps"),
        ("blastradius.maps", "load"),
        ("blastradius.maps", "MapSet"),
        ("blastradius.maps", "MapStore"),
        ("blastradius.maps_io", "Maps"),
    ):
        try:
            module = importlib.import_module(module_name)
            candidate = getattr(module, attribute, None)
        except ImportError:
            continue
        if candidate is None:
            continue
        try:
            maps_object = (
                candidate.load(maps_dir)
                if attribute in ("MapSet", "MapStore") and hasattr(candidate, "load")
                else candidate(maps_dir)
            )
            break
        except Exception as error:  # noqa: BLE001
            loader_error = f"{module_name}.{attribute}: {error}"
            maps_object = None

    if maps_object is not None:
        for module_name in ("blastradius.select", "blastradius.selector", "blastradius"):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            candidate = getattr(module, "select", None)
            if callable(candidate):
                return (
                    BlastRadiusBackend(candidate, maps_object, f"{module_name}.select", warn=warn),
                    Capabilities(available=True, detail=f"{module_name}.select", missing=[]),
                )

    detail = "blastradius is importable but exposes no usable entry point; " + native_error
    if loader_error:
        detail += f"; last loader error: {loader_error}"
    return None, Capabilities(available=False, detail=detail, missing=[REQUIRED_FROM_W2])


def load_backend(engine: str, maps: MapSet, warn=None):
    """engine in {"auto", "reference", "blastradius"}; returns a backend object."""
    warn = warn or (lambda message: None)
    if engine in ("reference", "auto"):
        reference = ReferenceBackend(maps, warn=warn)
        if engine == "reference":
            return reference
        found, capabilities = probe_blastradius(maps.directory, warn=warn)
        if found is not None:
            return found
        warn(f"using bench reference engine: {capabilities.detail}. Needed from W2: {REQUIRED_FROM_W2}")
        return reference
    if engine == "blastradius":
        found, capabilities = probe_blastradius(maps.directory, warn=warn)
        if found is None:
            raise BackendError(f"{capabilities.detail}. Needed from W2: {REQUIRED_FROM_W2}")
        return found
    raise BackendError(f"unknown engine {engine!r} (auto|reference|blastradius)")
