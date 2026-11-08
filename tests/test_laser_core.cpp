#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

#include "laser_core.h"

namespace {

constexpr std::uint16_t kWidth = 160U;
constexpr std::uint16_t kHeight = 100U;
constexpr std::uint32_t kStride = static_cast<std::uint32_t>(kWidth) * 3U;

struct TestImage {
  std::vector<std::uint8_t> pixels =
      std::vector<std::uint8_t>(static_cast<std::size_t>(kWidth) * kHeight * 3U,
                                30U);

  std::uint8_t* at(int x, int y) {
    return pixels.data() +
           (static_cast<std::size_t>(y) * kWidth + static_cast<std::size_t>(x)) * 3U;
  }

  void stripe(std::uint16_t y, int center, int strength = 190) {
    for (int dx = -2; dx <= 2; ++dx) {
      const int x = center + dx;
      if (x < 0 || x >= kWidth) {
        continue;
      }
      const int contribution = strength - std::abs(dx) * 55;
      std::uint8_t* pixel = at(x, y);
      pixel[0] = static_cast<std::uint8_t>(
          std::min(255, static_cast<int>(pixel[0]) + contribution));
    }
  }

  // Red ramp, a white (or warm) clipped core, red ramp: one physical stripe
  // whose centre is not red-dominant.
  void clippedCore(std::uint16_t y, std::uint8_t core_red,
                   std::uint8_t core_green, std::uint8_t core_blue) {
    for (int x = 65; x <= 68; ++x) {
      at(x, y)[0] = 220U;
    }
    for (int x = 69; x <= 75; ++x) {
      std::uint8_t* pixel = at(x, y);
      pixel[0] = core_red;
      pixel[1] = core_green;
      pixel[2] = core_blue;
    }
    for (int x = 76; x <= 79; ++x) {
      at(x, y)[0] = 220U;
    }
  }

  laser::FrameViewRgb888 view() const {
    laser::FrameViewRgb888 frame;
    frame.data = pixels.data();
    frame.width = kWidth;
    frame.height = kHeight;
    frame.stride_bytes = kStride;
    return frame;
  }
};

int failures = 0;

bool check(bool condition, const std::string& label) {
  if (!condition) {
    ++failures;
    std::cerr << "FAIL " << label << '\n';
  }
  return condition;
}

std::string atRow(const char* label, std::size_t row) {
  return std::string(label) + " (row " + std::to_string(row) + ")";
}

// Stop the current test at its first failure so one broken invariant does
// not print a line for every row.
#define EXPECT(condition, label) \
  do {                           \
    if (!check((condition), (label))) { \
      return;                    \
    }                            \
  } while (false)

bool near(float actual, float expected, float tolerance) {
  return std::fabs(actual - expected) < tolerance;
}

// Runs one extraction and reports whether the call itself succeeded.
bool run(const TestImage& on, const TestImage* off,
         std::vector<laser::ProfilePoint>& profile,
         laser::MeasurementSummary& summary,
         const laser::ExtractorConfig& config = laser::ExtractorConfig{}) {
  profile.assign(kHeight, laser::ProfilePoint{});
  summary = laser::MeasurementSummary{};
  const laser::FrameViewRgb888 on_view = on.view();
  laser::FrameViewRgb888 off_view{};
  if (off != nullptr) {
    off_view = off->view();
  }
  return laser::extractStripeRgb888(on_view, off != nullptr ? &off_view : nullptr,
                                    config, profile.data(), profile.size(),
                                    &summary);
}

laser::InvalidReason callError(const laser::ExtractorConfig& config) {
  const TestImage frame;
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  if (run(frame, &frame, profile, summary, config)) {
    return laser::InvalidReason::kNone;
  }
  return summary.invalid_reason;
}

void testStraightAndSubpixel() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40 + y / 5);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(on, &off, profile, summary), "straight extract call succeeds");
  EXPECT(summary.contract_version == 4U, "contract version 4");
  EXPECT(summary.valid, "straight stripe valid");
  EXPECT(summary.valid_points == kHeight, "straight coverage complete");
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    EXPECT(near(profile[y].x_px, static_cast<float>(40 + y / 5), 0.25F),
           atRow("straight center tolerance", y));
  }
}

