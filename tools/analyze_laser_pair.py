#!/usr/bin/env python3
"""Analyze a laser ON/OFF pair locally and emit auditable diagnostics.

Writes the score image, overlay, profile CSV and JSON summary to --output-dir
and prints the configuration and summary as JSON. Measurements stay in pixels.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from cli_common import (
    description,
    fail,
    float_range,
    int_range,
    json_safe,
    non_negative_float,
    non_negative_int,
    positive_int,
)
from laser_pipeline import ExtractorConfig, extract_stripe, load_rgb, save_diagnostics

PROG = "analyze_laser_pair"
EXIT_INVALID_PROFILE = 3
byte_value = int_range(0, 255)
ratio = float_range(0.0, 1.0)


def build_parser() -> argparse.ArgumentParser:
    defaults = ExtractorConfig()
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=description(__doc__),
        epilog=(
            "Exit codes: 0 valid profile; 1 input/processing error (missing or unreadable image, size mismatch); "
            f"2 usage error; {EXIT_INVALID_PROFILE} extraction ran but produced an invalid profile "
            "(diagnostics are still written)."
        ),
    )
    parser.add_argument("--on", type=Path, required=True, help="laser-ON image")
    parser.add_argument("--off", type=Path, help="laser-OFF image of the same scene (enables difference scoring)")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for diagnostics")
    parser.add_argument("--roi", nargs=4, type=non_negative_int, metavar=("X0", "Y0", "X1", "Y1"), default=(0, 0, 0, 0),
                        help="region of interest in pixels, exclusive X1/Y1; all zero = whole image (default)")
    parser.add_argument("--threshold", type=byte_value, default=defaults.score_threshold,
                        help=f"minimum pixel score 0..255 (default {defaults.score_threshold})")
    parser.add_argument("--max-width", type=positive_int, default=defaults.max_stripe_width_px,
                        help=f"widest accepted stripe in px (default {defaults.max_stripe_width_px})")
    parser.add_argument("--max-clipped-core-gap", type=non_negative_int, default=defaults.max_clipped_core_gap_px,
                        help=f"widest clipped core bridged in px (default {defaults.max_clipped_core_gap_px})")
    parser.add_argument("--clipped-core-min-luma", type=byte_value, default=defaults.clipped_core_min_luma,
                        help=f"0..255 (default {defaults.clipped_core_min_luma})")
    parser.add_argument("--clipped-core-min-delta", type=byte_value, default=defaults.clipped_core_min_delta,
                        help=f"0..255 (default {defaults.clipped_core_min_delta})")
    parser.add_argument("--clipped-core-max-chroma", type=byte_value, default=defaults.clipped_core_max_chroma,
                        help=f"0..255 (default {defaults.clipped_core_max_chroma})")
    parser.add_argument("--ghost-ratio", type=ratio, default=defaults.ghost_ratio,
                        help=f"secondary/primary weight ratio flagged as ghost, 0..1 (default {defaults.ghost_ratio})")
    parser.add_argument("--minimum-valid-ratio", type=ratio, default=defaults.minimum_valid_ratio,
                        help=f"valid-row fraction for a valid profile, 0..1 (default {defaults.minimum_valid_ratio})")
    parser.add_argument("--prediction-window", type=non_negative_float, default=defaults.prediction_window_px,
                        help="continuity tracking window in px around the previous row; 0 disables (default 0)")
    parser.add_argument("--initial-prediction", type=float_range(), default=defaults.initial_prediction_px,
                        help="seed x position in px for the first scanned row; negative = no seed (default -1)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    x0, y0, x1, y1 = args.roi
    if any(args.roi) and (x1 <= x0 or y1 <= y0):
        parser.error("--roi needs X1 > X0 and Y1 > Y0 (or all zero for the whole image)")

    try:
        on = load_rgb(args.on)
        off = load_rgb(args.off) if args.off else None
        if off is not None and off.size != on.size:
            raise ValueError(f"ON {on.size[0]}x{on.size[1]} and OFF {off.size[0]}x{off.size[1]} sizes differ")
        if any(args.roi) and (x1 > on.size[0] or y1 > on.size[1]):
            raise ValueError(f"--roi {x0} {y0} {x1} {y1} exceeds the {on.size[0]}x{on.size[1]} image")
        config = ExtractorConfig(
            roi_x0=x0, roi_y0=y0, roi_x1=x1, roi_y1=y1,
            score_threshold=args.threshold, max_stripe_width_px=args.max_width,
            max_clipped_core_gap_px=args.max_clipped_core_gap,
            clipped_core_min_luma=args.clipped_core_min_luma,
            clipped_core_min_delta=args.clipped_core_min_delta,
            clipped_core_max_chroma=args.clipped_core_max_chroma,
            ghost_ratio=args.ghost_ratio, minimum_valid_ratio=args.minimum_valid_ratio,
            prediction_window_px=args.prediction_window,
            initial_prediction_px=args.initial_prediction,
        )
        profile, summary, score = extract_stripe(on, off, config)
        save_diagnostics(args.output_dir, on, profile, summary, score, config)
    except (OSError, ValueError) as exc:
        raise fail(PROG, exc) from None
    report = {"output_dir": str(args.output_dir), "config": asdict(config), "summary": asdict(summary)}
    print(json.dumps(json_safe(report), indent=2, allow_nan=False))
    if not summary.valid:
        print(f"{PROG}: extraction produced an invalid profile (exit {EXIT_INVALID_PROFILE})", file=sys.stderr)
        return EXIT_INVALID_PROFILE
    return 0


if __name__ == "__main__":
    sys.exit(main())
