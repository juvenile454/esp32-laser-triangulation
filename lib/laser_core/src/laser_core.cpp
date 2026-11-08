#include "laser_core.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace laser {
namespace {

// Red channel level from which a stripe pixel counts as clipped.
constexpr std::uint8_t kSaturationLevel = 250U;
constexpr double kPi = 3.14159265358979323846;

struct Cluster {
  std::uint64_t weight = 0U;
  std::uint64_t weighted_x = 0U;
  std::uint16_t first_x = 0U;
  std::uint16_t last_x = 0U;
  std::uint8_t peak = 0U;
  bool saturated = false;
  bool present = false;

  std::uint16_t width() const {
    return static_cast<std::uint16_t>(last_x - first_x + 1U);
  }

  float center() const {
    return weight == 0U ? 0.0F
                        : static_cast<float>(weighted_x) /
                              static_cast<float>(weight);
  }
};

// Result of ranking every cluster on one scan row.
struct Selection {
  Cluster primary{};
  Cluster secondary{};
  Cluster rejected{};  // Strongest candidate that failed the width test.
  bool primary_tracked = false;
  bool secondary_tracked = false;
};

std::uint8_t pixelScore(const std::uint8_t* on, const std::uint8_t* off) {
  const int red_excess =
      std::max(0, static_cast<int>(on[0]) -
                      std::max(static_cast<int>(on[1]), static_cast<int>(on[2])));
  int red_difference = 0;
  if (off != nullptr) {
    const int dr = static_cast<int>(on[0]) - static_cast<int>(off[0]);
    const int dg = static_cast<int>(on[1]) - static_cast<int>(off[1]);
    const int db = static_cast<int>(on[2]) - static_cast<int>(off[2]);
    red_difference = std::max(0, dr - ((dg + db) / 2));
  }
  return static_cast<std::uint8_t>(
      std::min(255, std::max(red_excess, red_difference)));
}

std::uint8_t clippedCoreBridgeScore(const std::uint8_t* on,
                                    const std::uint8_t* off,
                                    const ExtractorConfig& config) {
  if (off == nullptr || config.max_clipped_core_gap_px == 0U) {
    return 0U;
  }
  const int on_sum = static_cast<int>(on[0]) + static_cast<int>(on[1]) +
                     static_cast<int>(on[2]);
  const int off_sum = static_cast<int>(off[0]) + static_cast<int>(off[1]) +
                      static_cast<int>(off[2]);
  const int on_luma = on_sum / 3;
  const int delta_luma = (on_sum - off_sum) / 3;
  const int maximum = std::max({static_cast<int>(on[0]),
                                static_cast<int>(on[1]),
                                static_cast<int>(on[2])});
  const int minimum = std::min({static_cast<int>(on[0]),
                                static_cast<int>(on[1]),
                                static_cast<int>(on[2])});
  if (on_luma < config.clipped_core_min_luma ||
      delta_luma < config.clipped_core_min_delta ||
      maximum - minimum > config.clipped_core_max_chroma) {
    return 0U;
  }
  return static_cast<std::uint8_t>(std::min(
      255, std::max(static_cast<int>(config.score_threshold), delta_luma)));
}

// Continuity beats strength: a candidate near the running prediction always
// outranks one outside the window, and weight only separates equals. This is
// what stops a bright specular blob from stealing the centreline whenever it
// happens to outshine the stripe on a single row. The prediction itself only
// follows tracked selections (see extractStripeRgb888), so a blob picked on a
// row where the stripe is missing cannot drag the window onto itself.
bool outranks(bool candidate_tracked, std::uint64_t candidate_weight,
              bool incumbent_tracked, std::uint64_t incumbent_weight) {
  if (candidate_tracked != incumbent_tracked) {
    return candidate_tracked;
  }
  return candidate_weight > incumbent_weight;
}

void considerCluster(const Cluster& candidate, const ExtractorConfig& config,
                     bool prediction_valid, float prediction,
                     Selection& selection) {
  if (!candidate.present) {
    return;
  }
  // Documented contract order: reject over-wide clusters *before* ranking, so a
  // broad blob can never displace a genuine narrow stripe that is also present.
  if (candidate.width() > config.max_stripe_width_px) {
    if (!selection.rejected.present ||
        candidate.weight > selection.rejected.weight) {
      selection.rejected = candidate;
    }
    return;
  }

  const bool tracked =
      prediction_valid && config.prediction_window_px > 0.0F &&
      std::fabs(candidate.center() - prediction) <= config.prediction_window_px;

  if (!selection.primary.present ||
      outranks(tracked, candidate.weight, selection.primary_tracked,
               selection.primary.weight)) {
    selection.secondary = selection.primary;
    selection.secondary_tracked = selection.primary_tracked;
    selection.primary = candidate;
    selection.primary_tracked = tracked;
  } else if (!selection.secondary.present ||
             outranks(tracked, candidate.weight, selection.secondary_tracked,
                      selection.secondary.weight)) {
    selection.secondary = candidate;
    selection.secondary_tracked = tracked;
  }
}

float clamp01(float value) {
  return std::max(0.0F, std::min(1.0F, value));
}

// Least-squares fit of x = intercept + slope * y over accumulated sums.
struct LineFit {
  double slope = 0.0;
  double intercept = 0.0;
  double sse = 0.0;
  bool valid = false;
};

struct Sums {
  double n = 0.0;
  double y = 0.0;
  double x = 0.0;
  double yy = 0.0;
  double xy = 0.0;
  double xx = 0.0;

