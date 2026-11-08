#!/usr/bin/env python3
"""Localhost-only MJPEG viewer for the GOOUUU ESP32-S3-CAM TTL link."""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import serial
from PIL import Image, ImageFilter, ImageStat

from capture_frame import (
    fnv1a32,
    read_exact,
    read_frame_end,
    read_header,
    wait_for_camera,
)

HTML_PATH = Path(__file__).with_name("live_viewer.html")



@dataclass
class SerialControl:
    payload: bytes
    rgb: tuple[int, int, int] | None = None
    expected_prefixes: tuple[str, ...] = ("TEST_LIGHT",)
    camera_action: str = ""
    laser_action: bool = False
    completed: threading.Event = field(default_factory=threading.Event)
    response: str = ""
    error: str = ""
    cancelled: bool = False


class FrameState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
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

    def set_laser(self, response: str) -> None:
        with self.condition:
            if "LASER ON" in response:
                self.laser = "On"
            elif "LASER OFF" in response:
                self.laser = "Off"
            else:
                self.laser = "Unavailable"
            self.condition.notify_all()

    def set_camera_control(self, action: str, response: str) -> None:
        with self.condition:
            if action == "unavailable":
                self.camera_automation = "Unavailable"
            elif all(token in response for token in ("aec=0", "agc=0", "awb=0", "awb_gain=0")):
                self.camera_automation = "Locked"
            elif all(token in response for token in ("aec=1", "agc=1", "awb=1")) and (
                "awb_gain=1" in response
                or all(token in response for token in ("awb_gain=0", "wb_mode=0"))
            ):
                self.camera_automation = "Automatic"
            else:
                self.camera_automation = "Unconfirmed"
            self.camera_controls = response
            self.condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self.condition:
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


def request_frame(port: serial.Serial, timeout: float) -> tuple[bytes, int, int]:
    port.write(b"C")
    port.flush()
    deadline = time.monotonic() + timeout
    fields = read_header(port, deadline)
    length = int(fields[1])
    width = int(fields[2])
    height = int(fields[3])
    expected_hash = int(fields[5], 16)
    if not 1024 <= length <= 2_000_000:
        raise RuntimeError(f"Implausible JPEG length: {length}")
    payload = read_exact(port, length, deadline)
    read_frame_end(port, deadline)
    if not payload.startswith(b"\xFF\xD8") or not payload.endswith(b"\xFF\xD9"):
        raise RuntimeError("Invalid JPEG markers")
    actual_hash = fnv1a32(payload)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Checksum mismatch device={expected_hash:08X} host={actual_hash:08X}"
        )
    return payload, width, height


def open_serial(device: str, baud: int) -> serial.Serial:
    port = serial.Serial()
    port.port = device
    port.baudrate = baud
    port.timeout = 0.15
    port.write_timeout = 2.0
    port.dtr = False
    port.rts = False
    port.open()
    port.reset_input_buffer()
    return port


def send_serial_control(
    port: serial.Serial,
    payload: bytes,
    expected_prefixes: tuple[str, ...] = ("TEST_LIGHT",),
    timeout: float = 2.0,
) -> str:
    port.write(payload)
    port.flush()
    deadline = time.monotonic() + timeout
    matched: dict[str, str] = {}
    while time.monotonic() < deadline:
        line = port.readline().decode("ascii", errors="ignore").strip()
        for prefix in expected_prefixes:
            if line.startswith(prefix):
                if " ERROR" in line or " FAIL" in line:
                    raise RuntimeError(line)
                matched[prefix] = line
        if len(matched) == len(expected_prefixes):
            return " | ".join(matched[prefix] for prefix in expected_prefixes)
    raise TimeoutError(f"No response to {payload!r}")


def complete_control(
    control: SerialControl,
    port: serial.Serial,
    state: FrameState,
) -> None:
    if control.cancelled:
        control.completed.set()
        return
    try:
        control.response = send_serial_control(
            port, control.payload, expected_prefixes=control.expected_prefixes
        )
        if control.camera_action == "lock" and not all(
            token in control.response
            for token in ("aec=0", "agc=0", "awb=0", "awb_gain=0")
        ):
            raise RuntimeError(
                f"Camera lock not fully confirmed by readback: {control.response}"
            )
        auto_controls_enabled = all(
            token in control.response for token in ("aec=1", "agc=1", "awb=1")
        ) and (
            "awb_gain=1" in control.response
            or all(
                token in control.response
                for token in ("awb_gain=0", "wb_mode=0")
            )
        )
        if control.camera_action == "auto" and not auto_controls_enabled:
            raise RuntimeError(
                f"Camera automation not fully confirmed by readback: {control.response}"
            )
        if control.rgb is not None:
            state.set_light(control.rgb)
        if control.camera_action:
            state.set_camera_control(control.camera_action, control.response)
        if control.laser_action:
            state.set_laser(control.response)
    except Exception as exc:
        control.error = str(exc)
    finally:
        control.completed.set()


