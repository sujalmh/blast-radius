# Ground-truth mutant runner for W5 (NOT the scorer image).
#
# Build (linux/amd64, emulated on Apple silicon):
#   docker build --platform linux/amd64 -t blast-mutantsweep \
#       -f tools/mutantsweep/runner.Dockerfile tools/mutantsweep
#
# Then run one mutant (mounts the corpus/ground dir at /data):
#   docker run --rm --platform linux/amd64 -e MUTANT_ID=<id> \
#       -v "$PWD/tools/mutantsweep:/data" blast-mutantsweep
#
# The base digest is the scorer's base image and requirements.lock is the
# committed lock, so the failing sets are observed in the same environment the
# event scores in.  No tracing, no coverage: plain pytest only.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_INTEGRATION=0 \
    PYTHONPATH=/pristine/sqlglot

COPY requirements.lock /work/requirements.lock
RUN python -m pip install --no-cache-dir -r /work/requirements.lock

COPY fetch_pin.py /runner/fetch_pin.py
RUN python /runner/fetch_pin.py "${SQLGLOT_SHA}" /pristine/sqlglot

COPY apply_patch.py /runner/apply_patch.py
COPY collect.py /runner/collect.py
COPY run_in_container.sh /runner/run_in_container.sh
RUN chmod 0755 /runner/run_in_container.sh

WORKDIR /work

ENTRYPOINT ["/bin/bash", "/runner/run_in_container.sh"]