void testStep() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, y < 50 ? 55 : 95);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(on, &off, profile, summary), "step extract call succeeds");
  EXPECT(summary.valid, "step stripe valid");
  EXPECT(near(profile[49].x_px, 55.0F, 0.25F), "step upper center");
  EXPECT(near(profile[50].x_px, 95.0F, 0.25F), "step lower center");
}

void testGhostAndGap() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    if (y < 20 || y >= 30) {
      on.stripe(y, 50, 190);
      on.stripe(y, 105, 140);
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig config{};
  config.ghost_ratio = 0.45F;
  EXPECT(run(on, &off, profile, summary, config), "ghost extract call succeeds");
  EXPECT(summary.valid, "ghost/gap remains usable");
  EXPECT(summary.valid_points == 90U, "gap count");
  EXPECT(summary.secondary_candidate_points == 90U, "ghost count");
  EXPECT(!profile[25].valid, "gap row invalid");
}

void testClippedCoreBridgesOneStripe(std::uint8_t core_red,
                                     std::uint8_t core_green,
                                     std::uint8_t core_blue, const char* name) {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.clippedCore(y, core_red, core_green, core_blue);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 20U;
  const std::string prefix = std::string(name) + " ";
  EXPECT(run(on, &off, profile, summary, config), prefix + "extract call succeeds");
  EXPECT(summary.valid, prefix + "stripe valid");
  EXPECT(summary.valid_points == kHeight, prefix + "coverage complete");
  EXPECT(summary.secondary_candidate_points == 0U, prefix + "is not a ghost");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    const laser::ProfilePoint& point = profile[y];
    EXPECT(near(point.x_px, 72.0F, 0.5F), atRow((prefix + "center").c_str(), y));
    EXPECT(point.width_px == 15U, atRow((prefix + "merged width").c_str(), y));
    EXPECT(point.saturated, atRow((prefix + "saturation retained").c_str(), y));
  }
}

// The bridge only spans max_clipped_core_gap_px pixels. The 7 px core must
// split the stripe in two once the limit is 6, and bridge again at 7.
void testClippedCoreGapLimit() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.clippedCore(y, 250U, 250U, 250U);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 20U;
  config.max_clipped_core_gap_px = 6U;
  EXPECT(run(on, &off, profile, summary, config), "gap-limit extract call succeeds");
  EXPECT(summary.valid, "split stripe still measurable");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    EXPECT(profile[y].width_px == 4U, atRow("core wider than the gap splits", y));
    EXPECT(near(profile[y].x_px, 66.5F, 0.5F),
           atRow("first equal-weight half wins the tie", y));
    EXPECT(profile[y].secondary_candidate,
           atRow("other half is reported as a competitor", y));
  }

  config.max_clipped_core_gap_px = 7U;
  EXPECT(run(on, &off, profile, summary, config), "gap-limit 7 call succeeds");
  EXPECT(profile[0].width_px == 15U, "core exactly at the gap limit bridges");
}

// Without an OFF frame scoring is ON-only and the clipped-core bridge, which
// needs the OFF frame, is disabled.
void testLaserOffNullptr() {
  TestImage on;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(on, nullptr, profile, summary), "ON-only extract call succeeds");
  EXPECT(summary.valid, "ON-only red stripe valid");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    EXPECT(near(profile[y].x_px, 40.0F, 0.25F), atRow("ON-only centre", y));
  }

  TestImage clipped;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    clipped.clippedCore(y, 250U, 250U, 250U);
  }
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 20U;
  EXPECT(run(clipped, nullptr, profile, summary, config),
         "ON-only clipped call succeeds");
  EXPECT(profile[0].valid && profile[0].width_px == 4U,
         "no OFF frame, no clipped-core bridge");
}

