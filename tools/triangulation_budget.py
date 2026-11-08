#!/usr/bin/env python3
"""Turn a mechanical layout into the measurement accuracy it can actually reach.

The sensor's pixel noise floor is fixed by the camera. What decides whether a
seam can be measured to a tenth of a millimetre is geometry: the baseline
between camera and laser, the working distance, and the focal length.

Model
-----
Two layouts are modelled (see evaluate() and stripe_shift_px()); in both the
triangulation angle between laser ray and camera ray is atan(B / Z).

tilted-camera (default): laser along the work-plane normal, camera offset by B
and aimed at the stripe. A surface raised by h images the stripe at

    u(h) = f_px * B * h / (B**2 + Z**2 - Z*h)    [pixels from the image centre]

tilted-laser: camera perpendicular, laser crossing its axis at Z0, so a
surface at distance Z images the stripe at

    u(Z) = f_px * B * (1/Z - 1/Z0)

Nothing here is a substitute for measuring. Use --from-measured-shift once a
block of known thickness is available; it derives f_px from the observed
pixel shift with the exact forward model of the selected --layout and needs no
lens data at all.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from cli_common import description, json_safe, positive_float, positive_int

TILTED_CAMERA = "tilted-camera"
TILTED_LASER = "tilted-laser"
LAYOUTS = (TILTED_CAMERA, TILTED_LASER)


@dataclass
class Budget:
    layout: str
    baseline_mm: float
    working_distance_mm: float
    focal_length_px: float
    noise_px: float
    image_width_px: int
    triangulation_angle_deg: float
    camera_distance_mm: float
    mm_per_px: float
    height_sensitivity_px_per_mm: float
    height_noise_mm: float
    lateral_noise_mm: float
    field_of_view_mm: float
    sensitivity_efficiency: float


def evaluate(
    baseline_mm: float,
    working_distance_mm: float,
    focal_length_px: float,
    noise_px: float,
    image_width_px: int,
    layout: str = TILTED_CAMERA,
) -> Budget:
    """Accuracy of one layout.

    tilted-camera (the standard arrangement): the laser fires perpendicular to
    the work plane and the camera is offset by the baseline and aimed back at
    the stripe. The measured point stays put on the workpiece when the height
    changes; only the camera's view of it shifts.

        alpha = atan(B / Z)
        d     = sqrt(B**2 + Z**2)
        dU/dh = f_px * sin(alpha) / d = f_px * sin(2*alpha) / (2*Z)

    Sensitivity therefore peaks at alpha = 45 degrees, where B equals Z, and
    sin(2*alpha) is the efficiency relative to that optimum.

    tilted-laser: camera perpendicular, laser aimed across to meet its axis.
    This is what the provisional carrier does. The stripe walks across the
    workpiece as the height changes, so a height error also displaces the
    measured location.

        dU/dh = f_px * B / Z**2
    """
    if baseline_mm <= 0 or working_distance_mm <= 0 or focal_length_px <= 0:
        raise ValueError("baseline, working distance and focal length must be positive")
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}")

    angle = math.atan(baseline_mm / working_distance_mm)
    camera_distance = math.hypot(baseline_mm, working_distance_mm)
    if layout == TILTED_CAMERA:
        sensitivity = focal_length_px * math.sin(angle) / camera_distance
        # The camera looks along the hypotenuse, so scale follows that distance.
        mm_per_px = camera_distance / focal_length_px
        efficiency = math.sin(2.0 * angle)
    else:
        sensitivity = focal_length_px * baseline_mm / (working_distance_mm ** 2)
        mm_per_px = working_distance_mm / focal_length_px
        efficiency = float("nan")

    return Budget(
        layout=layout,
        baseline_mm=baseline_mm,
        working_distance_mm=working_distance_mm,
        focal_length_px=focal_length_px,
        noise_px=noise_px,
        image_width_px=image_width_px,
        triangulation_angle_deg=math.degrees(angle),
        camera_distance_mm=camera_distance,
        mm_per_px=mm_per_px,
        height_sensitivity_px_per_mm=sensitivity,
        height_noise_mm=noise_px / sensitivity,
        lateral_noise_mm=noise_px * mm_per_px,
        field_of_view_mm=image_width_px * mm_per_px,
        sensitivity_efficiency=efficiency,
    )


def stripe_shift_px(
    baseline_mm: float,
    working_distance_mm: float,
    focal_length_px: float,
    step_mm: float,
    layout: str = TILTED_CAMERA,
) -> float:
    """Exact image displacement of the stripe when the surface rises by step_mm.

    Same geometry as :func:`evaluate`, without the small-step linearisation.

    tilted-camera: laser along the work-plane normal at x=0, camera at lateral
    offset B and distance Z, its optical axis through the nominal stripe point
    (0, Z). Raising the surface by h moves the lit point along the beam to
    (0, Z - h); the angle between that ray and the optical axis gives

        shift = f_px * B * h / (B**2 + Z**2 - Z*h)

    whose derivative at h = 0 is f_px * sin(alpha) / d, the evaluate() value.

    tilted-laser: u(Z) = f_px * B * (1/Z - 1/Z0), so

        shift = f_px * B * (1/(Z - h) - 1/Z)
    """
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}")
    if baseline_mm <= 0 or working_distance_mm <= 0 or focal_length_px <= 0:
        raise ValueError("baseline, working distance and focal length must be positive")
    if not 0 < step_mm < working_distance_mm:
        raise ValueError("step must be positive and smaller than the working distance")
    return focal_length_px * _shift_per_focal_px(baseline_mm, working_distance_mm, step_mm, layout)


def _shift_per_focal_px(baseline_mm: float, working_distance_mm: float, step_mm: float, layout: str) -> float:
    # Both forward models are linear in f_px, so the inverse is exact division.
    if layout == TILTED_CAMERA:
        return baseline_mm * step_mm / (
            baseline_mm ** 2 + working_distance_mm ** 2 - working_distance_mm * step_mm
        )
    near = working_distance_mm - step_mm
    return baseline_mm * (1.0 / near - 1.0 / working_distance_mm)


def focal_length_from_shift(
    baseline_mm: float,
    working_distance_mm: float,
    step_mm: float,
    shift_px: float,
    layout: str = TILTED_CAMERA,
) -> float:
    """Derive f_px from a measured height step. No lens data required.

    step_mm is how far the surface moved *towards* the camera; shift_px is the
    magnitude of the resulting stripe displacement. The inverse of
    :func:`stripe_shift_px` for the same layout, so it is exact for both.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {LAYOUTS}")
    if baseline_mm <= 0 or working_distance_mm <= 0:
        raise ValueError("baseline and working distance must be positive")
    if not 0 < step_mm < working_distance_mm:
        raise ValueError("step must be positive and smaller than the working distance")
    if not shift_px > 0 or not math.isfinite(shift_px):
        raise ValueError("measured pixel shift must be positive")
    return shift_px / _shift_per_focal_px(baseline_mm, working_distance_mm, step_mm, layout)


