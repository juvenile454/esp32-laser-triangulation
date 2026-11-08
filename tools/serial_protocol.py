"""Host side of the ESP32-S3-CAM USB-UART protocol, shared by all serial tools.

Wire format assumptions (see src/main.cpp):

* Text lines end in ``\\n`` or ``\\r\\n``; both are accepted everywhere.
* ``C`` answers ``FRAME_BEGIN <len> <width> <height> <format> <fnv1a32-hex>``,
  then exactly ``len`` JPEG bytes, then ``FRAME_END`` (or ``FRAME_ERROR ...``).
* ``P`` answers ``CAMERA_STATUS READY|FAILED`` and a ``LASER ON|OFF`` line.
* ``LASER ON|OFF pin=...`` lines may also arrive unsolicited, e.g. from a
  firmware on-time watchdog: ``LASER OFF pin=14 reason=timeout on_ms=...``.
* ``PAIR_END`` may carry trailing fields; ``ok=0`` marks a failed pair.

The byte sequence that switches the laser on lives in exactly one place,
:data:`LASER_ON_COMMAND`, so a later framed command needs a single change.
"""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import serial

FRAME_MIN_BYTES = 1024
FRAME_MAX_BYTES = 2_000_000
DEFAULT_CAMERA_TIMEOUT_S = 12.0
DEFAULT_BAUD = 921600
DEFAULT_PORT = "/dev/ttyUSB0"

# The ONLY definition of the laser switching commands. Firmware today takes a
# bare '1' / '0'; a framed command (e.g. b"@LASER ON\n") only changes these.
LASER_ON_COMMAND = b"1"
LASER_OFF_COMMAND = b"0"


def laser_command(on: bool) -> bytes:
    return LASER_ON_COMMAND if on else LASER_OFF_COMMAND


class ProtocolError(RuntimeError):
    """The device answered, but not in the documented format."""


class StopRequested(Exception):
    """A blocking read was abandoned because its stop condition became true."""


class StopCondition(Protocol):
    def is_set(self) -> bool: ...


class SerialPort(Protocol):
    def read(self, size: int = 1, /) -> bytes: ...
    def readline(self) -> bytes: ...
    def write(self, data: bytes, /) -> int | None: ...
    def flush(self) -> None: ...


LineHandler = Callable[[str], None]


def fnv1a32(data: bytes) -> int:
    value = 0x811C9DC5
    for byte in data:
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def open_port(
    device: str,
    baud: int = DEFAULT_BAUD,
    *,
    timeout: float = 0.2,
    write_timeout: float | None = 2.0,
) -> serial.Serial:
    """Open the board without toggling DTR/RTS (which would reset the ESP32)."""
    port = serial.Serial()
    port.port = device
    port.baudrate = baud
    port.timeout = timeout
    port.write_timeout = write_timeout
    port.dtr = False
    port.rts = False
    port.open()
    port.reset_input_buffer()
    return port


def decode_line(raw: bytes) -> str:
    """ASCII text of one line without its LF or CRLF terminator."""
    return raw.decode("ascii", errors="replace").strip()


def _check_stop(stop_event: StopCondition | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise StopRequested("stop requested")


def send(port: SerialPort, payload: bytes) -> None:
    port.write(payload)
    port.flush()


def read_line(port: SerialPort, deadline: float, stop_event: StopCondition | None = None) -> str:
    """Next non-empty line before ``deadline`` (time.monotonic)."""
    while time.monotonic() < deadline:
        _check_stop(stop_event)
        line = decode_line(port.readline())
        if line:
            return line
    raise TimeoutError("no response from device")


def matches_prefix(line: str, prefix: str) -> bool:
    """``line`` starts with the whole word(s) ``prefix`` (``LASER`` != ``LASER_PIN:``)."""
    return line == prefix or line.startswith(prefix + " ")


def expect_line(
    port: SerialPort,
    prefix: str,
    deadline: float,
    stop_event: StopCondition | None = None,
    on_line: LineHandler | None = None,
) -> str:
    """Wait for a line starting with ``prefix``; ``*_ERROR`` lines raise."""
    while True:
        line = read_line(port, deadline, stop_event)
        if matches_prefix(line, prefix):
            return line
        first = line.split(maxsplit=1)[0]
        if first.endswith("_ERROR") or " ERROR" in line:
            raise ProtocolError(line)
        if on_line is not None:
            on_line(line)


def read_exact(
    port: SerialPort, length: int, deadline: float, stop_event: StopCondition | None = None
) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        _check_stop(stop_event)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"JPEG timeout: {remaining} of {length} bytes missing")
        chunk = port.read(min(remaining, 4096))
        if chunk:
            chunks.append(chunk)
            remaining -= len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class FrameHeader:
    length: int
    width: int
    height: int
    pixel_format: int
    fnv1a32: int


