#!/usr/bin/env python3
"""Localhost-only MJPEG viewer for the GOOUUU ESP32-S3-CAM TTL link.

The viewer owns the serial port in one worker thread; the HTTP handlers only
queue controls for it. Every request must carry an allowed Host header
(DNS-rebinding guard). State-changing POSTs additionally need a same-origin
Origin (or Referer), Content-Type application/json and the per-start token
that is embedded in the served page; /status.json needs the token too.

Laser safety: the host switches the laser off after --laser-max-on-s, when the
page stops polling /status.json for 5 s, and on shutdown. If the shutdown OFF
cannot be confirmed the viewer prints LASER STATE UNKNOWN and exits non-zero.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import queue
import secrets
import signal
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import serial
from PIL import Image, ImageFilter, ImageStat

from cli_common import data_dir, description, fail, float_range, int_range, positive_int
from serial_protocol import (
    DEFAULT_BAUD,
    DEFAULT_CAMERA_TIMEOUT_S,
    DEFAULT_PORT,
    LASER_OFF_COMMAND,
    SerialPort,
    StopRequested,
    controls_automatic,
    controls_locked,
    decode_line,
    laser_command,
    matches_prefix,
    open_port,
    parse_controls,
    parse_laser_line,
    wait_for_camera,
    write_atomic,
)
from serial_protocol import request_frame as read_framed_jpeg

HTML_PATH = Path(__file__).with_name("live_viewer.html")
JS_PATH = Path(__file__).with_name("live_viewer.js")
TOKEN_PLACEHOLDER = "{{VIEWER_TOKEN}}"
TOKEN_HEADER = "X-Viewer-Token"
LOCAL_BIND_HOSTS = ("127.0.0.1", "::1", "localhost")

DEFAULT_LASER_MAX_ON_S = 60.0
LASER_MAX_ON_MIN_S = 1.0
LASER_MAX_ON_MAX_S = 600.0
HEARTBEAT_TIMEOUT_S = 5.0
FRAME_TIMEOUT_S = 8.0
CONTROL_ACK_TIMEOUT_S = 2.0
SHUTDOWN_ACK_TIMEOUT_S = 1.0
HANDLER_SOCKET_TIMEOUT_S = 10.0
MAX_STREAM_CLIENTS = 3
MAX_BODY_BYTES = 256
# A control can sit behind one frame request and then waits for its own ack.
HTTP_CONTROL_WAIT_S = FRAME_TIMEOUT_S + CONTROL_ACK_TIMEOUT_S + 1.0
# Worst case after stop: one in-flight control ack, one automatic laser-OFF ack,
# then the LED OFF and laser OFF shutdown acks, plus margin for write timeouts.
WORKER_JOIN_TIMEOUT_S = 2 * CONTROL_ACK_TIMEOUT_S + 2 * SHUTDOWN_ACK_TIMEOUT_S + 4.0
LASER_STATE_UNKNOWN_WARNING = "LASER STATE UNKNOWN - use the manual switch"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@dataclass
class SerialControl:
    """One queued serial command. Exactly one of claim()/cancel() succeeds."""

    payload: bytes
    rgb: tuple[int, int, int] | None = None
    expected_prefixes: tuple[str, ...] = ("TEST_LIGHT",)
    camera_action: str = ""
    laser_action: bool = False
    laser_reason: str | None = None
    completed: threading.Event = field(default_factory=threading.Event)
    response: str = ""
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _phase: str = "pending"

    def claim(self) -> bool:
        """Worker side: take the control unless HTTP already gave up on it."""
        with self._lock:
            if self._phase != "pending":
                return False
            self._phase = "claimed"
            return True

    def cancel(self) -> bool:
        """HTTP side: True if the control was withdrawn before it was sent."""
        with self._lock:
            if self._phase != "pending":
                return False
            self._phase = "cancelled"
            return True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._phase == "cancelled"


class FrameState:
    def __init__(
        self,
        laser_max_on_s: float = DEFAULT_LASER_MAX_ON_S,
        heartbeat_s: float = HEARTBEAT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.condition = threading.Condition()
        self.clock = clock
        self.frame: bytes | None = None
        self.sequence = 0
        self.width = 0
        self.height = 0
        self.focus = 0.0
        self.best_focus = 0.0
        self.fps = 0.0
        self.error = "Waiting for camera …"
        self.light_rgb = (0, 0, 0)
        self.laser = "Unknown"
        self.laser_on_since: float | None = None
        self.laser_off_reason: str | None = None
        self.laser_max_on_s = laser_max_on_s
        self.heartbeat_s = heartbeat_s
        self.laser_state_unknown = False
        self.last_poll = clock()
        self.camera_automation = "Unknown"
        self.camera_controls = ""
        self.capture_times: deque[float] = deque(maxlen=30)

    def update(self, frame: bytes, width: int, height: int, focus: float) -> None:
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.width = width
            self.height = height
            self.focus = focus
            self.best_focus = max(self.best_focus, focus)
            self.error = ""
            self.capture_times.append(time.monotonic())
            if len(self.capture_times) >= 2:
                elapsed = self.capture_times[-1] - self.capture_times[0]
                self.fps = (len(self.capture_times) - 1) / elapsed if elapsed > 0 else 0.0
            self.condition.notify_all()

    def set_error(self, message: str) -> None:
        with self.condition:
            self.error = message
            self.condition.notify_all()

    def set_light(self, rgb: tuple[int, int, int]) -> None:
        with self.condition:
            self.light_rgb = rgb
            self.condition.notify_all()

    def set_laser(self, response: str, reason: str | None = None) -> None:
        """Apply a firmware ``LASER ON|OFF ...`` report (ack or unsolicited)."""
        report = parse_laser_line(response)
        with self.condition:
            if report is None:
                self.laser = "Unavailable"
                self.laser_on_since = None
            elif report.on:
                if self.laser != "On" or self.laser_on_since is None:
                    self.laser_on_since = self.clock()
                self.laser = "On"
                self.laser_off_reason = None
            else:
                explicit = report.reason or reason
                if self.laser != "Off" or explicit is not None:
                    self.laser_off_reason = explicit
                self.laser = "Off"
                self.laser_on_since = None
                self.laser_state_unknown = False
            self.condition.notify_all()

    def handle_line(self, line: str) -> None:
        """Unsolicited firmware line seen while waiting for something else."""
        report = parse_laser_line(line)
        if report is None:
            return
        with self.condition:
            # A periodic "LASER OFF" report must not erase why it went off.
            if not report.on and report.reason is None and self.laser == "Off":
                return
        self.set_laser(line, reason=None if report.on else "firmware_report")

    def mark_poll(self) -> None:
        with self.condition:
            self.last_poll = self.clock()

    def laser_off_due(self) -> str | None:
        """Reason the host must switch the laser off now, else None."""
        with self.condition:
            if self.laser != "On" or self.laser_on_since is None:
                return None
            now = self.clock()
            if now - self.laser_on_since >= self.laser_max_on_s:
                return "host_max_on_time"
            if now - self.last_poll > self.heartbeat_s:
                return "page_heartbeat_lost"
            return None

    def set_camera_control(self, action: str, response: str) -> None:
        controls = parse_controls(response)
        with self.condition:
            if action == "unavailable":
                self.camera_automation = "Unavailable"
            elif controls_locked(controls):
                self.camera_automation = "Locked"
            elif controls_automatic(controls):
                self.camera_automation = "Automatic"
            else:
                self.camera_automation = "Unconfirmed"
            self.camera_controls = response
            self.condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self.condition:
            remaining = None
            if self.laser == "On" and self.laser_on_since is not None:
                remaining = round(max(0.0, self.laser_max_on_s - (self.clock() - self.laser_on_since)), 1)
            return {
                "frames": self.sequence,
                "width": self.width,
                "height": self.height,
                "bytes": len(self.frame) if self.frame else 0,
                "focus": self.focus,
                "best_focus": self.best_focus,
                "fps": self.fps,
                "error": self.error,
                "light_rgb": self.light_rgb,
                "laser": self.laser,
                "laser_remaining_s": remaining,
                "laser_max_on_s": self.laser_max_on_s,
                "laser_heartbeat_s": self.heartbeat_s,
                "laser_off_reason": self.laser_off_reason,
                "camera_automation": self.camera_automation,
                "camera_controls": self.camera_controls,
            }


def focus_score(payload: bytes) -> float:
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        gray = image.convert("L")
        gray.thumbnail((320, 240))
        edges = gray.filter(ImageFilter.FIND_EDGES)
        if edges.width > 4 and edges.height > 4:
            edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
        return float(ImageStat.Stat(edges).var[0])


def open_serial(device: str, baud: int) -> serial.Serial:
    return open_port(device, baud, timeout=0.15, write_timeout=2.0)


def send_serial_control(
    port: SerialPort,
    payload: bytes,
    expected_prefixes: tuple[str, ...] = ("TEST_LIGHT",),
    timeout: float = CONTROL_ACK_TIMEOUT_S,
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Send one command and collect one line per expected prefix."""
    port.write(payload)
    port.flush()
    deadline = time.monotonic() + timeout
    matched: dict[str, str] = {}
    while time.monotonic() < deadline:
        line = decode_line(port.readline())
        if not line:
            continue
        hit = False
        for prefix in expected_prefixes:
            if matches_prefix(line, prefix):
                if " ERROR" in line or " FAIL" in line:
                    raise RuntimeError(line)
                matched[prefix] = line
                hit = True
        if not hit and on_line is not None:
            on_line(line)
        if len(matched) == len(expected_prefixes):
            return " | ".join(matched[prefix] for prefix in expected_prefixes)
    raise TimeoutError(f"No response to {payload!r}")


