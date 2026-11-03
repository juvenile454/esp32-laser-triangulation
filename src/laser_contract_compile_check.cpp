#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "inference_contract.h"
#include "laser_core.h"

// This translation unit forces the portable contracts through the exact
// ESP32-S3 PlatformIO toolchain even while the board is unavailable. It makes
// no runtime or memory-performance claim.
static_assert(laser::kContractVersion == 3U, "unexpected laser contract");
static_assert(laser::kInferenceContractVersion == 1U,
              "unexpected inference contract");
static_assert(laser::kInferenceFeatureCount == 69U,
              "host/firmware feature contract mismatch");
static_assert(std::is_trivially_copyable<laser::ProfilePoint>::value,
              "profile points must remain simple bounded records");
static_assert(std::is_trivially_copyable<laser::SeamCorner>::value,
              "seam corners must remain simple bounded records");

namespace laser::firmware_compile_check {

ExtractorConfig defaultExtractorConfig() {
  ExtractorConfig config{};
  config.score_threshold = 20U;
  config.max_stripe_width_px = 16U;
  config.ghost_ratio = 0.45F;
  config.minimum_valid_ratio = 0.10F;
  // Continuity tracking is what keeps a specular blob from stealing the
  // centreline on blank steel; keep it exercised by the cross-build.
  config.prediction_window_px = 12.0F;
  return config;
}

bool compileCheckedExtract(const FrameViewRgb888& laser_on,
                           const FrameViewRgb888* laser_off,
                           ProfilePoint* output,
                           std::size_t output_capacity,
                           MeasurementSummary* summary) {
  return extractStripeRgb888(laser_on, laser_off, defaultExtractorConfig(),
                             output, output_capacity, summary);
}

bool compileCheckedSeamFit(const ProfilePoint* profile, std::size_t count,
                           SeamCorner* corner) {
  return fitSeamCorner(profile, count, SeamFitConfig{}, corner);
}

}  // namespace laser::firmware_compile_check