@dataclass(frozen=True)
class Frame:
    payload: bytes
    width: int
    height: int
    pixel_format: int
    fnv1a32: int


def parse_frame_header(line: str) -> FrameHeader:
    fields = line.split()
    if len(fields) != 6 or fields[0] != "FRAME_BEGIN":
        raise ProtocolError(f"malformed frame header (expected 6 fields): {line!r}")
    try:
        header = FrameHeader(
            length=int(fields[1], 10),
            width=int(fields[2], 10),
            height=int(fields[3], 10),
            pixel_format=int(fields[4], 10),
            fnv1a32=int(fields[5], 16),
        )
    except ValueError as exc:
        raise ProtocolError(f"malformed frame header (non-numeric field): {line!r}") from exc
    if not FRAME_MIN_BYTES <= header.length <= FRAME_MAX_BYTES:
        raise ProtocolError(f"implausible JPEG length: {header.length}")
    if header.width < 1 or header.height < 1:
        raise ProtocolError(f"implausible frame size: {header.width}x{header.height}")
    return header


def read_header(
    port: SerialPort,
    deadline: float,
    stop_event: StopCondition | None = None,
    on_line: LineHandler | None = None,
) -> FrameHeader:
    """Skip status chatter until ``FRAME_BEGIN``; ``FRAME_ERROR`` raises."""
    while True:
        try:
            line = read_line(port, deadline, stop_event)
        except TimeoutError:
            raise TimeoutError("no FRAME_BEGIN received") from None
        if line.startswith("FRAME_ERROR"):
            raise ProtocolError(line)
        if line.startswith("FRAME_BEGIN"):
            return parse_frame_header(line)
        if on_line is not None:
            on_line(line)


def read_frame_end(port: SerialPort, deadline: float, stop_event: StopCondition | None = None) -> None:
    try:
        line = read_line(port, deadline, stop_event)
    except TimeoutError:
        raise TimeoutError("no FRAME_END received") from None
    if line != "FRAME_END":
        raise ProtocolError(f"unexpected data after JPEG: {line!r}")


def verify_jpeg(payload: bytes, width: int, height: int) -> None:
    from PIL import Image  # deferred: Pillow is only needed for verification

    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        if image.size != (width, height):
            raise ProtocolError(f"dimension mismatch: header={width}x{height}, JPEG={image.size[0]}x{image.size[1]}")


def read_frame(
    port: SerialPort,
    deadline: float,
    stop_event: StopCondition | None = None,
    verify_image: bool = True,
    on_line: LineHandler | None = None,
) -> Frame:
    """Read one framed JPEG: header, exact length, FRAME_END, markers, FNV-1a."""
    header = read_header(port, deadline, stop_event, on_line)
    payload = read_exact(port, header.length, deadline, stop_event)
    read_frame_end(port, deadline, stop_event)
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise ProtocolError("JPEG SOI/EOI markers are invalid")
    actual = fnv1a32(payload)
    if actual != header.fnv1a32:
        raise ProtocolError(f"FNV-1a mismatch: device={header.fnv1a32:08X}, host={actual:08X}")
    if verify_image:
        verify_jpeg(payload, header.width, header.height)
    return Frame(payload, header.width, header.height, header.pixel_format, actual)


