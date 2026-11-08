#!/usr/bin/env python3
"""Build a provisional compact TinyML input vector from a stripe profile.

This validates the feature contract only. It does not train or claim a model.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

from cli_common import description, fail

PROFILE_FEATURES = 64
QUALITY_FEATURES = 5
REQUIRED_COLUMNS = ("x_px", "valid", "peak_score", "width_px", "saturated", "secondary_candidate")
PROG = "build_profile_features"


def load_profile(path: Path) -> list[dict[str, float | bool]]:
    rows: list[dict[str, float | bool]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"profile CSV is missing column(s) {', '.join(missing)}; expected the profile.csv "
                f"written by analyze_laser_pair.py ({', '.join(REQUIRED_COLUMNS)})"
            )
        for line, row in enumerate(reader, 2):
            try:
                rows.append(parse_row(row))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid value at CSV line {line}: {exc}") from exc
    if not rows:
        raise ValueError("empty profile")
    return rows


def parse_row(row: dict[str, str]) -> dict[str, float | bool]:
    return {
        "x_px": float(row["x_px"]),
        "valid": str(row["valid"]).lower() in {"1", "true"},
        "peak": float(row["peak_score"]),
        "width": float(row["width_px"]),
        "saturated": str(row["saturated"]).lower() in {"1", "true"},
        "secondary": str(row["secondary_candidate"]).lower() in {"1", "true"},
    }


def nearest_valid(rows: list[dict[str, float | bool]], index: float) -> float | None:
    center = int(round(index))
    for radius in range(len(rows)):
        for candidate in {center - radius, center + radius}:
            if 0 <= candidate < len(rows) and bool(rows[candidate]["valid"]):
                return float(rows[candidate]["x_px"])
    return None


def build_features(rows: list[dict[str, float | bool]]) -> dict[str, object]:
    valid = [row for row in rows if bool(row["valid"])]
    if not valid:
        raise ValueError("profile has no valid points")
    x_values = [float(row["x_px"]) for row in valid]
    center = statistics.median(x_values)
    span = max(1.0, max(x_values) - min(x_values))
    profile = []
    for feature_index in range(PROFILE_FEATURES):
        source_index = feature_index * (len(rows) - 1) / (PROFILE_FEATURES - 1)
        value = nearest_valid(rows, source_index)
        profile.append(0.0 if value is None else max(-1.0, min(1.0, (value - center) / span)))
    count = len(valid)
    quality = [
        count / len(rows),
        sum(float(row["peak"]) for row in valid) / count / 255.0,
        min(1.0, sum(float(row["width"]) for row in valid) / count / 32.0),
        sum(bool(row["saturated"]) for row in valid) / count,
        sum(bool(row["secondary"]) for row in valid) / count,
    ]
    values = profile + quality
    return {
        "contract_version": 1,
        "status": "provisional_feature_pipeline_smoke_test_only",
        "profile_features": PROFILE_FEATURES,
        "quality_features": QUALITY_FEATURES,
        "total_features": len(values),
        "normalization": {"x_center_px": center, "x_span_px": span},
        "values": values,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=description(__doc__),
        epilog="Exit codes: 0 success, 1 unreadable/invalid profile, 2 usage error.",
    )
    parser.add_argument("profile_csv", type=Path, help="profile.csv written by analyze_laser_pair.py")
    parser.add_argument("output_json", type=Path, help="destination feature JSON")
    args = parser.parse_args(argv)
    try:
        features = build_features(load_profile(args.profile_csv))
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(features, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise fail(PROG, f"{args.profile_csv}: {exc}") from None
    print(f"FEATURE_RESULT: PASS ({features['total_features']} values -> {args.output_json})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
