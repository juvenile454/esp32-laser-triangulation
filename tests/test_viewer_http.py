#!/usr/bin/env python3
"""In-process HTTP tests for tools/live_viewer.py with a fake board (no hardware)."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import live_viewer
from fakes import FakeBoard, FakeSerial, wait_until
from live_viewer import (
    LASER_STATE_UNKNOWN_WARNING,
    TOKEN_HEADER,
    Viewer,
    ViewerConfig,
    make_server,
)
from serial_protocol import LASER_OFF_COMMAND, LASER_ON_COMMAND


class Harness:
    def __init__(self, bind: str = "127.0.0.1", opener: Any = None, **overrides: Any) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="laser-viewer-test-")
        self.board = FakeBoard()
        settings: dict[str, Any] = {
            "device": "fake", "fps": 10.0, "save_dir": Path(self.temporary.name) / "live",
            "reconnect_delay_s": 0.1, "camera_timeout_s": 2.0, "frame_timeout_s": 1.0,
            "control_ack_timeout_s": 0.5, "shutdown_ack_timeout_s": 0.5,
        }
        settings.update(overrides)
        self.viewer = Viewer(ViewerConfig(**settings), opener=opener or (lambda _device, _baud: self.board.attach()))
        self.server = make_server(bind, 0, self.viewer)
        self.port = self.server.port
        self.host = f"{live_viewer.host_for_url(bind)}:{self.port}"
        self.serve_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.serve_thread.start()
        self.worker = threading.Thread(target=self.viewer.run_worker, daemon=True)
        self.worker.start()
        self.closed = False
        self.bind = bind

    @property
    def state(self) -> dict[str, Any]:
        return self.viewer.state.status()

    def wait_for_frames(self) -> None:
        assert wait_until(lambda: self.state["frames"] >= 1, timeout=5.0), self.state

    def connect(self) -> http.client.HTTPConnection:
        address = "::1" if ":" in self.bind else self.bind
        return http.client.HTTPConnection(address, self.port, timeout=15)

    def request(
        self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None,
        host: str | None = None, origin: str | None = "default", token: str | None = "default",
        content_type: str | None = "application/json", omit_host: bool = False,
    ) -> tuple[int, dict[str, str], Any]:
        connection = self.connect()
        all_headers: dict[str, str] = {}
        if not omit_host:
            all_headers["Host"] = host or self.host
        if method == "POST":
            if origin == "default":
                all_headers["Origin"] = f"http://{self.host}"
            elif origin is not None:
                all_headers["Origin"] = origin
            if content_type is not None:
                all_headers["Content-Type"] = content_type
        if token == "default":
            all_headers[TOKEN_HEADER] = self.viewer.token
        elif token is not None:
            all_headers[TOKEN_HEADER] = token
        all_headers.update(headers or {})
        payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        if method == "POST" and payload is None:
            payload = b"{}"
        if omit_host:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in all_headers.items():
                connection.putheader(name, value)
            if payload is not None:
                connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
        else:
            connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        if response_headers.get("content-type", "").startswith("application/json"):
            return response.status, response_headers, json.loads(data)
        return response.status, response_headers, data

    def post(self, path: str, body: Any = None, **kwargs: Any) -> tuple[int, Any]:
        status, _, payload = self.request("POST", path, body, **kwargs)
        return status, payload

    def laser_commands(self) -> list[bytes]:
        return [c for c in self.board.command_log() if c in (LASER_ON_COMMAND, LASER_OFF_COMMAND)]

    def close(self, join_timeout: float = 5.0) -> tuple[int, str]:
        if self.closed:
            return 0, ""
        self.closed = True
        self.server.shutdown()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = self.viewer.shutdown(self.server, self.worker, join_timeout)
        self.temporary.cleanup()
        return code, stderr.getvalue()


class ViewerTestCase(unittest.TestCase):
    def harness(self, **overrides: Any) -> Harness:
        with contextlib.redirect_stdout(io.StringIO()):
            harness = Harness(**overrides)
        self.addCleanup(harness.close)
        return harness


class SecurityTests(ViewerTestCase):
    def test_page_has_token_security_headers_and_no_inline_script(self) -> None:
        h = self.harness()
        status, headers, body = h.request("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn(f'<meta name="viewer-token" content="{h.viewer.token}">', page)
        self.assertNotIn("{{VIEWER_TOKEN}}", page)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", page))
        self.assertNotIn("onclick", page)
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["cache-control"], "no-store")
        status, headers, script = h.request("GET", "/viewer.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        self.assertIn(b"X-Viewer-Token", script)
        # Error responses carry the same headers.
        status, headers, _ = h.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_tokens_differ_per_start(self) -> None:
        self.assertNotEqual(Viewer(ViewerConfig()).token, Viewer(ViewerConfig()).token)

    def test_foreign_or_missing_host_is_421(self) -> None:
        h = self.harness()
        for host in ("evil.example", f"evil.example:{h.port}", "127.0.0.1:1", f"192.168.0.10:{h.port}"):
            with self.subTest(host=host):
                self.assertEqual(h.request("GET", "/", host=host)[0], 421)
                self.assertEqual(h.request("GET", "/status.json", host=host)[0], 421)
                self.assertEqual(h.request("GET", f"/stream.mjpg?token={h.viewer.token}", host=host)[0], 421)
                self.assertEqual(h.post("/laser", {"on": True}, host=host)[0], 421)
        self.assertEqual(h.request("GET", "/", omit_host=True)[0], 421)
        for host in (f"localhost:{h.port}", f"127.0.0.1:{h.port}", f"[::1]:{h.port}", f"LOCALHOST:{h.port}"):
            with self.subTest(host=host):
                self.assertEqual(h.request("GET", "/health", host=host)[0], 200)
        self.assertNotIn(LASER_ON_COMMAND, h.laser_commands())

    def test_status_requires_token(self) -> None:
        h = self.harness()
        self.assertEqual(h.request("GET", "/status.json", token=None)[0], 403)
        self.assertEqual(h.request("GET", "/status.json", token="wrong")[0], 403)
        status, _, payload = h.request("GET", "/status.json")
        self.assertEqual(status, 200)
        for key in ("laser", "laser_remaining_s", "laser_off_reason", "laser_max_on_s"):
            self.assertIn(key, payload)
        status, _, payload = h.request("GET", "/health", token=None)
        self.assertEqual((status, payload), (200, {"ok": True}))

    def test_post_requires_same_origin(self) -> None:
        h = self.harness()
        h.wait_for_frames()
        cases = {
            "missing": {"origin": None},
            "foreign": {"origin": "http://evil.example"},
            "null": {"origin": "null"},
            "other port": {"origin": "http://127.0.0.1:1"},
            "https": {"origin": f"https://{h.host}"},
            "foreign referer": {"origin": None, "headers": {"Referer": "http://evil.example/page"}},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                status, payload = h.post("/laser", {"on": True}, **kwargs)
                self.assertEqual(status, 403)
                self.assertFalse(payload["ok"])
        status, _ = h.post("/light", {"r": 1, "g": 2, "b": 3}, origin=None,
                           headers={"Referer": f"http://{h.host}/"})
        self.assertEqual(status, 200)
        self.assertNotIn(LASER_ON_COMMAND, h.laser_commands())

    def test_post_requires_json_content_type(self) -> None:
        h = self.harness()
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
            with self.subTest(content_type=content_type):
                status, _ = h.post("/laser", b'{"on": true}', content_type=content_type)
                self.assertEqual(status, 415)
        self.assertNotIn(LASER_ON_COMMAND, h.laser_commands())

    def test_post_requires_token(self) -> None:
        h = self.harness()
        for token in (None, "", "wrong", h.viewer.token + "x"):
            with self.subTest(token=token):
                self.assertEqual(h.post("/laser", {"on": True}, token=token)[0], 403)
                self.assertEqual(h.post("/shutdown", {}, token=token)[0], 403)
        self.assertFalse(h.viewer.stop_event.is_set())
        self.assertNotIn(LASER_ON_COMMAND, h.laser_commands())

    def test_bad_bodies_are_400(self) -> None:
        h = self.harness()
        for body in (b"not json", b"[]", b'{"on": "yes"}', b"x" * 300):
            with self.subTest(body=body[:20]):
                self.assertEqual(h.post("/laser", body)[0], 400)
        self.assertEqual(h.post("/light", {"r": 65, "g": 0, "b": 0})[0], 400)
        self.assertEqual(h.post("/camera", {"action": "explode"})[0], 400)


class FunctionalTests(ViewerTestCase):
    def test_happy_path_controls(self) -> None:
        h = self.harness()
        h.wait_for_frames()
        status, payload = h.post("/light", {"r": 10, "g": 0, "b": 64})
        self.assertEqual(status, 200, payload)
        self.assertEqual(h.state["light_rgb"], (10, 0, 64))
        status, payload = h.post("/camera", {"action": "lock"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(h.state["camera_automation"], "Locked")
        status, payload = h.post("/laser", {"on": True})
        self.assertEqual((status, payload["laser"]), (200, "On"))
        remaining = h.request("GET", "/status.json")[2]["laser_remaining_s"]
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 60)
        status, payload = h.post("/laser", {"on": False})
        self.assertEqual((status, payload["laser"]), (200, "Off"))
        self.assertEqual(h.state["laser_off_reason"], "user")

    def test_stream_requires_token_and_caps_clients(self) -> None:
        h = self.harness(max_stream_clients=1)
        h.wait_for_frames()
        self.assertEqual(h.request("GET", "/stream.mjpg")[0], 403)
        self.assertEqual(h.request("GET", "/stream.mjpg?token=wrong")[0], 403)
        first = h.connect()
        first.request("GET", f"/stream.mjpg?token={h.viewer.token}", headers={"Host": h.host})
        response = first.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"\xff\xd8", response.read(4096))
        self.assertEqual(h.request("GET", f"/stream.mjpg?token={h.viewer.token}")[0], 503)
        first.close()

    def test_save_writes_frame_and_reports_os_errors_as_json(self) -> None:
        h = self.harness()
        h.wait_for_frames()
        status, payload = h.post("/save", {})
        self.assertEqual(status, 200, payload)
        saved = Path(payload["file"])
        self.assertTrue(saved.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)
        blocker = Path(h.temporary.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        h.viewer.config.save_dir = blocker / "live"
        status, payload = h.post("/save", {})
        self.assertEqual(status, 500)
        self.assertIn("Could not save frame", payload["error"])

    def test_default_save_dir_is_not_cwd_relative(self) -> None:
        with mock_env(LASER_DATA_DIR=None):
            self.assertEqual(ViewerConfig().save_dir, Path(live_viewer.__file__).resolve().parents[1] / "data" / "live")
        with mock_env(LASER_DATA_DIR="/srv/laser"):
            self.assertEqual(ViewerConfig().save_dir, Path("/srv/laser/live"))


class LaserSafetyTests(ViewerTestCase):
    def test_host_max_on_time_switches_off(self) -> None:
        h = self.harness(laser_max_on_s=0.6, heartbeat_s=30.0)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        self.assertTrue(wait_until(lambda: h.state["laser"] == "Off", timeout=3.0), h.state)
        self.assertEqual(h.state["laser_off_reason"], "host_max_on_time")
        self.assertEqual(h.laser_commands()[-1], LASER_OFF_COMMAND)
        self.assertFalse(h.board.laser_on)

    def test_max_on_time_interrupts_a_stalled_frame(self) -> None:
        h = self.harness(laser_max_on_s=0.5, heartbeat_s=30.0, frame_timeout_s=6.0)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        h.board.stall_frames.set()
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: not h.board.laser_on, timeout=3.0), h.state)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(h.state["laser_off_reason"], "host_max_on_time")

    def test_page_heartbeat_loss_switches_off(self) -> None:
        h = self.harness(laser_max_on_s=60.0, heartbeat_s=0.6)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        # Polling keeps it on ...
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            self.assertEqual(h.request("GET", "/status.json")[0], 200)
            time.sleep(0.1)
        self.assertEqual(h.state["laser"], "On")
        # ... silence switches it off.
        self.assertTrue(wait_until(lambda: h.state["laser"] == "Off", timeout=3.0), h.state)
        self.assertEqual(h.state["laser_off_reason"], "page_heartbeat_lost")
        self.assertFalse(h.board.laser_on)

    def test_unsolicited_firmware_laser_off_updates_state(self) -> None:
        h = self.harness(heartbeat_s=30.0)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        h.board.laser_on = False
        h.board.unsolicited("LASER OFF pin=14 reason=timeout on_ms=60012 max_on_ms=60000")
        self.assertTrue(wait_until(lambda: h.state["laser"] == "Off", timeout=3.0), h.state)
        self.assertEqual(h.state["laser_off_reason"], "timeout")
        self.assertIsNone(h.state["laser_remaining_s"])

    def test_timeout_before_claim_never_sends_on(self) -> None:
        h = self.harness(frame_timeout_s=1.5, http_wait_s=0.3, heartbeat_s=30.0)
        h.wait_for_frames()
        h.board.stall_frames.set()
        time.sleep(0.3)  # let the worker block in a frame request
        status, payload = h.post("/laser", {"on": True})
        self.assertEqual(status, 504, payload)
        self.assertIn("OFF queued", payload["error"])
        h.board.stall_frames.clear()
        self.assertTrue(wait_until(lambda: LASER_OFF_COMMAND in h.laser_commands(), timeout=5.0))
        self.assertNotIn(LASER_ON_COMMAND, h.laser_commands())
        self.assertFalse(h.board.laser_on)

    def test_timeout_after_claim_is_followed_by_off(self) -> None:
        h = self.harness(http_wait_s=0.2, control_ack_timeout_s=1.5, heartbeat_s=30.0)
        h.wait_for_frames()
        h.board.laser_on_delay_s = 0.6
        status, _ = h.post("/laser", {"on": True})
        self.assertEqual(status, 504)
        self.assertTrue(wait_until(lambda: h.laser_commands()[-1:] == [LASER_OFF_COMMAND], timeout=5.0))
        self.assertEqual(h.laser_commands(), [LASER_ON_COMMAND, LASER_OFF_COMMAND])
        self.assertTrue(wait_until(lambda: h.state["laser"] == "Off", timeout=3.0), h.state)
        self.assertEqual(h.state["laser_off_reason"], "http_timeout")
        self.assertFalse(h.board.laser_on)

    def test_shutdown_sends_laser_off(self) -> None:
        h = self.harness(heartbeat_s=30.0)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        code, stderr = h.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(h.laser_commands()[-1], LASER_OFF_COMMAND)
        self.assertFalse(h.board.laser_on)
        self.assertEqual(h.viewer.state.status()["laser_off_reason"], "viewer_shutdown")
        self.assertFalse(h.worker.is_alive())

    def test_unacknowledged_shutdown_off_is_reported(self) -> None:
        h = self.harness(heartbeat_s=30.0)
        h.wait_for_frames()
        self.assertEqual(h.post("/laser", {"on": True})[0], 200)
        h.board.laser_supported = False
        code, stderr = h.close()
        self.assertEqual(code, 1)
        self.assertIn(LASER_STATE_UNKNOWN_WARNING, stderr)

    def test_stuck_worker_is_reported_and_off_attempted(self) -> None:
        stuck = FakeSerial(timeout=2.0)
        h = self.harness(opener=lambda _device, _baud: stuck, camera_timeout_s=30.0)
        time.sleep(0.2)
        code, stderr = h.close(join_timeout=0.3)
        self.assertEqual(code, 1)
        self.assertIn(LASER_STATE_UNKNOWN_WARNING, stderr)
        self.assertIn(LASER_OFF_COMMAND, stuck.writes)


class BindAndCliTests(ViewerTestCase):
    def test_ipv6_loopback_bind(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("no IPv6 support")
        try:
            h = self.harness(bind="::1")
        except OSError as exc:
            self.skipTest(f"cannot bind ::1: {exc}")
        self.assertEqual(h.request("GET", "/health")[0], 200)
        self.assertEqual(h.request("GET", "/health", host=f"[::1]:{h.port}")[0], 200)

    def test_main_serves_and_exits_cleanly_on_sigterm(self) -> None:
        import signal
        import subprocess

        missing_port = Path(tempfile.gettempdir()) / "laser-viewer-test-no-such-tty"
        process = subprocess.Popen(
            [sys.executable, str(Path(live_viewer.__file__)), "--port", str(missing_port), "--http-port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            ready = ""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not ready.startswith("VIEWER_READY"):
                ready = process.stdout.readline()
            self.assertTrue(ready.startswith("VIEWER_READY http://127.0.0.1:"), ready)
            port = int(ready.rsplit(":", 1)[1].strip().rstrip("/"))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/health")
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=15), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_cli_validation(self) -> None:
        parser = live_viewer.build_parser()
        for argv in (["--bind", "0.0.0.0"], ["--laser-max-on-s", "0"], ["--laser-max-on-s", "601"],
                     ["--fps", "20"], ["--http-port", "70000"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                parser.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)
        args = parser.parse_args(["--device", "/dev/ttyACM3", "--laser-max-on-s", "1"])
        self.assertEqual((args.port, args.laser_max_on_s), ("/dev/ttyACM3", 1.0))
        self.assertEqual(parser.parse_args([]).laser_max_on_s, 60.0)


@contextlib.contextmanager
def mock_env(**values: str | None):
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
