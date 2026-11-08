#!/usr/bin/env python3
"""Validate versioned laser sample metadata and referenced image files.

Each file is first checked against schemas/sample.schema.json (JSON Schema
draft 2020-12), then the referenced images are checked for existence, SHA-256
and pixel dimensions.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from PIL import Image

from cli_common import description

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "sample.schema.json"
EVIDENCE_TIERS = {"reusable_real_evidence", "synthetic_fixture", "calibration_grade_real"}
METADATA_GLOB = "*metadata.json"


@functools.lru_cache(maxsize=1)
def schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(payload: Any) -> list[str]:
    """Human-readable schema violations, sorted by location ('' when valid)."""
    messages = []
    for error in schema_validator().iter_errors(payload):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"{location}: {error.message}")
    return sorted(messages)


def validate_schema(payload: Any) -> None:
    errors = schema_errors(payload)
    if errors:
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise ValueError("schema violation: " + "; ".join(errors[:3]) + more)
    assert isinstance(payload, dict)
    timestamp = payload["capture"]["timestamp_utc"]
    if timestamp is not None:
        try:
            datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"capture/timestamp_utc is not a real date-time: {timestamp!r}") from exc


def image_path(metadata_path: Path, value: str, data_root: Path | None = None) -> Path:
    """Resolve an image reference.

    Without ``data_root``, relative paths resolve against the metadata file's
    directory and absolute paths are used as-is. With ``data_root`` (the shared
    capture directory) the reference must be relative and stay inside it.
    """
    candidate = Path(value)
    if data_root is None:
        return candidate if candidate.is_absolute() else metadata_path.parent / candidate
    if candidate.is_absolute():
        raise ValueError(f"absolute image path not allowed with --data-root: {value}")
    root = data_root.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"image path escapes --data-root: {value}")
    return resolved


def check_image(path: Path, reference: dict[str, Any], state: str, size: tuple[int, int], data_root: Path | None) -> None:
    image_file = image_path(path, str(reference["file"]), data_root)
    if not image_file.is_file():
        hint = "" if data_root is not None else " (use --data-root to point at the capture directory)"
        raise ValueError(f"images.{state}: image not found: {image_file}{hint}")
    actual_hash = hashlib.sha256(image_file.read_bytes()).hexdigest()
    if actual_hash != reference["sha256"]:
        raise ValueError(f"images.{state}: SHA-256 mismatch for {image_file}")
    with Image.open(image_file) as image:
        image.load()
        if image.size != size:
            raise ValueError(f"images.{state}: dimension mismatch for {image_file}: {image.size} != {size}")


def validate(path: Path, data_root: Path | None = None) -> None:
    """Raise ValueError describing the first problem with one metadata file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    validate_schema(payload)
    camera = payload["capture"]["camera"]
    size = (camera["width_px"], camera["height_px"])
    for state in ("laser_on", "laser_off"):
        check_image(path, payload["images"][state], state, size, data_root)


def collect(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob(METADATA_GLOB)))
        else:
            files.append(path)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=description(__doc__),
        epilog=f"Directories are searched recursively for '{METADATA_GLOB}'. Exit codes: 0 all valid, 1 any invalid, 2 usage error.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="metadata JSON files or directories to search")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="resolve relative image paths against this directory instead of the metadata file's directory; "
             "absolute paths and '..' escapes are then rejected",
    )
    args = parser.parse_args(argv)
    if args.data_root is not None and not args.data_root.is_dir():
        parser.error(f"--data-root is not a directory: {args.data_root}")
    files = collect(args.paths)
    if not files:
        parser.error(f"no metadata files ('{METADATA_GLOB}') found")
    failures = 0
    for path in files:
        try:
            validate(path, args.data_root)
            print(f"METADATA_PASS {path}")
        except (ValueError, OSError) as exc:
            failures += 1
            print(f"METADATA_FAIL {path}: {exc}")
    print(f"METADATA_RESULT: {'PASS' if failures == 0 else 'FAIL'} ({len(files) - failures}/{len(files)})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
