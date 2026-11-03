#!/usr/bin/env python3
"""Focused tests for the viewer's queued serial camera controls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from capture_frame import fnv1a32  # noqa: E402
from live_viewer import (  # noqa: E402
    HTML_PATH,
    FrameState,
    SerialControl,
    complete_control,
    request_frame,
    send_serial_control,
)

TIMEOUT_LINE = b"LASER OFF pin=14 reason=timeout on_ms=60004 max_on_ms=60000\n"


class FakeSerial:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = iter(lines)
        self.writes: list[bytes] = []
        self.flushes = 0

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        self.flushes += 1

    def readline(self) -> bytes:
        return next(self.lines, b"")


class FakeByteStream:
    """Serial stand-in that serves one byte stream to readline() and read()."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        end = self.data.find(b"\n")
        end = len(self.data) if end < 0 else end + 1
        line, self.data = self.data[:end], self.data[end:]
        return line

    def read(self, size: int) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


def framed_jpeg() -> tuple[bytes, bytes]:
    payload = b"\xFF\xD8" + bytes(1020) + b"\xFF\xD9"
    header = f"FRAME_BEGIN {len(payload)} 640 480 4 {fnv1a32(payload):08X}\r\n"
    return payload, header.encode("ascii") + payload + b"\r\nFRAME_END\r\n"


class LiveViewerLaserTests(unittest.TestCase):
    def test_laser_acknowledgement_updates_state(self) -> None:
        for payload, acknowledgement, expected in (
            (b"1", b"LASER ON pin=14\n", "On"),
            (b"0", b"LASER OFF pin=14\n", "Off"),
        ):
            with self.subTest(payload=payload):
                port = FakeSerial([b"ALIVE 3 | camera=READY | laser=OFF\n", acknowledgement])
                state = FrameState()
                control = SerialControl(
                    payload=payload, expected_prefixes=("LASER",), laser_action=True
                )
                complete_control(control, port, state)
                self.assertEqual(port.writes, [payload])
                self.assertEqual(control.error, "")
                self.assertEqual(state.status()["laser"], expected)

    def test_older_firmware_is_reported_not_crashed(self) -> None:
        """Firmware without the laser command simply never answers."""
        port = FakeSerial([b"ALIVE 1 | camera=READY\n"])
        state = FrameState()
        control = SerialControl(
            payload=b"1", expected_prefixes=("LASER",), laser_action=True
        )
        complete_control(control, port, state)
        self.assertIn("No response", control.error)
        self.assertEqual(state.status()["laser"], "Unknown")

    def test_unparsable_acknowledgement_is_not_reported_as_on(self) -> None:
        port = FakeSerial([b"LASER SOMETHING_ELSE pin=14\n"])
        state = FrameState()
        control = SerialControl(
            payload=b"1", expected_prefixes=("LASER",), laser_action=True
        )
        complete_control(control, port, state)
        self.assertEqual(state.status()["laser"], "Unavailable")


class LiveViewerLaserTimeoutTests(unittest.TestCase):
    def test_timeout_line_is_distinct_from_commanded_off(self) -> None:
        state = FrameState()
        state.set_laser("LASER OFF pin=14")
        self.assertEqual(state.status()["laser"], "Off")
        state.set_laser(TIMEOUT_LINE.decode().strip())
        self.assertEqual(state.status()["laser"], "Off (timeout)")

    def test_unsolicited_timeout_before_a_frame_updates_state(self) -> None:
        """The firmware prints the timeout between commands, so the viewer
        usually meets it while waiting for the next FRAME_BEGIN."""
        payload, framed = framed_jpeg()
        port = FakeByteStream(TIMEOUT_LINE + framed)
        state = FrameState()
        state.set_laser("LASER ON pin=14")
        frame, width, height = request_frame(
            port, timeout=1.0, on_line=state.observe_serial_line
        )
        self.assertEqual(port.writes, [b"C"])
        self.assertEqual((frame, width, height), (payload, 640, 480))
        self.assertEqual(state.status()["laser"], "Off (timeout)")

    def test_unsolicited_timeout_during_other_control_updates_state(self) -> None:
        port = FakeSerial(
            [
                TIMEOUT_LINE,
                b"CAMERA_CONTROLS aec=1 aec_value=120 agc=1 agc_gain=3 awb=1 awb_gain=1 wb_mode=0\n",
            ]
        )
        state = FrameState()
        state.set_laser("LASER ON pin=14")
        control = SerialControl(
            payload=b"S",
            expected_prefixes=("CAMERA_CONTROLS",),
            camera_action="status",
        )
        complete_control(control, port, state)
        self.assertEqual(control.error, "")
        self.assertEqual(state.status()["laser"], "Off (timeout)")

    def test_laser_on_after_timeout_is_reported_on(self) -> None:
        port = FakeSerial([b"LASER ON pin=14\n"])
        state = FrameState()
        state.set_laser(TIMEOUT_LINE.decode().strip())
        control = SerialControl(
            payload=b"1", expected_prefixes=("LASER",), laser_action=True
        )
        complete_control(control, port, state)
        self.assertEqual(state.status()["laser"], "On")


