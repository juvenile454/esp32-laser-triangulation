#!/usr/bin/env python3
"""Framing, checksum and line-ending tests for tools/serial_protocol.py."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fakes import FakeSerial, frame_bytes, make_jpeg
from serial_protocol import (
    LASER_OFF_COMMAND,
    LASER_ON_COMMAND,
    ProtocolError,
    StopRequested,
    camera_readiness,
    controls_automatic,
    controls_locked,
    expect_line,
    laser_command,
    parse_controls,
    parse_frame_header,
    parse_laser_line,
    parse_pair_end,
    read_frame,
    request_frame,
    wait_for_camera,
    write_atomic,
)

WIDTH, HEIGHT = 128, 96


class FrameReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jpeg = make_jpeg(WIDTH, HEIGHT)

    def read(self, data: bytes, timeout: float = 0.5, **kwargs):
        return read_frame(FakeSerial(data), time.monotonic() + timeout, **kwargs)

    def test_accepts_crlf_and_lf_line_endings(self) -> None:
        for eol in (b"\r\n", b"\n"):
            with self.subTest(eol=eol):
                chatter = b"ALIVE 4 | camera=READY | laser=OFF" + eol
                frame = self.read(chatter + frame_bytes(self.jpeg, WIDTH, HEIGHT, eol=eol))
                self.assertEqual(frame.payload, self.jpeg)
                self.assertEqual((frame.width, frame.height), (WIDTH, HEIGHT))

    def test_skipped_lines_reach_the_line_handler(self) -> None:
        seen: list[str] = []
        self.read(b"LASER OFF pin=14 reason=timeout\r\n" + frame_bytes(self.jpeg, WIDTH, HEIGHT), on_line=seen.append)
        self.assertEqual(seen, ["LASER OFF pin=14 reason=timeout"])

    def test_timeout_without_header(self) -> None:
        with self.assertRaisesRegex(TimeoutError, "FRAME_BEGIN"):
            self.read(b"ALIVE 1 | camera=READY\n", timeout=0.1)

    def test_wrong_length_is_detected(self) -> None:
        # Header announces fewer bytes than sent: the JPEG tail lands where FRAME_END belongs.
        short = frame_bytes(self.jpeg, WIDTH, HEIGHT, length=len(self.jpeg) - 10)
        with self.assertRaises(ProtocolError):
            self.read(short)
        # Header announces more bytes than ever arrive.
        long = frame_bytes(self.jpeg, WIDTH, HEIGHT, length=len(self.jpeg) + 500, end=b"")
        with self.assertRaisesRegex(TimeoutError, "bytes missing"):
            self.read(long, timeout=0.2)

    def test_wrong_checksum_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "FNV-1a mismatch"):
            self.read(frame_bytes(self.jpeg, WIDTH, HEIGHT, checksum=0x12345678))

    def test_missing_frame_end(self) -> None:
        with self.assertRaisesRegex(TimeoutError, "FRAME_END"):
            self.read(frame_bytes(self.jpeg, WIDTH, HEIGHT, end=b""), timeout=0.2)
        with self.assertRaisesRegex(ProtocolError, "unexpected data"):
            self.read(frame_bytes(self.jpeg, WIDTH, HEIGHT, end=b"\r\nGARBAGE\r\n"))

    def test_dimension_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "dimension mismatch"):
            self.read(frame_bytes(self.jpeg, WIDTH + 1, HEIGHT))
        frame = self.read(frame_bytes(self.jpeg, WIDTH + 1, HEIGHT), verify_image=False)
        self.assertEqual(frame.width, WIDTH + 1)

    def test_frame_error_and_malformed_headers(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "CAMERA_NOT_READY"):
            self.read(b"FRAME_ERROR CAMERA_NOT_READY\n")
        for line in ("FRAME_BEGIN 1 2 3", "FRAME_BEGIN x 1 1 4 00000000", "FRAME_BEGIN 10 1 1 4 00000000"):
            with self.subTest(line=line), self.assertRaises(ProtocolError):
                parse_frame_header(line)

    def test_stop_event_aborts_promptly(self) -> None:
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(StopRequested):
            read_frame(FakeSerial(b""), time.monotonic() + 10.0, stop_event=stop)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_request_frame_sends_capture_command(self) -> None:
        jpeg = self.jpeg
        port = FakeSerial(responder=lambda payload: frame_bytes(jpeg, WIDTH, HEIGHT) if payload == b"C" else b"")
        self.assertEqual(request_frame(port, 0.5).payload, jpeg)
        self.assertEqual(port.writes, [b"C"])


class LineProtocolTests(unittest.TestCase):
    def test_camera_readiness_compares_fields_not_whitespace(self) -> None:
        self.assertTrue(camera_readiness("CAMERA_RESULT:       PASS"))
        self.assertTrue(camera_readiness("CAMERA_RESULT: PASS"))
        self.assertFalse(camera_readiness("CAMERA_RESULT:\tFAIL"))
        self.assertTrue(camera_readiness("CAMERA_STATUS READY"))
        self.assertFalse(camera_readiness("CAMERA_STATUS FAILED"))
        self.assertTrue(camera_readiness("ALIVE 3 | camera=READY | laser=OFF"))
        self.assertIsNone(camera_readiness("CAMERA_WARMUP:       PASS (3 frames)"))

    def test_wait_for_camera(self) -> None:
        port = FakeSerial(responder=lambda payload: b"CAMERA_STATUS READY\r\nLASER OFF pin=14\r\n")
        wait_for_camera(port, time.monotonic() + 1.0)
        self.assertEqual(port.writes[0], b"P")
        with self.assertRaisesRegex(ProtocolError, "FAILED"):
            wait_for_camera(FakeSerial(b"CAMERA_STATUS FAILED\n"), time.monotonic() + 1.0)
        with self.assertRaises(TimeoutError):
            wait_for_camera(FakeSerial(), time.monotonic() + 0.1)

    def test_expect_line_matches_whole_words(self) -> None:
        port = FakeSerial(b"LASER_PIN:           GPIO 14 (off at boot)\nLASER ON pin=14\n")
        self.assertEqual(expect_line(port, "LASER", time.monotonic() + 0.5), "LASER ON pin=14")
        with self.assertRaisesRegex(ProtocolError, "PAIR_ERROR"):
            expect_line(FakeSerial(b"PAIR_ERROR CAMERA_NOT_READY\n"), "PAIR_END", time.monotonic() + 0.5)

    def test_laser_lines(self) -> None:
        report = parse_laser_line("LASER OFF pin=14 reason=timeout on_ms=60012 max_on_ms=60000")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertFalse(report.on)
        self.assertEqual(report.reason, "timeout")
        self.assertEqual(report.fields["max_on_ms"], "60000")
        self.assertTrue(parse_laser_line("LASER ON pin=14").on)
        self.assertIsNone(parse_laser_line("LASER_PIN:           GPIO 14"))
        self.assertIsNone(parse_laser_line("LASER SOMETHING_ELSE pin=14"))
        self.assertEqual(laser_command(True), LASER_ON_COMMAND)
        self.assertEqual(laser_command(False), LASER_OFF_COMMAND)

    def test_pair_end(self) -> None:
        self.assertTrue(parse_pair_end("PAIR_END"))
        self.assertTrue(parse_pair_end("PAIR_END ok=1"))
        self.assertFalse(parse_pair_end("PAIR_END ok=0 reason=frame"))
        with self.assertRaises(ProtocolError):
            parse_pair_end("PAIR_ENDING")

    def test_controls(self) -> None:
        locked = parse_controls("CAMERA_AUTOMATION LOCKED PASS results=0/0/0/0 | CAMERA_CONTROLS aec=0 aec_value=784 agc=0 agc_gain=16 awb=0 awb_gain=0 wb_mode=0")
        self.assertEqual(locked["aec_value"], 784)
        self.assertNotIn("results", locked)
        self.assertTrue(controls_locked(locked))
        self.assertFalse(controls_automatic(locked))
        self.assertTrue(controls_automatic(parse_controls("aec=1 agc=1 awb=1 awb_gain=0 wb_mode=0")))
        self.assertFalse(controls_automatic(parse_controls("aec=1 agc=1 awb=1 awb_gain=0 wb_mode=2")))
        self.assertFalse(controls_locked(parse_controls("aec=0 agc=0 awb=1 awb_gain=0")))


class AtomicWriteTests(unittest.TestCase):
    def test_write_atomic_sets_mode_and_leaves_no_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "frame.jpg"
            write_atomic(target, b"abc")
            write_atomic(target, b"defg")
            self.assertEqual(target.read_bytes(), b"defg")
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
            self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["frame.jpg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
