#!/usr/bin/env python3
"""Focused tests for the viewer's queued serial camera controls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from live_viewer import (  # noqa: E402
    HTML_PATH,
    JS_PATH,
    FrameState,
    SerialControl,
    complete_control,
    send_serial_control,
)


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


class LiveViewerStateTests(unittest.TestCase):
    def test_claim_and_cancel_are_mutually_exclusive(self) -> None:
        claimed = SerialControl(payload=b"1")
        self.assertTrue(claimed.claim())
        self.assertFalse(claimed.cancel())
        cancelled = SerialControl(payload=b"1")
        self.assertTrue(cancelled.cancel())
        self.assertFalse(cancelled.claim())
        self.assertTrue(cancelled.cancelled)
        port = FakeSerial([b"LASER ON pin=14\n"])
        complete_control(cancelled, port, FrameState())
        self.assertEqual(port.writes, [])
        self.assertTrue(cancelled.completed.is_set())

    def test_laser_timers_use_the_injected_clock(self) -> None:
        now = [100.0]
        state = FrameState(laser_max_on_s=10.0, heartbeat_s=5.0, clock=lambda: now[0])
        state.set_laser("LASER ON pin=14")
        self.assertIsNone(state.laser_off_due())
        now[0] += 4.0
        self.assertEqual(state.status()["laser_remaining_s"], 6.0)
        now[0] += 1.5
        self.assertEqual(state.laser_off_due(), "page_heartbeat_lost")
        state.mark_poll()
        self.assertIsNone(state.laser_off_due())
        now[0] += 5.0
        self.assertEqual(state.laser_off_due(), "host_max_on_time")
        state.handle_line("LASER OFF pin=14 reason=timeout on_ms=10012 max_on_ms=10000")
        self.assertEqual((state.laser, state.laser_off_reason), ("Off", "timeout"))
        self.assertIsNone(state.laser_off_due())
        state.handle_line("LASER OFF pin=14")  # periodic report keeps the reason
        self.assertEqual(state.laser_off_reason, "timeout")
        state.handle_line("LASER_PIN:           GPIO 14 (off at boot)")
        self.assertEqual(state.laser, "Off")

    def test_unsolicited_lines_reach_the_handler_during_controls(self) -> None:
        state = FrameState()
        state.set_laser("LASER ON pin=14")
        port = FakeSerial([b"LASER OFF pin=14 reason=timeout\r\n", b"TEST_LIGHT RGB r=0 g=0 b=0 pin=48 max=64\r\n"])
        send_serial_control(port, b"O", timeout=0.5, on_line=state.handle_line)
        self.assertEqual((state.laser, state.laser_off_reason), ("Off", "timeout"))


class LiveViewerPageTests(unittest.TestCase):
    def test_page_ships_next_to_the_viewer_in_english(self) -> None:
        page = HTML_PATH.read_text(encoding="utf-8")
        self.assertIn('<html lang="en">', page)
        for label in ("Laser on", "Laser off", "Stop viewer", "/stream.mjpg", '<script src="/viewer.js"'):
            self.assertIn(label, page)
        self.assertNotIn("onclick", page)

    def test_page_accessibility_and_xss_safety(self) -> None:
        page = HTML_PATH.read_text(encoding="utf-8")
        script = JS_PATH.read_text(encoding="utf-8")
        self.assertIn("<main>", page)
        self.assertRegex(page, r'id="message" role="status" aria-live="polite"')
        self.assertRegex(page, r'id="status" role="status" aria-live="polite"')
        self.assertRegex(page, r'id="error" role="alert"')
        self.assertIn('id="laser-banner"', page)
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "setInterval"):
            self.assertNotIn(unsafe, script)
        for required in ("window.confirm(", "setTimeout(refreshLoop", "addEventListener('error'", "X-Viewer-Token",
                         "button.disabled = true"):
            self.assertIn(required, script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
