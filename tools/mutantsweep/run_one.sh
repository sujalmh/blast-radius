#!/usr/bin/env bash
# Run ONE mutant (or "baseline") through the full pinned sqlglot suite.
#
#   ./run_one.sh <id|baseline>
#
# Requires the runner image (run_all.sh builds it if missing):
#   docker build --platform linux/amd64 -t blast-mutantsweep \
#       -f tools/mutantsweep/runner.Dockerfile tools/mutantsweep
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ID="${1:?usage: run_one.sh <id|baseline>}"
IMAGE="${MUTANTSWEEP_IMAGE:-blast-mutantsweep}"

mkdir -p "$HERE/ground"
docker run --rm --platform linux/amd64 \
  -e MUTANT_ID="$ID" \
  -v "$HERE:/data" \
  "$IMAGE"
