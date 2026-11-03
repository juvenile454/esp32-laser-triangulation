#!/usr/bin/env python3
"""Generate deterministic laser ON/OFF fixtures with exact pixel truth."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from pathlib import Path

from PIL import Image

WIDTH = 320
HEIGHT = 240

# Exact truth for the fillet-corner fixture, shared with the test suite.
CORNER_Y = 132
CORNER_X = 150.0
FIRST_LEG_SLOPE = -0.45
SECOND_LEG_SLOPE = 0.30


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def background(seed: int) -> Image.Image:
    rng = random.Random(seed)
    pixels = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            base = 52 + x // 14 + y // 20 + rng.randint(-3, 3)
            if x > 210 and y > 120:
                base += 14
            index = (y * WIDTH + x) * 3
            pixels[index:index + 3] = bytes((min(255, base + 3), min(255, base + 1), min(255, base)))
    return Image.frombytes("RGB", (WIDTH, HEIGHT), bytes(pixels))


def add_stripe(image: Image.Image, truth: list[float | None], amplitude: int, sigma: float, ghost_offset: float | None = None, ghost_ratio: float = 0.0) -> None:
    pixels = image.load()
    for y, center in enumerate(truth):
        if center is None:
            continue
        centers = [(center, amplitude)]
        if ghost_offset is not None:
            centers.append((center + ghost_offset, int(amplitude * ghost_ratio)))
        radius = max(2, int(math.ceil(4 * sigma)))
        for stripe_center, stripe_amplitude in centers:
            for x in range(max(0, int(stripe_center) - radius), min(WIDTH, int(stripe_center) + radius + 1)):
                intensity = int(stripe_amplitude * math.exp(-0.5 * ((x - stripe_center) / sigma) ** 2))
                r, g, b = pixels[x, y]
                pixels[x, y] = (min(255, r + intensity), min(255, g + intensity // 8), min(255, b + intensity // 12))


def add_clipped_white_core(image: Image.Image, truth: list[float | None], radius: int = 2) -> None:
    pixels = image.load()
    for y, center in enumerate(truth):
        if center is None:
            continue
        rounded = int(round(center))
        for x in range(max(0, rounded - radius), min(WIDTH, rounded + radius + 1)):
            pixels[x, y] = (250, 250, 250)


def add_clipped_warm_core(image: Image.Image, truth: list[float | None], radius: int = 2) -> None:
    pixels = image.load()
    for y, center in enumerate(truth):
        if center is None:
            continue
        rounded = int(round(center))
        for x in range(max(0, rounded - radius), min(WIDTH, rounded + radius + 1)):
            pixels[x, y] = (255, 255, 198)


def add_specular_blob(image: Image.Image, first_x: int, last_x: int, amplitude: int) -> None:
    """A broad, moderately red glare field, as blank steel produces.

    Its total weight deliberately exceeds the true stripe's, so selecting by
    strength alone picks the blob. The width filter must reject it first.
    """
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(max(0, first_x), min(WIDTH, last_x + 1)):
            r, g, b = pixels[x, y]
            pixels[x, y] = (
                min(255, r + amplitude),
                min(255, g + amplitude // 3),
                min(255, b + amplitude // 3),
            )


def corner_truth(corner_y: int, corner_x: float, first_slope: float, second_slope: float) -> list[float | None]:
    """Two straight legs meeting at an exactly known corner."""
    values: list[float | None] = []
    for y in range(HEIGHT):
        offset = y - corner_y
        slope = first_slope if offset < 0 else second_slope
        values.append(corner_x + slope * offset)
    return values


def write_ppm(image: Image.Image, path: Path) -> None:
    rgb = image.convert("RGB")
    path.write_bytes(f"P6\n{rgb.width} {rgb.height}\n255\n".encode("ascii") + rgb.tobytes())


def metadata(sample_id: str, on: Path, off: Path, limitations: list[str]) -> dict:
    return {
        "schema_version": "1.0",
        "sample_id": sample_id,
        "evidence_tier": "synthetic_fixture",
        "capture": {
            "fixture_revision": "synthetic-v1",
            "registered_pair": True,
            "timestamp_utc": None,
            "robot_pose": None,
            "camera": {"sensor": None, "width_px": WIDTH, "height_px": HEIGHT, "exposure": None, "gain": None, "white_balance_locked": True},
            "laser": {"wavelength_nm": 650, "power_class": "synthetic", "switch_method": "rendered"},
        },
        "images": {
            "laser_on": {"file": on.name, "sha256": sha256(on), "format": "PNG"},
            "laser_off": {"file": off.name, "sha256": sha256(off), "format": "PNG"},
        },
        "geometry": {"working_distance_mm": None, "baseline_mm": None, "laser_angle_deg": None},
        "ground_truth": {"lateral_offset_mm": None, "height_offset_mm": None, "source": "truth.csv pixel centreline"},
        "labels": {"valid": None, "reflection": None, "occlusion": None, "surface": "synthetic"},
        "limitations": limitations + ["Synthetic pixels do not establish optical accuracy or real-surface generalization."],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    scenarios: list[tuple[str, list[float | None], int, float, float | None, float, list[str]]] = []
    scenarios.append(("straight", [90.0 + 0.12 * y for y in range(HEIGHT)], 190, 1.2, None, 0.0, []))
    scenarios.append(("step", [92.0 + 0.08 * y if y < 120 else 132.0 + 0.08 * y for y in range(HEIGHT)], 185, 1.3, None, 0.0, []))
    scenarios.append(("fillet_like", [105.0 + 0.0025 * (y - 120) ** 2 for y in range(HEIGHT)], 175, 1.4, None, 0.0, []))
    gap = [112.0 + 0.05 * y for y in range(HEIGHT)]
    for y in range(80, 125):
        gap[y] = None
    scenarios.append(("occlusion_gap", gap, 180, 1.3, None, 0.0, ["Rows 80..124 intentionally contain no stripe."]))
    scenarios.append(("ghost", [96.0 + 0.10 * y for y in range(HEIGHT)], 180, 1.2, 38.0, 0.62, ["Secondary stripe intentionally rendered 38 px to the right."]))
    scenarios.append(("weak_broad", [120.0 - 0.04 * y for y in range(HEIGHT)], 65, 4.2, None, 0.0, ["Weak broad stripe challenges width and threshold handling."]))
    scenarios.append(("saturated", [82.0 + 0.18 * y for y in range(HEIGHT)], 255, 1.6, None, 0.0, ["Stripe core intentionally clips at 255."]))
    scenarios.append(("no_stripe", [None for _ in range(HEIGHT)], 0, 1.0, None, 0.0, ["Intentionally invalid no-stripe input."]))
    scenarios.append(("clipped_white_core", [88.0 + 0.14 * y for y in range(HEIGHT)], 190, 2.3, None, 0.0, ["One stripe has a neutral clipped core between two red flanks."]))
    scenarios.append(("clipped_warm_core", [86.0 + 0.15 * y for y in range(HEIGHT)], 190, 2.3, None, 0.0, ["One stripe has a warm yellow-white clipped core between two red flanks."]))
    scenarios.append((
        "fillet_corner",
        corner_truth(CORNER_Y, CORNER_X, FIRST_LEG_SLOPE, SECOND_LEG_SLOPE),
        185, 1.3, None, 0.0,
        [f"Two straight legs meet at exactly y={CORNER_Y}, x={CORNER_X}.",
         f"Leg slopes dx/dy are {FIRST_LEG_SLOPE} before and {SECOND_LEG_SLOPE} after the corner."],
    ))
    scenarios.append((
        "blob_and_stripe",
        [100.0 + 0.08 * y for y in range(HEIGHT)],
        200, 1.2, None, 0.0,
        ["A 44 px specular field at x=200..243 outweighs the true 5 px stripe.",
         "Selecting by strength before filtering width loses the stripe entirely."],
    ))

    manifest = {"generator_version": 1, "width_px": WIDTH, "height_px": HEIGHT, "samples": []}
    for index, (name, truth, amplitude, sigma, ghost_offset, ghost_ratio, limitations) in enumerate(scenarios, 1):
        sample_id = f"synthetic_{index:03d}_{name}"
        sample_dir = args.output / sample_id
        sample_dir.mkdir(parents=True, exist_ok=True)
        off = background(1000 + index)
        on = off.copy()
        add_stripe(on, truth, amplitude, sigma, ghost_offset, ghost_ratio)
        if name == "clipped_white_core":
            add_clipped_white_core(on, truth)
        elif name == "clipped_warm_core":
            add_clipped_warm_core(on, truth)
        elif name == "blob_and_stripe":
            add_specular_blob(on, 200, 243, 70)
        on_path = sample_dir / "laser_on.png"
        off_path = sample_dir / "laser_off.png"
        on.save(on_path)
        off.save(off_path)
        write_ppm(on, sample_dir / "laser_on.ppm")
        write_ppm(off, sample_dir / "laser_off.ppm")
        with (sample_dir / "truth.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("y_px", "x_px", "valid"))
            for y, x in enumerate(truth):
                writer.writerow((y, "" if x is None else f"{x:.6f}", str(x is not None).lower()))
        item_metadata = metadata(sample_id, on_path, off_path, limitations)
        (sample_dir / "metadata.json").write_text(json.dumps(item_metadata, indent=2) + "\n", encoding="utf-8")
        manifest["samples"].append({"sample_id": sample_id, "directory": sample_id})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"SYNTHETIC_RESULT: PASS ({len(scenarios)} samples in {args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
