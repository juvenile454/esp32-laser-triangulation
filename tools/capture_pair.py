#!/usr/bin/env python3
"""Capture one registered laser ON/OFF pair with only the laser state changing.

Requires firmware with the `R` command and a switched laser. Everything the
schema needs to make the pair reusable later is written alongside the images,
including the UTC capture time that a robot pose will one day be matched
against.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import time
from pathlib import Path

import serial
from PIL import Image, ImageStat

from capture_frame import fnv1a32, read_exact, read_frame_end, read_header, wait_for_camera


def read_line(port: serial.Serial, deadline: float) -> str:
    while time.monotonic() < deadline:
        line = port.readline().decode("ascii", errors="replace").strip()
        if line:
            return line
    raise TimeoutError("no response from device")


def expect_line(port: serial.Serial, prefix: str, deadline: float) -> str:
    while True:
        line = read_line(port, deadline)
        if line.startswith(prefix):
            return line
        if "ERROR" in line or line.startswith("PAIR_ERROR"):
            raise RuntimeError(line)


def read_one_frame(port: serial.Serial, deadline: float) -> tuple[bytes, int, int]:
    fields = read_header(port, deadline)
    length = int(fields[1])
    width = int(fields[2])
    height = int(fields[3])
    expected_hash = int(fields[5], 16)
    if not 1024 <= length <= 2_000_000:
        raise RuntimeError(f"implausible JPEG length: {length}")
    payload = read_exact(port, length, deadline)
    read_frame_end(port, deadline)
    if not payload.startswith(b"\xFF\xD8") or not payload.endswith(b"\xFF\xD9"):
        raise RuntimeError("invalid JPEG markers")
    actual = fnv1a32(payload)
    if actual != expected_hash:
        raise RuntimeError(f"checksum mismatch device={expected_hash:08X} host={actual:08X}")
    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        if image.size != (width, height):
            raise RuntimeError(f"dimension mismatch header={width}x{height} jpeg={image.size}")
    return payload, width, height


def send_command(port: serial.Serial, payload: bytes, prefix: str, timeout: float = 3.0) -> str:
    port.write(payload)
    port.flush()
    return expect_line(port, prefix, time.monotonic() + timeout)


def channel_means(payload: bytes) -> list[float]:
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        return list(ImageStat.Stat(image.convert("RGB")).mean)


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sample-id", default=None)
    parser.add_argument("--fixture-revision", default=None,
                        help="Record the fixture this pair belongs to. Without it "
                             "the pair cannot become calibration grade.")
    parser.add_argument("--exposure", type=int, default=None,
                        help="Set aec_value before capturing (also locks AEC).")
    parser.add_argument("--gain", type=int, default=None,
                        help="Set agc_gain before capturing (also locks AGC).")
    parser.add_argument("--lock", action="store_true",
                        help="Send K to freeze all automatic controls first.")
    parser.add_argument("--working-distance-mm", type=float, default=None)
    parser.add_argument("--baseline-mm", type=float, default=None)
    parser.add_argument("--laser-angle-deg", type=float, default=None)
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
        if args.lock:
            print(send_command(port, b"K", "CAMERA_AUTOMATION"))
        if args.exposure is not None:
            if not 0 <= args.exposure <= 9999:
                raise SystemExit("--exposure must be 0..9999")
            print(send_command(port, f"E{args.exposure:04d}".encode("ascii"), "CAMERA_EXPOSURE"))
        if args.gain is not None:
            if not 0 <= args.gain <= 99:
                raise SystemExit("--gain must be 0..99")
            print(send_command(port, f"G{args.gain:02d}".encode("ascii"), "CAMERA_GAIN"))

        controls = send_command(port, b"S", "CAMERA_CONTROLS")
        print(controls)

        captured_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        port.write(b"R")
        port.flush()
        deadline = time.monotonic() + args.timeout
        header = expect_line(port, "PAIR_BEGIN", deadline)
        print(header)

        expect_line(port, "PAIR_STATE OFF", deadline)
        off_payload, width, height = read_one_frame(port, deadline)
        expect_line(port, "PAIR_STATE ON", deadline)
        on_payload, on_width, on_height = read_one_frame(port, deadline)
        expect_line(port, "PAIR_END", deadline)
    finally:
        port.close()

    if (width, height) != (on_width, on_height):
        raise SystemExit(f"pair dimensions differ: OFF={width}x{height} ON={on_width}x{on_height}")

    stamp = captured_at.strftime("%Y%m%d_%H%M%S")
    sample_id = args.sample_id or f"pair_{stamp}"
    directory = args.output_dir / sample_id
    on_path = directory / "laser_on.jpg"
    off_path = directory / "laser_off.jpg"
    write_atomic(on_path, on_payload)
    write_atomic(off_path, off_payload)

    on_mean = channel_means(on_payload)
    off_mean = channel_means(off_payload)
    residual = [round(a - b, 3) for a, b in zip(on_mean, off_mean, strict=True)]

    exposure = gain = None
    locked = None
    for token in controls.split():
        key, _, value = token.partition("=")
        if key == "aec_value":
            exposure = int(value)
        elif key == "agc_gain":
            gain = int(value)
    locked = all(token in controls for token in ("aec=0", "agc=0", "awb=0", "awb_gain=0"))

    metadata = {
        "schema_version": "1.0",
        "sample_id": sample_id,
        "evidence_tier": "calibration_grade_real" if args.fixture_revision else "reusable_real_evidence",
        "capture": {
            "fixture_revision": args.fixture_revision,
            "registered_pair": True,
            "timestamp_utc": captured_at.isoformat().replace("+00:00", "Z"),
            "robot_pose": None,
            "camera": {
                "sensor": "OV3660",
                "width_px": width,
                "height_px": height,
                "exposure": exposure,
                "gain": gain,
                "white_balance_locked": locked,
            },
            "laser": {
                "wavelength_nm": 650,
                "power_class": "Class IIIa, label max 5 mW",
                "switch_method": "firmware-switched low-side transistor",
            },
        },
        "images": {
            "laser_on": {
                "file": on_path.name,
                "sha256": hashlib.sha256(on_payload).hexdigest(),
                "format": "JPEG",
            },
            "laser_off": {
                "file": off_path.name,
                "sha256": hashlib.sha256(off_payload).hexdigest(),
                "format": "JPEG",
            },
        },
        "geometry": {
            "working_distance_mm": args.working_distance_mm,
            "baseline_mm": args.baseline_mm,
            "laser_angle_deg": args.laser_angle_deg,
        },
        "ground_truth": {"lateral_offset_mm": None, "height_offset_mm": None, "source": None},
        "labels": {"valid": None, "reflection": None, "occlusion": None, "surface": None},
        "limitations": [
            "Laser state was switched by firmware; nothing else changed between the frames.",
            f"Whole-frame ON-minus-OFF residual per channel: {residual[0]}/{residual[1]}/{residual[2]} RGB levels.",
        ],
    }
    if not args.fixture_revision:
        metadata["limitations"].append(
            "No fixture revision recorded; this pair is not calibration grade."
        )
    metadata_path = directory / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print("PAIR_RESULT: PASS")
    print(f"DIRECTORY: {directory}")
    print(f"SIZE: {width}x{height}")
    print(f"ON  mean RGB: {on_mean[0]:.2f}/{on_mean[1]:.2f}/{on_mean[2]:.2f}")
    print(f"OFF mean RGB: {off_mean[0]:.2f}/{off_mean[1]:.2f}/{off_mean[2]:.2f}")
    print(f"RESIDUAL:     {residual[0]}/{residual[1]}/{residual[2]}")
    print(f"METADATA: {metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