class LiveViewerControlTests(unittest.TestCase):
    def test_collects_lock_and_status_acknowledgements(self) -> None:
        port = FakeSerial(
            [
                b"ALIVE 1 | camera=READY\n",
                b"CAMERA_CONTROLS aec=0 aec_value=120 agc=0 agc_gain=3 awb=0 awb_gain=0 wb_mode=0\n",
                b"CAMERA_AUTOMATION LOCKED PASS retained_aec_value=120 retained_agc_gain=3 retained_wb_mode=0 results=0/0/0/0\n",
            ]
        )
        response = send_serial_control(
            port,
            b"K",
            expected_prefixes=("CAMERA_AUTOMATION", "CAMERA_CONTROLS"),
            timeout=0.1,
        )
        self.assertEqual(port.writes, [b"K"])
        self.assertEqual(port.flushes, 1)
        self.assertIn("CAMERA_AUTOMATION LOCKED PASS", response)
        self.assertIn("CAMERA_CONTROLS aec=0", response)

    def test_rejects_camera_failure_anywhere_in_acknowledgement(self) -> None:
        port = FakeSerial(
            [b"CAMERA_AUTOMATION LOCKED FAIL retained_aec_value=120 results=0/-1/0/0\n"]
        )
        with self.assertRaisesRegex(RuntimeError, "LOCKED FAIL"):
            send_serial_control(
                port,
                b"K",
                expected_prefixes=("CAMERA_AUTOMATION", "CAMERA_CONTROLS"),
                timeout=0.1,
            )

    def test_camera_state_reports_auto_locked_and_unavailable(self) -> None:
        state = FrameState()
        state.set_camera_control(
            "status",
            "CAMERA_CONTROLS aec=1 aec_value=120 agc=1 agc_gain=3 awb=1 awb_gain=1 wb_mode=0",
        )
        self.assertEqual(state.status()["camera_automation"], "Automatic")
        state.set_camera_control(
            "lock",
            "CAMERA_AUTOMATION LOCKED PASS | CAMERA_CONTROLS aec=0 agc=0 awb=0 awb_gain=0",
        )
        self.assertEqual(state.status()["camera_automation"], "Locked")
        state.set_camera_control(
            "unavailable", "CAMERA_CONTROLS unavailable · older board firmware"
        )
        status = state.status()
        self.assertEqual(status["camera_automation"], "Unavailable")
        self.assertIn("older board firmware", status["camera_controls"])

    def test_camera_state_accepts_ov3660_auto_white_balance_readback(self) -> None:
        state = FrameState()
        state.set_camera_control(
            "auto",
            "CAMERA_AUTOMATION AUTO PASS results=0/0/0/0 | "
            "CAMERA_CONTROLS aec=1 agc=1 awb=1 awb_gain=0 wb_mode=0",
        )
        self.assertEqual(state.status()["camera_automation"], "Automatic")

    def test_auto_accepts_ov3660_auto_white_balance_readback(self) -> None:
        port = FakeSerial(
            [
                b"CAMERA_AUTOMATION AUTO PASS results=0/0/0/0\n",
                b"CAMERA_CONTROLS aec=1 agc=1 awb=1 awb_gain=0 wb_mode=0\n",
            ]
        )
        state = FrameState()
        control = SerialControl(
            payload=b"A",
            expected_prefixes=("CAMERA_AUTOMATION", "CAMERA_CONTROLS"),
            camera_action="auto",
        )
        complete_control(control, port, state)
        self.assertEqual(control.error, "")
        self.assertEqual(state.status()["camera_automation"], "Automatic")

    def test_auto_rejects_manual_ov3660_white_balance_mode(self) -> None:
        port = FakeSerial(
            [
                b"CAMERA_AUTOMATION AUTO PASS results=0/0/0/0\n",
                b"CAMERA_CONTROLS aec=1 agc=1 awb=1 awb_gain=0 wb_mode=2\n",
            ]
        )
        state = FrameState()
        control = SerialControl(
            payload=b"A",
            expected_prefixes=("CAMERA_AUTOMATION", "CAMERA_CONTROLS"),
            camera_action="auto",
        )
        complete_control(control, port, state)
        self.assertIn("not fully confirmed by readback", control.error)
        self.assertEqual(state.status()["camera_automation"], "Unknown")

    def test_lock_requires_all_four_disabled_flags(self) -> None:
        port = FakeSerial(
            [
                b"CAMERA_AUTOMATION LOCKED PASS retained_aec_value=120 results=0/0/0/0\n",
                b"CAMERA_CONTROLS aec=0 agc=0 awb=1 awb_gain=0\n",
            ]
        )
        state = FrameState()
        control = SerialControl(
            payload=b"K",
            expected_prefixes=("CAMERA_AUTOMATION", "CAMERA_CONTROLS"),
            camera_action="lock",
        )
        complete_control(control, port, state)
        self.assertIn("not fully confirmed by readback", control.error)
        self.assertEqual(state.status()["camera_automation"], "Unknown")


class LiveViewerPageTests(unittest.TestCase):
    def test_page_ships_next_to_the_viewer_in_english(self) -> None:
        page = HTML_PATH.read_text(encoding="utf-8")
        self.assertIn('<html lang="en">', page)
        for label in ("Laser on", "Laser off", "Stop viewer", "/stream.mjpg"):
            self.assertIn(label, page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
