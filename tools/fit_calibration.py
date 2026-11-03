#!/usr/bin/env python3
"""Fit and export a provisional pixel-to-physical calibration mapping.

Final coefficients are only valid when the input CSV comes from a measured,
rigid fixture. The tool itself can be smoke-tested with synthetic points.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
from pathlib import Path


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


def robust_fit(points: list[tuple[float, float]], degree: int, sigma: float, _max_iterations: int) -> tuple[list[float], list[bool]]:
    minimum = degree + 1
    if len(points) < minimum:
        raise ValueError("too few points for requested polynomial degree")

    # Start from the small subset whose polynomial minimizes the median
    # absolute residual. This keeps one gross outlier from tilting the first
    # least-squares fit and causing cascading false rejections.
    combinations = itertools.combinations(range(len(points)), minimum)
    best_coefficients: list[float] | None = None
    best_score = float("inf")
    for candidate_number, indices in enumerate(combinations):
        if candidate_number >= 5000:
            break
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


def read_points(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"observed_px", "physical_mm"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV requires observed_px,physical_mm and optional split columns")
        for line, row in enumerate(reader, 2):
            try:
                observed = float(row["observed_px"])
                physical = float(row["physical_mm"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid numeric value at CSV line {line}") from exc
            if not math.isfinite(observed) or not math.isfinite(physical):
                raise ValueError(f"non-finite value at CSV line {line}")
            rows.append({"observed_px": observed, "physical_mm": physical, "split": (row.get("split") or "fit").strip().lower()})
    return rows


def write_header(path: Path, coefficients: list[float], version: str) -> None:
    values = ", ".join(f"{value:.12g}F" for value in coefficients)
    path.write_text(
        "#pragma once\n\n"
        "// GENERATED calibration coefficients. Validity depends entirely on the\n"
        "// measured input CSV and fixture revision recorded alongside this file.\n"
        "#include <array>\n#include <cstddef>\n\n"
        "namespace laser::calibration {\n"
        f"inline constexpr char kVersion[] = \"{version}\";\n"
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


def write_svg(path: Path, records: list[dict[str, object]]) -> None:
    width, height, margin = 900, 500, 55
    xs = [float(record["observed_px"]) for record in records]
    ys = [float(record["physical_mm"]) for record in records] + [float(record["predicted_mm"]) for record in records]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax == xmin:
        xmax += 1.0
    if ymax == ymin:
        ymax += 1.0
    sx = lambda value: margin + (value - xmin) / (xmax - xmin) * (width - 2 * margin)
    sy = lambda value: height - margin - (value - ymin) / (ymax - ymin) * (height - 2 * margin)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--degree", type=int, choices=(1, 2), default=1)
    parser.add_argument("--outlier-sigma", type=float, default=3.5)
    parser.add_argument("--version", default="UNSET_FIXTURE_REVISION")
    args = parser.parse_args()
    rows = read_points(args.input)
    fit_indices = [index for index, row in enumerate(rows) if row["split"] != "validation"]
    if len(fit_indices) < args.degree + 2:
        raise SystemExit("Not enough fit points")
    fit_points = [(float(rows[index]["observed_px"]), float(rows[index]["physical_mm"])) for index in fit_indices]
    coefficients, fit_inliers = robust_fit(fit_points, args.degree, args.outlier_sigma, 8)
    global_inliers = [True] * len(rows)
    for index, include in zip(fit_indices, fit_inliers, strict=True):
        global_inliers[index] = include
    records = []
    for row, include in zip(rows, global_inliers, strict=True):
        prediction = evaluate(coefficients, float(row["observed_px"]))
        records.append({**row, "predicted_mm": prediction, "residual_mm": float(row["physical_mm"]) - prediction, "inlier": include})
    fit_residuals = [float(item["residual_mm"]) for item in records if item["split"] != "validation" and item["inlier"]]
    validation_residuals = [float(item["residual_mm"]) for item in records if item["split"] == "validation"]
    metric = lambda values: {"count": len(values), "rmse_mm": math.sqrt(sum(value * value for value in values) / len(values)) if values else None, "max_abs_mm": max(map(abs, values)) if values else None}
    report = {"tool_version": 1, "source_csv": str(args.input), "mapping": "physical_mm = c0 + c1*observed_px + c2*observed_px^2", "degree": args.degree, "fixture_revision": args.version, "coefficients": coefficients, "fit": metric(fit_residuals), "validation": metric(validation_residuals), "rejected_fit_points": sum(not value for value in fit_inliers), "warning": "Coefficients are metrology-valid only for measured rigid-fixture input data."}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (args.output_dir / "residuals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=records[0].keys())
        writer.writeheader(); writer.writerows(records)
    write_header(args.output_dir / "calibration_coefficients.h", coefficients, args.version)
    write_svg(args.output_dir / "calibration_plot.svg", records)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