// Output rows are relative to roi_y0 while y_px stays a frame row; the upper
// ROI bounds are exclusive.
void testRoiOffsets() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40 + y / 5);
    on.stripe(y, 140, 250);  // Stronger, but outside the ROI columns.
  }
  laser::ExtractorConfig config{};
  config.roi_x0 = 20U;
  config.roi_x1 = 120U;
  config.roi_y0 = 10U;
  config.roi_y1 = 30U;
  const std::size_t rows = 20U;
  std::vector<laser::ProfilePoint> profile(rows + 5U);
  laser::MeasurementSummary summary{};
  const laser::FrameViewRgb888 on_view = on.view();
  const laser::FrameViewRgb888 off_view = off.view();
  EXPECT(laser::extractStripeRgb888(on_view, &off_view, config, profile.data(),
                                    rows, &summary),
         "ROI output of exactly ROI height is enough");
  EXPECT(summary.profile_points == rows, "one point per ROI row");
  EXPECT(summary.valid_points == rows, "every ROI row valid");
  for (std::size_t i = 0; i < rows; ++i) {
    const auto frame_row = static_cast<std::uint16_t>(10U + i);
    EXPECT(profile[i].y_px == frame_row, atRow("y_px is the frame row", i));
    EXPECT(near(profile[i].x_px, static_cast<float>(40 + frame_row / 5), 0.25F),
           atRow("x_px is a frame column inside the ROI", i));
  }
  EXPECT(!laser::extractStripeRgb888(on_view, &off_view, config, profile.data(),
                                     rows - 1U, &summary),
         "one row short is rejected");
  EXPECT(summary.invalid_reason == laser::InvalidReason::kOutputTooSmall,
         "one row short reason");

  // roi_x1 is exclusive: a stripe whose pixels start at roi_x1 is invisible.
  config.roi_x1 = 138U;
  EXPECT(laser::extractStripeRgb888(on_view, &off_view, config, profile.data(),
                                    rows, &summary),
         "exclusive roi_x1 call succeeds");
  EXPECT(near(profile[0].x_px, 42.0F, 0.25F), "pixels at roi_x1 are excluded");
}

// Padding bytes beyond width*3 must never be read.
void testStrideLargerThanRow() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40 + y / 5);
  }
  constexpr std::uint32_t kPaddedStride = kStride + 7U;
  std::vector<std::uint8_t> padded_on(static_cast<std::size_t>(kPaddedStride) * kHeight, 255U);
  std::vector<std::uint8_t> padded_off(padded_on.size(), 0U);
  for (std::size_t y = 0; y < kHeight; ++y) {
    for (std::size_t byte = 0; byte < kStride; ++byte) {
      padded_on[y * kPaddedStride + byte] = on.pixels[y * kStride + byte];
      padded_off[y * kPaddedStride + byte] = off.pixels[y * kStride + byte];
    }
  }
  laser::FrameViewRgb888 on_view = on.view();
  laser::FrameViewRgb888 off_view = off.view();
  on_view.data = padded_on.data();
  on_view.stride_bytes = kPaddedStride;
  off_view.data = padded_off.data();
  off_view.stride_bytes = kPaddedStride;

  std::vector<laser::ProfilePoint> reference;
  laser::MeasurementSummary reference_summary{};
  EXPECT(run(on, &off, reference, reference_summary), "packed reference succeeds");
  std::vector<laser::ProfilePoint> profile(kHeight);
  laser::MeasurementSummary summary{};
  EXPECT(laser::extractStripeRgb888(on_view, &off_view, laser::ExtractorConfig{},
                                    profile.data(), profile.size(), &summary),
         "padded stride call succeeds");
  EXPECT(summary.valid_points == reference_summary.valid_points,
         "padded stride keeps coverage");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    EXPECT(profile[y].x_px == reference[y].x_px &&
               profile[y].width_px == reference[y].width_px &&
               profile[y].peak_score == reference[y].peak_score,
           atRow("padded stride matches packed rows", y));
  }
}

// A broad specular field outweighs the true stripe; filtering width before
// ranking must keep the stripe. Selecting by strength first loses the row.
void testBroadBlobDoesNotSuppressStripe() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40, 200);
    for (int x = 90; x <= 129; ++x) {
      std::uint8_t* pixel = on.at(x, y);
      pixel[0] = static_cast<std::uint8_t>(
          std::min(255, static_cast<int>(pixel[0]) + 70));
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 16U;
  EXPECT(run(on, &off, profile, summary, config), "blob extract call succeeds");
  EXPECT(summary.valid, "blob scene stays valid");
  EXPECT(summary.valid_points == kHeight, "blob scene keeps every row");
  EXPECT(summary.too_wide_points == 0U, "blob is rejected, not counted as row");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    EXPECT(near(profile[y].x_px, 40.0F, 0.25F), atRow("stripe survives the blob", y));
    EXPECT(profile[y].width_px <= 16U, atRow("selected cluster respects width", y));
  }
}

