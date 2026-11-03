#!/usr/bin/env python3
"""Render README images (light + dark) from HTML/SVG with Playwright."""

from __future__ import annotations

import base64
import math
import sys
from pathlib import Path

import json

REPO = Path(sys.argv[1])
SCRATCH = Path(sys.argv[2])
OUT = REPO / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "tools"))
from triangulation_budget import TILTED_CAMERA, TILTED_LASER, evaluate  # noqa: E402

THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#8a8984",
        "grid": "#e1e0d9", "axis": "#b9b8b0", "ramp": ["#86b6ef", "#2a78d6", "#104281"],
        "laser": "#d03b2f", "laser_soft": "rgba(208,59,47,.14)", "part": "#d9d8d1", "part_edge": "#9a998f",
        "accent": "#2a78d6", "card": "#f3f2ee",
    },
    "dark": {
        "surface": "#161b22", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#8f8e86",
        "grid": "#2c2f35", "axis": "#4a4d55", "ramp": ["#184f95", "#3987e5", "#9ec5f4"],
        "laser": "#ff6b5e", "laser_soft": "rgba(255,107,94,.16)", "part": "#2f333b", "part_edge": "#6b6f78",
        "accent": "#3987e5", "card": "#1f252e",
    },
}
FONT = "font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;"