  void add(double y_value, double x_value) {
    n += 1.0;
    y += y_value;
    x += x_value;
    yy += y_value * y_value;
    xy += y_value * x_value;
    xx += x_value * x_value;
  }

  // Written field by field rather than as an aggregate: the ESP32 toolchain
  // compiles this translation unit under an older standard in which a struct
  // with default member initialisers is not an aggregate.
  Sums minus(const Sums& other) const {
    Sums result;
    result.n = n - other.n;
    result.y = y - other.y;
    result.x = x - other.x;
    result.yy = yy - other.yy;
    result.xy = xy - other.xy;
    result.xx = xx - other.xx;
    return result;
  }
};

// A fit point must be a valid measurement with a finite position; a single
// NaN would otherwise poison every accumulated sum.
bool usableForFit(const ProfilePoint& point) {
  return point.valid && std::isfinite(point.x_px);
}

LineFit fitLine(const Sums& sums) {
  LineFit fit;
  const double denominator = sums.n * sums.yy - sums.y * sums.y;
  if (sums.n < 2.0 || std::fabs(denominator) < 1e-9) {
    return fit;
  }
  fit.slope = (sums.n * sums.xy - sums.y * sums.x) / denominator;
  fit.intercept = (sums.x - fit.slope * sums.y) / sums.n;
  // Normal-equation shortcut for the residual sum of squares.
  fit.sse = std::max(0.0, sums.xx - fit.intercept * sums.x - fit.slope * sums.xy);
  fit.valid = true;
  return fit;
}

}  // namespace

