#!/usr/bin/env python3
"""Fit and export a provisional pixel-to-physical calibration mapping.

Final coefficients are only valid when the input CSV comes from a measured,
rigid fixture. The tool itself can be smoke-tested with synthetic points.

The firmware header (calibration_coefficients.h) is only written when an
explicit --fixture-revision names the rigid fixture the data was measured on.
Without it, report.json, residuals.csv and the plot are still written and
marked provisional.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import random
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cli_common import (
    description,
    fail,
    json_safe,
    output_dir_arguments,
    positive_float,
    resolve_output_dir,
)

PROG = "fit_calibration"
FIXTURE_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
UNSET_FIXTURE_REVISION = "UNSET_FIXTURE_REVISION"
# Exhaustive search below this many minimal subsets, deterministic sampling above.
MAX_INITIAL_SUBSETS = 5000
SUBSET_SEED = 20260813


@dataclass
class Point:
    observed_px: float
    physical_mm: float
    split: str


def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular calibration fit")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[row][-1] for row in range(size)]


def fit_polynomial(points: list[tuple[float, float]], degree: int) -> list[float]:
    count = degree + 1
    matrix = [[0.0 for _ in range(count)] for _ in range(count)]
    vector = [0.0 for _ in range(count)]
    for x, y in points:
        powers = [1.0]
        for _ in range(2 * degree):
            powers.append(powers[-1] * x)
        for row in range(count):
            vector[row] += y * powers[row]
            for column in range(count):
                matrix[row][column] += powers[row + column]
    return solve(matrix, vector)


def evaluate(coefficients: list[float], x: float) -> float:
    value = 0.0
    for coefficient in reversed(coefficients):
        value = value * x + coefficient
    return value


def candidate_subsets(count: int, size: int, limit: int = MAX_INITIAL_SUBSETS, seed: int = SUBSET_SEED):
    """All size-subsets of range(count) if there are at most ``limit``, else
    ``limit`` distinct deterministic random ones (a fixed-seed RANSAC start).

    Taking only the first ``limit`` in lexicographic order would never draw a
    subset without index 0 for large inputs, so an outlier there survived.
    """
    if math.comb(count, size) <= limit:
        yield from itertools.combinations(range(count), size)
        return
    rng = random.Random(seed)
    seen: set[tuple[int, ...]] = set()
    while len(seen) < limit:
        subset = tuple(sorted(rng.sample(range(count), size)))
        if subset not in seen:
            seen.add(subset)
            yield subset


def robust_fit(points: list[tuple[float, float]], degree: int, sigma: float) -> tuple[list[float], list[bool]]:
    minimum = degree + 1
    if len(points) < minimum:
        raise ValueError("too few points for requested polynomial degree")

    # Start from the small subset whose polynomial minimizes the median
    # absolute residual. This keeps one gross outlier from tilting the first
    # least-squares fit and causing cascading false rejections.
    best_coefficients: list[float] | None = None
    best_score = float("inf")
    for indices in candidate_subsets(len(points), minimum):
        try:
            candidate = fit_polynomial([points[index] for index in indices], degree)
        except ValueError:
            continue
        score = statistics.median(
            abs(y - evaluate(candidate, x)) for x, y in points
        )
        if score < best_score:
            best_score = score
            best_coefficients = candidate
    if best_coefficients is None:
        raise ValueError("could not construct a non-singular robust calibration fit")

    residuals = [y - evaluate(best_coefficients, x) for x, y in points]
    center = statistics.median(residuals)
    deviations = sorted(abs(value - center) for value in residuals)
    mad = statistics.median(deviations)
    upper_quartile = deviations[min(len(deviations) - 1, math.ceil(0.75 * len(deviations)) - 1)]
    robust_scale = max(1e-9, 1.4826 * max(mad, upper_quartile))
    inliers = [abs(residual - center) <= sigma * robust_scale for residual in residuals]
    if sum(inliers) < minimum:
        inliers = [True] * len(points)

    selected = [point for point, include in zip(points, inliers, strict=True) if include]
    return fit_polynomial(selected, degree), inliers


def read_points(path: Path) -> list[Point]:
    rows: list[Point] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"observed_px", "physical_mm"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            found = ",".join(reader.fieldnames or []) or "<none>"
            raise ValueError(f"CSV requires observed_px,physical_mm and optional split columns (found: {found})")
        for line, row in enumerate(reader, 2):
            try:
                observed = float(row["observed_px"])
                physical = float(row["physical_mm"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid numeric value at CSV line {line}") from exc
            if not math.isfinite(observed) or not math.isfinite(physical):
                raise ValueError(f"non-finite value at CSV line {line}")
            rows.append(Point(observed, physical, (row.get("split") or "fit").strip().lower()))
    return rows


def cpp_float_literal(value: float) -> str:
    """A C++ float literal that always compiles: '2' -> '2.000000000e+00F'."""
    if not math.isfinite(value):
        raise ValueError(f"non-finite calibration coefficient: {value!r}")
    return f"{value:.9e}F"


def write_header(path: Path, coefficients: list[float], fixture_revision: str) -> None:
    if not FIXTURE_REVISION_PATTERN.fullmatch(fixture_revision):
        raise ValueError(f"invalid fixture revision {fixture_revision!r}")
    values = ", ".join(cpp_float_literal(value) for value in coefficients)
    path.write_text(
        "#pragma once\n\n"
        "// GENERATED calibration coefficients. Validity depends entirely on the\n"
        "// measured input CSV and fixture revision recorded alongside this file.\n"
        "#include <array>\n#include <cstddef>\n\n"
        "namespace laser::calibration {\n"
        f"inline constexpr char kVersion[] = \"{fixture_revision}\";\n"
        f"inline constexpr std::array<float, {len(coefficients)}> kPixelToMm = {{{values}}};\n"
        "inline float pixelToMm(float pixel) {\n"
        "  float result = 0.0F;\n"
        "  for (std::size_t index = kPixelToMm.size(); index-- > 0U;) {\n"
        "    result = result * pixel + kPixelToMm[index];\n"
        "  }\n"
        "  return result;\n"
        "}\n}  // namespace laser::calibration\n",
        encoding="utf-8",
    )


def write_svg(path: Path, records: list[dict[str, Any]]) -> None:
    width, height, margin = 900, 500, 55
    xs = [float(record["observed_px"]) for record in records]
    ys = [float(record["physical_mm"]) for record in records] + [float(record["predicted_mm"]) for record in records]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax += 1.0
    if ymax == ymin:
        ymax += 1.0
    def sx(value: float) -> float:
        return margin + (value - xmin) / (xmax - xmin) * (width - 2 * margin)

    def sy(value: float) -> float:
        return height - margin - (value - ymin) / (ymax - ymin) * (height - 2 * margin)

    ordered = sorted(records, key=lambda item: float(item["observed_px"]))
    line = " ".join(f"{sx(float(item['observed_px'])):.1f},{sy(float(item['predicted_mm'])):.1f}" for item in ordered)
    circles = []
    for item in records:
        color = "#d62728" if not item["inlier"] else ("#ff7f0e" if item["split"] == "validation" else "#1f77b4")
        circles.append(f'<circle cx="{sx(float(item["observed_px"])):.1f}" cy="{sy(float(item["physical_mm"])):.1f}" r="4" fill="{color}"/>')
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        '<rect width="100%" height="100%" fill="white"/>\n'
        f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="black"/>\n'
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="black"/>\n'
        f'<polyline points="{line}" fill="none" stroke="#2ca02c" stroke-width="2"/>\n'
        + "\n".join(circles)
        + f'\n<text x="{width/2}" y="{height-10}" text-anchor="middle">observed pixel</text>\n'
        + f'<text x="18" y="{height/2}" transform="rotate(-90 18 {height/2})" text-anchor="middle">physical mm</text>\n'
        + '</svg>\n',
        encoding="utf-8",
    )


def fixture_revision_type(text: str) -> str:
    if not FIXTURE_REVISION_PATTERN.fullmatch(text):
        raise argparse.ArgumentTypeError(f"invalid fixture revision {text!r}: expected ^[A-Za-z0-9._-]{{1,64}}$")
    if text == UNSET_FIXTURE_REVISION:
        raise argparse.ArgumentTypeError(f"{UNSET_FIXTURE_REVISION} is the placeholder, not a fixture revision")
    return text


class DeprecatedVersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(f"{PROG}: warning: --version is deprecated, use --fixture-revision", file=sys.stderr)
        setattr(namespace, self.dest, values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=description(__doc__),
        epilog="Exit codes: 0 success (header only with --fixture-revision), 1 invalid input/fit, 2 usage error.",
    )
    parser.add_argument("input", type=Path, help="CSV with observed_px, physical_mm and optional split (fit|validation)")
    output_dir_arguments(parser, "directory for report.json, residuals.csv, calibration_plot.svg and the header")
    parser.add_argument("--degree", type=int, choices=(1, 2), default=1, help="polynomial degree (default 1)")
    parser.add_argument("--outlier-sigma", type=positive_float, default=3.5,
                        help="reject fit points beyond this many robust sigmas (default 3.5)")
    parser.add_argument("--fixture-revision", type=fixture_revision_type, default=None,
                        help="revision of the measured rigid fixture, ^[A-Za-z0-9._-]{1,64}$; required to write the header")
    parser.add_argument("--version", dest="fixture_revision", type=fixture_revision_type,
                        action=DeprecatedVersionAction, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = resolve_output_dir(parser, args)
    assert output_dir is not None
    try:
        rows = read_points(args.input)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise fail(PROG, f"{args.input}: {exc}") from None
    fit_indices = [index for index, row in enumerate(rows) if row.split != "validation"]
    if len(fit_indices) < args.degree + 2:
        raise fail(PROG, f"not enough fit points: {len(fit_indices)} (need {args.degree + 2})")
    fit_points = [(rows[index].observed_px, rows[index].physical_mm) for index in fit_indices]
    try:
        coefficients, fit_inliers = robust_fit(fit_points, args.degree, args.outlier_sigma)
    except ValueError as exc:
        raise fail(PROG, exc) from None
    if not all(math.isfinite(value) for value in coefficients):
        raise fail(PROG, f"fit produced non-finite coefficients: {coefficients}")
    global_inliers = [True] * len(rows)
    for index, include in zip(fit_indices, fit_inliers, strict=True):
        global_inliers[index] = include
    records: list[dict[str, Any]] = []
    for row, include in zip(rows, global_inliers, strict=True):
        prediction = evaluate(coefficients, row.observed_px)
        records.append({**asdict(row), "predicted_mm": prediction, "residual_mm": row.physical_mm - prediction, "inlier": include})
    fit_residuals = [row.physical_mm - evaluate(coefficients, row.observed_px)
                     for row, include in zip(rows, global_inliers, strict=True) if row.split != "validation" and include]
    validation_residuals = [row.physical_mm - evaluate(coefficients, row.observed_px)
                            for row in rows if row.split == "validation"]

    def metric(values: list[float]) -> dict[str, object]:
        return {"count": len(values), "rmse_mm": math.sqrt(sum(value * value for value in values) / len(values)) if values else None, "max_abs_mm": max(map(abs, values)) if values else None}

    provisional = args.fixture_revision is None
    report = {
        "tool_version": 2,
        "source_csv": str(args.input),
        "mapping": "physical_mm = c0 + c1*observed_px + c2*observed_px^2",
        "degree": args.degree,
        "fixture_revision": args.fixture_revision,
        "provisional": provisional,
        "header_written": not provisional,
        "coefficients": coefficients,
        "fit": metric(fit_residuals),
        "validation": metric(validation_residuals),
        "rejected_fit_points": sum(not value for value in fit_inliers),
        "warning": "Coefficients are metrology-valid only for measured rigid-fixture input data."
        + (" PROVISIONAL: no --fixture-revision given, firmware header not written." if provisional else ""),
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(json.dumps(json_safe(report), indent=2, allow_nan=False) + "\n", encoding="utf-8")
        with (output_dir / "residuals.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        header = output_dir / "calibration_coefficients.h"
        if provisional:
            if header.exists():
                header.unlink()
        else:
            write_header(header, coefficients, args.fixture_revision)
        write_svg(output_dir / "calibration_plot.svg", records)
    except (OSError, ValueError) as exc:
        raise fail(PROG, exc) from None
    print(json.dumps(json_safe(report), indent=2))
    if provisional:
        print(f"{PROG}: PROVISIONAL fit: calibration_coefficients.h NOT written; pass --fixture-revision "
              "<rigid-fixture-revision> once the data comes from a measured rigid fixture", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
