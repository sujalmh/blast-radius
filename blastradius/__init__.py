"""Blast Radius selector runtime (stdlib only).

Modules:
    patch        - unified-diff parser
    maps_io      - readers for the committed dependency maps (v1 + optional v2)
    neutral      - comment-only / AST-equality neutrality
    select       - selection rules (v1 rules + dialect/keyread refinements)
    split        - LPT worker split
    bench_adapter- stable knobs/provenance hook for the calibration harness
"""

__all__ = ["bench_adapter", "maps_io", "neutral", "patch", "select", "split"]