def complete_control(
    control: SerialControl,
    port: SerialPort,
    state: FrameState,
    timeout: float = CONTROL_ACK_TIMEOUT_S,
    on_line: Callable[[str], None] | None = None,
) -> None:
    if not control.claim():
        control.completed.set()
        return
    try:
        control.response = send_serial_control(
            port, control.payload, expected_prefixes=control.expected_prefixes, timeout=timeout, on_line=on_line
        )
        readback = parse_controls(control.response)
        if control.camera_action == "lock" and not controls_locked(readback):
            raise RuntimeError(f"Camera lock not fully confirmed by readback: {control.response}")
        if control.camera_action == "auto" and not controls_automatic(readback):
            raise RuntimeError(f"Camera automation not fully confirmed by readback: {control.response}")
        if control.rgb is not None:
            state.set_light(control.rgb)
        if control.camera_action:
            state.set_camera_control(control.camera_action, control.response)
        if control.laser_action:
            state.set_laser(control.response, control.laser_reason)
    except Exception as exc:
        control.error = str(exc)
    finally:
        control.completed.set()


def fail_pending_controls(controls: queue.Queue[SerialControl], message: str) -> None:
    while True:
        try:
            control = controls.get_nowait()
        except queue.Empty:
            return
        if control.claim():
            control.error = message
        control.completed.set()