def page(body: str, width: int, height: int, theme: dict) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:{theme['surface']};}}
.wrap{{width:{width}px;height:{height}px;{FONT}color:{theme['ink']};}}
</style></head><body><div class="wrap">{body}</div></body></html>"""


# --------------------------------------------------------------------------- chart
def chart(theme: dict) -> tuple[str, int, int]:
    width, height = 900, 460
    left, right, top, bottom = 78, 150, 64, 58
    pw, ph = width - left - right, height - top - bottom
    z_min, z_max = 80.0, 600.0
    y_min, y_max = 0.02, 8.0

    def sx(z: float) -> float:
        return left + (z - z_min) / (z_max - z_min) * pw

    def sy(v: float) -> float:
        return top + (math.log10(y_max) - math.log10(v)) / (math.log10(y_max) - math.log10(y_min)) * ph

    parts = []
    for v in (0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0):
        y = sy(v)
        parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{theme["grid"]}" stroke-width="1"/>')
        label = f"{v:g}"
        parts.append(f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="13" fill="{theme["ink2"]}">{label}</text>')
    for z in range(100, 601, 100):
        x = sx(z)
        parts.append(f'<text x="{x:.1f}" y="{top + ph + 22}" text-anchor="middle" font-size="13" fill="{theme["ink2"]}">{z}</text>')
    parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}" stroke="{theme["axis"]}" stroke-width="1"/>')

    # 0.1 mm target band
    ty = sy(0.1)
    parts.append(f'<line x1="{left}" x2="{left + pw}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{theme["ink2"]}" stroke-width="1.5" stroke-dasharray="6 5"/>')
    parts.append(f'<text x="{left + pw - 6}" y="{ty - 8:.1f}" text-anchor="end" font-size="13" fill="{theme["ink2"]}" paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5">0.1 mm target for a fillet weld</text>')

    baselines = (35.0, 70.0, 140.0)
    for baseline, color in zip(baselines, theme["ramp"], strict=True):
        points = []
        z = z_min
        while z <= z_max + 0.1:
            b = evaluate(baseline, z, 375.0, 0.22, 640, TILTED_CAMERA)
            points.append((sx(z), sy(min(max(b.height_noise_mm, y_min), y_max))))
            z += 5.0
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points))
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
        end = evaluate(baseline, z_max, 375.0, 0.22, 640, TILTED_CAMERA)
        parts.append(f'<circle cx="{sx(z_max):.1f}" cy="{sy(end.height_noise_mm):.1f}" r="4" fill="{color}" stroke="{theme["surface"]}" stroke-width="2"/>')
        parts.append(f'<text x="{sx(z_max) + 12:.1f}" y="{sy(end.height_noise_mm) + 4:.1f}" font-size="13" fill="{theme["ink"]}">B = {baseline:g} mm</text>')

    # current provisional carrier
    now = evaluate(70.0, 400.0, 375.0, 0.22, 640, TILTED_LASER)
    cx, cy = sx(400.0), sy(now.height_noise_mm)
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{theme["laser"]}" stroke="{theme["surface"]}" stroke-width="2.5"/>')
    parts.append(f'<text x="{cx + 14:.1f}" y="{cy - 6:.1f}" font-size="13" font-weight="600" fill="{theme["ink"]}" paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5">Today: {now.height_noise_mm:.2f} mm</text>')
    parts.append(f'<text x="{cx + 14:.1f}" y="{cy + 11:.1f}" font-size="12" fill="{theme["ink2"]}" paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5">provisional carrier (tilted laser), B 70 · Z 400 mm</text>')
    goal = evaluate(84.0, 120.0, 375.0, 0.22, 640, TILTED_CAMERA)
    gx, gy = sx(120.0), sy(goal.height_noise_mm)
    parts.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="7" fill="none" stroke="{theme["ink"]}" stroke-width="2"/>')
    parts.append(f'<text x="{gx + 12:.1f}" y="{gy + 22:.1f}" font-size="13" font-weight="600" fill="{theme["ink"]}" paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5">Recommended: {goal.height_noise_mm:.2f} mm</text>')
    parts.append(f'<text x="{gx + 12:.1f}" y="{gy + 38:.1f}" font-size="12" fill="{theme["ink2"]}" paint-order="stroke" stroke="{theme["surface"]}" stroke-width="5">rail fixture, B 84 · Z 120 mm</text>')

    title = (f'<text x="{left - 60}" y="28" font-size="18" font-weight="650" fill="{theme["ink"]}">'
             'Height noise falls with working distance</text>'
             f'<text x="{left - 60}" y="48" font-size="13" fill="{theme["ink2"]}">'
             '1σ height noise in mm (log scale) at the measured 0.22 px centroid noise · tilted-camera layout · f ≈ 375 px</text>')
    xlabel = f'<text x="{left + pw / 2:.1f}" y="{height - 12}" text-anchor="middle" font-size="13" fill="{theme["ink2"]}">working distance Z (mm)</text>'
    svg = f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">{title}{"".join(parts)}{xlabel}</svg>'
    return svg, width, height


# --------------------------------------------------------------------------- principle
def principle(theme: dict) -> tuple[str, int, int]:
    width, height = 900, 450
    t = theme
    laser_x, top_y = 330, 70
    cam_x, cam_y = 520, 92
    # workpiece: a fillet standing on its corner forms a V
    v_left, v_mid, v_right = (210, 250), (330, 345), (450, 250)
    svg = f"""
