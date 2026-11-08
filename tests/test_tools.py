#!/usr/bin/env python3
"""Regression tests for fit_calibration, triangulation_budget, seam_displacement and CLI error handling."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from fit_calibration import candidate_subsets, cpp_float_literal, robust_fit, write_header  # noqa: E402
from triangulation_budget import (  # noqa: E402
    LAYOUTS,
    TILTED_CAMERA,
    TILTED_LASER,
    evaluate,
    focal_length_from_shift,
    stripe_shift_px,
)


def run_tool(script: str, *argv: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(TOOLS / script), *argv], cwd=cwd, capture_output=True, text=True)


class FitCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="laser-fit-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def write_csv(self, rows: list[tuple[float, float, str]], header: str = "observed_px,physical_mm,split") -> Path:
        path = self.base / "points.csv"
        path.write_text(header + "\n" + "".join(f"{x},{y},{split}\n" for x, y, split in rows), encoding="utf-8")
        return path

    def linear_rows(self) -> list[tuple[float, float, str]]:
        rows = [(float(x), 1.0 + 0.05 * x + (0.002 if x % 40 else -0.002), "fit") for x in range(80, 281, 20)]
        return rows + [(150.0, 1.0 + 0.05 * 150, "validation")]

    def test_outlier_at_index_zero_is_rejected_for_large_inputs(self) -> None:
        points = [(float(x), 0.5 + 0.01 * x + 0.001 * x * x + (0.001 if x % 2 else -0.001)) for x in range(120)]
        points[0] = (0.0, 40.0)
        coefficients, inliers = robust_fit(points, 2, 3.5)
        self.assertFalse(inliers[0])
        self.assertAlmostEqual(coefficients[0], 0.5, delta=0.005)
        self.assertEqual(sum(not value for value in inliers), 1)

    def test_subset_enumeration_is_exhaustive_or_deterministic(self) -> None:
        self.assertEqual(len(list(candidate_subsets(10, 3))), math.comb(10, 3))
        sampled = list(candidate_subsets(120, 3))
        self.assertEqual(len(sampled), 5000)
        self.assertEqual(len(set(sampled)), 5000)
        self.assertEqual(sampled, list(candidate_subsets(120, 3)))
        self.assertTrue(any(0 not in subset for subset in sampled[:10]))

    def test_float_literals_always_compile(self) -> None:
        self.assertEqual(cpp_float_literal(2.0), "2.000000000e+00F")
        for bad in (math.nan, math.inf):
            with self.assertRaises(ValueError):
                cpp_float_literal(bad)
        with self.assertRaises(ValueError):
            write_header(self.base / "x.h", [1.0], 'x"; int evil = 1; //')

    @unittest.skipUnless(shutil.which("g++"), "g++ not available")
    def test_integer_coefficient_header_compiles(self) -> None:
        write_header(self.base / "calibration_coefficients.h", [2.0, 3.0], "rigid-v1")
        source = self.base / "smoke.cpp"
        source.write_text(
            '#include "calibration_coefficients.h"\n#include <cmath>\n#include <cstring>\n'
            'int main() { return std::fabs(laser::calibration::pixelToMm(10.0F) - 32.0F) < 1e-4F && '
            'std::strcmp(laser::calibration::kVersion, "rigid-v1") == 0 ? 0 : 1; }\n',
            encoding="utf-8",
        )
        binary = self.base / "smoke"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(self.base), str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)

    def test_header_requires_explicit_fixture_revision(self) -> None:
        source = self.write_csv(self.linear_rows())
        output = self.base / "out"
        result = run_tool("fit_calibration.py", str(source), "--output-dir", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PROVISIONAL", result.stderr)
        self.assertFalse((output / "calibration_coefficients.h").exists())
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["provisional"])
        self.assertIsNone(report["fixture_revision"])
        self.assertTrue((output / "residuals.csv").exists())

        result = run_tool("fit_calibration.py", str(source), str(output), "--fixture-revision", "rigid-v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('kVersion[] = "rigid-v1"', (output / "calibration_coefficients.h").read_text(encoding="utf-8"))
        self.assertFalse(json.loads((output / "report.json").read_text(encoding="utf-8"))["provisional"])

    def test_deprecated_version_alias_and_injection(self) -> None:
        source = self.write_csv(self.linear_rows())
        output = self.base / "alias"
        result = run_tool("fit_calibration.py", str(source), str(output), "--version", "rigid-v2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--version is deprecated", result.stderr)
        self.assertTrue((output / "calibration_coefficients.h").exists())
        for revision in ('x"; int evil = 1; //', "UNSET_FIXTURE_REVISION", "", "a" * 65):
            with self.subTest(revision=revision):
                result = run_tool("fit_calibration.py", str(source), str(self.base / "bad"), "--fixture-revision", revision)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_bad_csv_is_a_clean_error(self) -> None:
        source = self.write_csv([(1.0, 2.0, "fit")], header="px,mm,split")
        result = run_tool("fit_calibration.py", str(source), str(self.base / "o"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("observed_px,physical_mm", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        result = run_tool("fit_calibration.py", str(self.base / "missing.csv"), str(self.base / "o"))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


class TriangulationBudgetTests(unittest.TestCase):
    def test_audit_example_recovers_true_focal_length(self) -> None:
        shift = stripe_shift_px(105.0, 150.0, 375.0, 10.0, TILTED_CAMERA)
        self.assertAlmostEqual(focal_length_from_shift(105.0, 150.0, 10.0, shift), 375.0, places=9)
        # The old tilted-laser inversion applied to this layout gave ~245.9 px.
        self.assertAlmostEqual(focal_length_from_shift(105.0, 150.0, 10.0, shift, TILTED_LASER), 245.9, delta=0.1)

    def test_round_trip_both_layouts(self) -> None:
        for layout in LAYOUTS:
            for baseline, distance, focal, step in ((105.0, 150.0, 375.0, 10.0), (70.0, 400.0, 512.0, 20.0),
                                                     (30.0, 90.0, 1200.0, 0.5), (200.0, 120.0, 300.0, 60.0)):
                with self.subTest(layout=layout, baseline=baseline, distance=distance, step=step):
                    shift = stripe_shift_px(baseline, distance, focal, step, layout)
                    recovered = focal_length_from_shift(baseline, distance, step, shift, layout)
                    self.assertLess(abs(recovered - focal) / focal, 1e-6)

    def test_forward_model_matches_evaluate_sensitivity(self) -> None:
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                budget = evaluate(105.0, 150.0, 375.0, 0.22, 640, layout)
                step = 1e-4
                slope = stripe_shift_px(105.0, 150.0, 375.0, step, layout) / step
                self.assertAlmostEqual(slope, budget.height_sensitivity_px_per_mm, places=4)

    def test_json_has_schema_version_and_no_nan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "budget.json"
            result = run_tool("triangulation_budget.py", "--baseline-mm", "70", "--working-distance-mm", "400",
                              "--focal-length-px", "375", "--layout", "tilted-laser", "--json", str(target))
            self.assertEqual(result.returncode, 0, result.stderr)
            text = target.read_text(encoding="utf-8")
            self.assertNotIn("NaN", text)
            self.assertNotIn("Infinity", text)
            payload = json.loads(text)
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertIsNone(payload["budget"]["sensitivity_efficiency"])

    def test_cli_rejects_invalid_arguments_cleanly(self) -> None:
        base = ["--baseline-mm", "105", "--working-distance-mm", "150"]
        cases = [
            [*base, "--focal-length-px", "0"],
            [*base, "--focal-length-px", "-3"],
            [*base, "--focal-length-px", "nan"],
            [*base],
            [*base, "--focal-length-px", "375", "--from-measured-shift", "10", "12"],
            [*base, "--focal-length-mm", "3.6"],
            [*base, "--focal-length-px", "375", "--pixel-pitch-um", "3"],
            [*base, "--from-measured-shift", "150", "12"],
            [*base, "--from-measured-shift", "10", "0"],
            [*base, "--focal-length-px", "375", "--noise-px", "0"],
            [*base, "--focal-length-px", "375", "--target-accuracy-mm", "0"],
            ["--baseline-mm", "0", "--working-distance-mm", "150", "--focal-length-px", "375"],
            ["--baseline-mm", "10", "--working-distance-mm", "-1", "--focal-length-px", "375"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                result = run_tool("triangulation_budget.py", *argv)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("Traceback", result.stderr)


class SeamDisplacementTests(unittest.TestCase):
    def test_json_output_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "seam.json"
            result = run_tool("seam_displacement.py", "--json", str(target))
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(target.read_text(encoding="utf-8"), parse_constant=lambda name: self.fail(name))

    def test_json_safe_replaces_non_finite(self) -> None:
        from cli_common import json_safe

        self.assertEqual(json_safe({"a": [math.inf, 1.0, (math.nan,)]}), {"a": [None, 1.0, [None]]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
