#!/usr/bin/env python3
"""Render the README images (light + dark variants) from HTML/SVG.

Usage (from the repository root):

    python3 docs/images/source/render_images.py prepare /tmp/laser-images
    node docs/images/source/shoot.js /tmp/laser-images/manifest.json
    python3 docs/images/source/render_images.py quantize /tmp/laser-images

``prepare`` generates the synthetic fixtures, runs the offline analysis on
three of them and writes one HTML page per image plus a Playwright manifest.
``quantize`` palette-quantises the rendered PNGs to keep the repository small.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "docs" / "images"
sys.path.insert(0, str(REPO / "tools"))
from triangulation_budget import TILTED_CAMERA, TILTED_LASER, evaluate  # noqa: E402

FOCAL_PX = 375.0  # estimated, not measured; see docs/GEOMETRY.md
NOISE_PX = 0.22  # static centroid floor; reached only with an unclipped core
FIXTURES = (
    ("011_fillet_corner", "Fillet corner", "corner recovered to 0.001 px"),
    ("005_ghost", "Ghost reflection", "secondary line flagged, not chosen"),
    ("004_occlusion_gap", "Occlusion gap", "missing rows reported, not invented"),
)

THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#3f3e3b", "muted": "#6f6e69",
        "grid": "#e1e0d9", "axis": "#b9b8b0", "ramp": ["#86b6ef", "#2a78d6", "#104281"],
        "laser": "#d03b2f", "laser_soft": "rgba(208,59,47,.12)", "part": "#d9d8d1", "part_edge": "#9a998f",
        "accent": "#2a78d6", "card": "#f3f2ee", "trace": "#ff9f1c",
    },
    "dark": {
        "surface": "#161b22", "ink": "#f0efec", "ink2": "#d2d1c8", "muted": "#9d9c94",
        "grid": "#2c2f35", "axis": "#4a4d55", "ramp": ["#3f7fd0", "#7fb2f0", "#cfe2fb"],
        "laser": "#ff6b5e", "laser_soft": "rgba(255,107,94,.14)", "part": "#2f333b", "part_edge": "#6b6f78",
        "accent": "#3987e5", "card": "#1f252e", "trace": "#ffb347",
    },
}
FONT = "font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;"


def page(body: str, width: int, height: int, theme: dict) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:{theme['surface']};}}
.wrap{{width:{width}px;height:{height}px;{FONT}color:{theme['ink']};}}
</style></head><body><div class="wrap">{body}</div></body></html>"""


def halo(theme: dict) -> str:
    return f'paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5" stroke-linejoin="round"'


