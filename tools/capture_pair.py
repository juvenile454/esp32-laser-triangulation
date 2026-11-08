#!/usr/bin/env python3
"""Capture one registered laser ON/OFF pair with only the laser state changing.

Requires firmware with the `R` command and a switched laser. Everything the
schema needs to make the pair reusable later is written alongside the images,
including the UTC capture time that a robot pose will one day be matched
against. The written metadata is validated against the sample schema.

Pairs are 'reusable_real_evidence' by default. 'calibration_grade_real' is only
recorded with --calibration-grade, which additionally requires --lock,
--fixture-revision, --working-distance-mm and --baseline-mm, and a camera
readback confirming locked automation with known exposure and gain.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial
from PIL import Image, ImageStat

from cli_common import (
    description,
    fail,
    float_range,
    int_range,
    output_dir_arguments,
    positive_float,
    positive_int,
    resolve_output_dir,
)
from serial_protocol import (
    DEFAULT_BAUD,
    DEFAULT_CAMERA_TIMEOUT_S,
    DEFAULT_PORT,
    LASER_OFF_COMMAND,
    Frame,
    ProtocolError,
    SerialPort,
    StopRequested,
    controls_locked,
    expect_line,
    open_port,
    parse_controls,
    parse_pair_end,
    read_frame,
    send,
    wait_for_camera,
    write_atomic,
)
from validate_dataset import validate as validate_metadata

PROG = "capture_pair"
SAMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
FIXTURE_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REUSABLE = "reusable_real_evidence"
CALIBRATION_GRADE = "calibration_grade_real"


class CaptureAbort(RuntimeError):
    """A precondition for the requested evidence tier is not met."""


@dataclass
class CapturedPair:
    captured_at: datetime.datetime
    controls: dict[str, int]
    off: Frame
    on: Frame


def sample_id_type(text: str) -> str:
    if not SAMPLE_ID_PATTERN.fullmatch(text) or set(text) == {"."}:
        raise argparse.ArgumentTypeError(
            f"invalid sample id {text!r}: use letters, digits, '_', '.', '-' only (no path separators)"
        )
    return text


def fixture_revision_type(text: str) -> str:
    if not FIXTURE_REVISION_PATTERN.fullmatch(text):
        raise argparse.ArgumentTypeError(f"invalid fixture revision {text!r}: expected ^[A-Za-z0-9._-]{{1,64}}$")
    return text


def sample_directory(output_dir: Path, sample_id: str) -> Path:
    """Resolved sample directory, guaranteed to be a direct child of output_dir."""
    root = output_dir.resolve()
    directory = (root / sample_id).resolve()
    if directory.parent != root:
        raise ValueError(f"sample directory escapes the output directory: {sample_id!r}")
    return directory


def send_command(
    port: SerialPort, payload: bytes, prefix: str, timeout: float = 3.0, trailing_controls: bool = False
) -> str:
    """Send one command and return its result line.

    K, E and G print a CAMERA_CONTROLS readback after their result line; with
    ``trailing_controls`` it is consumed too, so a later S never reads it stale.
    """
    send(port, payload)
    deadline = time.monotonic() + timeout
    line = expect_line(port, prefix, deadline)
    if " FAIL" in line:
        raise ProtocolError(line)
    if trailing_controls:
        expect_line(port, "CAMERA_CONTROLS", deadline)
    return line


def channel_means(payload: bytes) -> list[float]:
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        return list(ImageStat.Stat(image.convert("RGB")).mean)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=description(__doc__),
        epilog="Exit codes: 0 pair written and validated, 1 capture/validation failure, 2 usage error.",
    )
    output_dir_arguments(parser, "directory that receives <sample-id>/laser_{on,off}.jpg and metadata.json")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"serial device (default {DEFAULT_PORT})")
    parser.add_argument("--baud", type=positive_int, default=DEFAULT_BAUD, help=f"baud rate (default {DEFAULT_BAUD})")
    parser.add_argument("--timeout", type=positive_float, default=30.0,
                        help="seconds allowed for the whole pair after sending R (default 30)")
    parser.add_argument("--camera-timeout", type=positive_float, default=DEFAULT_CAMERA_TIMEOUT_S,
                        help=f"seconds to wait for the camera to report READY (default {DEFAULT_CAMERA_TIMEOUT_S:g})")
    parser.add_argument("--sample-id", type=sample_id_type, default=None,
                        help="sample directory name (default pair_<UTC timestamp>); [A-Za-z0-9_.-] only")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing sample directory's files instead of refusing")
    parser.add_argument("--fixture-revision", type=fixture_revision_type, default=None,
                        help="fixture this pair belongs to; required for --calibration-grade")
    parser.add_argument("--exposure", type=int_range(0, 9999), default=None,
                        help="set aec_value 0..9999 before capturing (also locks AEC)")
    parser.add_argument("--gain", type=int_range(0, 99), default=None,
                        help="set agc_gain 0..99 before capturing (also locks AGC)")
    parser.add_argument("--lock", action="store_true", help="send K to freeze all automatic camera controls first")
    parser.add_argument("--working-distance-mm", type=positive_float, default=None,
                        help="measured working distance, recorded in the metadata")
    parser.add_argument("--baseline-mm", type=positive_float, default=None,
                        help="measured camera-laser baseline, recorded in the metadata")
    parser.add_argument("--laser-angle-deg", type=float_range(-90.0, 90.0), default=None,
                        help="laser plane angle, recorded in the metadata")
    parser.add_argument("--calibration-grade", action="store_true",
                        help="record evidence_tier calibration_grade_real (strict preconditions, see above)")
    return parser


def check_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path | None:
    """All checks that need no device. Runs before the port is opened."""
    output_dir = resolve_output_dir(parser, args)
    assert output_dir is not None
    if output_dir.exists() and not output_dir.is_dir():
        parser.error(f"output directory is not a directory: {output_dir}")
    if args.calibration_grade:
        missing = [
            flag for flag, value in (
                ("--lock", args.lock),
                ("--fixture-revision", args.fixture_revision),
                ("--working-distance-mm", args.working_distance_mm),
                ("--baseline-mm", args.baseline_mm),
            ) if not value
        ]
        if missing:
            parser.error("--calibration-grade also requires " + ", ".join(missing))
    if args.sample_id is None:
        return None
    try:
        directory = sample_directory(output_dir, args.sample_id)
    except ValueError as exc:
        parser.error(str(exc))
    if directory.exists() and not args.overwrite:
        parser.error(f"sample directory already exists: {directory} (use --overwrite to replace it)")
    return directory


def capture(port: SerialPort, args: argparse.Namespace) -> CapturedPair:
    wait_for_camera(port, time.monotonic() + args.camera_timeout)
    if args.lock:
        print(send_command(port, b"K", "CAMERA_AUTOMATION", trailing_controls=True))
    if args.exposure is not None:
        print(send_command(port, f"E{args.exposure:04d}".encode("ascii"), "CAMERA_EXPOSURE", trailing_controls=True))
    if args.gain is not None:
        print(send_command(port, f"G{args.gain:02d}".encode("ascii"), "CAMERA_GAIN", trailing_controls=True))

    controls_line = send_command(port, b"S", "CAMERA_CONTROLS")
    print(controls_line)
    controls = parse_controls(controls_line)
    if args.calibration_grade:
        problems = []
        if not controls_locked(controls):
            problems.append("camera readback does not confirm aec=0 agc=0 awb=0 awb_gain=0")
        if "aec_value" not in controls or "agc_gain" not in controls:
            problems.append("exposure/gain readback missing")
        if problems:
            raise CaptureAbort("not calibration grade: " + "; ".join(problems) + " (nothing captured)")

    captured_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    send(port, b"R")
    deadline = time.monotonic() + args.timeout
    try:
        print(expect_line(port, "PAIR_BEGIN", deadline))
        expect_line(port, "PAIR_STATE OFF", deadline)
        off = read_frame(port, deadline)
        expect_line(port, "PAIR_STATE ON", deadline)
        on = read_frame(port, deadline)
        end_line = expect_line(port, "PAIR_END", deadline)
        if not parse_pair_end(end_line):
            raise ProtocolError(f"device reported a failed pair: {end_line}")
    except BaseException:
        # The pair switches the laser itself; never leave it on after a failure.
        try:
            send(port, LASER_OFF_COMMAND)
        except Exception as exc:
            print(f"{PROG}: warning: could not send laser OFF: {exc}", file=sys.stderr)
        raise
    if (off.width, off.height) != (on.width, on.height):
        raise ProtocolError(f"pair dimensions differ: OFF={off.width}x{off.height} ON={on.width}x{on.height}")
    return CapturedPair(captured_at, controls, off, on)


def build_metadata(
    args: argparse.Namespace, sample_id: str, pair: CapturedPair, on_name: str, off_name: str
) -> tuple[dict[str, object], list[list[float]]]:
    controls = pair.controls
    on_mean = channel_means(pair.on.payload)
    off_mean = channel_means(pair.off.payload)
    residual = [round(a - b, 3) for a, b in zip(on_mean, off_mean, strict=True)]
    white_balance_locked = (
        controls["awb"] == 0 and controls["awb_gain"] == 0 if "awb" in controls and "awb_gain" in controls else None
    )
    tier = CALIBRATION_GRADE if args.calibration_grade else REUSABLE
    limitations = [
        "Laser state was switched by firmware; nothing else changed between the frames.",
        f"Whole-frame ON-minus-OFF residual per channel: {residual[0]}/{residual[1]}/{residual[2]} RGB levels.",
    ]
    if tier != CALIBRATION_GRADE:
        limitations.append("Not calibration grade: captured without --calibration-grade (confirmed lock and geometry).")
    if not args.fixture_revision:
        limitations.append("No fixture revision recorded.")
    metadata: dict[str, object] = {
        "schema_version": "1.0",
        "sample_id": sample_id,
        "evidence_tier": tier,
        "capture": {
            "fixture_revision": args.fixture_revision,
            "registered_pair": True,
            "timestamp_utc": pair.captured_at.isoformat().replace("+00:00", "Z"),
            "robot_pose": None,
            "camera": {
                "sensor": "OV3660",
                "width_px": pair.on.width,
                "height_px": pair.on.height,
                "exposure": controls.get("aec_value"),
                "gain": controls.get("agc_gain"),
                "white_balance_locked": white_balance_locked,
            },
            "laser": {
                "wavelength_nm": 650,
                "power_class": "Class IIIa, label max 5 mW",
                "switch_method": "firmware-switched low-side transistor",
            },
        },
        "images": {
            "laser_on": {"file": on_name, "sha256": hashlib.sha256(pair.on.payload).hexdigest(), "format": "JPEG"},
            "laser_off": {"file": off_name, "sha256": hashlib.sha256(pair.off.payload).hexdigest(), "format": "JPEG"},
        },
        "geometry": {
            "working_distance_mm": args.working_distance_mm,
            "baseline_mm": args.baseline_mm,
            "laser_angle_deg": args.laser_angle_deg,
        },
        "ground_truth": {"lateral_offset_mm": None, "height_offset_mm": None, "source": None},
        "labels": {"valid": None, "reflection": None, "occlusion": None, "surface": None},
        "limitations": limitations,
    }
    return metadata, [on_mean, off_mean, residual]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    directory = check_arguments(parser, args)

    try:
        port = open_port(args.port, args.baud)
    except (serial.SerialException, OSError) as exc:
        raise fail(PROG, f"cannot open {args.port}: {exc}") from None
    try:
        pair = capture(port, args)
    except (serial.SerialException, OSError, TimeoutError, ProtocolError, CaptureAbort, StopRequested) as exc:
        raise fail(PROG, exc) from None
    finally:
        port.close()

    sample_id = args.sample_id or f"pair_{pair.captured_at.strftime('%Y%m%d_%H%M%S')}"
    if directory is None:
        directory = sample_directory(args.output_dir, sample_id)
        if directory.exists() and not args.overwrite:
            raise fail(PROG, f"sample directory already exists: {directory} (use --overwrite); nothing written")
    on_path = directory / "laser_on.jpg"
    off_path = directory / "laser_off.jpg"
    metadata, (on_mean, off_mean, residual) = build_metadata(args, sample_id, pair, on_path.name, off_path.name)
    metadata_path = directory / "metadata.json"
    try:
        write_atomic(on_path, pair.on.payload)
        write_atomic(off_path, pair.off.payload)
        write_atomic(metadata_path, (json.dumps(metadata, indent=2, allow_nan=False) + "\n").encode("utf-8"))
    except OSError as exc:
        raise fail(PROG, f"cannot write sample: {exc}") from None
    try:
        validate_metadata(metadata_path)
    except (ValueError, OSError) as exc:
        raise fail(PROG, f"WRITTEN METADATA IS INVALID: {metadata_path}: {exc}") from None

    print("PAIR_RESULT: PASS")
    print(f"DIRECTORY: {directory}")
    print(f"EVIDENCE_TIER: {metadata['evidence_tier']}")
    print(f"SIZE: {pair.on.width}x{pair.on.height}")
    print(f"ON  mean RGB: {on_mean[0]:.2f}/{on_mean[1]:.2f}/{on_mean[2]:.2f}")
    print(f"OFF mean RGB: {off_mean[0]:.2f}/{off_mean[1]:.2f}/{off_mean[2]:.2f}")
    print(f"RESIDUAL:     {residual[0]}/{residual[1]}/{residual[2]}")
    print(f"METADATA: {metadata_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