// When nothing narrow is present the caller must be able to tell a glare field
// from an unlit scene.
void testAllTooWideIsItsOwnReason() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    for (int x = 60; x <= 119; ++x) {
      on.at(x, y)[0] = 200U;
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 16U;
  EXPECT(run(on, &off, profile, summary, config), "glare extract call succeeds");
  EXPECT(!summary.valid, "glare field is not a measurement");
  EXPECT(summary.invalid_reason == laser::InvalidReason::kAllClustersTooWide,
         "glare field reports all_clusters_too_wide");
  EXPECT(summary.too_wide_points == kHeight, "every row records the rejection");
  EXPECT(profile[0].too_wide && !profile[0].valid, "row flags the rejection");
  EXPECT(profile[0].width_px == 60U, "rejected candidate width is reported");
}

// Continuity tracking: a stronger competitor away from the running prediction
// must not steal the centreline.
void testPredictionWindowKeepsContinuity() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40, 150);
    if (y >= 40 && y < 60) {
      on.stripe(y, 110, 240);  // Brighter intruder for twenty rows.
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  laser::ExtractorConfig tracked{};
  tracked.prediction_window_px = 12.0F;
  tracked.initial_prediction_px = 40.0F;
  EXPECT(run(on, &off, profile, summary, tracked), "tracked extract call succeeds");
  EXPECT(summary.valid, "tracked scene valid");
  for (std::size_t y = 0; y < profile.size(); ++y) {
    EXPECT(near(profile[y].x_px, 40.0F, 0.25F),
           atRow("tracking holds the original stripe", y));
  }

  EXPECT(run(on, &off, profile, summary), "untracked extract call succeeds");
  EXPECT(near(profile[50].x_px, 110.0F, 0.25F),
         "without tracking the brighter intruder wins");
}

// Regression (contract v4): one row without the stripe used to select the
// blob, move the prediction onto it and then prefer the blob as "tracked"
// for its whole length. The blob row itself is still reported, but the
// prediction stays on the stripe, so every other row returns to x = 40.
void testTrackingSurvivesGapRow() {
  TestImage off;
  TestImage on = off;
  constexpr std::uint16_t kGapRow = 45U;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    if (y != kGapRow) {
      on.stripe(y, 40, 150);
    }
    if (y >= 40 && y < 60) {
      on.stripe(y, 110, 240);
    }
  }
  for (int seeded = 0; seeded < 2; ++seeded) {
    laser::ExtractorConfig config{};
    config.prediction_window_px = 12.0F;
    config.initial_prediction_px = seeded != 0 ? 40.0F : -1.0F;
    std::vector<laser::ProfilePoint> profile;
    laser::MeasurementSummary summary{};
    EXPECT(run(on, &off, profile, summary, config), "gap-row call succeeds");
    EXPECT(summary.valid, "gap-row scene valid");
    EXPECT(summary.valid_points == kHeight, "gap row still yields a point");
    EXPECT(near(profile[kGapRow].x_px, 110.0F, 0.25F),
           "gap row reports the only candidate");
    for (std::size_t y = 0; y < profile.size(); ++y) {
      if (y == kGapRow) {
        continue;
      }
      EXPECT(profile[y].valid && near(profile[y].x_px, 40.0F, 0.25F),
             atRow(seeded != 0 ? "seeded tracking survives the gap row"
                               : "self-seeded tracking survives the gap row",
                   y));
    }
  }
}

void testInsufficientCoverage() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < 5U; ++y) {
    on.stripe(y, 40);
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(on, &off, profile, summary), "coverage call succeeds");
  EXPECT(!summary.valid, "5 % coverage is not a measurement");
  EXPECT(summary.invalid_reason == laser::InvalidReason::kInsufficientCoverage,
         "insufficient_coverage reason");
  EXPECT(summary.valid_points == 5U, "covered rows still counted");

  // NaN must not disable the coverage gate (NaN < x is always false).
  laser::ExtractorConfig config{};
  config.minimum_valid_ratio = std::numeric_limits<float>::quiet_NaN();
  EXPECT(!run(on, &off, profile, summary, config), "NaN ratio is a call error");
  EXPECT(summary.invalid_reason == laser::InvalidReason::kBadArgument,
         "NaN ratio reason");
  EXPECT(!summary.valid, "NaN ratio is never a valid measurement");
}

