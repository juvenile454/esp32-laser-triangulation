#!/usr/bin/env python3
"""capture_pair / capture_frame against a scripted fake board (no hardware)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import capture_frame
import capture_pair
from fakes import FakeBoard, FakeSerial
from serial_protocol import LASER_OFF_COMMAND
from validate_dataset import validate


def run(main, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code is not None:
                stderr.write(str(exc.code))
    return code, stdout.getvalue(), stderr.getvalue()


class CapturePairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="laser-capture-test-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "captures"
        self.board = FakeBoard(eol=b"\r\n")
        patcher = mock.patch.object(capture_pair, "open_port", side_effect=lambda *a, **k: self.board.attach())
        self.open_port = patcher.start()
        self.addCleanup(patcher.stop)

    def capture(self, *extra: str) -> tuple[int, str, str]:
        return run(capture_pair.main, ["--output-dir", str(self.output), *extra])

    def test_default_tier_is_reusable_even_with_fixture_revision(self) -> None:
        code, out, err = self.capture("--sample-id", "pair_a", "--fixture-revision", "rigid-v1")
        self.assertEqual(code, 0, err)
        metadata_path = self.output / "pair_a" / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["evidence_tier"], "reusable_real_evidence")
        self.assertRegex(metadata["capture"]["timestamp_utc"], r"Z$")
        validate(metadata_path)
        for name in ("laser_on.jpg", "laser_off.jpg", "metadata.json"):
            self.assertEqual(stat.S_IMODE(os.stat(self.output / "pair_a" / name).st_mode), 0o600)
        self.assertNotIn(".partial", " ".join(os.listdir(self.output / "pair_a")))

    def test_legacy_positional_output_dir(self) -> None:
        code, _, err = run(capture_pair.main, [str(self.output), "--sample-id", "legacy"])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.output / "legacy" / "metadata.json").is_file())

    def test_calibration_grade_with_confirmed_lock(self) -> None:
        code, out, err = self.capture(
            "--sample-id", "cal", "--calibration-grade", "--lock", "--exposure", "120", "--gain", "0",
            "--fixture-revision", "rigid-v1", "--working-distance-mm", "120", "--baseline-mm", "70",
        )
        self.assertEqual(code, 0, err)
        metadata = json.loads((self.output / "cal" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["evidence_tier"], "calibration_grade_real")
        self.assertEqual(metadata["capture"]["camera"]["exposure"], 120)
        self.assertEqual(metadata["capture"]["camera"]["gain"], 0)
        self.assertTrue(metadata["capture"]["camera"]["white_balance_locked"])

    def test_calibration_grade_preconditions_abort_before_port_open(self) -> None:
        cases = [
            ["--calibration-grade", "--fixture-revision", "rigid-v1", "--working-distance-mm", "120", "--baseline-mm", "70"],
            ["--calibration-grade", "--lock", "--working-distance-mm", "120", "--baseline-mm", "70"],
            ["--calibration-grade", "--lock", "--fixture-revision", "rigid-v1", "--baseline-mm", "70"],
            ["--calibration-grade", "--lock", "--fixture-revision", "rigid-v1", "--working-distance-mm", "120"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _, err = self.capture(*argv)
                self.assertEqual(code, 2)
                self.assertIn("--calibration-grade also requires", err)
        self.open_port.assert_not_called()

    def test_calibration_grade_requires_locked_readback(self) -> None:
        original = self.board.__call__

        def ignore_lock(payload: bytes) -> bytes:
            response = original(payload)
            self.board.locked = False
            return response

        with mock.patch.object(capture_pair, "open_port", side_effect=lambda *a, **k: FakeSerial(responder=ignore_lock)):
            code, _, err = self.capture(
                "--calibration-grade", "--lock", "--fixture-revision", "rigid-v1",
                "--working-distance-mm", "120", "--baseline-mm", "70",
            )
        self.assertEqual(code, 1)
        self.assertIn("not calibration grade", err)
        self.assertFalse(self.output.exists())

    def test_invalid_sample_ids_and_fixture_revisions(self) -> None:
        for sample_id in ("../x", "/abs", "a/b", "..", ""):
            with self.subTest(sample_id=sample_id):
                code, _, err = self.capture("--sample-id", sample_id)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)
        for revision in ('x"; system("rm', "a b", "x" * 65):
            with self.subTest(revision=revision):
                self.assertEqual(self.capture("--fixture-revision", revision)[0], 2)
        self.open_port.assert_not_called()

    def test_existing_sample_needs_overwrite(self) -> None:
        (self.output / "taken").mkdir(parents=True)
        code, _, err = self.capture("--sample-id", "taken")
        self.assertEqual(code, 2)
        self.assertIn("--overwrite", err)
        self.open_port.assert_not_called()
        self.assertEqual(self.capture("--sample-id", "taken", "--overwrite")[0], 0)

    def test_failed_pair_end_and_pair_error_fail_and_switch_laser_off(self) -> None:
        for attribute, value, expected in (
            ("pair_end", "PAIR_END ok=0 reason=frame", "failed pair"),
            ("pair_error", "PAIR_ERROR CAMERA_NOT_READY", "PAIR_ERROR"),
        ):
            with self.subTest(value=value):
                self.board = FakeBoard(eol=b"\r\n")
                setattr(self.board, attribute, value)
                code, _, err = self.capture("--sample-id", f"fail_{attribute}")
                self.assertEqual(code, 1)
                self.assertIn(expected, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(self.board.command_log()[-1], LASER_OFF_COMMAND)
                self.assertFalse((self.output / f"fail_{attribute}").exists())

    def test_pair_end_with_ok_field_succeeds(self) -> None:
        self.board.pair_end = "PAIR_END ok=1"
        self.assertEqual(self.capture("--sample-id", "ok1")[0], 0)

    def test_serial_exception_is_a_clean_error(self) -> None:
        with mock.patch.object(capture_pair, "open_port", side_effect=serial.SerialException("no such device")):
            code, _, err = self.capture("--sample-id", "nodev")
        self.assertEqual(code, 1)
        self.assertIn("cannot open /dev/ttyUSB0: no such device", err)
        self.assertNotIn("Traceback", err)

    def test_camera_timeout_option(self) -> None:
        with mock.patch.object(capture_pair, "open_port", side_effect=lambda *a, **k: FakeSerial(responder=lambda _: b"")):
            code, _, err = self.capture("--sample-id", "slow", "--camera-timeout", "0.2")
        self.assertEqual(code, 1)
        self.assertIn("camera did not become READY", err)


class CaptureFrameTests(unittest.TestCase):
    def test_capture_frame_writes_validated_jpeg(self) -> None:
        board = FakeBoard()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(capture_frame, "open_port", side_effect=lambda *a, **k: board.attach()):
            target = Path(directory) / "frame.jpg"
            code, out, err = run(capture_frame.main, [str(target), "--camera-timeout", "1"])
            self.assertEqual(code, 0, err)
            self.assertEqual(target.read_bytes(), board.jpeg)
            self.assertIn("CAPTURE_RESULT: PASS", out)

    def test_capture_frame_reports_protocol_errors_cleanly(self) -> None:
        board = FakeBoard()
        board.stall_frames.set()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(capture_frame, "open_port", side_effect=lambda *a, **k: board.attach()):
            code, _, err = run(capture_frame.main, [str(Path(directory) / "f.jpg"), "--timeout", "0.2"])
        self.assertEqual(code, 1)
        self.assertIn("FRAME_BEGIN", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(run(capture_frame.main, ["x.jpg", "--camera-timeout", "0"])[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