@dataclass
class ViewerConfig:
    device: str = DEFAULT_PORT
    baud: int = DEFAULT_BAUD
    fps: float = 5.0
    save_dir: Path = field(default_factory=lambda: data_dir() / "live")
    laser_max_on_s: float = DEFAULT_LASER_MAX_ON_S
    heartbeat_s: float = HEARTBEAT_TIMEOUT_S
    frame_timeout_s: float = FRAME_TIMEOUT_S
    camera_timeout_s: float = DEFAULT_CAMERA_TIMEOUT_S
    control_ack_timeout_s: float = CONTROL_ACK_TIMEOUT_S
    shutdown_ack_timeout_s: float = SHUTDOWN_ACK_TIMEOUT_S
    http_wait_s: float | None = None
    max_stream_clients: int = MAX_STREAM_CLIENTS
    reconnect_delay_s: float = 1.0

    @property
    def control_wait_s(self) -> float:
        if self.http_wait_s is not None:
            return self.http_wait_s
        return self.frame_timeout_s + self.control_ack_timeout_s + 1.0


class _WorkerAbort:
    """Stop condition for blocking reads: shutdown or a due laser OFF."""

    def __init__(self, stop_event: threading.Event, state: FrameState) -> None:
        self.stop_event = stop_event
        self.state = state

    def is_set(self) -> bool:
        return self.stop_event.is_set() or self.state.laser_off_due() is not None