# --------------------------------------------------------------------------- chart
def chart(theme: dict) -> tuple[str, int, int]:
    width, height = 900, 500
    left, right, top, bottom = 92, 130, 84, 58
    pw, ph = width - left - right, height - top - bottom
    z_min, z_max = 80.0, 600.0
    y_min, y_max = 0.02, 8.0
    t = theme

    def sx(z: float) -> float:
        return left + (z - z_min) / (z_max - z_min) * pw

    def sy(v: float) -> float:
        return top + (math.log10(y_max) - math.log10(v)) / (math.log10(y_max) - math.log10(y_min)) * ph

    def noise(baseline: float, z: float, layout: str = TILTED_CAMERA) -> float:
        return evaluate(baseline, z, FOCAL_PX, NOISE_PX, 640, layout).height_noise_mm

    parts = []
    for v in (0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0):
        y = sy(v)
        parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{t["grid"]}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 10}" y="{y + 5:.1f}" text-anchor="end" font-size="14" fill="{t["ink2"]}">{v:g}</text>')
    for z in range(100, 601, 100):
        x = sx(z)
        parts.append(f'<text x="{x:.1f}" y="{top + ph + 24}" text-anchor="middle" font-size="14" fill="{t["ink2"]}">{z}</text>')
    parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}" stroke="{t["axis"]}" stroke-width="1"/>')

    ty = sy(0.1)
    parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{t["ink2"]}" stroke-width="1.5" stroke-dasharray="6 5"/>')
    parts.append(f'<text x="{left + pw - 6}" y="{ty - 12:.1f}" text-anchor="end" font-size="14" fill="{t["ink2"]}" {halo(t)}>0.1 mm target for a fillet weld</text>')

    baselines = (35.0, 84.0, 140.0)
    for baseline, color in zip(baselines, t["ramp"], strict=True):
        points = []
        z = z_min
        while z <= z_max + 0.1:
            points.append((sx(z), sy(min(max(noise(baseline, z), y_min), y_max))))
            z += 5.0
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
        end = noise(baseline, z_max)
        parts.append(f'<circle cx="{sx(z_max):.1f}" cy="{sy(end):.1f}" r="4" fill="{color}" stroke="{t["surface"]}" stroke-width="2"/>')
        parts.append(f'<text x="{sx(z_max) + 12:.1f}" y="{sy(end) + 5:.1f}" font-size="14" font-weight="600" fill="{t["ink"]}">B = {baseline:g} mm</text>')

    # Today's provisional carrier uses the other layout; mark it with a distinct square.
    now = noise(70.0, 400.0, TILTED_LASER)
    cx, cy = sx(400.0), sy(now)
    lx, ly = sx(215.0), sy(4.2)
    parts.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{lx + 4:.1f}" y2="{ly + 8:.1f}" stroke="{t["ink2"]}" stroke-width="1"/>')
    parts.append(f'<rect x="{cx - 7:.1f}" y="{cy - 7:.1f}" width="14" height="14" rx="2" fill="{t["laser"]}" stroke="{t["surface"]}" stroke-width="2.5"/>')
    parts.append(f'<text x="{lx:.1f}" y="{ly - 8:.1f}" text-anchor="end" font-size="15" font-weight="650" fill="{t["ink"]}" {halo(t)}>Today: {now:.2f} mm</text>')
    parts.append(f'<text x="{lx:.1f}" y="{ly + 10:.1f}" text-anchor="end" font-size="13" fill="{t["ink2"]}" {halo(t)}>provisional carrier, B 70 · Z 400 mm</text>')
    parts.append(f'<text x="{lx:.1f}" y="{ly + 27:.1f}" text-anchor="end" font-size="13" fill="{t["ink2"]}" {halo(t)}>tilted-laser layout (not on the curves)</text>')

    goal = noise(84.0, 120.0)
    gx, gy = sx(120.0), sy(goal)
    rx, ry = sx(250.0), sy(0.045)
    parts.append(f'<line x1="{gx:.1f}" y1="{gy:.1f}" x2="{rx - 4:.1f}" y2="{ry - 14:.1f}" stroke="{t["ink2"]}" stroke-width="1"/>')
    parts.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="7" fill="{t["surface"]}" stroke="{t["ink"]}" stroke-width="2.5"/>')
    parts.append(f'<text x="{rx:.1f}" y="{ry - 4:.1f}" font-size="15" font-weight="650" fill="{t["ink"]}" {halo(t)}>Recommended: {goal:.2f} mm</text>')
    parts.append(f'<text x="{rx:.1f}" y="{ry + 14:.1f}" font-size="13" fill="{t["ink2"]}" {halo(t)}>rail fixture, B 84 · Z 120 mm, tilted camera</text>')

    ylabel_y = top + ph / 2
    parts.append(f'<text x="24" y="{ylabel_y:.1f}" text-anchor="middle" font-size="14" fill="{t["ink2"]}" '
                 f'transform="rotate(-90 24 {ylabel_y:.1f})">1σ height noise (mm, log scale)</text>')
    title = (f'<text x="24" y="32" font-size="20" font-weight="650" fill="{t["ink"]}">'
             'Height noise grows with working distance</text>'
             f'<text x="24" y="56" font-size="14" fill="{t["ink2"]}">'
             'Curves: tilted-camera layout, B = camera–laser baseline, estimated f ≈ 375 px, '
             '0.22 px centroid floor (needs an unclipped core)</text>')
    xlabel = f'<text x="{left + pw / 2:.1f}" y="{height - 12}" text-anchor="middle" font-size="14" fill="{t["ink2"]}">working distance Z (mm)</text>'
    svg = f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">{title}{"".join(parts)}{xlabel}</svg>'
    return svg, width, height


