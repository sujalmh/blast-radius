# Map generator for MAPS_SPEC.md.  Not the scorer image: this one has network
# at build time and sqlglot installed at the pin, and it is only ever used to
# (re)generate the committed maps/ bundle.
#
# The build context is tools/mapgen/ (the repo-root .dockerignore excludes
# tools/ and requirements.lock from the scorer context; the copy of the lock in
# this directory is byte-identical to the repo root one):
#
#   docker build --platform linux/amd64 -t blast-mapgen \
#       -f tools/mapgen/mapgen.Dockerfile tools/mapgen
#   docker run --rm --platform linux/amd64 -v "$PWD/maps:/out" blast-mapgen
#
# The base digest and requirements.lock are the environment the scorer labeled
# the commits in, so the per-test line data matches what the selector reasons
# about at run time.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ARG COVERAGE_VERSION=7.17.4

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_INTEGRATION=0 \
    PYTHONPATH=/work/sqlglot

# Same environment the labels were produced in (PyPI allowed at build time).
COPY requirements.lock /work/requirements.lock
RUN python -m pip install --no-cache-dir -r /work/requirements.lock "coverage==${COVERAGE_VERSION}"

# Pin the sqlglot tree.  Done before copying the small tools so edits to the
# generator do not re-download it.
COPY fetch_pin.py /mapgen/fetch_pin.py
RUN python /mapgen/fetch_pin.py "${SQLGLOT_SHA}" /work/sqlglot

COPY trace_conftest.py /mapgen/trace_conftest.py
COPY gen_maps.py /mapgen/gen_maps.py
COPY run_maps.sh /mapgen/run_maps.sh
COPY inspect_maps.py /mapgen/inspect_maps.py
COPY br_trace_boot.py /mapgen/br_trace_boot.py
COPY br_coverage.coveragerc /work/br_coverage.coveragerc
RUN chmod 0755 /mapgen/run_maps.sh

# Subprocess tracing (MAPS_SPEC v2): every interpreter in this image -- in
# particular the spawn workers of tests/test_optimizer.py and
# tests/test_executor.py -- runs br_trace_boot at site startup.  It is a no-op
# unless BR_CHILD_TRACE=1, which only the traced pytest run sets.
RUN SITE="$(python -c 'import site; print(site.getsitepackages()[0])')" \
    && cp /mapgen/br_trace_boot.py "$SITE/br_trace_boot.py" \
    && printf 'import br_trace_boot\n' > "$SITE/zz_blastradius_trace.pth" \
    && python -c 'import br_trace_boot'

WORKDIR /work/sqlglot

# Default: full suite -> /out (mount the repo's maps/ there).
ENTRYPOINT ["/bin/bash", "/mapgen/run_maps.sh"]
CMD ["/out"]