def fail_pending_controls(
    controls: queue.Queue[SerialControl],
    message: str,
) -> None:
    while True:
        try:
            control = controls.get_nowait()
        except queue.Empty:
            return
        control.error = message
        control.completed.set()


def capture_worker(
    state: FrameState,
    stop_event: threading.Event,
    controls: queue.Queue[SerialControl],
    device: str,
    baud: int,
    fps_limit: float,
) -> None:
    interval = 1.0 / fps_limit
    while not stop_event.is_set():
        port: serial.Serial | None = None
        try:
            port = open_serial(device, baud)
            wait_for_camera(port, time.monotonic() + 12.0)
            send_serial_control(port, b"O")
            state.set_light((0, 0, 0))
            try:
                camera_status = send_serial_control(
                    port, b"S", expected_prefixes=("CAMERA_CONTROLS",)
                )
                state.set_camera_control("status", camera_status)
            except TimeoutError:
                state.set_camera_control(
                    "unavailable",
                    "CAMERA_CONTROLS unavailable · older board firmware",
                )
            print(f"SERIAL_CONNECTED {device} {baud}", flush=True)
            while not stop_event.is_set():
                started = time.monotonic()
                while True:
                    try:
                        control = controls.get_nowait()
                    except queue.Empty:
                        break
                    complete_control(control, port, state)
                payload, width, height = request_frame(port, timeout=8.0)
                state.update(payload, width, height, focus_score(payload))
                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    stop_event.wait(remaining)
        except Exception as exc:
            state.set_error(f"Camera connection: {exc}")
            fail_pending_controls(controls, f"Camera connection: {exc}")
            print(f"CAPTURE_ERROR {exc}", flush=True)
            stop_event.wait(1.0)
        finally:
            if port is not None and port.is_open:
                try:
                    send_serial_control(port, b"O", timeout=1.0)
                    state.set_light((0, 0, 0))
                    print("TEST_LIGHT_SAFE_OFF", flush=True)
                except Exception as exc:
                    print(f"TEST_LIGHT_OFF_ERROR {exc}", flush=True)
                # Never leave a laser running on a port nobody is watching.
                # Older firmware simply does not answer; that is not an error.
                try:
                    send_serial_control(port, b"0", ("LASER",), timeout=1.0)
                    state.set_laser("LASER OFF")
                    print("LASER_SAFE_OFF", flush=True)
                except TimeoutError:
                    pass
                except Exception as exc:
                    print(f"LASER_OFF_ERROR {exc}", flush=True)
                port.close()
    fail_pending_controls(controls, "Viewer is shutting down")


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state: FrameState,
        stop_event: threading.Event,
        controls: queue.Queue[SerialControl],
        save_dir: Path,
    ) -> None:
        super().__init__(address, ViewerHandler)
        self.state = state
        self.stop_event = stop_event
        self.controls = controls
        self.save_dir = save_dir


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", HTML_PATH.read_bytes())
        elif path in {"/status.json", "/health"}:
            payload = json.dumps(self.server.state.status()).encode()
            self.send_bytes(HTTPStatus.OK, "application/json", payload)
        elif path == "/stream.mjpg":
            self.send_response(HTTPStatus.OK)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            sequence = -1
            try:
                while not self.server.stop_event.is_set():
                    with self.server.state.condition:
                        self.server.state.condition.wait_for(
                            lambda seen=sequence: self.server.state.sequence != seen
                            or self.server.stop_event.is_set(),
                            timeout=2.0,
                        )
                        frame = self.server.state.frame
                        sequence = self.server.state.sequence
                    if frame is None:
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_bytes(HTTPStatus.NOT_FOUND, "text/plain", b"Not found\n")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/light":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= content_length <= 256:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(content_length))
                values = tuple(body[channel] for channel in ("r", "g", "b"))
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 64
                    for value in values
                ):
                    raise ValueError("RGB values must be integers from 0 to 64")
                rgb = (values[0], values[1], values[2])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    "text/plain; charset=utf-8",
                    f"Invalid LED command: {exc}".encode(),
                )
                return

            control = SerialControl(
                payload=f"X{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}".encode("ascii"),
                rgb=rgb,
            )
            self.server.controls.put(control)
            if not control.completed.wait(3.0):
                control.cancelled = True
                self.send_bytes(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "text/plain; charset=utf-8",
                    b"LED command not acknowledged in time",
                )
                return
            if control.error:
                self.send_bytes(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "text/plain; charset=utf-8",
                    f"LED error: {control.error}".encode(),
                )
                return
            self.send_bytes(
                HTTPStatus.OK,
                "text/plain; charset=utf-8",
                f"LED RGB {rgb[0]}/{rgb[1]}/{rgb[2]} · acknowledged".encode(),
            )
        elif path == "/camera":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= content_length <= 256:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(content_length))
                action = body["action"]
                commands = {
                    "status": (b"S", ("CAMERA_CONTROLS",)),
                    "lock": (b"K", ("CAMERA_AUTOMATION", "CAMERA_CONTROLS")),
                    "auto": (b"A", ("CAMERA_AUTOMATION", "CAMERA_CONTROLS")),
                }
                payload, expected_prefixes = commands[action]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    "text/plain; charset=utf-8",
                    f"Invalid camera command: {exc}".encode(),
                )
                return

            control = SerialControl(
                payload=payload,
                expected_prefixes=expected_prefixes,
                camera_action=action,
            )
            self.server.controls.put(control)
            if not control.completed.wait(3.0):
                control.cancelled = True
                self.send_bytes(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "text/plain; charset=utf-8",
                    b"Camera command not acknowledged in time",
                )
                return
            if control.error:
                self.send_bytes(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "text/plain; charset=utf-8",
                    f"Camera error: {control.error}".encode(),
                )
                return
            self.send_bytes(
                HTTPStatus.OK,
                "text/plain; charset=utf-8",
                control.response.encode(),
            )
        elif path == "/laser":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= content_length <= 256:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(content_length))
                on = body["on"]
                if not isinstance(on, bool):
                    raise ValueError("'on' must be true or false")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self.send_bytes(
                    HTTPStatus.BAD_REQUEST,
                    "text/plain; charset=utf-8",
                    f"Invalid laser command: {exc}".encode(),
                )
                return

            control = SerialControl(
                payload=b"1" if on else b"0",
                expected_prefixes=("LASER",),
                laser_action=True,
            )
            self.server.controls.put(control)
            if not control.completed.wait(3.0):
                control.cancelled = True
                self.send_bytes(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "text/plain; charset=utf-8",
                    "Laser command not acknowledged in time · older firmware?".encode(),
                )
                return
            if control.error:
                self.send_bytes(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "text/plain; charset=utf-8",
                    f"Laser error: {control.error}".encode(),
                )
                return
            self.send_bytes(
                HTTPStatus.OK, "text/plain; charset=utf-8", control.response.encode()
            )
        elif path == "/save":
            with self.server.state.condition:
                frame = self.server.state.frame
                sequence = self.server.state.sequence
            if frame is None:
                self.send_bytes(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "text/plain; charset=utf-8",
                    b"No frame available yet",
                )
                return
            self.server.save_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            destination = self.server.save_dir / f"live_{timestamp}_{sequence:06d}.jpg"
            temporary = destination.with_suffix(".jpg.partial")
            temporary.write_bytes(frame)
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
            self.send_bytes(
                HTTPStatus.OK,
                "text/plain; charset=utf-8",
                f"Saved: {destination}".encode(),
            )
        elif path == "/shutdown":
            self.send_bytes(
                HTTPStatus.OK,
                "text/plain; charset=utf-8",
                b"Viewer is shutting down",
            )
            self.server.stop_event.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_bytes(HTTPStatus.NOT_FOUND, "text/plain", b"Not found\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=Path(os.environ.get("LASER_DATA_DIR", "data")) / "live",
        help="where saved frames go (default: $LASER_DATA_DIR/live, else ./data/live)",
    )
    args = parser.parse_args()
    if args.bind not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Refusing non-local bind address")
    if not 0.5 <= args.fps <= 10.0:
        raise SystemExit("--fps must be between 0.5 and 10")

    state = FrameState()
    stop_event = threading.Event()
    controls: queue.Queue[SerialControl] = queue.Queue()
    worker = threading.Thread(
        target=capture_worker,
        args=(state, stop_event, controls, args.device, args.baud, args.fps),
        daemon=True,
    )
    worker.start()
    server = ViewerServer(
        (args.bind, args.http_port), state, stop_event, controls, args.save_dir
    )

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(f"VIEWER_READY http://{args.bind}:{args.http_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stop_event.set()
        server.server_close()
        worker.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
