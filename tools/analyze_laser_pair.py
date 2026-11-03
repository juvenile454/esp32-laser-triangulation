#!/usr/bin/env python3
"""Analyze a laser ON/OFF pair locally and emit auditable diagnostics."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from laser_pipeline import ExtractorConfig, extract_stripe, load_rgb, save_diagnostics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--on", type=Path, required=True)
    parser.add_argument("--off", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--roi", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"), default=(0, 0, 0, 0))
    parser.add_argument("--threshold", type=int, default=20)
    parser.add_argument("--max-width", type=int, default=16)
    parser.add_argument("--max-clipped-core-gap", type=int, default=12)
    parser.add_argument("--clipped-core-min-luma", type=int, default=145)
    parser.add_argument("--clipped-core-min-delta", type=int, default=25)
    parser.add_argument("--clipped-core-max-chroma", type=int, default=80)
    parser.add_argument("--ghost-ratio", type=float, default=0.45)
    parser.add_argument("--minimum-valid-ratio", type=float, default=0.10)
    args = parser.parse_args()

    on = load_rgb(args.on)
    off = load_rgb(args.off) if args.off else None
    config = ExtractorConfig(
        roi_x0=args.roi[0], roi_y0=args.roi[1], roi_x1=args.roi[2], roi_y1=args.roi[3],
        score_threshold=args.threshold, max_stripe_width_px=args.max_width,
        max_clipped_core_gap_px=args.max_clipped_core_gap,
        clipped_core_min_luma=args.clipped_core_min_luma,
        clipped_core_min_delta=args.clipped_core_min_delta,
        clipped_core_max_chroma=args.clipped_core_max_chroma,
        ghost_ratio=args.ghost_ratio, minimum_valid_ratio=args.minimum_valid_ratio,
    )
    profile, summary, score = extract_stripe(on, off, config)
    save_diagnostics(args.output_dir, on, profile, summary, score, config)
    print(json.dumps({"output_dir": str(args.output_dir), "config": asdict(config), "summary": asdict(summary)}, indent=2))
    return 0 if summary.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
