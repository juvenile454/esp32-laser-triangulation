#!/usr/bin/env python3
"""Executed host-side verification for the chip-free preparation path."""

from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from generate_synthetic_dataset import (  # noqa: E402
    CORNER_X,
    CORNER_Y,
    FIRST_LEG_SLOPE,
    SECOND_LEG_SLOPE,
)
from laser_pipeline import (  # noqa: E402
    ExtractorConfig,
    extract_stripe,
    fit_seam_corner,
    load_rgb,
)


class HostPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="laser-host-test-")
        cls.base = Path(cls.temporary.name)
        cls.dataset = cls.base / "synthetic"
        subprocess.run(
            [sys.executable, str(TOOLS / "generate_synthetic_dataset.py"), str(cls.dataset)],
            check=True,
            cwd=ROOT,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def sample(self, suffix: str) -> Path:
        matches = list(self.dataset.glob(f"*_{suffix}"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    @staticmethod
    def truth(path: Path) -> list[float | None]:
        with path.open(newline="", encoding="utf-8") as handle:
            return [float(row["x_px"]) if row["valid"] == "true" else None for row in csv.DictReader(handle)]

    def extract(self, suffix: str, config: ExtractorConfig | None = None):
        sample = self.sample(suffix)
        on = load_rgb(sample / "laser_on.png")
        off = load_rgb(sample / "laser_off.png")
        return (*extract_stripe(on, off, config or ExtractorConfig()), self.truth(sample / "truth.csv"))

    def test_exact_truth_scenarios(self) -> None:
        for suffix in ("straight", "step", "fillet_like", "saturated", "clipped_white_core", "clipped_warm_core"):
            with self.subTest(suffix=suffix):
                profile, summary, _score, truth = self.extract(suffix)
                errors = [abs(point.x_px - expected) for point, expected in zip(profile, truth, strict=True) if point.valid and expected is not None]
                self.assertTrue(summary.valid)
                self.assertGreater(summary.valid_ratio, 0.98)
                self.assertLess(math.sqrt(sum(error * error for error in errors) / len(errors)), 0.35)

    def test_declared_failure_modes(self) -> None:
        profile, summary, _score, truth = self.extract("occlusion_gap")
        self.assertTrue(summary.valid)
        self.assertEqual(summary.valid_points, sum(value is not None for value in truth))
        self.assertTrue(all(not profile[index].valid for index in range(80, 125)))

        _profile, ghost_summary, _score, _truth = self.extract("ghost")
        self.assertTrue(ghost_summary.valid)
        self.assertGreater(ghost_summary.secondary_candidate_points, 220)

        clipped_profile, clipped_summary, _score, clipped_truth = self.extract("clipped_white_core")
        self.assertTrue(clipped_summary.valid)
        self.assertGreater(clipped_summary.valid_ratio, 0.98)
        self.assertEqual(clipped_summary.secondary_candidate_points, 0)
        clipped_errors = [
            abs(point.x_px - expected)
            for point, expected in zip(clipped_profile, clipped_truth, strict=True)
            if point.valid and expected is not None
        ]
        self.assertLess(math.sqrt(sum(error * error for error in clipped_errors) / len(clipped_errors)), 0.5)

        warm_profile, warm_summary, _score, warm_truth = self.extract("clipped_warm_core")
        self.assertTrue(warm_summary.valid)
        self.assertGreater(warm_summary.valid_ratio, 0.98)
        self.assertEqual(warm_summary.secondary_candidate_points, 0)
        warm_errors = [
            abs(point.x_px - expected)
            for point, expected in zip(warm_profile, warm_truth, strict=True)
            if point.valid and expected is not None
        ]
        self.assertLess(math.sqrt(sum(error * error for error in warm_errors) / len(warm_errors)), 0.5)

        # Contract v3 separates "only over-wide candidates" from "nothing there".
        _profile, broad_summary, _score, _truth = self.extract(
            "weak_broad", ExtractorConfig(max_stripe_width_px=8)
        )
        self.assertFalse(broad_summary.valid)
        self.assertEqual(broad_summary.invalid_reason, "all_clusters_too_wide")

        _profile, no_summary, _score, _truth = self.extract("no_stripe")
        self.assertFalse(no_summary.valid)
        self.assertEqual(no_summary.invalid_reason, "no_stripe")

    def test_broad_blob_does_not_suppress_stripe(self) -> None:
        """Regression for the documented contract order (width filter first)."""
        profile, summary, _score, truth = self.extract("blob_and_stripe")
        self.assertTrue(summary.valid)
        self.assertEqual(summary.valid_points, summary.profile_points)
        self.assertEqual(summary.too_wide_points, 0)
        errors = [
            abs(point.x_px - expected)
            for point, expected in zip(profile, truth, strict=True)
            if point.valid and expected is not None
        ]
        self.assertLess(max(errors), 0.5)
        self.assertTrue(all(point.width_px <= 16 for point in profile if point.valid))

    def test_all_clusters_too_wide_is_distinct(self) -> None:
        """A glare field must not look like an unlit scene."""
        _profile, summary, _score, _truth = self.extract(
            "weak_broad", ExtractorConfig(max_stripe_width_px=8)
        )
        self.assertFalse(summary.valid)
        self.assertEqual(summary.invalid_reason, "all_clusters_too_wide")
        self.assertGreater(summary.too_wide_points, 0)

    def test_seam_corner_fit(self) -> None:
        profile, summary, _score, _truth = self.extract("fillet_corner")
        self.assertTrue(summary.valid)
        corner = fit_seam_corner(profile)
        self.assertIsNotNone(corner)
        self.assertTrue(corner.valid)
        self.assertAlmostEqual(corner.x_px, CORNER_X, delta=0.5)
        self.assertAlmostEqual(corner.y_px, float(CORNER_Y), delta=1.0)
        self.assertLess(corner.rms_px, 1.0)
        expected_deviation = abs(
            math.degrees(math.atan(SECOND_LEG_SLOPE)) - math.degrees(math.atan(FIRST_LEG_SLOPE))
        )
        self.assertAlmostEqual(180.0 - corner.included_angle_deg, expected_deviation, delta=1.0)

        straight_profile, _summary, _score, _truth = self.extract("straight")
        straight = fit_seam_corner(straight_profile)
        self.assertIsNotNone(straight)
        self.assertFalse(straight.valid)
        self.assertGreater(straight.included_angle_deg, 170.0)

        self.assertIsNone(fit_seam_corner(profile[:4]))

    def test_prediction_window_keeps_continuity(self) -> None:
        """The ghost fixture has a strong second line; tracking must ignore it."""
        sample = self.sample("ghost")
        on = load_rgb(sample / "laser_on.png")
        off = load_rgb(sample / "laser_off.png")
        truth = self.truth(sample / "truth.csv")
        tracked, summary, _score = extract_stripe(
            on,
            off,
            ExtractorConfig(prediction_window_px=10.0, initial_prediction_px=truth[0]),
        )
        self.assertTrue(summary.valid)
        errors = [
            abs(point.x_px - expected)
            for point, expected in zip(tracked, truth, strict=True)
            if point.valid and expected is not None
        ]
        self.assertLess(max(errors), 0.5)

    def test_cpp_python_parity(self) -> None:
        executable = ROOT / "build/host/laser_cli"
        self.assertTrue(executable.exists(), "run make all first")
        for suffix in ("straight", "step", "fillet_like", "occlusion_gap", "ghost", "saturated", "clipped_white_core", "clipped_warm_core", "fillet_corner", "blob_and_stripe"):
            with self.subTest(suffix=suffix):
                sample = self.sample(suffix)
                cpp_csv = self.base / f"cpp_{suffix}.csv"
                subprocess.run(
                    [str(executable), str(sample / "laser_on.ppm"), str(sample / "laser_off.ppm"), str(cpp_csv)],
                    check=True,
                    cwd=ROOT,
                )
                on = load_rgb(sample / "laser_on.png")
                off = load_rgb(sample / "laser_off.png")
                python_profile, _summary, _score = extract_stripe(on, off, ExtractorConfig())
                with cpp_csv.open(newline="", encoding="utf-8") as handle:
                    cpp_profile = list(csv.DictReader(handle))
                self.assertEqual(len(cpp_profile), len(python_profile))
                for cpp, python in zip(cpp_profile, python_profile, strict=True):
                    self.assertEqual(cpp["valid"] == "1", python.valid)
                    self.assertAlmostEqual(float(cpp["x_px"]), python.x_px, delta=1e-5)
                    self.assertEqual(int(cpp["width_px"]), python.width_px)
                    self.assertEqual(int(cpp["peak_score"]), python.peak_score)
                    self.assertEqual(cpp["saturated"] == "1", python.saturated)
                    self.assertEqual(cpp["secondary_candidate"] == "1", python.secondary_candidate)
                    self.assertEqual(cpp["too_wide"] == "1", python.too_wide)

    def test_metadata_files_and_hashes(self) -> None:
        subprocess.run(
            [sys.executable, str(TOOLS / "validate_dataset.py"), str(self.dataset)],
            check=True,
            cwd=ROOT,
        )

    def test_metadata_relative_paths_resolve_against_data_root(self) -> None:
        sample = sorted(self.dataset.glob("synthetic_*"))[0]
        payload = json.loads((sample / "metadata.json").read_text(encoding="utf-8"))
        for state in ("laser_on", "laser_off"):
            name = Path(payload["images"][state]["file"]).name
            payload["images"][state]["file"] = f"{sample.name}/{name}"
        detached = self.base / "detached"
        detached.mkdir(exist_ok=True)
        metadata = detached / "example_metadata.json"
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        validator = [sys.executable, str(TOOLS / "validate_dataset.py"), str(metadata)]
        without_root = subprocess.run(validator, cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(without_root.returncode, 0)
        with_root = subprocess.run(
            [*validator, "--data-root", str(self.dataset)], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(with_root.returncode, 0, with_root.stdout + with_root.stderr)

    def test_calibration_fit_outlier_and_validation(self) -> None:
        source = self.base / "calibration.csv"
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("observed_px", "physical_mm", "split"))
            for pixel in range(80, 281, 20):
                measured = -4.0 + 0.075 * pixel + (0.004 if pixel % 40 else -0.004)
                writer.writerow((pixel, measured, "fit"))
            writer.writerow((190, 50.0, "fit"))
            writer.writerow((110, -4.0 + 0.075 * 110, "validation"))
            writer.writerow((250, -4.0 + 0.075 * 250, "validation"))
        destination = self.base / "calibration_output"
        subprocess.run(
            [sys.executable, str(TOOLS / "fit_calibration.py"), str(source), str(destination), "--degree", "1", "--version", "synthetic-test"],
            check=True,
            cwd=ROOT,
        )
        report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(report["rejected_fit_points"], 1)
        self.assertAlmostEqual(report["coefficients"][0], -4.0, delta=0.03)
        self.assertAlmostEqual(report["coefficients"][1], 0.075, delta=0.0002)
        self.assertLess(report["validation"]["rmse_mm"], 0.02)
        self.assertTrue((destination / "calibration_coefficients.h").exists())
        self.assertTrue((destination / "calibration_plot.svg").exists())
        smoke_source = self.base / "calibration_header_smoke.cpp"
        smoke_source.write_text(
            '#include "calibration_coefficients.h"\n'
            '#include <cmath>\n'
            'int main() { return std::fabs(laser::calibration::pixelToMm(200.0F) - 11.0F) < 0.02F ? 0 : 1; }\n',
            encoding="utf-8",
        )
        smoke_binary = self.base / "calibration_header_smoke"
        subprocess.run(
            ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(destination), str(smoke_source), "-o", str(smoke_binary)],
            check=True,
            cwd=ROOT,
        )
        subprocess.run([str(smoke_binary)], check=True, cwd=ROOT)

    def test_triangulation_budget_tilted_laser(self) -> None:
        from triangulation_budget import (
            TILTED_LASER,
            evaluate,
            focal_length_from_shift,
            required_baseline,
            required_distance,
        )

        budget = evaluate(
            baseline_mm=70.0, working_distance_mm=400.0,
            focal_length_px=375.0, noise_px=0.22, image_width_px=640,
            layout=TILTED_LASER,
        )
        self.assertAlmostEqual(budget.triangulation_angle_deg, 9.93, delta=0.02)
        self.assertAlmostEqual(budget.mm_per_px, 400.0 / 375.0, places=6)
        self.assertAlmostEqual(
            budget.height_sensitivity_px_per_mm, 375.0 * 70.0 / 400.0 ** 2, places=9
        )

        baseline = required_baseline(400.0, 375.0, 0.22, 0.1, TILTED_LASER)
        self.assertAlmostEqual(
            evaluate(baseline, 400.0, 375.0, 0.22, 640, TILTED_LASER).height_noise_mm,
            0.1, places=9,
        )
        distance = required_distance(70.0, 375.0, 0.22, 0.1, TILTED_LASER)
        self.assertAlmostEqual(
            evaluate(70.0, distance, 375.0, 0.22, 640, TILTED_LASER).height_noise_mm,
            0.1, places=9,
        )

        # A synthetic step must round-trip back to the focal length that made it.
        near, far, baseline_mm, focal_px = 380.0, 400.0, 70.0, 375.0
        shift = focal_px * baseline_mm * (1.0 / near - 1.0 / far)
        self.assertAlmostEqual(
            focal_length_from_shift(baseline_mm, far, far - near, shift),
            focal_px, places=6,
        )

    def test_triangulation_budget_tilted_camera(self) -> None:
        from triangulation_budget import (
            TILTED_CAMERA,
            best_distance,
            evaluate,
            required_baseline,
            required_distance,
        )

        def noise_at(baseline: float, distance: float) -> float:
            return evaluate(baseline, distance, 375.0, 0.22, 640, TILTED_CAMERA).height_noise_mm

        budget = evaluate(
            baseline_mm=105.0, working_distance_mm=150.0,
            focal_length_px=375.0, noise_px=0.22, image_width_px=640,
        )
        self.assertAlmostEqual(budget.triangulation_angle_deg, 34.99, delta=0.02)
        self.assertAlmostEqual(budget.camera_distance_mm, math.hypot(105.0, 150.0), places=6)
        self.assertAlmostEqual(
            budget.height_sensitivity_px_per_mm,
            375.0 * 105.0 / (105.0 ** 2 + 150.0 ** 2),
            places=9,
        )
        # Scale follows the camera-to-stripe distance, not the vertical drop.
        self.assertAlmostEqual(budget.mm_per_px, budget.camera_distance_mm / 375.0, places=9)

        # Sensitivity peaks at 45 degrees, where the baseline equals the distance.
        self.assertLess(noise_at(150.0, 150.0), noise_at(105.0, 150.0))
        self.assertLess(noise_at(150.0, 150.0), noise_at(214.0, 150.0))
        self.assertAlmostEqual(
            evaluate(150.0, 150.0, 375.0, 0.22, 640).sensitivity_efficiency, 1.0, places=9
        )
        # At the optimum the noise is 2 * Z * sigma / f.
        self.assertAlmostEqual(noise_at(150.0, 150.0), 2 * 150.0 * 0.22 / 375.0, places=9)

        # Distance dominates: thirds the distance, thirds the noise at fixed angle.
        self.assertAlmostEqual(noise_at(50.0, 50.0), noise_at(150.0, 150.0) / 3.0, places=9)

        # Solvers invert the forward model.
        baseline = required_baseline(150.0, 375.0, 0.22, 0.2, TILTED_CAMERA)
        self.assertAlmostEqual(noise_at(baseline, 150.0), 0.2, places=9)
        distance = required_distance(105.0, 375.0, 0.22, 0.2, TILTED_CAMERA)
        self.assertAlmostEqual(noise_at(105.0, distance), 0.2, places=9)

        # Beyond the optimum the target is simply unreachable, not a wrong number.
        self.assertIsNone(required_baseline(400.0, 375.0, 0.22, 0.1, TILTED_CAMERA))
        optimum = best_distance(375.0, 0.22, 0.1)
        self.assertAlmostEqual(noise_at(optimum, optimum), 0.1, places=9)

        with self.assertRaises(ValueError):
            evaluate(105.0, 150.0, 375.0, 0.22, 640, "sideways")
        for bad in ({"baseline_mm": 0.0}, {"working_distance_mm": -1.0}, {"focal_length_px": 0.0}):
            arguments = {
                "baseline_mm": 70.0, "working_distance_mm": 400.0,
                "focal_length_px": 375.0, "noise_px": 0.22, "image_width_px": 640,
            }
            arguments.update(bad)
            with self.assertRaises(ValueError):
                evaluate(**arguments)

    def test_seam_displacement_observability(self) -> None:
        """The teach-and-compare scheme: what it recovers and what it cannot."""
        from seam_displacement import (
            corner_precision_penalty,
            norm,
            observability,
            observe,
            profile_corner_angle_deg,
            scenario_crossed_but_centred,
            scenario_mixed_seams,
            scenario_parallel_seams,
            solve_displacement,
            subtract,
        )

        origin = (0.0, 110.0, 0.0)
        translation = (1.8, -0.6, 0.4)
        rotation = (0.0, 0.0, 0.0025)

        # A shift straight along a seam moves nothing in that seam's scan.
        single = scenario_mixed_seams()[0]
        blind = observe(single, single.direction, (0.0, 0.0, 0.0), origin)
        self.assertAlmostEqual(blind[0], 0.0, places=12)
        self.assertAlmostEqual(blind[1], 0.0, places=12)

        # Ten parallel seams never fill that gap in.
        rank, null_basis = observability(scenario_parallel_seams(), origin, translation_only=True)
        self.assertEqual(rank, 2)
        self.assertEqual(len(null_basis), 1)
        self.assertAlmostEqual(abs(null_basis[0][0]), 1.0, places=9)

        # Differing directions alone are not sufficient when the scans are centred.
        rank, null_basis = observability(scenario_crossed_but_centred(), origin)
        self.assertEqual(rank, 5)
        self.assertEqual(len(null_basis), 1)
        self.assertAlmostEqual(abs(null_basis[0][5]), 1.0, places=6)  # blind to rz

        # A spread-out set recovers all six degrees of freedom exactly.
        mixed = scenario_mixed_seams()
        rank, null_basis = observability(mixed, origin)
        self.assertEqual(rank, 6)
        self.assertEqual(null_basis, [])
        solved_t, solved_r = solve_displacement(
            mixed, [observe(s, translation, rotation, origin) for s in mixed], origin
        )
        self.assertLess(norm(subtract(solved_t, translation)), 1e-9)
        self.assertLess(norm(subtract(solved_r, rotation)), 1e-12)

        # Two non-parallel seams already pin down a pure translation.
        two = [mixed[0], mixed[2]]
        solved_t, _ = solve_displacement(
            two, [observe(s, translation, (0.0, 0.0, 0.0), origin) for s in two],
            origin, translation_only=True,
        )
        self.assertLess(norm(subtract(solved_t, translation)), 1e-9)

        # Corner sharpness, and therefore precision, does depend on orientation.
        horizontal, upright = (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)
        perpendicular = profile_corner_angle_deg(horizontal, upright, (1.0, 0.0, 0.0))
        self.assertAlmostEqual(perpendicular, 90.0, places=6)
        self.assertAlmostEqual(corner_precision_penalty(perpendicular), 1.0, places=9)
        shallow = profile_corner_angle_deg(
            horizontal, upright, (0.259, 0.683, 0.683)
        )
        self.assertLess(shallow, 35.0)
        self.assertGreater(corner_precision_penalty(shallow), 1.7)

    def test_base_plate_locating(self) -> None:
        """Locating the fixture plate is better conditioned than scanning welds."""
        from seam_displacement import (
            condition_estimate,
            norm,
            observability,
            observe,
            planar_condition,
            scenario_base_plate_corners,
            scenario_mixed_seams,
            solve_displacement,
            subtract,
        )

        base = (0.0, 0.0, 0.0)
        translation = (1.8, -0.6, 0.4)
        rotation = (0.0, 0.0, 0.0025)
        plate = scenario_base_plate_corners()

        rank, null_basis = observability(plate, base)
        self.assertEqual(rank, 6)
        self.assertEqual(null_basis, [])

        solved_t, solved_r = solve_displacement(
            plate, [observe(s, translation, rotation, base) for s in plate], base
        )
        self.assertLess(norm(subtract(solved_t, translation)), 1e-9)
        self.assertLess(norm(subtract(solved_r, rotation)), 1e-12)

        # Better conditioned than the seam scenario, and both average error down.
        plate_gain = condition_estimate(plate, base)
        seam_gain = condition_estimate(scenario_mixed_seams(), (0.0, 110.0, 0.0))
        self.assertLess(plate_gain, seam_gain)
        self.assertLess(plate_gain, 1.0)

        # In-plane motion is what a bolted plate actually does.
        planar_rank, planar_gain = planar_condition(plate, base)
        self.assertEqual(planar_rank, 3)
        self.assertLess(planar_gain, 1.0)

        # Two opposite corners still work; one corner cannot fix an orientation.
        two_corners = [plate[0], plate[4], plate[3], plate[7]]
        self.assertEqual(observability(two_corners, base)[0], 6)
        self.assertEqual(planar_condition(two_corners, base)[0], 3)

        single = [plate[0], plate[4]]
        self.assertEqual(observability(single, base)[0], 4)
        single_rank, single_gain = planar_condition(single, base)
        self.assertEqual(single_rank, 2)
        self.assertEqual(single_gain, float("inf"))

    def test_feature_contract(self) -> None:
        sample = self.sample("fillet_like")
        analysis = self.base / "feature_analysis"
        subprocess.run(
            [sys.executable, str(TOOLS / "analyze_laser_pair.py"), "--on", str(sample / "laser_on.png"), "--off", str(sample / "laser_off.png"), "--output-dir", str(analysis)],
            check=True,
            cwd=ROOT,
        )
        output = self.base / "features.json"
        subprocess.run(
            [sys.executable, str(TOOLS / "build_profile_features.py"), str(analysis / "profile.csv"), str(output)],
            check=True,
            cwd=ROOT,
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "provisional_feature_pipeline_smoke_test_only")
        self.assertEqual(payload["total_features"], 69)
        self.assertEqual(len(payload["values"]), 69)
        self.assertTrue(all(math.isfinite(float(value)) for value in payload["values"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