bool extractStripeRgb888(const FrameViewRgb888& laser_on,
                         const FrameViewRgb888* laser_off,
                         const ExtractorConfig& config,
                         ProfilePoint* output,
                         std::size_t output_capacity,
                         MeasurementSummary* summary) {
  if (summary == nullptr) {
    return false;
  }
  *summary = MeasurementSummary{};
  if (laser_on.data == nullptr || output == nullptr || laser_on.width == 0U ||
      laser_on.height == 0U ||
      laser_on.stride_bytes < static_cast<std::uint32_t>(laser_on.width) * 3U) {
    summary->invalid_reason = InvalidReason::kBadArgument;
    return false;
  }
  if (laser_off != nullptr &&
      (laser_off->data == nullptr || laser_off->width != laser_on.width ||
       laser_off->height != laser_on.height ||
       laser_off->stride_bytes <
           static_cast<std::uint32_t>(laser_off->width) * 3U)) {
    summary->invalid_reason = InvalidReason::kDimensionMismatch;
    return false;
  }

  const std::uint16_t x0 = std::min(config.roi_x0, laser_on.width);
  const std::uint16_t y0 = std::min(config.roi_y0, laser_on.height);
  const std::uint16_t x1 = config.roi_x1 == 0U
                               ? laser_on.width
                               : std::min(config.roi_x1, laser_on.width);
  const std::uint16_t y1 = config.roi_y1 == 0U
                               ? laser_on.height
                               : std::min(config.roi_y1, laser_on.height);
  // Written as positive range tests combined with std::isfinite so that NaN,
  // for which every ordered comparison is false, cannot slip through.
  const bool ratios_valid =
      std::isfinite(config.ghost_ratio) && config.ghost_ratio >= 0.0F &&
      config.ghost_ratio <= 1.0F && std::isfinite(config.minimum_valid_ratio) &&
      config.minimum_valid_ratio >= 0.0F && config.minimum_valid_ratio <= 1.0F;
  const bool tracking_valid = std::isfinite(config.prediction_window_px) &&
                              config.prediction_window_px >= 0.0F &&
                              std::isfinite(config.initial_prediction_px);
  if (x0 >= x1 || y0 >= y1 || config.score_threshold == 0U ||
      config.max_stripe_width_px == 0U || !ratios_valid || !tracking_valid) {
    summary->invalid_reason = InvalidReason::kBadArgument;
    return false;
  }

  const std::size_t required = static_cast<std::size_t>(y1 - y0);
  if (output_capacity < required) {
    summary->invalid_reason = InvalidReason::kOutputTooSmall;
    return false;
  }

  const bool tracking_enabled = config.prediction_window_px > 0.0F;
  bool prediction_valid = config.initial_prediction_px >= 0.0F;
  float prediction = prediction_valid ? config.initial_prediction_px : 0.0F;

  std::uint32_t peak_sum = 0U;
  for (std::uint16_t y = y0; y < y1; ++y) {
    ProfilePoint& point = output[y - y0];
    point = ProfilePoint{};
    point.y_px = y;
    Selection selection{};
    Cluster current{};
    Cluster pending_bridge{};
    std::uint8_t pending_bridge_length = 0U;

    // std::size_t arithmetic: y * stride can exceed 32 bits for large strides.
    const std::uint8_t* on_row =
        laser_on.data +
        static_cast<std::size_t>(y) * static_cast<std::size_t>(laser_on.stride_bytes);
    const std::uint8_t* off_row =
        laser_off == nullptr
            ? nullptr
            : laser_off->data + static_cast<std::size_t>(y) *
                                    static_cast<std::size_t>(laser_off->stride_bytes);

    for (std::uint32_t scan_x = x0; scan_x <= x1; ++scan_x) {
      const bool at_end = scan_x == x1;
      const auto x = static_cast<std::uint16_t>(scan_x);
      std::uint8_t score = 0U;
      std::uint8_t bridge_score = 0U;
      const std::uint8_t* on_pixel = nullptr;
      if (!at_end) {
        on_pixel = on_row + static_cast<std::size_t>(x) * 3U;
        const std::uint8_t* off_pixel =
            off_row == nullptr ? nullptr
                               : off_row + static_cast<std::size_t>(x) * 3U;
        score = pixelScore(on_pixel, off_pixel);
        bridge_score = clippedCoreBridgeScore(on_pixel, off_pixel, config);
      }

      if (!at_end && score >= config.score_threshold) {
        if (!current.present) {
          current.present = true;
          current.first_x = x;
        } else if (pending_bridge.present) {
          current.last_x = pending_bridge.last_x;
          current.weight += pending_bridge.weight;
          current.weighted_x += pending_bridge.weighted_x;
          current.peak = std::max(current.peak, pending_bridge.peak);
          current.saturated = current.saturated || pending_bridge.saturated;
          pending_bridge = Cluster{};
          pending_bridge_length = 0U;
        }
        current.last_x = x;
        current.weight += score;
        current.weighted_x += static_cast<std::uint64_t>(x) * score;
        current.peak = std::max(current.peak, score);
        current.saturated = current.saturated || on_pixel[0] >= kSaturationLevel;
      } else if (!at_end && current.present && bridge_score > 0U &&
                 pending_bridge_length < config.max_clipped_core_gap_px) {
        if (!pending_bridge.present) {
          pending_bridge.present = true;
          pending_bridge.first_x = x;
        }
        pending_bridge.last_x = x;
        pending_bridge.weight += bridge_score;
        pending_bridge.weighted_x +=
            static_cast<std::uint64_t>(x) * bridge_score;
        pending_bridge.peak = std::max(pending_bridge.peak, bridge_score);
        pending_bridge.saturated =
            pending_bridge.saturated || on_pixel[0] >= kSaturationLevel;
        ++pending_bridge_length;
      } else if (current.present) {
        considerCluster(current, config, prediction_valid, prediction,
                        selection);
        current = Cluster{};
        pending_bridge = Cluster{};
        pending_bridge_length = 0U;
      }
    }

    if (!selection.primary.present) {
      // Nothing survived the width filter. Report the strongest rejected
      // candidate so the operator can tell an unlit scene from a glare field.
      if (selection.rejected.present) {
        point.x_px = selection.rejected.center();
        point.peak_score = selection.rejected.peak;
        point.width_px = static_cast<std::uint8_t>(
            std::min<std::uint16_t>(255U, selection.rejected.width()));
        point.saturated = selection.rejected.saturated;
        point.too_wide = true;
        ++summary->too_wide_points;
      }
      continue;
    }

    point.x_px = selection.primary.center();
    point.peak_score = selection.primary.peak;
    point.width_px = static_cast<std::uint8_t>(
        std::min<std::uint16_t>(255U, selection.primary.width()));
    point.saturated = selection.primary.saturated;
    point.secondary_candidate =
        selection.secondary.present &&
        static_cast<float>(selection.secondary.weight) >=
            static_cast<float>(selection.primary.weight) * config.ghost_ratio;
    point.valid = true;

    // Only a tracked selection moves the prediction. An untracked pick made
    // while a prediction exists (stripe missing on this row, foreign blob
    // selected by strength) is still reported, but must not re-centre the
    // window on the blob. Without a prediction, or with tracking disabled,
    // every selection seeds or follows as before.
    if (selection.primary_tracked || !prediction_valid || !tracking_enabled) {
      prediction = point.x_px;
      prediction_valid = true;
    }

    ++summary->valid_points;
    peak_sum += point.peak_score;
    if (point.saturated) {
      ++summary->saturated_points;
    }
    if (point.secondary_candidate) {
      ++summary->secondary_candidate_points;
    }
  }

  summary->profile_points = static_cast<std::uint16_t>(required);
  summary->missing_points =
      static_cast<std::uint16_t>(summary->profile_points - summary->valid_points);
  summary->valid_ratio =
      static_cast<float>(summary->valid_points) / summary->profile_points;
  summary->mean_peak_score =
      summary->valid_points == 0U
          ? 0.0F
          : static_cast<float>(peak_sum) / summary->valid_points;
  const float signal_quality =
      clamp01(summary->mean_peak_score /
              (static_cast<float>(config.score_threshold) * 2.0F));
  const float saturation_penalty =
      summary->valid_points == 0U
          ? 1.0F
          : 1.0F - 0.25F * static_cast<float>(summary->saturated_points) /
                       summary->valid_points;
  const float secondary_penalty =
      summary->valid_points == 0U
          ? 1.0F
          : 1.0F - 0.30F *
                       static_cast<float>(summary->secondary_candidate_points) /
                       summary->valid_points;
  summary->confidence = clamp01(summary->valid_ratio * signal_quality *
                                saturation_penalty * secondary_penalty);

  if (summary->valid_points == 0U) {
    summary->invalid_reason = summary->too_wide_points > 0U
                                  ? InvalidReason::kAllClustersTooWide
                                  : InvalidReason::kNoStripe;
  } else if (summary->valid_ratio < config.minimum_valid_ratio) {
    summary->invalid_reason = InvalidReason::kInsufficientCoverage;
  } else {
    summary->valid = true;
    summary->invalid_reason = InvalidReason::kNone;
  }
  return true;
}

