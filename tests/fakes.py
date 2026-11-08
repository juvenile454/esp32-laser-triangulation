"""In-memory serial fakes for host-tool tests. No hardware, no serial ports."""

from __future__ import annotations

import io
import random
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from serial_protocol import LASER_OFF_COMMAND, LASER_ON_COMMAND, fnv1a32  # noqa: E402


def make_jpeg(width: int = 128, height: int = 96, seed: int = 1) -> bytes:
    """A noisy JPEG comfortably above the protocol's 1024-byte minimum."""
    from PIL import Image

    rng = random.Random(seed)
    image = Image.frombytes("RGB", (width, height), bytes(rng.randrange(256) for _ in range(width * height * 3)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def frame_bytes(
    payload: bytes,
    width: int,
    height: int,
    *,
    eol: bytes = b"\r\n",
    length: int | None = None,
    checksum: int | None = None,
    end: bytes | None = None,
) -> bytes:
    """Encode one frame exactly as the firmware does, with optional corruption."""
    header = (
        f"FRAME_BEGIN {len(payload) if length is None else length} {width} {height} 4 "
        f"{fnv1a32(payload) if checksum is None else checksum:08X}"
    ).encode("ascii")
    trailer = eol + b"FRAME_END" + eol if end is None else end
    return header + eol + payload + trailer


class FakeSerial:
    """Duck-typed stand-in for ``serial.Serial``.

    Bytes passed to :meth:`feed` are what the "device" sends. ``responder`` is
    called with every write and may return bytes to feed back.
    """

    def __init__(
        self,
        data: bytes = b"",
        responder: Callable[[bytes], bytes] | None = None,
        timeout: float = 0.01,
    ) -> None:
        self._buffer = bytearray(data)
        self._condition = threading.Condition()
        self.responder = responder
        self.timeout = timeout
        self.writes: list[bytes] = []
        self.flushes = 0
        self.is_open = True

    def feed(self, data: bytes) -> None:
        with self._condition:
            self._buffer += data
            self._condition.notify_all()

    def _wait_for(self, predicate: Callable[[], bool]) -> None:
        self._condition.wait_for(predicate, timeout=self.timeout)

    def read(self, size: int = 1) -> bytes:
        with self._condition:
            self._wait_for(lambda: bool(self._buffer))
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk

    def readline(self) -> bytes:
        with self._condition:
            self._wait_for(lambda: b"\n" in self._buffer)
            index = self._buffer.find(b"\n")
            end = len(self._buffer) if index < 0 else index + 1
            line = bytes(self._buffer[:end])
            del self._buffer[:end]
            return line

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("port closed")
        payload = bytes(data)
        with self._condition:
            self.writes.append(payload)
        if self.responder is not None:
            response = self.responder(payload)
            if response:
                self.feed(response)
        return len(payload)

    def flush(self) -> None:
        self.flushes += 1

    def reset_input_buffer(self) -> None:
        with self._condition:
            self._buffer.clear()

    def close(self) -> None:
        self.is_open = False


class FakeBoard:
    """Scriptable firmware emulation for the viewer and capture tools."""

    def __init__(self, width: int = 128, height: int = 96, eol: bytes = b"\n") -> None:
        self.width = width
        self.height = height
        self.eol = eol
        self.jpeg = make_jpeg(width, height)
        self.laser_on = False
        self.laser_supported = True
        self.stall_frames = threading.Event()
        self.laser_on_delay_s = 0.0
        self.port: FakeSerial | None = None
        self.commands: list[bytes] = []
        self._lock = threading.Lock()

    def attach(self, timeout: float = 0.01) -> FakeSerial:
        self.port = FakeSerial(responder=self, timeout=timeout)
        return self.port

    def line(self, text: str) -> bytes:
        return text.encode("ascii") + self.eol

    def laser_line(self) -> bytes:
        return self.line(f"LASER {'ON' if self.laser_on else 'OFF'} pin=14")

    def unsolicited(self, text: str) -> None:
        assert self.port is not None
        self.port.feed(self.line(text))

    def command_log(self) -> list[bytes]:
        with self._lock:
            return list(self.commands)

    def __call__(self, payload: bytes) -> bytes:
        with self._lock:
            self.commands.append(payload)
        if payload == LASER_ON_COMMAND or payload == LASER_OFF_COMMAND:
            if not self.laser_supported:
                return b""
            self.laser_on = payload == LASER_ON_COMMAND
            if self.laser_on and self.laser_on_delay_s > 0:
                response = self.laser_line()
                timer = threading.Timer(self.laser_on_delay_s, lambda: self.port.feed(response) if self.port else None)
                timer.daemon = True
                timer.start()
                return b""
            return self.laser_line()
        command = payload[:1]
        if command == b"P":
            return self.line("CAMERA_STATUS READY") + (self.laser_line() if self.laser_supported else b"")
        if command == b"C":
            if self.stall_frames.is_set():
                return b""
            return frame_bytes(self.jpeg, self.width, self.height, eol=b"\r\n")
        if command in {b"O", b"X"}:
            rgb = (0, 0, 0) if command == b"O" else tuple(int(payload[i:i + 2], 16) for i in (1, 3, 5))
            return self.line(f"TEST_LIGHT RGB r={rgb[0]} g={rgb[1]} b={rgb[2]} pin=48 max=64")
        if command == b"S":
            return self.line("CAMERA_CONTROLS aec=1 aec_value=300 agc=1 agc_gain=4 awb=1 awb_gain=1 wb_mode=0")
        if command == b"K":
            return self.line("CAMERA_AUTOMATION LOCKED PASS retained_aec_value=300 retained_agc_gain=4 "
                             "retained_wb_mode=0 results=0/0/0/0") + self.line(
                "CAMERA_CONTROLS aec=0 aec_value=300 agc=0 agc_gain=4 awb=0 awb_gain=0 wb_mode=0")
        if command == b"A":
            return self.line("CAMERA_AUTOMATION AUTO PASS results=0/0/0/0") + self.line(
                "CAMERA_CONTROLS aec=1 aec_value=300 agc=1 agc_gain=4 awb=1 awb_gain=1 wb_mode=0")
        return b""


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