<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arr" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path d="M0,0 L8,4 L0,8 z" fill="{t['ink2']}"/>
    </marker>
  </defs>
  <text x="40" y="34" font-size="18" font-weight="650" fill="{t['ink']}">How a laser line becomes a height profile</text>
  <g transform="translate(0 30)">
  <!-- laser fan -->
  <polygon points="{laser_x},{top_y + 22} {v_left[0] + 18},{v_left[1] + 8} {v_mid[0]},{v_mid[1]} {v_right[0] - 18},{v_right[1] + 8}" fill="{t['laser_soft']}"/>
  <line x1="{laser_x}" y1="{top_y + 22}" x2="{v_mid[0]}" y2="{v_mid[1]}" stroke="{t['laser']}" stroke-width="1.5" stroke-dasharray="4 4"/>
  <!-- part -->
  <polygon points="{v_left[0] - 30},{v_left[1] - 30} {v_mid[0]},{v_mid[1]} {v_right[0] + 30},{v_right[1] - 30} {v_right[0] + 30},{v_right[1] - 18} {v_mid[0]},{v_mid[1] + 18} {v_left[0] - 30},{v_left[1] - 18}" fill="{t['part']}" stroke="{t['part_edge']}" stroke-width="1.2"/>
  <polyline points="{v_left[0] + 18},{v_left[1] + 8 - 30 + 30} {v_mid[0]},{v_mid[1]} {v_right[0] - 18},{v_right[1] + 8}" fill="none" stroke="{t['laser']}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="{v_mid[0]}" y="{v_mid[1] + 46}" text-anchor="middle" font-size="13" fill="{t['ink2']}">fillet joint · stripe bends at the seam</text>
  <!-- laser -->
  <rect x="{laser_x - 18}" y="{top_y - 6}" width="36" height="28" rx="5" fill="{t['laser']}"/>
  <text x="{laser_x - 30}" y="{top_y + 12}" text-anchor="end" font-size="14" font-weight="600" fill="{t['ink']}">650 nm line laser</text>
  <text x="{laser_x - 30}" y="{top_y + 30}" text-anchor="end" font-size="12" fill="{t['ink2']}">GPIO14 · 2N2222 switch</text>
  <!-- camera -->
  <g transform="rotate(32 {cam_x} {cam_y})">
    <rect x="{cam_x - 26}" y="{cam_y - 16}" width="52" height="32" rx="6" fill="{t['accent']}"/>
    <rect x="{cam_x - 9}" y="{cam_y + 14}" width="18" height="10" rx="2" fill="{t['accent']}"/>
  </g>
  <line x1="{cam_x - 10}" y1="{cam_y + 22}" x2="{v_mid[0] + 6}" y2="{v_mid[1] - 6}" stroke="{t['accent']}" stroke-width="1.5" stroke-dasharray="4 4"/>
  <text x="{cam_x + 40}" y="{cam_y - 4}" font-size="14" font-weight="600" fill="{t['ink']}">ESP32-S3 + OV3660</text>
  <text x="{cam_x + 40}" y="{cam_y + 14}" font-size="12" fill="{t['ink2']}">sees the stripe from the side</text>
  <!-- baseline + standoff -->
  <line x1="{laser_x}" y1="{top_y - 26}" x2="{cam_x}" y2="{top_y - 26}" stroke="{t['ink2']}" stroke-width="1.2" marker-start="url(#arr)" marker-end="url(#arr)"/>
  <text x="{(laser_x + cam_x) / 2}" y="{top_y - 34}" text-anchor="middle" font-size="13" fill="{t['ink2']}">baseline B</text>
  <line x1="{laser_x - 225}" y1="{top_y + 10}" x2="{laser_x - 225}" y2="{v_mid[1]}" stroke="{t['ink2']}" stroke-width="1.2" marker-start="url(#arr)" marker-end="url(#arr)"/>
  <text x="{laser_x - 235}" y="{(top_y + v_mid[1]) / 2 + 10}" text-anchor="middle" font-size="13" fill="{t['ink2']}" transform="rotate(-90 {laser_x - 235} {(top_y + v_mid[1]) / 2 + 10})">standoff Z</text>
  <!-- what the camera sees -->
  <g transform="translate(650 150)">
    <rect x="0" y="0" width="200" height="150" rx="8" fill="{t['card']}" stroke="{t['axis']}"/>
    <text x="100" y="-12" text-anchor="middle" font-size="13" font-weight="600" fill="{t['ink']}">camera image</text>
    <polyline points="70,8 110,75 80,142" fill="none" stroke="{t['laser']}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="110" cy="75" r="6" fill="none" stroke="{t['ink']}" stroke-width="2"/>
    <line x1="110" y1="75" x2="160" y2="75" stroke="{t['ink2']}" stroke-width="1" stroke-dasharray="3 3"/>
    <text x="100" y="172" text-anchor="middle" font-size="12" fill="{t['ink2']}">row-wise sub-pixel centreline,</text>
    <text x="100" y="188" text-anchor="middle" font-size="12" fill="{t['ink2']}">two-leg fit finds the seam corner</text>
  </g>
  <line x1="{v_right[0] + 50}" y1="225" x2="636" y2="225" stroke="{t['ink2']}" stroke-width="1.2" marker-end="url(#arr)"/>
  </g>