class Viewer:
    def __init__(
        self,
        config: ViewerConfig,
        opener: Callable[[str, int], Any] = open_serial,
        token: str | None = None,
    ) -> None:
        self.config = config
        self.opener = opener
        self.token = token or secrets.token_urlsafe(32)
        self.state = FrameState(config.laser_max_on_s, config.heartbeat_s)
        self.stop_event = threading.Event()
        self.controls: queue.Queue[SerialControl] = queue.Queue()
        self._port_lock = threading.Lock()
        self._port: Any = None

    # ---- worker thread -------------------------------------------------
    def _set_port(self, port: Any) -> None:
        with self._port_lock:
            self._port = port

    def _send(self, port: SerialPort, payload: bytes, prefixes: tuple[str, ...], timeout: float) -> str:
        return send_serial_control(port, payload, prefixes, timeout, on_line=self.state.handle_line)

    def _enforce_laser_limits(self, port: SerialPort) -> None:
        reason = self.state.laser_off_due()
        if reason is None:
            return
        print(f"LASER_AUTO_OFF {reason}", flush=True)
        try:
            response = self._send(port, LASER_OFF_COMMAND, ("LASER",), self.config.control_ack_timeout_s)
            self.state.set_laser(response, reason)
        except Exception as exc:
            # State stays "On", so the next loop iteration retries.
            self.state.set_error(f"Automatic laser OFF ({reason}) not confirmed: {exc}")
            print(f"LASER_AUTO_OFF_ERROR {exc}", flush=True)

    def _process_controls(self, port: SerialPort) -> None:
        # On stop, leave the rest to fail_pending_controls so shutdown stays bounded.
        while not self.stop_event.is_set():
            try:
                control = self.controls.get_nowait()
            except queue.Empty:
                return
            complete_control(control, port, self.state, self.config.control_ack_timeout_s, self.state.handle_line)

    def _idle(self, seconds: float, abort: _WorkerAbort) -> None:
        deadline = time.monotonic() + seconds
        while not abort.is_set() and self.controls.empty():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(0.05, remaining))

    def _safe_shutdown(self, port: SerialPort) -> None:
        try:
            self._send(port, b"O", ("TEST_LIGHT",), self.config.shutdown_ack_timeout_s)
            self.state.set_light((0, 0, 0))
            print("TEST_LIGHT_SAFE_OFF", flush=True)
        except Exception as exc:
            print(f"TEST_LIGHT_OFF_ERROR {exc}", flush=True)
        # Never leave a laser running on a port nobody is watching.
        was_on = self.state.laser == "On"
        try:
            response = self._send(port, LASER_OFF_COMMAND, ("LASER",), self.config.shutdown_ack_timeout_s)
            self.state.set_laser(response, "viewer_shutdown")
            print("LASER_SAFE_OFF", flush=True)
        except TimeoutError:
            # Older firmware without a laser command never answers; only a
            # laser we know was on makes this an unknown state.
            if was_on:
                self.state.laser_state_unknown = True
        except Exception as exc:
            print(f"LASER_OFF_ERROR {exc}", flush=True)
            if was_on or self.state.laser != "Off":
                self.state.laser_state_unknown = True

    def run_worker(self) -> None:
        config = self.config
        interval = 1.0 / config.fps
        abort = _WorkerAbort(self.stop_event, self.state)
        while not self.stop_event.is_set():
            port: Any = None
            try:
                port = self.opener(config.device, config.baud)
                self._set_port(port)
                wait_for_camera(port, time.monotonic() + config.camera_timeout_s, self.stop_event,
                                on_line=self.state.handle_line)
                self._send(port, b"O", ("TEST_LIGHT",), config.control_ack_timeout_s)
                self.state.set_light((0, 0, 0))
                try:
                    camera_status = self._send(port, b"S", ("CAMERA_CONTROLS",), config.control_ack_timeout_s)
                    self.state.set_camera_control("status", camera_status)
                except TimeoutError:
                    self.state.set_camera_control("unavailable", "CAMERA_CONTROLS unavailable · older board firmware")
                print(f"SERIAL_CONNECTED {config.device} {config.baud}", flush=True)
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    self._enforce_laser_limits(port)
                    self._process_controls(port)
                    self._enforce_laser_limits(port)
                    try:
                        frame = read_framed_jpeg(port, config.frame_timeout_s, abort, verify_image=False,
                                                 on_line=self.state.handle_line)
                    except StopRequested:
                        if self.stop_event.is_set():
                            break
                        # A laser deadline interrupted the frame: switch off first,
                        # then drop the half-read frame and resynchronise.
                        self._enforce_laser_limits(port)
                        reset = getattr(port, "reset_input_buffer", None)
                        if reset is not None:
                            reset()
                        continue
                    self.state.update(frame.payload, frame.width, frame.height, focus_score(frame.payload))
                    self._idle(interval - (time.monotonic() - started), abort)
            except StopRequested:
                pass
            except Exception as exc:
                self.state.set_error(f"Camera connection: {exc}")
                fail_pending_controls(self.controls, f"Camera connection: {exc}")
                print(f"CAPTURE_ERROR {exc}", flush=True)
                self.stop_event.wait(config.reconnect_delay_s)
            finally:
                if port is not None:
                    try:
                        self._safe_shutdown(port)
                    finally:
                        self._set_port(None)
                        try:
                            port.close()
                        except Exception as exc:
                            print(f"SERIAL_CLOSE_ERROR {exc}", flush=True)
        fail_pending_controls(self.controls, "Viewer is shutting down")

    # ---- shutdown ------------------------------------------------------
    def emergency_laser_off(self) -> None:
        """Last resort from the main thread while the worker is stuck."""
        with self._port_lock:
            port = self._port
        if port is None:
            return
        try:
            port.write(LASER_OFF_COMMAND)
            port.flush()
        except Exception as exc:
            print(f"LASER_EMERGENCY_OFF_ERROR {exc}", file=sys.stderr, flush=True)

    def shutdown(self, server: ThreadingHTTPServer | None, worker: threading.Thread,
                 join_timeout: float = WORKER_JOIN_TIMEOUT_S) -> int:
        """Stop everything; 0 when the laser is confirmed safe, else 1."""
        self.stop_event.set()
        if server is not None:
            server.server_close()
        worker.join(timeout=join_timeout)
        if worker.is_alive():
            self.emergency_laser_off()
            print(f"{LASER_STATE_UNKNOWN_WARNING} (serial worker did not stop within {join_timeout:g} s)",
                  file=sys.stderr, flush=True)
            return 1
        if self.state.laser_state_unknown:
            print(f"{LASER_STATE_UNKNOWN_WARNING} (laser OFF was not acknowledged)", file=sys.stderr, flush=True)
            return 1
        return 0