def required_baseline(
    working_distance_mm: float,
    focal_length_px: float,
    noise_px: float,
    target_mm: float,
    layout: str = TILTED_CAMERA,
) -> float | None:
    """Baseline needed to reach target_mm height noise at a fixed distance.

    Returns None when no baseline reaches the target at that distance, which
    happens for tilted-camera whenever the distance is already too large: the
    optimum sits at B = Z and widening past it makes things worse again.
    """
    if layout == TILTED_LASER:
        return noise_px * working_distance_mm ** 2 / (focal_length_px * target_mm)
    # noise * (B^2 + Z^2) / (f * B) = target  ->  noise*B^2 - f*target*B + noise*Z^2 = 0
    linear = focal_length_px * target_mm
    discriminant = linear ** 2 - 4.0 * noise_px ** 2 * working_distance_mm ** 2
    if discriminant < 0.0:
        return None
    return (linear - math.sqrt(discriminant)) / (2.0 * noise_px)


def required_distance(
    baseline_mm: float,
    focal_length_px: float,
    noise_px: float,
    target_mm: float,
    layout: str = TILTED_CAMERA,
) -> float | None:
    """Working distance needed to reach target_mm height noise at fixed baseline."""
    if layout == TILTED_LASER:
        return math.sqrt(focal_length_px * baseline_mm * target_mm / noise_px)
    squared = (
        focal_length_px * target_mm * baseline_mm / noise_px - baseline_mm ** 2
    )
    if squared <= 0.0:
        return None
    return math.sqrt(squared)


def best_distance(focal_length_px: float, noise_px: float, target_mm: float) -> float:
    """Largest standoff that still reaches the target, at the optimal 45 degrees.

    At B = Z the height noise is 2 * Z * noise_px / f_px, so this inverts to the
    distance at which the target is exactly met.
    """
    return focal_length_px * target_mm / (2.0 * noise_px)