</svg>"""
    return svg, width, height


# --------------------------------------------------------------------------- extraction panel
def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def panel(theme: dict) -> tuple[str, int, int]:
    rows = (
        ("011_fillet_corner", "Fillet corner", "corner recovered to 0.001 px"),
        ("005_ghost", "Ghost reflection", "secondary line flagged, not chosen"),
        ("004_occlusion_gap", "Occlusion gap", "missing rows reported, not invented"),
    )
    cols = ("Laser-ON frame", "Score (ON − OFF, red excess)", "Extracted centreline")
    cell_w, cell_h, gap, label_w = 256, 192, 10, 170
    width = label_w + 3 * cell_w + 2 * gap + 30
    height = 70 + 3 * cell_h + 2 * gap + 30
    html = [f'<div style="position:relative;width:{width}px;height:{height}px">']
    html.append(f'<div style="position:absolute;left:20px;top:16px;font-size:18px;font-weight:650">'
                f'Stripe extraction on the synthetic regression fixtures</div>')
    for c, name in enumerate(cols):
        x = label_w + c * (cell_w + gap)
        html.append(f'<div style="position:absolute;left:{x}px;top:48px;width:{cell_w}px;font-size:13px;'
                    f'color:{theme["ink2"]};text-align:center">{name}</div>')
    for r, (key, title, note) in enumerate(rows):
        y = 70 + r * (cell_h + gap)
        html.append(f'<div style="position:absolute;left:20px;top:{y + cell_h / 2 - 22}px;width:{label_w - 30}px">'
                    f'<div style="font-size:14px;font-weight:600">{title}</div>'
                    f'<div style="font-size:12px;color:{theme["ink2"]};margin-top:4px;line-height:1.35">{note}</div></div>')
        images = (
            SCRATCH / "syn" / f"synthetic_{key}" / "laser_on.png",
            SCRATCH / f"an_{key}" / "score.png",
            SCRATCH / f"an_{key}" / "overlay.png",
        )
        for c, image in enumerate(images):
            x = label_w + c * (cell_w + gap)
            html.append(f'<img src="{data_uri(image)}" style="position:absolute;left:{x}px;top:{y}px;width:{cell_w}px;'
                        f'height:{cell_h}px;border-radius:6px;image-rendering:auto">')
    html.append("</div>")
    return "".join(html), width, height


# --------------------------------------------------------------------------- social preview
def social(theme: dict) -> tuple[str, int, int]:
    width, height = 1280, 640
    chart_svg, _, _ = principle(theme)
    inner = chart_svg.replace('<svg width="900" height="450"', '<svg width="720" height="360"')
    body = f"""
<div style="position:relative;width:{width}px;height:{height}px;overflow:hidden">
  <div style="position:absolute;left:72px;top:92px;width:470px">
    <div style="font-size:20px;color:{theme['laser']};font-weight:600;letter-spacing:.04em">OPEN-SOURCE PROTOTYPE</div>
    <div style="font-size:58px;font-weight:750;line-height:1.05;margin-top:18px">ESP32 Laser<br>Triangulation</div>
    <div style="font-size:24px;color:{theme['ink2']};line-height:1.4;margin-top:24px">Laser-line profiling with a
      low-cost ESP32-S3 camera board. Firmware, host tools and a tested C++ stripe extractor.</div>
    <div style="font-size:20px;margin-top:40px;color:{theme['ink']}"><b>0.22 px</b> stripe repeatability</div>
  </div>
  <div style="position:absolute;left:520px;top:150px">{inner}</div>
</div>"""
    return body, width, height


def main() -> None:
    jobs = {
        "height-noise": chart,
        "measurement-principle": principle,
        "extraction-fixtures": panel,
    }
    html_dir = SCRATCH / "html"
    html_dir.mkdir(exist_ok=True)
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
    (SCRATCH / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