void testBadConfigs() {
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  struct Case {
    const char* label;
    laser::ExtractorConfig config;
  };
  std::vector<Case> cases;
  laser::ExtractorConfig config{};
  config.score_threshold = 0U;
  cases.push_back({"threshold 0", config});
  config = laser::ExtractorConfig{};
  config.max_stripe_width_px = 0U;
  cases.push_back({"max width 0", config});
  const float bad_ratios[] = {-0.1F, 1.5F, nan, inf, -inf};
  for (const float value : bad_ratios) {
    config = laser::ExtractorConfig{};
    config.ghost_ratio = value;
    cases.push_back({"ghost_ratio out of range / non-finite", config});
    config = laser::ExtractorConfig{};
    config.minimum_valid_ratio = value;
    cases.push_back({"minimum_valid_ratio out of range / non-finite", config});
  }
  const float bad_windows[] = {-1.0F, nan, inf};
  for (const float value : bad_windows) {
    config = laser::ExtractorConfig{};
    config.prediction_window_px = value;
    cases.push_back({"prediction_window_px negative / non-finite", config});
  }
  const float bad_seeds[] = {nan, inf, -inf};
  for (const float value : bad_seeds) {
    config = laser::ExtractorConfig{};
    config.prediction_window_px = 12.0F;
    config.initial_prediction_px = value;
    cases.push_back({"initial_prediction_px non-finite", config});
  }
  config = laser::ExtractorConfig{};
  config.roi_x0 = kWidth;
  cases.push_back({"ROI starts at the right edge", config});
  config = laser::ExtractorConfig{};
  config.roi_y0 = 30U;
  config.roi_y1 = 30U;
  cases.push_back({"empty ROI rows", config});

  for (std::size_t index = 0; index < cases.size(); ++index) {
    EXPECT(callError(cases[index].config) == laser::InvalidReason::kBadArgument,
           atRow(cases[index].label, index));
  }

  // Boundaries that must stay accepted.
  config = laser::ExtractorConfig{};
  config.ghost_ratio = 1.0F;
  config.minimum_valid_ratio = 0.0F;
  EXPECT(callError(config) == laser::InvalidReason::kNone,
         "closed ratio bounds accepted");
}

void testNoStripeAndErrors() {
  TestImage off;
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(off, &off, profile, summary), "no-stripe call succeeds");
  EXPECT(!summary.valid, "no stripe invalid");
  EXPECT(summary.invalid_reason == laser::InvalidReason::kNoStripe,
         "no stripe reason");

  laser::MeasurementSummary error{};
  const laser::ExtractorConfig config{};
  const laser::FrameViewRgb888 off_view = off.view();
  EXPECT(!laser::extractStripeRgb888(off_view, &off_view, config, profile.data(),
                                     1U, &error),
         "small output rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kOutputTooSmall,
         "small output reason");

  laser::FrameViewRgb888 wrong = off.view();
  wrong.width = static_cast<std::uint16_t>(wrong.width - 1U);
  EXPECT(!laser::extractStripeRgb888(off_view, &wrong, config, profile.data(),
                                     profile.size(), &error),
         "dimension mismatch rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kDimensionMismatch,
         "dimension mismatch reason");

  EXPECT(!laser::extractStripeRgb888(off_view, &off_view, config, profile.data(),
                                     profile.size(), nullptr),
         "summary nullptr rejected without a crash");

  laser::FrameViewRgb888 no_data = off.view();
  no_data.data = nullptr;
  EXPECT(!laser::extractStripeRgb888(no_data, &off_view, config, profile.data(),
                                     profile.size(), &error),
         "ON data nullptr rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kBadArgument,
         "ON data nullptr reason");
  EXPECT(!laser::extractStripeRgb888(off_view, &no_data, config, profile.data(),
                                     profile.size(), &error),
         "OFF data nullptr rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kDimensionMismatch,
         "OFF data nullptr reason");
  EXPECT(!laser::extractStripeRgb888(off_view, &off_view, config, nullptr,
                                     profile.size(), &error),
         "output nullptr rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kBadArgument,
         "output nullptr reason");

  laser::FrameViewRgb888 short_stride = off.view();
  short_stride.stride_bytes = kStride - 1U;
  EXPECT(!laser::extractStripeRgb888(short_stride, nullptr, config,
                                     profile.data(), profile.size(), &error),
         "stride below width*3 rejected");
  EXPECT(error.invalid_reason == laser::InvalidReason::kBadArgument,
         "short stride reason");
}

