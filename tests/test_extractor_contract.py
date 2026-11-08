#!/usr/bin/env python3
"""Contract v4 checks for the Python reference extractor and the native CLI."""

from __future__ import annotations

import csv
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from build_profile_features import build_features, load_profile  # noqa: E402
from laser_pipeline import (  # noqa: E402
    CONTRACT_VERSION,
    ExtractorConfig,
    ProfilePoint,
    clamp_roi,
    extract_stripe,
    fit_seam_corner,
    load_rgb,
    save_diagnostics,
    save_profile_csv,
)

CLI = ROOT / "build/host/laser_cli"
WIDTH, HEIGHT = 160, 100
GAP_ROW = 45


def blank() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT), (30, 30, 30))


def add_stripe(image: Image.Image, y: int, center: int, strength: int = 190) -> None:
    """Same 5 px red profile as TestImage::stripe in tests/test_laser_core.cpp."""
    for dx in range(-2, 3):
        x = center + dx
        if 0 <= x < WIDTH:
            red, green, blue = image.getpixel((x, y))  # type: ignore[misc]
            image.putpixel((x, y), (min(255, red + strength - abs(dx) * 55), green, blue))


def gap_row_scene() -> tuple[Image.Image, Image.Image]:
    """Stripe at x=40 missing on one row, brighter blob at x=110 on rows 40-59."""
    on = blank()
    for y in range(HEIGHT):
        if y != GAP_ROW:
            add_stripe(on, y, 40, 150)
        if 40 <= y < 60:
            add_stripe(on, y, 110, 240)
    return on, blank()


def write_ppm(image: Image.Image, path: Path) -> None:
    rgb = image.convert("RGB")
    path.write_bytes(f"P6\n{rgb.width} {rgb.height}\n255\n".encode("ascii") + rgb.tobytes())


class ExtractorContractTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    base: Path
    dataset: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="laser-contract-test-")
        cls.base = Path(cls.temporary.name)
        cls.dataset = cls.base / "synthetic"
        subprocess.run(
            [sys.executable, str(TOOLS / "generate_synthetic_dataset.py"), str(cls.dataset)],
            check=True,
            cwd=ROOT,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.assertTrue(CLI.exists(), "run make all first")
        return subprocess.run([str(CLI), *arguments], cwd=ROOT, capture_output=True, text=True)

    def assert_parity(self, cpp_csv: Path, python_profile: list[ProfilePoint], label: str) -> None:
        with cpp_csv.open(newline="", encoding="utf-8") as handle:
            cpp_profile = list(csv.DictReader(handle))
        self.assertEqual(len(cpp_profile), len(python_profile), label)
        for row, (cpp, python) in enumerate(zip(cpp_profile, python_profile, strict=True)):
            expected = (
                python.valid, python.width_px, python.peak_score,
                python.saturated, python.secondary_candidate, python.too_wide,
            )
            actual = (
                cpp["valid"] == "1", int(cpp["width_px"]), int(cpp["peak_score"]),
                cpp["saturated"] == "1", cpp["secondary_candidate"] == "1", cpp["too_wide"] == "1",
            )
            self.assertEqual(actual, expected, f"{label} row {row}")
            # C++ centres are float32, Python's are double.
            self.assertAlmostEqual(float(cpp["x_px"]), python.x_px, delta=1e-4, msg=f"{label} row {row}")

    # -- tracking rule (contract v4) -------------------------------------

    def test_contract_version(self) -> None:
        self.assertEqual(CONTRACT_VERSION, 4)
        on, off = gap_row_scene()
        _profile, summary, _score = extract_stripe(on, off, ExtractorConfig())
        self.assertEqual(summary.contract_version, 4)

    def test_tracking_survives_gap_row(self) -> None:
        on, off = gap_row_scene()
        for seed in (40.0, -1.0):
            with self.subTest(initial_prediction_px=seed):
                profile, summary, _score = extract_stripe(
                    on, off, ExtractorConfig(prediction_window_px=12.0, initial_prediction_px=seed)
                )
                self.assertTrue(summary.valid)
                self.assertEqual(summary.valid_points, HEIGHT)
                # The row without the stripe still reports its only candidate ...
                self.assertAlmostEqual(profile[GAP_ROW].x_px, 110.0, delta=0.25)
                # ... but the blob never becomes the tracked line.
                for row, point in enumerate(profile):
                    if row != GAP_ROW:
                        self.assertTrue(point.valid, row)
                        self.assertAlmostEqual(point.x_px, 40.0, delta=0.25, msg=f"row {row}")

    def test_tracking_parity_gap_row(self) -> None:
        on, off = gap_row_scene()
        on_ppm, off_ppm = self.base / "gap_on.ppm", self.base / "gap_off.ppm"
        write_ppm(on, on_ppm)
        write_ppm(off, off_ppm)
        for seed in ("40", "-1"):
            with self.subTest(initial_prediction=seed):
                cpp_csv = self.base / f"gap_{seed}.csv"
                result = self.cli(str(on_ppm), str(off_ppm), str(cpp_csv), "20", "16", "12", seed)
                self.assertEqual(result.returncode, 0, result.stderr)
                python, _summary, _score = extract_stripe(
                    on, off, ExtractorConfig(prediction_window_px=12.0, initial_prediction_px=float(seed))
                )
                self.assertAlmostEqual(python[GAP_ROW + 1].x_px, 40.0, delta=0.25)
                self.assert_parity(cpp_csv, python, f"gap seed={seed}")

    def test_tracking_parity_all_fixtures(self) -> None:
        samples = sorted(self.dataset.glob("synthetic_*"))
        self.assertEqual(len(samples), 12)
        for sample in samples:
            with self.subTest(sample=sample.name):
                cpp_csv = self.base / f"tracked_{sample.name}.csv"
                result = self.cli(
                    str(sample / "laser_on.ppm"), str(sample / "laser_off.ppm"), str(cpp_csv), "20", "16", "12.5"
                )
                self.assertIn(result.returncode, (0, 3), result.stderr)
                python, summary, _score = extract_stripe(
                    load_rgb(sample / "laser_on.png"), load_rgb(sample / "laser_off.png"),
                    ExtractorConfig(prediction_window_px=12.5),
                )
                self.assertEqual(result.returncode, 0 if summary.valid else 3)
                self.assert_parity(cpp_csv, python, sample.name)

    def test_on_only_parity(self) -> None:
        for suffix in ("straight", "ghost", "clipped_white_core"):
            with self.subTest(suffix=suffix):
                sample = next(self.dataset.glob(f"*_{suffix}"))
                cpp_csv = self.base / f"on_only_{suffix}.csv"
                result = self.cli(str(sample / "laser_on.ppm"), "-", str(cpp_csv))
                python, summary, _score = extract_stripe(load_rgb(sample / "laser_on.png"), None, ExtractorConfig())
                self.assertEqual(result.returncode, 0 if summary.valid else 3, result.stderr)
                self.assert_parity(cpp_csv, python, f"on-only {suffix}")

    # -- configuration validation ---------------------------------------

    def test_non_finite_config_rejected(self) -> None:
        on, off = gap_row_scene()
        nan, inf = math.nan, math.inf
        bad: list[dict[str, float]] = [
            {"ghost_ratio": nan}, {"ghost_ratio": inf}, {"ghost_ratio": 1.5}, {"ghost_ratio": -0.1},
            {"minimum_valid_ratio": nan}, {"minimum_valid_ratio": -inf}, {"minimum_valid_ratio": 1.01},
            {"prediction_window_px": nan}, {"prediction_window_px": inf}, {"prediction_window_px": -1.0},
            {"initial_prediction_px": nan}, {"initial_prediction_px": inf}, {"initial_prediction_px": -inf},
            {"score_threshold": 0}, {"max_stripe_width_px": 0},
        ]
        for overrides in bad:
            with self.subTest(**{key: str(value) for key, value in overrides.items()}):
                with self.assertRaises(ValueError):
                    extract_stripe(on, off, ExtractorConfig(**overrides))  # type: ignore[arg-type]
        # Closed bounds stay accepted.
        extract_stripe(on, off, ExtractorConfig(ghost_ratio=1.0, minimum_valid_ratio=0.0))

    def test_nan_ratio_cannot_bypass_coverage_gate(self) -> None:
        on = blank()
        for y in range(5):
            add_stripe(on, y, 40)
        _profile, summary, _score = extract_stripe(on, blank(), ExtractorConfig())
        self.assertFalse(summary.valid)
        self.assertEqual(summary.invalid_reason, "insufficient_coverage")
        with self.assertRaises(ValueError):
            extract_stripe(on, blank(), ExtractorConfig(minimum_valid_ratio=math.nan))

    # -- seam fit --------------------------------------------------------

    def test_seam_fit_skips_non_finite_and_bounds_count(self) -> None:
        sample = next(self.dataset.glob("*_fillet_corner"))
        profile, _summary, _score = extract_stripe(
            load_rgb(sample / "laser_on.png"), load_rgb(sample / "laser_off.png"), ExtractorConfig()
        )
        reference = fit_seam_corner(profile)
        assert reference is not None
        poisoned = list(profile)
        poisoned[30] = ProfilePoint(math.nan, 30, 100, 5, True, False, False)
        poisoned[200] = ProfilePoint(math.inf, 200, 100, 5, True, False, False)
        corner = fit_seam_corner(poisoned)
        assert corner is not None
        self.assertTrue(corner.valid)
        self.assertTrue(math.isfinite(corner.x_px) and math.isfinite(corner.y_px))
        self.assertAlmostEqual(corner.x_px, reference.x_px, delta=0.5)
        self.assertEqual(
            corner.first_leg_points + corner.second_leg_points,
            reference.first_leg_points + reference.second_leg_points - 2,
        )
        self.assertIsNone(fit_seam_corner([ProfilePoint(math.nan, y, 1, 1, True, False, False) for y in range(64)]))
        self.assertIsNone(fit_seam_corner([ProfilePoint(10.0, y % 65536, 1, 1, True, False, False) for y in range(65536)]))

    # -- CSV and diagnostics --------------------------------------------

    def test_profile_csv_uses_cli_boolean_encoding(self) -> None:
        sample = next(self.dataset.glob("*_fillet_like"))
        profile, _summary, _score = extract_stripe(
            load_rgb(sample / "laser_on.png"), load_rgb(sample / "laser_off.png"), ExtractorConfig()
        )
        python_csv = self.base / "python_profile.csv"
        save_profile_csv(python_csv, profile)
        with python_csv.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            list(rows[0]),
            ["x_px", "y_px", "peak_score", "width_px", "valid", "saturated", "secondary_candidate", "too_wide"],
        )
        for column in ("valid", "saturated", "secondary_candidate", "too_wide"):
            self.assertTrue({row[column] for row in rows} <= {"0", "1"}, column)
        self.assertIn("1", {row["valid"] for row in rows})
        # The downstream feature builder must still read the 1/0 encoding.
        loaded = load_profile(python_csv)
        self.assertEqual(sum(bool(row["valid"]) for row in loaded), sum(point.valid for point in profile))
        self.assertEqual(build_features(loaded)["total_features"], 69)

    def test_diagnostics_draw_the_clamped_roi(self) -> None:
        on, off = gap_row_scene()
        config = ExtractorConfig(roi_x0=10, roi_y0=20, roi_x1=10_000, roi_y1=10_000)
        self.assertEqual(clamp_roi(config, WIDTH, HEIGHT), (10, 20, WIDTH, HEIGHT))
        profile, summary, score = extract_stripe(on, off, config)
        self.assertEqual(summary.profile_points, HEIGHT - 20)
        output = self.base / "diagnostics"
        save_diagnostics(output, on, profile, summary, score, config)
        overlay = load_rgb(output / "overlay.png")
        # Right and bottom edges of the scanned ROI lie inside the frame.
        self.assertEqual(overlay.getpixel((WIDTH - 1, 60)), (0, 255, 255))
        self.assertEqual(overlay.getpixel((80, HEIGHT - 1)), (0, 255, 255))

    # -- CLI contract ----------------------------------------------------

    def test_cli_exit_codes_and_arguments(self) -> None:
        sample = next(self.dataset.glob("*_straight"))
        on, off = str(sample / "laser_on.ppm"), str(sample / "laser_off.ppm")
        out = str(self.base / "cli.csv")

        self.assertEqual(self.cli(on, off).returncode, 2)  # too few arguments
        for arguments in (
            ("12abc",), ("0",), ("256",), ("-5",), ("20", "4294967297"), ("20", "16", "nan"),
            ("20", "16", "-1"), ("20", "16", "inf"), ("20", "16", "12", "4e40"), ("20", "16", "12", "x"),
        ):
            with self.subTest(arguments=arguments):
                result = self.cli(on, off, out, *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("ERROR invalid", result.stderr)

        self.assertEqual(self.cli(on, off, out, "20", "16", "12.5", "-1").returncode, 0)
        self.assertEqual(self.cli(on, off, out, "20", "16", "0", "41.25").returncode, 0)

        # I/O errors.
        self.assertEqual(self.cli(str(self.base / "missing.ppm"), off, out).returncode, 1)
        self.assertEqual(self.cli(on, off, str(self.base / "no_such_dir" / "x.csv")).returncode, 1)
        overflow = self.base / "overflow.ppm"
        overflow.write_bytes(b"P6\n4294967297 1\n255\n" + bytes(3))
        result = self.cli(str(overflow), "-", out)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid PPM width", result.stderr)
        trailing = self.base / "trailing.ppm"
        trailing.write_bytes(b"P6\n1x 1\n255\n" + bytes(3))
        self.assertEqual(self.cli(str(trailing), "-", out).returncode, 1)

        # Invalid measurement: CSV still written, exit 3.
        empty = next(self.dataset.glob("*_no_stripe"))
        empty_csv = self.base / "empty.csv"
        result = self.cli(str(empty / "laser_on.ppm"), str(empty / "laser_off.ppm"), str(empty_csv))
        self.assertEqual(result.returncode, 3)
        self.assertIn("reason=no_stripe", result.stdout)
        self.assertTrue(empty_csv.exists())

        # Core call error (ON/OFF size mismatch): exit 3 with EXTRACTION_ERROR.
        small = self.base / "small.ppm"
        small.write_bytes(b"P6\n2 2\n255\n" + bytes(12))
        result = self.cli(on, str(small), out)
        self.assertEqual(result.returncode, 3)
        self.assertIn("EXTRACTION_ERROR dimension_mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