# ---- HTTP --------------------------------------------------------------
def host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET

    def __init__(self, address: tuple[str, int], viewer: Viewer) -> None:
        self.viewer = viewer
        self.bind_host = address[0]
        self.stream_slots = threading.BoundedSemaphore(viewer.config.max_stream_clients)
        super().__init__(address, ViewerHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def allowed_hosts(self) -> set[str]:
        names = {"127.0.0.1", "localhost", "[::1]", host_for_url(self.bind_host)}
        return {f"{name}:{self.port}".lower() for name in names}


class ViewerServer6(ViewerServer):
    address_family = socket.AF_INET6


def make_server(bind: str, port: int, viewer: Viewer) -> ViewerServer:
    server_class = ViewerServer6 if ":" in bind else ViewerServer
    return server_class((bind, port), viewer)


class RequestRejected(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer
    timeout = HANDLER_SOCKET_TIMEOUT_S
    server_version = "LaserViewer/2"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    @property
    def viewer(self) -> Viewer:
        return self.server.viewer

    def send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def reply(self, status: HTTPStatus, message: str, **extra: Any) -> None:
        ok = 200 <= int(status) < 300
        self.send_json(status, {"ok": ok, "message" if ok else "error": message, **extra})

    # ---- request checks ----------------------------------------------
    def check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            raise RequestRejected(HTTPStatus.MISDIRECTED_REQUEST, "Host header not allowed")

    def own_origin(self) -> str:
        return f"http://{(self.headers.get('Host') or '').strip().lower()}"

    def check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            if origin.strip().lower() != self.own_origin():
                raise RequestRejected(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return
        referer = self.headers.get("Referer")
        if referer:
            parts = urlsplit(referer)
            if f"{parts.scheme}://{parts.netloc}".lower() == self.own_origin():
                return
        raise RequestRejected(HTTPStatus.FORBIDDEN, "missing or foreign Origin/Referer")

    def token_matches(self, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied.encode("utf-8"), self.viewer.token.encode("utf-8"))

    def check_token(self) -> None:
        if not self.token_matches(self.headers.get(TOKEN_HEADER)):
            raise RequestRejected(HTTPStatus.FORBIDDEN, f"missing or invalid {TOKEN_HEADER}")

    def read_json_body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise RequestRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        self.check_token()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from None
        if not 1 <= length <= MAX_BODY_BYTES:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, f"request body must be 1..{MAX_BODY_BYTES} bytes")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from None
        if not isinstance(body, dict):
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return body

    # ---- GET ---------------------------------------------------------
    def do_GET(self) -> None:
        try:
            self.check_host()
            parts = urlsplit(self.path)
            if parts.path == "/":
                page = HTML_PATH.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, self.viewer.token)
                self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", page.encode("utf-8"))
            elif parts.path == "/viewer.js":
                self.send_bytes(HTTPStatus.OK, "text/javascript; charset=utf-8", JS_PATH.read_bytes())
            elif parts.path == "/health":
                self.send_json(HTTPStatus.OK, {"ok": True})
            elif parts.path == "/status.json":
                self.check_token()
                self.viewer.state.mark_poll()
                self.send_json(HTTPStatus.OK, self.viewer.state.status())
            elif parts.path == "/stream.mjpg":
                token = parse_qs(parts.query).get("token", [None])[0]
                if not self.token_matches(token):
                    raise RequestRejected(HTTPStatus.FORBIDDEN, "missing or invalid stream token")
                self.stream()
            else:
                self.reply(HTTPStatus.NOT_FOUND, "not found")
        except RequestRejected as exc:
            self.reply(exc.status, str(exc))

    def stream(self) -> None:
        if not self.server.stream_slots.acquire(blocking=False):
            raise RequestRejected(HTTPStatus.SERVICE_UNAVAILABLE, "too many stream clients")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            state = self.viewer.state
            stop_event = self.viewer.stop_event
            seen = -1

            def changed(previous: int) -> Callable[[], bool]:
                return lambda: state.sequence != previous or stop_event.is_set()

            while not stop_event.is_set():
                with state.condition:
                    state.condition.wait_for(changed(seen), timeout=2.0)
                    frame = state.frame
                    seen = state.sequence
                if frame is None or stop_event.is_set():
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.stream_slots.release()

    # ---- POST --------------------------------------------------------
    def do_POST(self) -> None:
        try:
            self.check_host()
            self.check_origin()
            body = self.read_json_body()
            path = urlsplit(self.path).path
            handler = {
                "/light": self.post_light,
                "/camera": self.post_camera,
                "/laser": self.post_laser,
                "/save": self.post_save,
                "/shutdown": self.post_shutdown,
            }.get(path)
            if handler is None:
                raise RequestRejected(HTTPStatus.NOT_FOUND, "not found")
            handler(body)
        except RequestRejected as exc:
            self.reply(exc.status, str(exc))

    def run_control(self, control: SerialControl) -> bool:
        """Queue a control and wait. False on timeout (control withdrawn if still pending)."""
        self.viewer.controls.put(control)
        if control.completed.wait(self.viewer.config.control_wait_s):
            return True
        control.cancel()
        return False

    def post_light(self, body: dict[str, Any]) -> None:
        try:
            values = tuple(body[channel] for channel in ("r", "g", "b"))
        except KeyError as exc:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, f"Invalid LED command: missing {exc}") from None
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 64 for value in values):
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "Invalid LED command: RGB values must be integers from 0 to 64")
        rgb = (int(values[0]), int(values[1]), int(values[2]))
        control = SerialControl(payload=f"X{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}".encode("ascii"), rgb=rgb)
        if not self.run_control(control):
            raise RequestRejected(HTTPStatus.GATEWAY_TIMEOUT, "LED command not acknowledged in time")
        if control.error:
            raise RequestRejected(HTTPStatus.SERVICE_UNAVAILABLE, f"LED error: {control.error}")
        self.reply(HTTPStatus.OK, f"LED RGB {rgb[0]}/{rgb[1]}/{rgb[2]} · acknowledged")

    def post_camera(self, body: dict[str, Any]) -> None:
        commands = {
            "status": (b"S", ("CAMERA_CONTROLS",)),
            "lock": (b"K", ("CAMERA_AUTOMATION", "CAMERA_CONTROLS")),
            "auto": (b"A", ("CAMERA_AUTOMATION", "CAMERA_CONTROLS")),
        }
        action = body.get("action")
        if not isinstance(action, str) or action not in commands:
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "Invalid camera command: action must be status, lock or auto")
        payload, expected_prefixes = commands[action]
        control = SerialControl(payload=payload, expected_prefixes=expected_prefixes, camera_action=action)
        if not self.run_control(control):
            raise RequestRejected(HTTPStatus.GATEWAY_TIMEOUT, "Camera command not acknowledged in time")
        if control.error:
            raise RequestRejected(HTTPStatus.SERVICE_UNAVAILABLE, f"Camera error: {control.error}")
        self.reply(HTTPStatus.OK, control.response)

    def post_laser(self, body: dict[str, Any]) -> None:
        on = body.get("on")
        if not isinstance(on, bool):
            raise RequestRejected(HTTPStatus.BAD_REQUEST, "Invalid laser command: 'on' must be true or false")
        self.viewer.state.mark_poll()
        control = SerialControl(
            payload=laser_command(on), expected_prefixes=("LASER",), laser_action=True,
            laser_reason=None if on else "user",
        )
        if not self.run_control(control):
            if on:
                # The worker may have sent the ON just before we gave up; make
                # sure an OFF follows either way.
                self.viewer.controls.put(SerialControl(
                    payload=LASER_OFF_COMMAND, expected_prefixes=("LASER",), laser_action=True,
                    laser_reason="http_timeout",
                ))
                raise RequestRejected(HTTPStatus.GATEWAY_TIMEOUT,
                                      "Laser ON not acknowledged in time · OFF queued · older firmware?")
            raise RequestRejected(HTTPStatus.GATEWAY_TIMEOUT, "Laser OFF not acknowledged in time · use the manual switch")
        if control.error:
            raise RequestRejected(HTTPStatus.SERVICE_UNAVAILABLE, f"Laser error: {control.error}")
        self.reply(HTTPStatus.OK, control.response, laser=self.viewer.state.status()["laser"])

    def post_save(self, _body: dict[str, Any]) -> None:
        state = self.viewer.state
        with state.condition:
            frame = state.frame
            sequence = state.sequence
        if frame is None:
            raise RequestRejected(HTTPStatus.SERVICE_UNAVAILABLE, "No frame available yet")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = self.viewer.config.save_dir / f"live_{timestamp}_{sequence:06d}.jpg"
        try:
            write_atomic(destination, frame)
        except OSError as exc:
            raise RequestRejected(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not save frame: {exc}") from None
        self.reply(HTTPStatus.OK, f"Saved: {destination}", file=str(destination))

    def post_shutdown(self, _body: dict[str, Any]) -> None:
        self.reply(HTTPStatus.OK, "Viewer is shutting down")
        self.viewer.stop_event.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description(__doc__),
        epilog="Exit codes: 0 clean shutdown with laser confirmed off, 1 laser state unknown or startup failure, "
               "2 usage error.",
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"serial device (default {DEFAULT_PORT})")
    parser.add_argument("--device", dest="port", help=argparse.SUPPRESS)
    parser.add_argument("--baud", type=positive_int, default=DEFAULT_BAUD, help=f"baud rate (default {DEFAULT_BAUD})")
    parser.add_argument("--bind", default="127.0.0.1", choices=LOCAL_BIND_HOSTS,
                        help="local address to serve on (default 127.0.0.1); never a network interface")
    parser.add_argument("--http-port", type=int_range(0, 65535), default=8765,
                        help="HTTP port (default 8765; 0 picks a free port)")
    parser.add_argument("--fps", type=float_range(0.5, 10.0), default=5.0, help="frame rate limit 0.5..10 (default 5)")
    parser.add_argument("--save-dir", type=Path, default=None,
                        help="where saved frames go (default $LASER_DATA_DIR/live, else <repo>/data/live)")
    parser.add_argument("--laser-max-on-s", type=float_range(LASER_MAX_ON_MIN_S, LASER_MAX_ON_MAX_S),
                        default=DEFAULT_LASER_MAX_ON_S,
                        help=f"host-side maximum laser on-time {LASER_MAX_ON_MIN_S:g}..{LASER_MAX_ON_MAX_S:g} s "
                             f"(default {DEFAULT_LASER_MAX_ON_S:g})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = ViewerConfig(
        device=args.port, baud=args.baud, fps=args.fps,
        save_dir=args.save_dir if args.save_dir is not None else data_dir() / "live",
        laser_max_on_s=args.laser_max_on_s,
    )
    viewer = Viewer(config)
    try:
        server = make_server(args.bind, args.http_port, viewer)
    except OSError as exc:
        raise fail("live_viewer", f"cannot serve on {args.bind}:{args.http_port}: {exc}") from None
    worker = threading.Thread(target=viewer.run_worker, name="serial-worker", daemon=True)
    worker.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        viewer.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(f"VIEWER_READY http://{host_for_url(args.bind)}:{server.port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        code = viewer.shutdown(server, worker)
    return code


if __name__ == "__main__":
    sys.exit(main())