const char* invalidReasonName(InvalidReason reason) {
  switch (reason) {
    case InvalidReason::kNone:
      return "none";
    case InvalidReason::kBadArgument:
      return "bad_argument";
    case InvalidReason::kDimensionMismatch:
      return "dimension_mismatch";
    case InvalidReason::kOutputTooSmall:
      return "output_too_small";
    case InvalidReason::kNoStripe:
      return "no_stripe";
    case InvalidReason::kInsufficientCoverage:
      return "insufficient_coverage";
    case InvalidReason::kAllClustersTooWide:
      return "all_clusters_too_wide";
  }
  return "unknown";
}

bool fitSeamCorner(const ProfilePoint* profile,
                   std::size_t count,
                   const SeamFitConfig& config,
                   SeamCorner* corner) {
  if (corner == nullptr) {
    return false;
  }
  *corner = SeamCorner{};
  // Leg counts are reported as std::uint16_t; longer profiles would truncate.
  if (profile == nullptr || count == 0U ||
      count > static_cast<std::size_t>(std::numeric_limits<std::uint16_t>::max()) ||
      config.minimum_leg_points < 2U) {
    return false;
  }

  Sums total{};
  float first_y = 0.0F;
  float last_y = 0.0F;
  bool seen = false;
  for (std::size_t index = 0; index < count; ++index) {
    if (!usableForFit(profile[index])) {
      continue;
    }
    const double y = static_cast<double>(profile[index].y_px);
    total.add(y, static_cast<double>(profile[index].x_px));
    last_y = static_cast<float>(y);
    if (!seen) {
      first_y = static_cast<float>(y);
      seen = true;
    }
  }
  const double fraction = static_cast<double>(
      std::max(0.0F, std::min(0.5F, config.minimum_leg_fraction)));
  const double minimum_leg =
      std::max(static_cast<double>(config.minimum_leg_points),
               std::ceil(total.n * static_cast<double>(fraction)));
  if (total.n < 2.0 * minimum_leg) {
    return false;
  }

  Sums left{};
  double best_sse = 0.0;
  bool found = false;
  LineFit best_first{};
  LineFit best_second{};
  for (std::size_t index = 0; index < count; ++index) {
    if (!usableForFit(profile[index])) {
      continue;
    }
    left.add(static_cast<double>(profile[index].y_px),
             static_cast<double>(profile[index].x_px));
    if (left.n < minimum_leg || total.n - left.n < minimum_leg) {
      continue;
    }
    const LineFit first = fitLine(left);
    const LineFit second = fitLine(total.minus(left));
    if (!first.valid || !second.valid) {
      continue;
    }
    const double sse = first.sse + second.sse;
    if (!found || sse < best_sse) {
      found = true;
      best_sse = sse;
      best_first = first;
      best_second = second;
      corner->first_leg_points = static_cast<std::uint16_t>(left.n);
      corner->second_leg_points =
          static_cast<std::uint16_t>(total.n - left.n);
    }
  }
  if (!found) {
    return false;
  }

  const double slope_difference = best_first.slope - best_second.slope;
  corner->first_leg_slope = static_cast<float>(best_first.slope);
  corner->second_leg_slope = static_cast<float>(best_second.slope);
  corner->rms_px = static_cast<float>(std::sqrt(best_sse / total.n));

  // Angle each leg makes with the stripe axis; both directions point along
  // increasing y, so the difference is the direction change at the corner.
  const double first_angle = std::atan(best_first.slope);
  const double second_angle = std::atan(best_second.slope);
  const double deviation_deg =
      std::fabs(second_angle - first_angle) * 180.0 / kPi;
  corner->included_angle_deg = static_cast<float>(180.0 - deviation_deg);

  if (std::fabs(slope_difference) < 1e-9) {
    return true;  // Parallel legs: a straight profile, reported as not valid.
  }
  const double corner_y =
      (best_second.intercept - best_first.intercept) / slope_difference;
  const double corner_x = best_first.intercept + best_first.slope * corner_y;
  corner->y_px = static_cast<float>(corner_y);
  corner->x_px = static_cast<float>(corner_x);

  corner->valid = deviation_deg >=
                      static_cast<double>(config.minimum_corner_deviation_deg) &&
                  corner->rms_px <= config.maximum_rms_px &&
                  corner_y >= static_cast<double>(first_y) &&
                  corner_y <= static_cast<double>(last_y);
  return true;
}

}  // namespace laser
