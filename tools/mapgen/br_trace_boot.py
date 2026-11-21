"""Site-startup shim for spawned test workers (MAPS_SPEC v2, bug 1).

Installed into site-packages by ``mapgen.Dockerfile`` and activated by the
``zz_blastradius_trace.pth`` file next to it, so *every* Python interpreter in
the mapgen image runs it before user code:

* when ``BR_CHILD_TRACE=1`` (set by ``trace_conftest.py`` in the pytest parent,
  inherited by the ``ProcessPoolExecutor(mp_context="spawn")`` workers that
  ``tests/test_optimizer.py`` / ``tests/test_executor.py`` create), it

  1. calls ``coverage.process_startup()`` so the worker measures sqlglot lines
     into parallel-mode ``.coverage.<pid>.<rand>`` data (``COVERAGE_PROCESS_START``
     points at ``br_coverage.coveragerc``, exported by ``run_maps.sh``), and
  2. imports ``trace_conftest`` -- the same module pytest loads in the parent --
     which installs the ``sys.meta_path`` proxy hook and the dialect hooks at
     import time, so the worker sees the exact same instrumentation.

* otherwise it does nothing (a normal ``python``/``pytest`` run in the image is
  untouched).

``trace_conftest.py`` is copied into the checkout root by ``run_maps.sh`` and
the checkout is on ``PYTHONPATH``, which spawn children inherit, so the import
resolves in workers.  Everything is best-effort: a failed import must never
break a test worker.
"""

from __future__ import annotations

import os
import sys


def _boot() -> None:
    if os.environ.get("BR_CHILD_TRACE") != "1":
        return
    try:
        import coverage

        coverage.process_startup()
    except Exception as error:  # noqa: BLE001 - never break the worker
        print(f"[br_trace_boot] coverage.process_startup failed: {error}", file=sys.stderr)
    try:
        import trace_conftest  # noqa: F401 - installs proxies + dialect hooks
    except Exception as error:  # noqa: BLE001
        print(f"[br_trace_boot] trace_conftest import failed: {error}", file=sys.stderr)


_boot()
