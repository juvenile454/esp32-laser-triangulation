#!/usr/bin/env python3
"""Dependency-light reference pipeline for laser stripe extraction.

The algorithm mirrors lib/laser_core closely enough for host-side parity tests.
All positions remain in pixels until a measured calibration is supplied.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PIL import Image, ImageDraw

# v4: the continuity prediction only follows tracked selections, and
# non-finite float configuration is rejected. Mirrors laser::kContractVersion.
CONTRACT_VERSION = 4
# Red channel level from which a stripe pixel counts as clipped
# (laser_core.cpp kSaturationLevel).
SATURATION_LEVEL = 250


@dataclass(frozen=True)
class ExtractorConfig:
    # Half-open ROI [roi_x0, roi_x1) x [roi_y0, roi_y1); 0 for an upper bound
    # means full frame width/height. Bounds are clamped to the frame.
    roi_x0: int = 0
    roi_y0: int = 0
    roi_x1: int = 0
    roi_y1: int = 0
    score_threshold: int = 20
    max_stripe_width_px: int = 16
    max_clipped_core_gap_px: int = 12
    clipped_core_min_luma: int = 145
    clipped_core_min_delta: int = 25
    clipped_core_max_chroma: int = 80
    ghost_ratio: float = 0.45
    minimum_valid_ratio: float = 0.10
    # Continuity tracking; 0 restores pure strongest-cluster selection. The
    # prediction only follows tracked selections (see laser_core.h).
    prediction_window_px: float = 0.0
    # Seed for the first scanned row; negative means "no seed".
    initial_prediction_px: float = -1.0


@dataclass
class ProfilePoint:
    x_px: float
    y_px: int
    peak_score: int
    width_px: int
    valid: bool
    saturated: bool
    secondary_candidate: bool
    too_wide: bool = False


@dataclass
class ExtractionSummary:
    contract_version: int
    profile_points: int
    valid_points: int
    missing_points: int
    saturated_points: int
    secondary_candidate_points: int
    too_wide_points: int
    valid_ratio: float
    mean_peak_score: float
    confidence: float
    valid: bool
    invalid_reason: str


@dataclass
class SeamFitConfig:
    minimum_leg_points: int = 8
    minimum_leg_fraction: float = 0.15
    maximum_rms_px: float = 3.0
    minimum_corner_deviation_deg: float = 5.0


@dataclass
class SeamCorner:
    valid: bool
    x_px: float
    y_px: float
    included_angle_deg: float
    rms_px: float
    first_leg_slope: float
    second_leg_slope: float
    first_leg_points: int
    second_leg_points: int


@dataclass
class Cluster:
    """One run of above-threshold (or bridged) pixels on a scan row."""

    first_x: int
    last_x: int
    weight: int
    weighted_x: int
    peak: int
    saturated: bool = False

    @property
    def width(self) -> int:
        return self.last_x - self.first_x + 1

    @property
    def center(self) -> float:
        return self.weighted_x / self.weight if self.weight else 0.0


@dataclass
class _Selection:
    primary: Cluster | None = None
    secondary: Cluster | None = None
    rejected: Cluster | None = None  # Strongest candidate failing the width test.
    primary_tracked: bool = False
    secondary_tracked: bool = False


def load_rgb(path: Path) -> Image.Image:
    with Image.open(path) as source:
        source.load()
        return source.convert("RGB")


def _trunc_div(numerator: int, denominator: int) -> int:
    """C++ integer division, which truncates toward zero."""
    quotient = abs(numerator) // denominator
    return quotient if numerator >= 0 else -quotient


def pixel_score(on: Sequence[int], off: Sequence[int] | None) -> int:
    red_excess = max(0, on[0] - max(on[1], on[2]))
    red_difference = 0
    if off is not None:
        dr = on[0] - off[0]
        dg = on[1] - off[1]
        db = on[2] - off[2]
        red_difference = max(0, dr - _trunc_div(dg + db, 2))
    return min(255, max(red_excess, red_difference))


def clipped_core_bridge_score(on: Sequence[int], off: Sequence[int] | None, config: ExtractorConfig) -> int:
    """Mirror of clippedCoreBridgeScore: white/warm clipped core inside a stripe."""
    if off is None or config.max_clipped_core_gap_px == 0:
        return 0
    on_sum = on[0] + on[1] + on[2]
    off_sum = off[0] + off[1] + off[2]
    on_luma = on_sum // 3
    delta_luma = _trunc_div(on_sum - off_sum, 3)
    chroma = max(on[0], on[1], on[2]) - min(on[0], on[1], on[2])
    if (
        on_luma < config.clipped_core_min_luma
        or delta_luma < config.clipped_core_min_delta
        or chroma > config.clipped_core_max_chroma
    ):
        return 0
    return min(255, max(config.score_threshold, delta_luma))


def _cluster_candidates(
    scores: list[int],
    threshold: int,
    x0: int,
    bridge_scores: list[int] | None = None,
    max_bridge_gap: int = 0,
) -> list[Cluster]:
    clusters: list[Cluster] = []
    start: int | None = None
    last_x: int | None = None
    weight = weighted_x = peak = 0
    pending_bridge: list[tuple[int, int]] = []
    for offset in range(len(scores) + 1):
        score = scores[offset] if offset < len(scores) else 0
        if score >= threshold:
            x = x0 + offset
            if start is None:
                start = x
            elif pending_bridge:
                for bridge_x, bridge_score in pending_bridge:
                    weight += bridge_score
                    weighted_x += bridge_x * bridge_score
                    peak = max(peak, bridge_score)
                pending_bridge.clear()
            last_x = x
            weight += score
            weighted_x += x * score
            peak = max(peak, score)
        elif (
            start is not None
            and offset < len(scores)
            and bridge_scores is not None
            and bridge_scores[offset] > 0
            and len(pending_bridge) < max_bridge_gap
        ):
            pending_bridge.append((x0 + offset, bridge_scores[offset]))
        elif start is not None:
            if last_x is None:
                raise AssertionError("cluster has no confirmed last_x")
            # saturated is filled in by the caller, which has the pixels
            # needed to test the cluster's own span.
            clusters.append(Cluster(start, last_x, weight, weighted_x, peak))
            start = None
            last_x = None
            weight = weighted_x = peak = 0
            pending_bridge.clear()
    return clusters


def _outranks(candidate_tracked: bool, candidate_weight: int, incumbent_tracked: bool, incumbent_weight: int) -> bool:
    # Continuity beats strength: a candidate inside the prediction window
    # always outranks one outside it, weight only separates equals.
    if candidate_tracked != incumbent_tracked:
        return candidate_tracked
    return candidate_weight > incumbent_weight


def _select_candidates(clusters: Iterable[Cluster], config: ExtractorConfig, prediction: float | None) -> _Selection:
    """Mirror of considerCluster, applied left to right over one row."""
    selection = _Selection()
    for candidate in clusters:
        # Documented contract order: reject over-wide clusters *before*
        # ranking, so a broad blob can never displace a genuine narrow stripe.
        if candidate.width > config.max_stripe_width_px:
            if selection.rejected is None or candidate.weight > selection.rejected.weight:
                selection.rejected = candidate
            continue
        tracked = (
            prediction is not None
            and config.prediction_window_px > 0.0
            and abs(candidate.center - prediction) <= config.prediction_window_px
        )
        if selection.primary is None or _outranks(
            tracked, candidate.weight, selection.primary_tracked, selection.primary.weight
        ):
            selection.secondary = selection.primary
            selection.secondary_tracked = selection.primary_tracked
            selection.primary = candidate
            selection.primary_tracked = tracked
        elif selection.secondary is None or _outranks(
            tracked, candidate.weight, selection.secondary_tracked, selection.secondary.weight
        ):
            selection.secondary = candidate
            selection.secondary_tracked = tracked
    return selection


def clamp_roi(config: ExtractorConfig, width: int, height: int) -> tuple[int, int, int, int]:
    """The ROI the extractor actually scans: (x0, y0, x1, y1), upper bounds exclusive."""
    x0 = min(max(config.roi_x0, 0), width)
    y0 = min(max(config.roi_y0, 0), height)
    x1 = width if config.roi_x1 == 0 else min(max(config.roi_x1, 0), width)
    y1 = height if config.roi_y1 == 0 else min(max(config.roi_y1, 0), height)
    return x0, y0, x1, y1


def _validate_config(config: ExtractorConfig, roi: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = roi
    if not (x0 < x1 and y0 < y1):
        raise ValueError("empty ROI")
    if not (1 <= config.score_threshold <= 255):
        raise ValueError("score_threshold must be 1..255")
    if config.max_stripe_width_px < 1:
        raise ValueError("max_stripe_width_px must be positive")
    for name, value in (
        ("max_clipped_core_gap_px", config.max_clipped_core_gap_px),
        ("clipped_core_min_luma", config.clipped_core_min_luma),
        ("clipped_core_min_delta", config.clipped_core_min_delta),
        ("clipped_core_max_chroma", config.clipped_core_max_chroma),
    ):
        if not 0 <= value <= 255:
            raise ValueError(f"{name} must be 0..255")
    # Explicit isfinite: NaN fails every ordered comparison.
    if not (math.isfinite(config.ghost_ratio) and 0.0 <= config.ghost_ratio <= 1.0):
        raise ValueError("ghost_ratio must be finite and 0..1")
    if not (math.isfinite(config.minimum_valid_ratio) and 0.0 <= config.minimum_valid_ratio <= 1.0):
        raise ValueError("minimum_valid_ratio must be finite and 0..1")
    if not (math.isfinite(config.prediction_window_px) and config.prediction_window_px >= 0.0):
        raise ValueError("prediction_window_px must be finite and not negative")
    if not math.isfinite(config.initial_prediction_px):
        raise ValueError("initial_prediction_px must be finite")


def _profile_point(selection: _Selection, y: int, ghost_ratio: float) -> ProfilePoint:
    primary = selection.primary
    if primary is None:
        rejected = selection.rejected
        if rejected is None:
            return ProfilePoint(0.0, y, 0, 0, False, False, False, False)
        # Nothing survived the width filter: report the strongest rejected
        # candidate so a glare field is distinguishable from an unlit scene.
        return ProfilePoint(
            x_px=float(rejected.center),
            y_px=y,
            peak_score=rejected.peak,
            width_px=min(255, rejected.width),
            valid=False,
            saturated=rejected.saturated,
            secondary_candidate=False,
            too_wide=True,
        )
    secondary = selection.secondary is not None and selection.secondary.weight >= primary.weight * ghost_ratio
    return ProfilePoint(
        x_px=float(primary.center),
        y_px=y,
        peak_score=primary.peak,
        width_px=min(255, primary.width),
        valid=True,
        saturated=primary.saturated,
        secondary_candidate=secondary,
        too_wide=False,
    )


def _summarize(profile: list[ProfilePoint], config: ExtractorConfig) -> ExtractionSummary:
    valid = [point for point in profile if point.valid]
    valid_points = len(valid)
    ratio = valid_points / len(profile)
    mean_peak = sum(point.peak_score for point in valid) / valid_points if valid else 0.0
    saturation_count = sum(point.saturated for point in valid)
    secondary_count = sum(point.secondary_candidate for point in valid)
    too_wide_count = sum(point.too_wide for point in profile)
    signal_quality = min(1.0, max(0.0, mean_peak / (config.score_threshold * 2.0)))
    saturation_penalty = 1.0 - 0.25 * saturation_count / valid_points if valid else 1.0
    secondary_penalty = 1.0 - 0.30 * secondary_count / valid_points if valid else 1.0
    confidence = min(1.0, max(0.0, ratio * signal_quality * saturation_penalty * secondary_penalty))
    if not valid:
        reason = "all_clusters_too_wide" if too_wide_count else "no_stripe"
    elif ratio < config.minimum_valid_ratio:
        reason = "insufficient_coverage"
    else:
        reason = "none"
    return ExtractionSummary(
        contract_version=CONTRACT_VERSION,
        profile_points=len(profile),
        valid_points=valid_points,
        missing_points=len(profile) - valid_points,
        saturated_points=saturation_count,
        secondary_candidate_points=secondary_count,
        too_wide_points=too_wide_count,
        valid_ratio=ratio,
        mean_peak_score=mean_peak,
        confidence=confidence,
        valid=reason == "none",
        invalid_reason=reason,
    )


def extract_stripe(
    laser_on: Image.Image,
    laser_off: Image.Image | None,
    config: ExtractorConfig,
) -> tuple[list[ProfilePoint], ExtractionSummary, Image.Image]:
    on = laser_on.convert("RGB")
    off = laser_off.convert("RGB") if laser_off is not None else None
    if off is not None and off.size != on.size:
        raise ValueError(f"dimension mismatch: ON={on.size}, OFF={off.size}")
    width, height = on.size
    roi = clamp_roi(config, width, height)
    _validate_config(config, roi)
    x0, y0, x1, y1 = roi

    # Packed RGB888 rows, the same layout the C++ core reads.
    on_bytes = on.tobytes()
    off_bytes = off.tobytes() if off is not None else None
    score_bytes = bytearray(width * height)
    profile: list[ProfilePoint] = []
    tracking_enabled = config.prediction_window_px > 0.0
    prediction: float | None = config.initial_prediction_px if config.initial_prediction_px >= 0.0 else None

    for y in range(y0, y1):
        row = y * width * 3
        scores: list[int] = []
        bridge_scores: list[int] = []
        for x in range(x0, x1):
            index = row + x * 3
            on_pixel = on_bytes[index:index + 3]
            off_pixel = off_bytes[index:index + 3] if off_bytes is not None else None
            score = pixel_score(on_pixel, off_pixel)
            scores.append(score)
            bridge_scores.append(clipped_core_bridge_score(on_pixel, off_pixel, config))
            score_bytes[y * width + x] = score
        clusters = _cluster_candidates(scores, config.score_threshold, x0, bridge_scores, config.max_clipped_core_gap_px)
        # Saturation belongs to a cluster and is checked from its span.
        for cluster in clusters:
            cluster.saturated = any(
                on_bytes[row + x * 3] >= SATURATION_LEVEL for x in range(cluster.first_x, cluster.last_x + 1)
            )
        selection = _select_candidates(clusters, config, prediction)
        point = _profile_point(selection, y, config.ghost_ratio)
        # Only a tracked selection moves the prediction; an untracked pick
        # while a prediction exists (stripe missing, foreign blob chosen by
        # strength) must not re-centre the window on the blob.
        if point.valid and (selection.primary_tracked or prediction is None or not tracking_enabled):
            prediction = point.x_px
        profile.append(point)

    score_image = Image.frombytes("L", (width, height), bytes(score_bytes))
    return profile, _summarize(profile, config), score_image


def fit_seam_corner(
    profile: list[ProfilePoint],
    config: SeamFitConfig | None = None,
) -> SeamCorner | None:
    """Mirror of laser::fitSeamCorner. Two straight legs, best split, intersect."""
    config = config or SeamFitConfig()
    # Leg counts are uint16 in C++; longer profiles are rejected there too.
    if not profile or len(profile) > 65535 or config.minimum_leg_points < 2:
        return None
    # Non-finite positions are skipped: one NaN would poison every sum.
    points = [(float(point.y_px), float(point.x_px)) for point in profile if point.valid and math.isfinite(point.x_px)]
    fraction = max(0.0, min(0.5, config.minimum_leg_fraction))
    minimum_leg = max(config.minimum_leg_points, math.ceil(len(points) * fraction))
    if len(points) < 2 * minimum_leg:
        return None

    def sums(items: Iterable[tuple[float, float]]) -> list[float]:
        acc = [0.0] * 6
        for y, x in items:
            acc[0] += 1.0
            acc[1] += y
            acc[2] += x
            acc[3] += y * y
            acc[4] += y * x
            acc[5] += x * x
        return acc

    def fit(acc: list[float]) -> tuple[float, float, float] | None:
        n, sy, sx, syy, sxy, sxx = acc
        denominator = n * syy - sy * sy
        if n < 2.0 or abs(denominator) < 1e-9:
            return None
        slope = (n * sxy - sy * sx) / denominator
        intercept = (sx - slope * sy) / n
        sse = max(0.0, sxx - intercept * sx - slope * sxy)
        return slope, intercept, sse

    total = sums(points)
    left = [0.0] * 6
    best: tuple[float, tuple[float, float, float], tuple[float, float, float], int, int] | None = None
    for y, x in points:
        left[0] += 1.0
        left[1] += y
        left[2] += x
        left[3] += y * y
        left[4] += y * x
        left[5] += x * x
        right = [t - l for t, l in zip(total, left, strict=True)]
        if left[0] < minimum_leg or right[0] < minimum_leg:
            continue
        first = fit(left)
        second = fit(right)
        if first is None or second is None:
            continue
        sse = first[2] + second[2]
        if best is None or sse < best[0]:
            best = (sse, first, second, int(left[0]), int(right[0]))
    if best is None:
        return None

    sse, first, second, first_count, second_count = best
    deviation_deg = abs(math.atan(second[0]) - math.atan(first[0])) * 180.0 / math.pi
    rms = math.sqrt(sse / total[0])
    slope_difference = first[0] - second[0]
    corner = SeamCorner(
        valid=False,
        x_px=0.0,
        y_px=0.0,
        included_angle_deg=180.0 - deviation_deg,
        rms_px=rms,
        first_leg_slope=first[0],
        second_leg_slope=second[0],
        first_leg_points=first_count,
        second_leg_points=second_count,
    )
    if abs(slope_difference) < 1e-9:
        return corner
    corner.y_px = (second[1] - first[1]) / slope_difference
    corner.x_px = first[1] + first[0] * corner.y_px
    corner.valid = (
        deviation_deg >= config.minimum_corner_deviation_deg
        and rms <= config.maximum_rms_px
        and points[0][0] <= corner.y_px <= points[-1][0]
    )
    return corner


PROFILE_CSV_FIELDS = tuple(field.name for field in fields(ProfilePoint))


def save_profile_csv(path: Path, profile: Iterable[ProfilePoint]) -> None:
    """Same columns and boolean encoding (1/0) as host/laser_cli."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PROFILE_CSV_FIELDS))
        writer.writeheader()
        for point in profile:
            writer.writerow({key: int(value) if isinstance(value, bool) else value for key, value in asdict(point).items()})


def save_diagnostics(
    output_dir: Path,
    on: Image.Image,
    profile: list[ProfilePoint],
    summary: ExtractionSummary,
    score_image: Image.Image,
    config: ExtractorConfig,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    overlay = on.convert("RGB")
    draw = ImageDraw.Draw(overlay)
    # The ROI the extractor actually scanned, not the requested one.
    x0, y0, x1, y1 = clamp_roi(config, overlay.width, overlay.height)
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(0, 255, 255), width=1)
    for point in profile:
        if point.valid:
            color = (255, 255, 0) if point.secondary_candidate else (0, 255, 0)
            if point.saturated:
                color = (255, 128, 0)
            x = int(round(point.x_px))
            draw.point((x, point.y_px), fill=color)
        else:
            draw.point((x0, point.y_px), fill=(255, 0, 255))
    overlay.save(output_dir / "overlay.png")
    score_image.save(output_dir / "score.png")
    save_profile_csv(output_dir / "profile.csv", profile)
    (output_dir / "result.json").write_text(
        json.dumps(
            {"config": asdict(config), "summary": asdict(summary)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
