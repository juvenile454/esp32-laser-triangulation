"""Shared argparse helpers so every host tool validates input the same way.

Range validators raise ``argparse.ArgumentTypeError`` so a bad value becomes a
one-line usage error (exit code 2) instead of a traceback.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Exit codes shared by the tools. 2 is reserved by argparse for usage errors.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def description(doc: str | None) -> str:
    """First paragraph of a module docstring, for ArgumentParser(description=...)."""
    if not doc:
        return ""
    return doc.strip().split("\n\n", 1)[0].replace("\n", " ")


def data_dir() -> Path:
    """$LASER_DATA_DIR, else <repo root>/data (never relative to the CWD)."""
    configured = os.environ.get("LASER_DATA_DIR")
    return Path(configured) if configured else REPO_ROOT / "data"


def _number(text: str, kind: Callable[[str], float]) -> float:
    try:
        value = kind(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {text!r}") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite: {text!r}")
    return value


def float_range(
    minimum: float | None = None,
    maximum: float | None = None,
    *,
    min_inclusive: bool = True,
    max_inclusive: bool = True,
) -> Callable[[str], float]:
    """argparse type: a finite float within [minimum, maximum]."""

    def parse(text: str) -> float:
        value = _number(text, float)
        if minimum is not None and (value < minimum if min_inclusive else value <= minimum):
            bound = ">=" if min_inclusive else ">"
            raise argparse.ArgumentTypeError(f"must be {bound} {minimum:g}, got {text}")
        if maximum is not None and (value > maximum if max_inclusive else value >= maximum):
            bound = "<=" if max_inclusive else "<"
            raise argparse.ArgumentTypeError(f"must be {bound} {maximum:g}, got {text}")
        return value

    parse.__name__ = "number"
    return parse


def int_range(minimum: int | None = None, maximum: int | None = None) -> Callable[[str], int]:
    """argparse type: an integer within [minimum, maximum]."""

    def parse(text: str) -> int:
        try:
            value = int(text, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid integer: {text!r}") from exc
        if minimum is not None and value < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}, got {text}")
        if maximum is not None and value > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum}, got {text}")
        return value

    parse.__name__ = "integer"
    return parse


positive_float = float_range(0.0, min_inclusive=False)
non_negative_float = float_range(0.0)
positive_int = int_range(1)
non_negative_int = int_range(0)


def output_dir_arguments(parser: argparse.ArgumentParser, help_text: str, *, required: bool = True) -> None:
    """Add ``--output-dir`` plus the legacy positional form of the same value."""
    parser.add_argument("legacy_output_dir", nargs="?", type=Path, metavar="OUTPUT_DIR",
                        help=f"{help_text} (legacy positional form of --output-dir)")
    parser.add_argument("--output-dir", type=Path, default=None, help=help_text)
    parser.set_defaults(_output_dir_required=required)


def resolve_output_dir(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path | None:
    positional = getattr(args, "legacy_output_dir", None)
    option = getattr(args, "output_dir", None)
    if positional is not None and option is not None and positional != option:
        parser.error("give the output directory either positionally or with --output-dir, not both")
    chosen = option if option is not None else positional
    if chosen is None and getattr(args, "_output_dir_required", True):
        parser.error("the following arguments are required: --output-dir")
    args.output_dir = chosen
    return chosen


def fail(prog: str, message: object, code: int = EXIT_FAILURE) -> SystemExit:
    """One-line error for an expected runtime failure (not a usage error)."""
    print(f"{prog}: error: {message}", file=sys.stderr)
    return SystemExit(code)


def json_safe(value: object) -> object:
    """Replace NaN/Infinity by None so json.dump(..., allow_nan=False) succeeds."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