def format_budget(budget: Budget, target_mm: float | None) -> str:
    lines = [
        f"LAYOUT  {budget.layout}",
        "",
        "GEOMETRY",
        f"  baseline B                {budget.baseline_mm:8.1f} mm",
        f"  working distance Z        {budget.working_distance_mm:8.1f} mm",
        f"  camera to stripe          {budget.camera_distance_mm:8.1f} mm",
        f"  focal length              {budget.focal_length_px:8.1f} px",
        f"  triangulation angle       {budget.triangulation_angle_deg:8.2f} deg",
    ]
    if budget.layout == TILTED_CAMERA:
        lines.append(
            f"  sensitivity efficiency    {budget.sensitivity_efficiency:8.3f}"
            "   (1.000 at 45 deg)"
        )
    lines += [
        "",
        "SCALE",
        f"  millimetres per pixel     {budget.mm_per_px:8.4f} mm/px",
        f"  field of view (width)     {budget.field_of_view_mm:8.1f} mm",
        "",
        "ACCURACY",
        f"  assumed centroid noise    {budget.noise_px:8.4f} px",
        f"  height sensitivity        {budget.height_sensitivity_px_per_mm:8.4f} px/mm",
        f"  height noise (1 sigma)    {budget.height_noise_mm:8.4f} mm",
        f"  lateral noise (1 sigma)   {budget.lateral_noise_mm:8.4f} mm",
    ]
    angle = budget.triangulation_angle_deg
    if angle < 20.0:
        lines.append("")
        lines.append(
            f"  WARNING: {angle:.1f} deg is a shallow triangulation angle, well"
        )
        lines.append(
            "           below the 45 deg optimum. The sensor spends its pixel"
        )
        lines.append(
            "           budget on lateral detail it does not need."
        )
    elif budget.layout == TILTED_CAMERA and angle > 60.0:
        lines.append("")
        lines.append(
            f"  WARNING: {angle:.1f} deg is past the optimum. Sensitivity is"
        )
        lines.append(
            "           falling again and the near leg of a fillet weld starts"
        )
        lines.append(
            "           to occlude the far one."
        )
    if target_mm is not None:
        lines.append("")
        lines.append("TARGET")
        lines.append(f"  requested height noise    {target_mm:8.4f} mm")
        met = budget.height_noise_mm <= target_mm
        lines.append(f"  met by this geometry      {'yes' if met else 'NO':>8s}")
        if not met:
            baseline = required_baseline(
                budget.working_distance_mm, budget.focal_length_px,
                budget.noise_px, target_mm, budget.layout,
            )
            distance = required_distance(
                budget.baseline_mm, budget.focal_length_px,
                budget.noise_px, target_mm, budget.layout,
            )
            if baseline is None:
                lines.append("  ... keeping Z             unreachable at any baseline")
            else:
                lines.append(f"  ... keeping Z, needs B    {baseline:8.1f} mm")
            if distance is None:
                lines.append("  ... keeping B             unreachable at any distance")
            else:
                lines.append(f"  ... keeping B, needs Z    {distance:8.1f} mm")
            if budget.layout == TILTED_CAMERA:
                optimum = best_distance(
                    budget.focal_length_px, budget.noise_px, target_mm
                )
                lines.append(
                    f"  ... best case B = Z       {optimum:8.1f} mm  (45 deg)"
                )
    return "\n".join(lines)


def sweep(
    focal_length_px: float,
    noise_px: float,
    image_width_px: int,
    angles_deg: list[float],
    distances: list[float],
    layout: str = TILTED_CAMERA,
) -> str:
    header = "  Z \\ angle" + "".join(f"{value:>10.0f}" for value in angles_deg)
    lines = [
        "ALTERNATIVES (height noise in mm, 1 sigma; baseline follows the angle)",
        header,
    ]
    for distance in distances:
        cells = []
        for angle in angles_deg:
            baseline = distance * math.tan(math.radians(angle))
            budget = evaluate(
                baseline, distance, focal_length_px, noise_px, image_width_px, layout
            )
            cells.append(f"{budget.height_noise_mm:>10.3f}")
        lines.append(f"  {distance:>7.0f} " + "".join(cells))
    lines.append("  (columns are triangulation angle in degrees, rows are distance in mm)")
    lines.append("")
    lines.append("  matching baselines B = Z * tan(angle), in mm:")
    for distance in distances:
        row = "".join(
            f"{distance * math.tan(math.radians(angle)):>10.0f}" for angle in angles_deg
        )
        lines.append(f"  {distance:>7.0f} " + row)
    return "\n".join(lines)