# --------------------------------------------------------------------------- principle
def principle_svg(theme: dict, *, compact: bool = False) -> tuple[str, int, int]:
    width, height = 900, 470
    t = theme
    laser_x, top_y = 330, 100
    cam_x, cam_y = 540, 122
    v_left, v_mid, v_right = (210, 280), (330, 375), (450, 280)
    beam_top = top_y + 22
    # angle arc between the laser axis (straight up from the root) and the camera ray
    ray_dx, ray_dy = cam_x - 10 - v_mid[0], cam_y + 22 - v_mid[1]
    ray_len = math.hypot(ray_dx, ray_dy)
    arc_r = 58
    ax0, ay0 = v_mid[0], v_mid[1] - arc_r
    ax1, ay1 = v_mid[0] + ray_dx / ray_len * arc_r, v_mid[1] + ray_dy / ray_len * arc_r
    title = "" if compact else (
        f'<text x="40" y="38" font-size="20" font-weight="650" fill="{t["ink"]}">How the laser line turns height into image position</text>')
    svg = f"""
<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path d="M0,0 L8,4 L0,8 z" fill="{t['ink2']}"/>
    </marker>
    <marker id="arrs" markerWidth="8" markerHeight="8" refX="1" refY="4" orient="auto">
      <path d="M8,0 L0,4 L8,8 z" fill="{t['ink2']}"/>
    </marker>
  </defs>
  {title}
  <polygon points="{laser_x},{beam_top} {v_left[0] + 18},{v_left[1] + 8} {v_mid[0]},{v_mid[1]} {v_right[0] - 18},{v_right[1] + 8}" fill="{t['laser_soft']}"/>
  <line x1="{laser_x}" y1="{beam_top}" x2="{v_mid[0]}" y2="{v_mid[1]}" stroke="{t['laser']}" stroke-width="1.5" stroke-dasharray="4 4"/>
  <polygon points="{v_left[0] - 30},{v_left[1] - 30} {v_mid[0]},{v_mid[1]} {v_right[0] + 30},{v_right[1] - 30} {v_right[0] + 30},{v_right[1] - 18} {v_mid[0]},{v_mid[1] + 18} {v_left[0] - 30},{v_left[1] - 18}" fill="{t['part']}" stroke="{t['part_edge']}" stroke-width="1.2"/>
  <polyline points="{v_left[0] + 18},{v_left[1] + 8} {v_mid[0]},{v_mid[1]} {v_right[0] - 18},{v_right[1] + 8}" fill="none" stroke="{t['laser']}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="{v_mid[0]}" y="{v_mid[1] + 50}" text-anchor="middle" font-size="15" fill="{t['ink2']}">fillet joint: the stripe bends at the seam</text>
  <rect x="{laser_x - 18}" y="{top_y - 6}" width="36" height="28" rx="5" fill="{t['laser']}"/>
  <text x="{laser_x - 30}" y="{top_y + 14}" text-anchor="end" font-size="16" font-weight="600" fill="{t['ink']}">650 nm line laser</text>
  <g transform="rotate(32 {cam_x} {cam_y})">
    <rect x="{cam_x - 26}" y="{cam_y - 16}" width="52" height="32" rx="6" fill="{t['accent']}"/>
    <rect x="{cam_x - 9}" y="{cam_y + 14}" width="18" height="10" rx="2" fill="{t['accent']}"/>
  </g>
  <line x1="{cam_x - 10}" y1="{cam_y + 22}" x2="{v_mid[0] + 6}" y2="{v_mid[1] - 6}" stroke="{t['accent']}" stroke-width="1.8" stroke-dasharray="5 4"/>
  <text x="{cam_x + 42}" y="{cam_y - 2}" font-size="16" font-weight="600" fill="{t['ink']}">ESP32-S3 + OV3660</text>
  <text x="{cam_x + 42}" y="{cam_y + 18}" font-size="14" fill="{t['ink2']}">views the stripe at angle α</text>
  <path d="M{ax0:.1f},{ay0:.1f} A{arc_r},{arc_r} 0 0 1 {ax1:.1f},{ay1:.1f}" fill="none" stroke="{t['ink']}" stroke-width="1.6"/>
  <text x="{v_mid[0] + 16}" y="{v_mid[1] - arc_r - 6}" font-size="21" font-style="italic" font-weight="700" fill="{t['ink']}" {halo(t)}>α</text>
  <line x1="{laser_x}" y1="{top_y - 30}" x2="{cam_x}" y2="{top_y - 30}" stroke="{t['ink2']}" stroke-width="1.3" marker-start="url(#arrs)" marker-end="url(#arr)"/>
  <text x="{(laser_x + cam_x) / 2}" y="{top_y - 40}" text-anchor="middle" font-size="15" fill="{t['ink2']}">baseline B</text>
  <line x1="{laser_x - 80}" y1="{beam_top}" x2="{laser_x - 80}" y2="{v_mid[1]}" stroke="{t['ink2']}" stroke-width="1.3" marker-start="url(#arrs)" marker-end="url(#arr)"/>
  <line x1="{laser_x - 90}" y1="{beam_top}" x2="{laser_x - 20}" y2="{beam_top}" stroke="{t['axis']}" stroke-width="1" stroke-dasharray="2 3"/>
  <line x1="{laser_x - 90}" y1="{v_mid[1]}" x2="{v_mid[0] - 10}" y2="{v_mid[1]}" stroke="{t['axis']}" stroke-width="1" stroke-dasharray="2 3"/>
  <text x="{laser_x - 92}" y="{(beam_top + v_mid[1]) / 2 + 5}" text-anchor="end" font-size="15" fill="{t['ink2']}">standoff Z</text>
  <g transform="translate(650 190)">
    <rect x="0" y="0" width="210" height="160" rx="8" fill="{t['card']}" stroke="{t['axis']}"/>
    <text x="105" y="-14" text-anchor="middle" font-size="15" font-weight="600" fill="{t['ink']}">camera image</text>
    <polyline points="72,10 116,80 84,150" fill="none" stroke="{t['laser']}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="116" cy="80" r="7" fill="none" stroke="{t['ink']}" stroke-width="2"/>
    <text x="128" y="72" font-size="13" fill="{t['ink2']}">seam corner</text>
    <line x1="14" y1="146" x2="60" y2="146" stroke="{t['ink2']}" stroke-width="1.2" marker-end="url(#arr)"/>
    <text x="14" y="138" font-size="12" fill="{t['ink2']}">x = disparity</text>
    <line x1="14" y1="16" x2="14" y2="60" stroke="{t['ink2']}" stroke-width="1.2" marker-end="url(#arr)"/>
    <text x="22" y="30" font-size="12" fill="{t['ink2']}">row y</text>
    <text x="105" y="186" text-anchor="middle" font-size="14" fill="{t['ink2']}">sub-pixel centre per row,</text>
    <text x="105" y="204" text-anchor="middle" font-size="14" fill="{t['ink2']}">two-leg fit locates the corner</text>
  </g>
  <line x1="{v_right[0] + 50}" y1="265" x2="636" y2="265" stroke="{t['ink2']}" stroke-width="1.3" marker-end="url(#arr)"/>
  <text x="{(v_right[0] + 50 + 636) / 2}" y="255" text-anchor="middle" font-size="13" fill="{t['ink2']}">height change</text>
  <text x="{(v_right[0] + 50 + 636) / 2}" y="285" text-anchor="middle" font-size="13" fill="{t['ink2']}">moves the stripe in x</text>
</svg>"""
    return svg, width, height


