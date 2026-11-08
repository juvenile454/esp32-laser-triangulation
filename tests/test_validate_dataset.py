#!/usr/bin/env python3
"""Schema, hash, dimension and containment tests for tools/validate_dataset.py."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from validate_dataset import schema_errors, validate, validate_schema  # noqa: E402


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="laser-validate-test-")
        cls.base = Path(cls.temporary.name)
        cls.dataset = cls.base / "synthetic"
        subprocess.run(
            [sys.executable, str(TOOLS / "generate_synthetic_dataset.py"), "--output-dir", str(cls.dataset)],
            check=True, cwd=ROOT, capture_output=True,
        )
        cls.sample = sorted(cls.dataset.glob("synthetic_*"))[0]
        cls.payload = json.loads((cls.sample / "metadata.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def write(self, payload: dict, name: str = "candidate_metadata.json", directory: Path | None = None) -> Path:
        target = (directory or self.sample) / name
        target.write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(target.unlink, missing_ok=True)
        return target

    def calibration_grade(self) -> dict:
        payload = copy.deepcopy(self.payload)
        payload["evidence_tier"] = "calibration_grade_real"
        payload["capture"]["fixture_revision"] = "rigid-v1"
        payload["capture"]["timestamp_utc"] = "2026-08-13T16:53:14Z"
        payload["capture"]["camera"].update({"exposure": 784, "gain": 16, "white_balance_locked": True})
        payload["geometry"].update({"working_distance_mm": 120.0, "baseline_mm": 70.0})
        return payload

    def test_examples_and_generator_output_are_schema_valid(self) -> None:
        for example in sorted((ROOT / "examples").glob("*.json")):
            with self.subTest(example=example.name):
                self.assertEqual(schema_errors(json.loads(example.read_text(encoding="utf-8"))), [])
        for metadata in sorted(self.dataset.rglob("metadata.json")):
            with self.subTest(metadata=metadata.parent.name):
                validate(metadata)
        self.assertEqual(schema_errors(self.calibration_grade()), [])

    def test_schema_invalid_values_fail(self) -> None:
        cases = {
            "baseline string": ("geometry", "baseline_mm", "abc"),
            "empty labels": ("labels", None, {}),
            "bad timestamp": ("capture", "timestamp_utc", "not a date"),
            "local timestamp": ("capture", "timestamp_utc", "2026-08-13T16:53:14+02:00"),
            "uppercase hash": ("images", "laser_on", {"file": "laser_on.png", "sha256": "A" * 64, "format": "PNG"}),
            "short hash": ("images", "laser_on", {"file": "laser_on.png", "sha256": "ab" * 31, "format": "PNG"}),
            "unknown tier": (None, "evidence_tier", "trust_me"),
            "unknown field": (None, "extra", 1),
        }
        for name, (section, key, value) in cases.items():
            with self.subTest(name):
                payload = copy.deepcopy(self.payload)
                target = payload if section is None else payload
                if section is not None and key is None:
                    payload[section] = value
                else:
                    target = payload[section] if section is not None else payload
                    target[key] = value
                with self.assertRaisesRegex(ValueError, "schema violation"):
                    validate(self.write(payload))

    def test_impossible_date_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["capture"]["timestamp_utc"] = "2026-02-30T10:00:00Z"
        with self.assertRaisesRegex(ValueError, "real date-time"):
            validate_schema(payload)

    def test_calibration_grade_requires_known_fields(self) -> None:
        for section, key in (
            ("capture", "fixture_revision"),
            ("camera", "exposure"),
            ("camera", "gain"),
            ("camera", "white_balance_locked"),
            ("geometry", "working_distance_mm"),
            ("geometry", "baseline_mm"),
        ):
            with self.subTest(field=key):
                payload = self.calibration_grade()
                target = payload["capture"]["camera"] if section == "camera" else payload[section]
                target[key] = None
                self.assertNotEqual(schema_errors(payload), [])

    def test_hash_and_dimension_mismatch(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["images"]["laser_on"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            validate(self.write(payload))
        payload = copy.deepcopy(self.payload)
        payload["capture"]["camera"]["width_px"] += 1
        with self.assertRaisesRegex(ValueError, "dimension mismatch"):
            validate(self.write(payload))

    def test_missing_image_mentions_data_root(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["images"]["laser_on"]["file"] = "elsewhere/laser_on.png"
        with self.assertRaisesRegex(ValueError, "image not found.*--data-root"):
            validate(self.write(payload))

    def test_data_root_containment(self) -> None:
        detached = self.base / "detached"
        detached.mkdir(exist_ok=True)
        for file_value in ("../outside.png", str(self.sample / "laser_on.png")):
            with self.subTest(file=file_value):
                payload = copy.deepcopy(self.payload)
                payload["images"]["laser_on"]["file"] = file_value
                with self.assertRaisesRegex(ValueError, "escapes --data-root|absolute image path"):
                    validate(self.write(payload, directory=detached), self.dataset)
        payload = copy.deepcopy(self.payload)
        for state in ("laser_on", "laser_off"):
            payload["images"][state]["file"] = f"{self.sample.name}/{state}.png"
        validate(self.write(payload, directory=detached), self.dataset)

    def test_directory_mode_finds_prefixed_metadata(self) -> None:
        directory = self.base / "prefixed"
        directory.mkdir(exist_ok=True)
        payload = copy.deepcopy(self.payload)
        for state in ("laser_on", "laser_off"):
            payload["images"][state]["file"] = str(self.sample / f"{state}.png")
        (directory / "example_metadata.json").write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(TOOLS / "validate_dataset.py"), str(directory)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("example_metadata.json", result.stdout)
        empty = self.base / "empty"
        empty.mkdir(exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(TOOLS / "validate_dataset.py"), str(empty)], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
