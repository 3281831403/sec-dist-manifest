"""Public exports for the incremental distribution manifest package."""

from .apply import (
    apply_delta,
    check_baseline,
    delta_batch,
    diff_manifest,
    normalize_path,
    parse_manifest,
    validate_delta,
    verify_result,
)

__all__ = [
    "apply_delta",
    "check_baseline",
    "delta_batch",
    "diff_manifest",
    "normalize_path",
    "parse_manifest",
    "validate_delta",
    "verify_result",
]