def principle(theme: dict) -> tuple[str, int, int]:
    return principle_svg(theme)


# --------------------------------------------------------------------------- extraction panel
def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true"}


def centreline_paths(profile_csv: Path, scale_x: float, scale_y: float) -> list[str]:
    """Polyline segments of valid rows; invalid rows break the line instead of being bridged."""
    segments: list[list[tuple[float, float]]] = [[]]
    with profile_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if _truthy(row["valid"]):
                segments[-1].append((float(row["x_px"]) * scale_x, (float(row["y_px"]) + 0.5) * scale_y))
            elif segments[-1]:
                segments.append([])
    paths = []
    for segment in segments:
        if len(segment) >= 2:
            paths.append(" ".join(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}" for i, (x, y) in enumerate(segment)))
    return paths


def panel(theme: dict, scratch: Path) -> tuple[str, int, int]:
    t = theme
    cols = ("Laser-ON frame", "Score image (ON − OFF)", "Extracted centre per row")
    cell_w, cell_h, gap, label_w = 256, 192, 10, 180
    width = label_w + 3 * cell_w + 2 * gap + 30
    height = 96 + 3 * cell_h + 2 * gap + 46
    html = [f'<div style="position:relative;width:{width}px;height:{height}px">']
    html.append('<div style="position:absolute;left:20px;top:16px;font-size:20px;font-weight:650">'
                'Stripe extraction on the synthetic regression fixtures</div>')
    for c, name in enumerate(cols):
        x = label_w + c * (cell_w + gap)
        html.append(f'<div style="position:absolute;left:{x}px;top:66px;width:{cell_w}px;font-size:14px;'
                    f'color:{t["ink2"]};text-align:center">{name}</div>')
    for r, (key, title, note) in enumerate(FIXTURES):
        y = 92 + r * (cell_h + gap)
        html.append(f'<div style="position:absolute;left:20px;top:{y + cell_h / 2 - 24}px;width:{label_w - 30}px">'
                    f'<div style="font-size:15px;font-weight:650">{title}</div>'
                    f'<div style="font-size:13px;color:{t["ink2"]};margin-top:4px;line-height:1.35">{note}</div></div>')
        on_png = scratch / "syn" / f"synthetic_{key}" / "laser_on.png"
        score_png = scratch / f"an_{key}" / "score.png"
        for c, image in enumerate((on_png, score_png)):
            x = label_w + c * (cell_w + gap)
            html.append(f'<img src="{data_uri(image)}" style="position:absolute;left:{x}px;top:{y}px;width:{cell_w}px;'
                        f'height:{cell_h}px;border-radius:6px">')
        x = label_w + 2 * (cell_w + gap)
        paths = centreline_paths(scratch / f"an_{key}" / "profile.csv", cell_w / 320.0, cell_h / 240.0)
        lines = "".join(f'<path d="{d}" fill="none" stroke="{t["trace"]}" stroke-width="3" '
                        'stroke-linecap="round" stroke-linejoin="round"/>' for d in paths)
        html.append(f'<div style="position:absolute;left:{x}px;top:{y}px;width:{cell_w}px;height:{cell_h}px;'
                    'border-radius:6px;overflow:hidden">'
                    f'<img src="{data_uri(on_png)}" style="width:{cell_w}px;height:{cell_h}px;filter:brightness(.55)">'
                    f'<svg width="{cell_w}" height="{cell_h}" style="position:absolute;left:0;top:0">{lines}</svg></div>')
    legend_y = 92 + 3 * cell_h + 2 * gap + 16
    html.append(f'<div style="position:absolute;left:{label_w}px;top:{legend_y}px;font-size:14px;color:{t["ink2"]}">'
                f'<span style="color:{t["laser"]};font-weight:700">━━</span> laser stripe in the frame&nbsp;&nbsp;&nbsp;'
                f'<span style="color:{t["trace"]};font-weight:700">━━</span> extracted centre&nbsp;&nbsp;&nbsp;'
                'breaks in the line = rows reported invalid</div>')
    html.append("</div>")
    return "".join(html), width, height


# --------------------------------------------------------------------------- social preview
def social(theme: dict) -> tuple[str, int, int]:
    width, height = 1280, 640
    t = theme
    art = f"""
<svg width="600" height="500" viewBox="0 0 560 460" xmlns="http://www.w3.org/2000/svg">
  <polygon points="250,40 70,300 250,400 430,300" fill="rgba(255,107,94,.30)"/>
  <polygon points="40,270 250,400 460,270 460,292 250,422 40,292" fill="{t['part']}" stroke="{t['part_edge']}" stroke-width="3"/>
  <polyline points="80,300 250,400 420,300" fill="none" stroke="{t['laser']}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="218" y="6" width="64" height="44" rx="9" fill="{t['laser']}"/>
  <g transform="rotate(32 470 70)"><rect x="426" y="42" width="88" height="56" rx="10" fill="{t['accent']}"/></g>
  <line x1="455" y1="108" x2="260" y2="390" stroke="{t['accent']}" stroke-width="5" stroke-dasharray="14 10"/>
  <circle cx="250" cy="400" r="20" fill="none" stroke="{t['ink']}" stroke-width="5"/>
</svg>"""
    body = f"""
<div style="position:relative;width:{width}px;height:{height}px;overflow:hidden">
  <div style="position:absolute;left:72px;top:96px;width:610px">
    <div style="font-size:24px;color:{t['laser']};font-weight:700;letter-spacing:.05em">OPEN-SOURCE RESEARCH PROTOTYPE</div>
    <div style="font-size:72px;font-weight:800;line-height:1.03;margin-top:18px">ESP32 Laser<br>Triangulation</div>
    <div style="font-size:31px;color:{t['ink']};line-height:1.35;margin-top:28px">Sub-pixel laser-stripe extraction
      on a low-cost ESP32-S3 camera board</div>
    <div style="font-size:24px;color:{t['ink2']};line-height:1.5;margin-top:30px">Firmware · host tools · C++ core with Python parity</div>
    <div style="font-size:22px;color:{t['muted']};margin-top:14px">github.com/juvenile454/esp32-laser-triangulation</div>
  </div>
  <div style="position:absolute;left:660px;top:44px">{art}</div>
</div>"""
    return body, width, height


# --------------------------------------------------------------------------- commands
def prepare(scratch: Path) -> None:
    python = sys.executable
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run([python, str(REPO / "tools" / "generate_synthetic_dataset.py"), str(scratch / "syn")],
                   check=True, stdout=subprocess.DEVNULL)
    for key, _, _ in FIXTURES:
        fixture = scratch / "syn" / f"synthetic_{key}"
        subprocess.run([python, str(REPO / "tools" / "analyze_laser_pair.py"),
                        "--on", str(fixture / "laser_on.png"), "--off", str(fixture / "laser_off.png"),
                        "--output-dir", str(scratch / f"an_{key}")], check=True, stdout=subprocess.DEVNULL)
    html_dir = scratch / "html"
    html_dir.mkdir(exist_ok=True)
    jobs = {
        "height-noise": chart,
        "measurement-principle": principle,
        "extraction-fixtures": lambda theme: panel(theme, scratch),
    }
    manifest = []
    for name, builder in jobs.items():
        for mode, theme in THEMES.items():
            body, width, height = builder(theme)
            path = html_dir / f"{name}-{mode}.html"
            path.write_text(page(body, width, height, theme), encoding="utf-8")
            manifest.append({"html": str(path), "png": str(OUT / f"{name}-{mode}.png"), "w": width, "h": height, "scale": 2})
    body, width, height = social(THEMES["dark"])
    path = html_dir / "social-preview.html"
    path.write_text(page(body, width, height, THEMES["dark"]), encoding="utf-8")
    manifest.append({"html": str(path), "png": str(OUT / "social-preview.png"), "w": width, "h": height, "scale": 1})
    (scratch / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"wrote {len(manifest)} pages; next: node docs/images/source/shoot.js {scratch / 'manifest.json'}")


def quantize(scratch: Path) -> None:
    from PIL import Image

    for entry in json.loads((scratch / "manifest.json").read_text(encoding="utf-8")):
        path = Path(entry["png"])
        with Image.open(path) as image:
            image.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
        print(f"quantised {path.relative_to(REPO)} ({path.stat().st_size // 1024} KiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("prepare", "quantize"))
    parser.add_argument("scratch", type=Path, help="Scratch directory outside the repository.")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.scratch)
    else:
        quantize(args.scratch)


if __name__ == "__main__":
    main()