JSON_SCHEMA_VERSION = "1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description(__doc__),
        epilog="Exactly one focal-length source is required. Exit codes: 0 success, 2 usage error.",
    )
    parser.add_argument("--baseline-mm", type=positive_float, required=True,
                        help="camera-laser baseline B in mm (> 0)")
    parser.add_argument("--working-distance-mm", type=positive_float, required=True,
                        help="nominal working distance Z in mm (> 0)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--focal-length-px",
        type=positive_float,
        help="Camera focal length in pixels at the capture resolution (> 0).",
    )
    source.add_argument(
        "--focal-length-mm",
        type=positive_float,
        help="Lens focal length; requires --pixel-pitch-um.",
    )
    source.add_argument(
        "--from-measured-shift",
        nargs=2,
        type=positive_float,
        metavar=("STEP_MM", "SHIFT_PX"),
        help="Derive the focal length from an observed height step (inverted with the "
             "selected --layout). Preferred: needs no lens data and folds in the real optics.",
    )
    parser.add_argument(
        "--pixel-pitch-um",
        type=positive_float,
        help="Effective pixel pitch at the capture resolution, in micrometres (with --focal-length-mm).",
    )
    parser.add_argument("--layout", choices=LAYOUTS, default=TILTED_CAMERA,
                        help="tilted-camera: laser perpendicular to the work "
                             "plane, camera offset and aimed back at the stripe "
                             "(the standard arrangement, default). tilted-laser: camera "
                             "perpendicular, laser aimed across to its axis.")
    parser.add_argument("--noise-px", type=positive_float, default=0.22,
                        help="Measured centroid repeatability in px (> 0), default 0.22.")
    parser.add_argument("--image-width-px", type=positive_int, default=640,
                        help="Image width at the capture resolution, default 640.")
    parser.add_argument("--target-accuracy-mm", type=positive_float,
                        help="Requested 1-sigma height noise in mm (> 0); prints what reaching it needs.")
    parser.add_argument("--sweep", action="store_true",
                        help="Print an angle/distance table around the given point.")
    parser.add_argument("--json", type=Path,
                        help="Also write the budget to this path (NaN/Infinity written as null).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pixel_pitch_um is not None and args.focal_length_mm is None:
        parser.error("--pixel-pitch-um is only used with --focal-length-mm")
    if args.from_measured_shift:
        step_mm, shift_px = args.from_measured_shift
        if step_mm >= args.working_distance_mm:
            parser.error("--from-measured-shift: STEP_MM must be smaller than --working-distance-mm")
        focal_length_px = focal_length_from_shift(
            args.baseline_mm, args.working_distance_mm, step_mm, shift_px, args.layout
        )
        source = (f"derived from a measured {step_mm:g} mm step producing "
                  f"{shift_px:g} px of shift ({args.layout} model)")
    elif args.focal_length_px is not None:
        focal_length_px = args.focal_length_px
        source = "given directly"
    else:
        if args.pixel_pitch_um is None:
            parser.error("--focal-length-mm requires --pixel-pitch-um")
        focal_length_px = args.focal_length_mm / (args.pixel_pitch_um / 1000.0)
        source = (f"estimated from {args.focal_length_mm:g} mm lens and "
                  f"{args.pixel_pitch_um:g} um pixel pitch")

    budget = evaluate(
        args.baseline_mm, args.working_distance_mm, focal_length_px,
        args.noise_px, args.image_width_px, args.layout,
    )
    print(f"focal length {focal_length_px:.1f} px, {source}")
    print()
    print(format_budget(budget, args.target_accuracy_mm))
    if args.sweep:
        print()
        distance = args.working_distance_mm
        print(sweep(
            focal_length_px, args.noise_px, args.image_width_px,
            [20.0, 30.0, 35.0, 40.0, 45.0, 55.0],
            [distance * factor for factor in (0.25, 0.4, 0.6, 0.8, 1.0)],
            args.layout,
        ))
    if args.json:
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "focal_length_source": source,
            "target_accuracy_mm": args.target_accuracy_mm,
            "budget": asdict(budget),
        }
        try:
            with args.json.open("w", encoding="utf-8") as handle:
                json.dump(json_safe(payload), handle, indent=2, allow_nan=False)
                handle.write("\n")
        except OSError as exc:
            parser.exit(1, f"{parser.prog}: error: cannot write {args.json}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
