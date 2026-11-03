#pragma once

#include <cstddef>
#include <cstdint>

namespace laser {

constexpr std::uint16_t kContractVersion = 3U;

enum class InvalidReason : std::uint8_t {
  kNone = 0,
  kBadArgument,
  kDimensionMismatch,
  kOutputTooSmall,
  kNoStripe,
  kInsufficientCoverage,
  // Clusters were present on every scanned row but all of them exceeded
  // max_stripe_width_px. Kept distinct from kNoStripe because a broad specular
  // field and an unlit scene need different operator responses.
  kAllClustersTooWide,
};

struct FrameViewRgb888 {
  const std::uint8_t* data = nullptr;
  std::uint16_t width = 0U;
  std::uint16_t height = 0U;
  std::uint32_t stride_bytes = 0U;
};

struct ExtractorConfig {
  std::uint16_t roi_x0 = 0U;
  std::uint16_t roi_y0 = 0U;
  std::uint16_t roi_x1 = 0U;  // 0 means frame width.
  std::uint16_t roi_y1 = 0U;  // 0 means frame height.
  std::uint8_t score_threshold = 20U;
  std::uint8_t max_stripe_width_px = 16U;
  std::uint8_t max_clipped_core_gap_px = 12U;
  std::uint8_t clipped_core_min_luma = 145U;
  std::uint8_t clipped_core_min_delta = 25U;
  std::uint8_t clipped_core_max_chroma = 80U;
  float ghost_ratio = 0.45F;
  float minimum_valid_ratio = 0.10F;
  // Continuity tracking. A candidate whose centre lies within
  // prediction_window_px of the running prediction always outranks one that
  // does not; strength only decides inside the same tier. 0 disables tracking
  // and restores pure strongest-cluster selection.
  float prediction_window_px = 0.0F;
  // Seed for the first scanned row. Negative means "no seed": the first row
  // falls back to strongest-cluster selection and seeds the prediction itself.
  float initial_prediction_px = -1.0F;
};

struct ProfilePoint {
  float x_px = 0.0F;
  std::uint16_t y_px = 0U;
  std::uint8_t peak_score = 0U;
  std::uint8_t width_px = 0U;
  bool valid = false;
  bool saturated = false;
  bool secondary_candidate = false;
  // Every candidate on this row exceeded max_stripe_width_px. x_px and
  // width_px then describe the strongest rejected candidate for diagnostics.
  bool too_wide = false;
};

struct MeasurementSummary {
  std::uint16_t contract_version = kContractVersion;
  std::uint16_t profile_points = 0U;
  std::uint16_t valid_points = 0U;
  std::uint16_t missing_points = 0U;
  std::uint16_t saturated_points = 0U;
  std::uint16_t secondary_candidate_points = 0U;
  std::uint16_t too_wide_points = 0U;
  float valid_ratio = 0.0F;
  float mean_peak_score = 0.0F;
  float confidence = 0.0F;
  bool valid = false;
  InvalidReason invalid_reason = InvalidReason::kBadArgument;
};

// Pure, allocation-free extraction. One output point is produced for every
// scan row in the ROI. Physical units are intentionally not part of this API.
bool extractStripeRgb888(const FrameViewRgb888& laser_on,
                         const FrameViewRgb888* laser_off,
                         const ExtractorConfig& config,
                         ProfilePoint* output,
                         std::size_t output_capacity,
                         MeasurementSummary* summary);

const char* invalidReasonName(InvalidReason reason);

// ---------------------------------------------------------------------------
// Seam feature fitting
//
// A fillet weld images as two straight legs meeting in a corner. Fitting both
// legs and intersecting them locates the corner far more precisely than any
// single profile point, because every inlier contributes. Still pixel-only:
// converting the result to millimetres is the calibration layer's job.
// ---------------------------------------------------------------------------

struct SeamFitConfig {
  // Rows kept clear of each end so a split never degenerates into a
  // two-point leg.
  std::uint16_t minimum_leg_points = 8U;
  // Each leg must also hold this share of all valid points. Without it, pixel
  // quantisation near either end of a straight profile fits a short flat leg
  // against a long sloped one and reports a corner that is not there.
  float minimum_leg_fraction = 0.15F;
  // Rejection threshold on the combined residual of both legs.
  float maximum_rms_px = 3.0F;
  // How far the profile direction must turn at the split for it to count as a
  // corner rather than measurement noise on a straight line.
  float minimum_corner_deviation_deg = 5.0F;
};

struct SeamCorner {
  bool valid = false;
  float x_px = 0.0F;  // Corner position on the disparity axis.
  float y_px = 0.0F;  // Corner position along the stripe.
  float included_angle_deg = 0.0F;
  float rms_px = 0.0F;
  float first_leg_slope = 0.0F;   // dx/dy of the leg before the corner.
  float second_leg_slope = 0.0F;  // dx/dy of the leg after the corner.
  std::uint16_t first_leg_points = 0U;
  std::uint16_t second_leg_points = 0U;
};

// Fits x = a + b*y independently to the profile before and after every
// admissible split, keeps the split with the smallest combined squared error
// and reports the intersection of the two fitted legs. Runs in O(n) with
// constant memory; invalid profile points are skipped.
bool fitSeamCorner(const ProfilePoint* profile,
                   std::size_t count,
                   const SeamFitConfig& config,
                   SeamCorner* corner);

}  // namespace laser