std::vector<laser::ProfilePoint> cornerProfile() {
  TestImage off;
  TestImage on = off;
  constexpr int kCornerY = 55;
  constexpr float kCornerX = 70.0F;
  constexpr float kFirstSlope = -0.40F;
  constexpr float kSecondSlope = 0.25F;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    const int offset = static_cast<int>(y) - kCornerY;
    const float slope = offset < 0 ? kFirstSlope : kSecondSlope;
    on.stripe(y, static_cast<int>(kCornerX + slope * static_cast<float>(offset) + 0.5F));
  }
  std::vector<laser::ProfilePoint> profile;
  laser::MeasurementSummary summary{};
  run(on, &off, profile, summary);
  return profile;
}

void testSeamCornerFit() {
  const std::vector<laser::ProfilePoint> profile = cornerProfile();
  laser::SeamCorner corner{};
  const laser::SeamFitConfig config{};
  EXPECT(laser::fitSeamCorner(profile.data(), profile.size(), config, &corner),
         "corner fit runs");
  EXPECT(corner.valid, "corner accepted");
  EXPECT(near(corner.y_px, 55.0F, 1.5F), "corner y within tolerance");
  EXPECT(near(corner.x_px, 70.0F, 1.0F), "corner x within tolerance");
  EXPECT(corner.rms_px < 1.0F, "corner residual small");
  EXPECT(corner.first_leg_points > 20U && corner.second_leg_points > 20U,
         "both legs populated");

  // A straight profile has no corner to find.
  TestImage off;
  TestImage straight_on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    straight_on.stripe(y, 60 + y / 8);
  }
  std::vector<laser::ProfilePoint> straight_profile;
  laser::MeasurementSummary summary{};
  EXPECT(run(straight_on, &off, straight_profile, summary), "straight call succeeds");
  laser::SeamCorner straight{};
  EXPECT(laser::fitSeamCorner(straight_profile.data(), straight_profile.size(),
                              config, &straight),
         "straight corner fit runs");
  EXPECT(!straight.valid, "straight profile reports no corner");
  EXPECT(straight.included_angle_deg > 170.0F, "straight legs stay near 180 deg");

  // Too few points must fail closed rather than extrapolate.
  laser::SeamCorner sparse{};
  EXPECT(!laser::fitSeamCorner(profile.data(), 4U, config, &sparse),
         "sparse profile rejected");
  EXPECT(!sparse.valid, "sparse corner not valid");
  EXPECT(!laser::fitSeamCorner(nullptr, profile.size(), config, &sparse),
         "profile nullptr rejected");
  EXPECT(!laser::fitSeamCorner(profile.data(), profile.size(), config, nullptr),
         "corner nullptr rejected");
}

void testSeamCornerSkipsNonFinitePoints() {
  const std::vector<laser::ProfilePoint> clean = cornerProfile();
  const laser::SeamFitConfig config{};
  laser::SeamCorner reference{};
  EXPECT(laser::fitSeamCorner(clean.data(), clean.size(), config, &reference),
         "reference fit runs");

  std::vector<laser::ProfilePoint> poisoned = clean;
  poisoned[30].x_px = std::numeric_limits<float>::quiet_NaN();
  poisoned[70].x_px = std::numeric_limits<float>::infinity();
  laser::SeamCorner corner{};
  EXPECT(laser::fitSeamCorner(poisoned.data(), poisoned.size(), config, &corner),
         "fit with non-finite points runs");
  EXPECT(corner.valid && std::isfinite(corner.x_px) && std::isfinite(corner.y_px),
         "non-finite points are skipped, not propagated");
  EXPECT(near(corner.x_px, reference.x_px, 0.5F) &&
             near(corner.y_px, reference.y_px, 1.0F),
         "skipping two points barely moves the corner");
  EXPECT(corner.first_leg_points + corner.second_leg_points ==
             reference.first_leg_points + reference.second_leg_points - 2U,
         "skipped points are not counted");

  for (auto& point : poisoned) {
    point.x_px = std::numeric_limits<float>::quiet_NaN();
  }
  EXPECT(!laser::fitSeamCorner(poisoned.data(), poisoned.size(), config, &corner),
         "all-NaN profile rejected");
}

