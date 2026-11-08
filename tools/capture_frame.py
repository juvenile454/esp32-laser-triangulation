#!/usr/bin/env python3
"""Request one length-framed JPEG from the ESP32-S3-CAM TTL interface.

The frame is checked for length, JPEG markers, FNV-1a checksum and header
dimensions before it is written atomically with mode 0600.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import serial
from PIL import Image, ImageStat

from cli_common import description, fail, positive_float, positive_int
from serial_protocol import (
    DEFAULT_BAUD,
    DEFAULT_CAMERA_TIMEOUT_S,
    DEFAULT_PORT,
    ProtocolError,
    StopRequested,
    open_port,
    request_frame,
    wait_for_camera,
    write_atomic,
)

PROG = "capture_frame"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=description(__doc__))
    parser.add_argument("output", type=Path, help="destination JPEG file")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"serial device (default {DEFAULT_PORT})")
    parser.add_argument("--baud", type=positive_int, default=DEFAULT_BAUD, help=f"baud rate (default {DEFAULT_BAUD})")
    parser.add_argument("--timeout", type=positive_float, default=20.0,
                        help="seconds to wait for the frame after requesting it (default 20)")
    parser.add_argument("--camera-timeout", type=positive_float, default=DEFAULT_CAMERA_TIMEOUT_S,
                        help=f"seconds to wait for the camera to report READY (default {DEFAULT_CAMERA_TIMEOUT_S:g})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output.exists() and args.output.is_dir():
        parser.error(f"output is a directory: {args.output}")

    try:
        port = open_port(args.port, args.baud)
    except (serial.SerialException, OSError) as exc:
        raise fail(PROG, f"cannot open {args.port}: {exc}") from None
    try:
        wait_for_camera(port, time.monotonic() + args.camera_timeout)
        frame = request_frame(port, args.timeout)
    except (serial.SerialException, OSError, TimeoutError, ProtocolError, StopRequested) as exc:
        raise fail(PROG, exc) from None
    finally:
        port.close()

    with Image.open(io.BytesIO(frame.payload)) as image:
        image.load()
        grayscale = image.convert("L")
        stats = ImageStat.Stat(grayscale)
        extrema = grayscale.getextrema()
        mode = image.mode
        image_format = image.format

    try:
        write_atomic(args.output, frame.payload)
    except OSError as exc:
        raise fail(PROG, f"cannot write {args.output}: {exc}") from None

    print("CAPTURE_RESULT: PASS")
    print(f"FILE: {args.output}")
    print(f"JPEG: {frame.width}x{frame.height}, {len(frame.payload)} bytes, mode={mode}, format={image_format}")
    print(f"PIXEL_FORMAT_ID: {frame.pixel_format}")
    print(f"FNV1A32: {frame.fnv1a32:08X}")
    print(f"LUMA_MEAN: {stats.mean[0]:.2f}")
    print(f"LUMA_STDDEV: {stats.stddev[0]:.2f}")
    print(f"LUMA_RANGE: {extrema[0]}..{extrema[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