def request_frame(
    port: SerialPort,
    timeout: float,
    stop_event: StopCondition | None = None,
    verify_image: bool = True,
    on_line: LineHandler | None = None,
) -> Frame:
    send(port, b"C")
    return read_frame(port, time.monotonic() + timeout, stop_event, verify_image, on_line)


def camera_readiness(line: str) -> bool | None:
    """True for a READY report, False for FAILED, None for unrelated lines."""
    fields = line.split()
    if fields[:1] == ["CAMERA_STATUS"] and len(fields) >= 2:
        return {"READY": True, "FAILED": False}.get(fields[1])
    if fields[:1] == ["CAMERA_RESULT:"] and len(fields) >= 2:
        return {"PASS": True, "FAIL": False}.get(fields[1])
    if fields[:1] == ["ALIVE"]:
        camera = parse_key_values(line).get("camera")
        return {"READY": True, "FAILED": False}.get(camera or "")
    return None


def wait_for_camera(
    port: SerialPort,
    deadline: float,
    stop_event: StopCondition | None = None,
    on_line: LineHandler | None = None,
    probe_interval: float = 0.4,
) -> None:
    next_probe = 0.0
    while time.monotonic() < deadline:
        _check_stop(stop_event)
        now = time.monotonic()
        if now >= next_probe:
            send(port, b"P")
            next_probe = now + probe_interval
        line = decode_line(port.readline())
        if not line:
            continue
        ready = camera_readiness(line)
        if ready is True:
            return
        if ready is False:
            raise ProtocolError("camera firmware reports FAILED")
        if on_line is not None:
            on_line(line)
    raise TimeoutError("camera did not become READY")


def parse_key_values(line: str) -> dict[str, str]:
    """All ``key=value`` tokens of a line (later duplicates win)."""
    values: dict[str, str] = {}
    for token in line.split():
        key, separator, value = token.partition("=")
        if separator and key:
            values[key] = value
    return values


def parse_controls(line: str) -> dict[str, int]:
    """Integer camera readback fields from a CAMERA_CONTROLS response."""
    controls: dict[str, int] = {}
    for key, value in parse_key_values(line).items():
        try:
            controls[key] = int(value, 10)
        except ValueError:
            continue
    return controls


def controls_locked(controls: dict[str, int]) -> bool:
    """AEC, AGC, AWB and AWB gain all read back as disabled."""
    return all(controls.get(key) == 0 for key in ("aec", "agc", "awb", "awb_gain"))


def controls_automatic(controls: dict[str, int]) -> bool:
    """Automation fully enabled; the OV3660 may report awb_gain=0 with wb_mode=0."""
    if not all(controls.get(key) == 1 for key in ("aec", "agc", "awb")):
        return False
    return controls.get("awb_gain") == 1 or (controls.get("awb_gain") == 0 and controls.get("wb_mode") == 0)


@dataclass(frozen=True)
class LaserReport:
    on: bool
    reason: str | None
    fields: dict[str, str]


def parse_laser_line(line: str) -> LaserReport | None:
    """``LASER ON|OFF [key=value ...]``; anything else (incl. ``LASER_PIN:``) is None."""
    tokens = line.split()
    if len(tokens) < 2 or tokens[0] != "LASER" or tokens[1] not in {"ON", "OFF"}:
        return None
    fields = parse_key_values(line)
    return LaserReport(on=tokens[1] == "ON", reason=fields.get("reason"), fields=fields)


def parse_pair_end(line: str) -> bool:
    """True when a ``PAIR_END`` line reports success (absent ``ok=`` means success)."""
    if not matches_prefix(line, "PAIR_END"):
        raise ProtocolError(f"expected PAIR_END, got {line!r}")
    return parse_key_values(line).get("ok", "1") == "1"


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write via a sibling temporary file and rename, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