// Two exact legs whose lines intersect far before the first row: the fit runs
// but must not report that intersection as a corner.
void testSeamCornerOutsideRange() {
  std::vector<laser::ProfilePoint> profile(kHeight);
  for (std::size_t y = 0; y < profile.size(); ++y) {
    profile[y].y_px = static_cast<std::uint16_t>(y);
    profile[y].valid = true;
    profile[y].x_px = y < 50U ? 50.0F : 80.0F + 0.5F * static_cast<float>(y);
  }
  laser::SeamCorner corner{};
  EXPECT(laser::fitSeamCorner(profile.data(), profile.size(), laser::SeamFitConfig{},
                              &corner),
         "out-of-range fit runs");
  EXPECT(corner.y_px < 0.0F, "legs intersect before the first row");
  EXPECT(!corner.valid, "corner outside the profile is rejected");
}

void testSeamCornerCountBound() {
  std::vector<laser::ProfilePoint> profile(65536U);
  for (std::size_t y = 0; y < profile.size(); ++y) {
    profile[y].y_px = static_cast<std::uint16_t>(y & 0xFFFFU);
    profile[y].valid = true;
    profile[y].x_px = 10.0F;
  }
  laser::SeamCorner corner{};
  EXPECT(!laser::fitSeamCorner(profile.data(), profile.size(), laser::SeamFitConfig{},
                               &corner),
         "more than 65535 points rejected (leg counts are uint16)");
}

// Regression for the uint16_t maximum: the scan loop must terminate and the
// weighted centroid must not overflow at the far right of a wide frame.
void testMaximumWidth() {
  constexpr std::uint16_t width = 65535U;
  std::vector<std::uint8_t> off(static_cast<std::size_t>(width) * 3U, 20U);
  std::vector<std::uint8_t> on = off;
  for (std::uint32_t x = 65530U; x < 65535U; ++x) {
    on[static_cast<std::size_t>(x) * 3U] = 220U;
  }
  laser::FrameViewRgb888 on_view{};
  on_view.data = on.data();
  on_view.width = width;
  on_view.height = 1U;
  on_view.stride_bytes = static_cast<std::uint32_t>(width) * 3U;
  laser::FrameViewRgb888 off_view = on_view;
  off_view.data = off.data();
  laser::ProfilePoint point{};
  laser::MeasurementSummary summary{};
  EXPECT(laser::extractStripeRgb888(on_view, &off_view, laser::ExtractorConfig{},
                                    &point, 1U, &summary),
         "maximum-width extraction terminates");
  EXPECT(summary.valid && near(point.x_px, 65532.0F, 0.25F),
         "maximum-width centroid");
}

}  // namespace

int main() {
  testStraightAndSubpixel();
  testStep();
  testGhostAndGap();
  testClippedCoreBridgesOneStripe(250U, 250U, 250U, "clipped white core");
  testClippedCoreBridgesOneStripe(255U, 255U, 198U, "clipped warm core");
  testClippedCoreGapLimit();
  testLaserOffNullptr();
  testRoiOffsets();
  testStrideLargerThanRow();
  testBroadBlobDoesNotSuppressStripe();
  testAllTooWideIsItsOwnReason();
  testPredictionWindowKeepsContinuity();
  testTrackingSurvivesGapRow();
  testInsufficientCoverage();
  testBadConfigs();
  testNoStripeAndErrors();
  testSeamCornerFit();
  testSeamCornerSkipsNonFinitePoints();
  testSeamCornerOutsideRange();
  testSeamCornerCountBound();
  testMaximumWidth();

  if (failures != 0) {
    std::cerr << "TEST_RESULT FAIL failures=" << failures << '\n';
    return 1;
  }
  std::cout << "TEST_RESULT PASS\n";
  return 0;
}
