#!/usr/bin/env python3
"""Validate versioned laser sample metadata and referenced image files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image

EVIDENCE_TIERS = {"reusable_real_evidence", "synthetic_fixture", "calibration_grade_real"}


def require(mapping: dict, path: str):
    value = mapping
    for component in path.split("."):
        if not isinstance(value, dict) or component not in value:
            raise ValueError(f"missing required field: {path}")
        value = value[component]
    return value


def image_path(metadata_path: Path, value: str, data_root: Path | None = None) -> Path:
    """Resolve an image reference.

    Absolute paths are used as-is. Relative paths resolve against ``data_root``
    when one is given (the shared capture directory), otherwise against the
    directory holding the metadata file.
    """
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (data_root if data_root is not None else metadata_path.parent) / candidate


def validate(path: Path, data_root: Path | None = None) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if require(payload, "schema_version") != "1.0":
        raise ValueError("unsupported schema_version")
    if not str(require(payload, "sample_id")):
        raise ValueError("empty sample_id")
    tier = require(payload, "evidence_tier")
    if tier not in EVIDENCE_TIERS:
        raise ValueError(f"invalid evidence_tier: {tier}")
    width = require(payload, "capture.camera.width_px")
    height = require(payload, "capture.camera.height_px")
    if not isinstance(width, int) or isinstance(width, bool) or width < 1:
        raise ValueError("capture.camera.width_px must be a positive integer")
    if not isinstance(height, int) or isinstance(height, bool) or height < 1:
        raise ValueError("capture.camera.height_px must be a positive integer")
    require(payload, "capture.registered_pair")
    require(payload, "capture.timestamp_utc")
    pose = payload["capture"].get("robot_pose")
    if pose is not None:
        if not isinstance(pose, dict):
            raise ValueError("capture.robot_pose must be an object or null")
        if not str(pose.get("frame", "")):
            raise ValueError("capture.robot_pose.frame must name the reference frame")
        for axis in ("x_mm", "y_mm", "z_mm", "rx_deg", "ry_deg", "rz_deg"):
            value = pose.get(axis)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"capture.robot_pose.{axis} must be a number")
    require(payload, "geometry.working_distance_mm")
    require(payload, "geometry.baseline_mm")
    require(payload, "geometry.laser_angle_deg")
    require(payload, "ground_truth.lateral_offset_mm")
    require(payload, "ground_truth.height_offset_mm")
    limitations = require(payload, "limitations")
    if not isinstance(limitations, list):
        raise ValueError("limitations must be a list")

    for state in ("laser_on", "laser_off"):
        reference = require(payload, f"images.{state}")
        if not isinstance(reference, dict):
            raise ValueError(f"images.{state} must be an object")
        file_name = require(payload, f"images.{state}.file")
        expected_hash = require(payload, f"images.{state}.sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError(f"images.{state}.sha256 must contain 64 hex characters")
        try:
            int(expected_hash, 16)
        except ValueError as exc:
            raise ValueError(f"images.{state}.sha256 is not hexadecimal") from exc
        image_file = image_path(path, str(file_name), data_root)
        if not image_file.is_file():
            raise ValueError(f"referenced image not found: {image_file}")
        actual_hash = hashlib.sha256(image_file.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"SHA-256 mismatch for {image_file}")
        with Image.open(image_file) as image:
            image.load()
            if image.size != (width, height):
                raise ValueError(
                    f"dimension mismatch for {image_file}: {image.size} != {(width, height)}"
                )

    if tier == "calibration_grade_real":
        for field in (
            "capture.fixture_revision",
            "capture.camera.exposure",
            "capture.camera.gain",
            "capture.camera.white_balance_locked",
            "geometry.working_distance_mm",
        ):
            if require(payload, field) is None:
                raise ValueError(f"calibration-grade sample cannot leave {field} unknown")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="resolve relative image paths against this directory instead of the metadata file's directory",
    )
    args = parser.parse_args()
    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("metadata.json")))
        else:
            files.append(path)
    if not files:
        raise SystemExit("No metadata files found")
    failures = 0
    for path in files:
        try:
            validate(path, args.data_root)
            print(f"METADATA_PASS {path}")
        except Exception as exc:
            failures += 1
            print(f"METADATA_FAIL {path}: {exc}")
    print(f"METADATA_RESULT: {'PASS' if failures == 0 else 'FAIL'} ({len(files) - failures}/{len(files)})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
