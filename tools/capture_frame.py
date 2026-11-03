#!/usr/bin/env python3
"""Request one length-framed JPEG from the ESP32-S3-CAM TTL interface."""

from __future__ import annotations

import argparse
import io
import os
import time
from collections.abc import Callable
from pathlib import Path

import serial
from PIL import Image, ImageStat


def fnv1a32(data: bytes) -> int:
    value = 0x811C9DC5
    for byte in data:
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def read_exact(port: serial.Serial, length: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"JPEG timeout: {remaining} bytes missing")
        chunk = port.read(min(remaining, 4096))
        if chunk:
            chunks.append(chunk)
            remaining -= len(chunk)
    return b"".join(chunks)


def read_header(
    port: serial.Serial,
    deadline: float,
    on_line: Callable[[str], None] | None = None,
) -> list[str]:
    """Wait for FRAME_BEGIN. Lines skipped on the way are passed to on_line."""
    while time.monotonic() < deadline:
        line = port.readline().decode("ascii", errors="replace").strip()
        if line and on_line is not None:
            on_line(line)
        if line.startswith("FRAME_ERROR"):
            raise RuntimeError(line)
        if line.startswith("FRAME_BEGIN "):
            fields = line.split()
            if len(fields) != 6:
                raise RuntimeError(f"Malformed frame header: {line!r}")
            return fields
    raise TimeoutError("No FRAME_BEGIN received")


def read_frame_end(port: serial.Serial, deadline: float) -> None:
    while time.monotonic() < deadline:
        line = port.readline().decode("ascii", errors="replace").strip()
        if not line:
            continue
        if line == "FRAME_END":
            return
        raise RuntimeError(f"Unexpected data after JPEG: {line!r}")
    raise TimeoutError("No FRAME_END received")


def wait_for_camera(port: serial.Serial, deadline: float) -> None:
    next_probe = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_probe:
            port.write(b"P")
            port.flush()
            next_probe = now + 0.4
        line = port.readline().decode("ascii", errors="ignore").strip()
        if line in {"CAMERA_STATUS READY", "CAMERA_RESULT:       PASS"} or "camera=READY" in line:
            return
        if line == "CAMERA_STATUS FAILED" or "CAMERA_RESULT:       FAIL" in line:
            raise RuntimeError("Camera firmware reports FAILED")
    raise TimeoutError("Camera did not become READY")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    port = serial.Serial()
    port.port = args.port
    port.baudrate = args.baud
    port.timeout = 0.2
    port.dtr = False
    port.rts = False
    port.open()
    try:
        port.reset_input_buffer()
        wait_for_camera(port, time.monotonic() + 12.0)
        port.write(b"C")
        port.flush()
        deadline = time.monotonic() + args.timeout
        fields = read_header(port, deadline)
        length = int(fields[1])
        width = int(fields[2])
        height = int(fields[3])
        pixel_format = int(fields[4])
        expected_hash = int(fields[5], 16)
        if not 1024 <= length <= 2_000_000:
            raise RuntimeError(f"Implausible JPEG length: {length}")

        payload = read_exact(port, length, deadline)
        read_frame_end(port, deadline)
    finally:
        port.close()

    if not payload.startswith(b"\xFF\xD8") or not payload.endswith(b"\xFF\xD9"):
        raise RuntimeError("JPEG SOI/EOI markers are invalid")
    actual_hash = fnv1a32(payload)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"FNV-1a mismatch: device={expected_hash:08X}, host={actual_hash:08X}"
        )

    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        if image.size != (width, height):
            raise RuntimeError(
                f"Dimension mismatch: header={width}x{height}, JPEG={image.size}"
            )
        grayscale = image.convert("L")
        stats = ImageStat.Stat(grayscale)
        extrema = grayscale.getextrema()
        mode = image.mode
        image_format = image.format

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".partial")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    temporary.replace(args.output)

    print("CAPTURE_RESULT: PASS")
    print(f"FILE: {args.output}")
    print(f"JPEG: {width}x{height}, {length} bytes, mode={mode}, format={image_format}")
    print(f"PIXEL_FORMAT_ID: {pixel_format}")
    print(f"FNV1A32: {actual_hash:08X}")
    print(f"LUMA_MEAN: {stats.mean[0]:.2f}")
    print(f"LUMA_STDDEV: {stats.stddev[0]:.2f}")
    print(f"LUMA_RANGE: {extrema[0]}..{extrema[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
