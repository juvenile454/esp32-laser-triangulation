#include <cmath>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#include "laser_core.h"

namespace {

constexpr std::uint16_t kWidth = 160U;
constexpr std::uint16_t kHeight = 100U;

struct TestImage {
  std::vector<std::uint8_t> pixels =
      std::vector<std::uint8_t>(static_cast<std::size_t>(kWidth) * kHeight * 3U,
                                30U);

  void stripe(std::uint16_t y, int center, int strength = 190) {
    for (int dx = -2; dx <= 2; ++dx) {
      const int x = center + dx;
      if (x < 0 || x >= kWidth) {
        continue;
      }
      const int contribution = strength - std::abs(dx) * 55;
      auto* pixel = pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = static_cast<std::uint8_t>(
          std::min(255, static_cast<int>(pixel[0]) + contribution));
    }
  }

  laser::FrameViewRgb888 view() const {
    return {pixels.data(), kWidth, kHeight,
            static_cast<std::uint32_t>(kWidth) * 3U};
  }
};

int failures = 0;

void check(bool condition, const std::string& label) {
  if (!condition) {
    ++failures;
    std::cerr << "FAIL " << label << '\n';
  }
}

laser::MeasurementSummary run(const TestImage& on, const TestImage& off,
                              std::vector<laser::ProfilePoint>& profile,
                              laser::ExtractorConfig config = {}) {
  profile.resize(kHeight);
  laser::MeasurementSummary summary{};
  const auto on_view = on.view();
  const auto off_view = off.view();
  check(laser::extractStripeRgb888(on_view, &off_view, config, profile.data(),
                                   profile.size(), &summary),
        "extract call succeeds");
  return summary;
}

void testStraightAndSubpixel() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40 + y / 5);
  }
  std::vector<laser::ProfilePoint> profile;
  const auto summary = run(on, off, profile);
  check(summary.valid, "straight stripe valid");
  check(summary.valid_points == kHeight, "straight coverage complete");
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    check(std::fabs(profile[y].x_px - static_cast<float>(40 + y / 5)) < 0.25F,
          "straight center tolerance");
  }
}

void testStep() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, y < 50 ? 55 : 95);
  }
  std::vector<laser::ProfilePoint> profile;
  const auto summary = run(on, off, profile);
  check(summary.valid, "step stripe valid");
  check(std::fabs(profile[49].x_px - 55.0F) < 0.25F, "step upper center");
  check(std::fabs(profile[50].x_px - 95.0F) < 0.25F, "step lower center");
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
  laser::ExtractorConfig config{};
  config.ghost_ratio = 0.45F;
  const auto summary = run(on, off, profile, config);
  check(summary.valid, "ghost/gap remains usable");
  check(summary.valid_points == 90U, "gap count");
  check(summary.secondary_candidate_points == 90U, "ghost count");
  check(!profile[25].valid, "gap row invalid");
}

void testClippedWhiteCoreBridgesOneStripe() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    for (int x = 65; x <= 68; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 220U;
    }
    for (int x = 69; x <= 75; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 250U;
      pixel[1] = 250U;
      pixel[2] = 250U;
    }
    for (int x = 76; x <= 79; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 220U;
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 20U;
  const auto summary = run(on, off, profile, config);
  check(summary.valid, "clipped white core stripe valid");
  check(summary.valid_points == kHeight, "clipped white core coverage complete");
  check(summary.secondary_candidate_points == 0U,
        "clipped white core is not a ghost");
  for (const auto& point : profile) {
    check(std::fabs(point.x_px - 72.0F) < 0.5F,
          "clipped white core center tolerance");
    check(point.width_px == 15U, "clipped white core merged width");
    check(point.saturated, "clipped white core saturation retained");
  }
}

void testClippedWarmCoreBridgesOneStripe() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    for (int x = 65; x <= 68; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 220U;
    }
    for (int x = 69; x <= 75; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 255U;
      pixel[1] = 255U;
      pixel[2] = 198U;
    }
    for (int x = 76; x <= 79; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 220U;
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 20U;
  const auto summary = run(on, off, profile, config);
  check(summary.valid, "clipped warm core stripe valid");
  check(summary.valid_points == kHeight,
        "clipped warm core coverage complete");
  check(summary.secondary_candidate_points == 0U,
        "clipped warm core is not a ghost");
  for (const auto& point : profile) {
    check(std::fabs(point.x_px - 72.0F) < 0.5F,
          "clipped warm core center tolerance");
    check(point.width_px == 15U, "clipped warm core merged width");
    check(point.saturated, "clipped warm core saturation retained");
  }
}

// Regression for the documented contract order. A broad specular field
// outweighs the true stripe; filtering width before ranking must keep the
// stripe. Selecting by strength first loses the row entirely.
void testBroadBlobDoesNotSuppressStripe() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    on.stripe(y, 40, 200);
    for (int x = 90; x <= 129; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = static_cast<std::uint8_t>(
          std::min(255, static_cast<int>(pixel[0]) + 70));
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 16U;
  const auto summary = run(on, off, profile, config);
  check(summary.valid, "blob scene stays valid");
  check(summary.valid_points == kHeight, "blob scene keeps every row");
  check(summary.too_wide_points == 0U, "blob is rejected, not counted as row");
  for (const auto& point : profile) {
    check(std::fabs(point.x_px - 40.0F) < 0.25F, "stripe survives the blob");
    check(point.width_px <= 16U, "selected cluster respects the width limit");
  }
}

// When nothing narrow is present the caller must be able to tell a glare field
// from an unlit scene.
void testAllTooWideIsItsOwnReason() {
  TestImage off;
  TestImage on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    for (int x = 60; x <= 119; ++x) {
      auto* pixel = on.pixels.data() +
                    (static_cast<std::size_t>(y) * kWidth + x) * 3U;
      pixel[0] = 200U;
    }
  }
  std::vector<laser::ProfilePoint> profile;
  laser::ExtractorConfig config{};
  config.max_stripe_width_px = 16U;
  const auto summary = run(on, off, profile, config);
  check(!summary.valid, "glare field is not a measurement");
  check(summary.invalid_reason == laser::InvalidReason::kAllClustersTooWide,
        "glare field reports all_clusters_too_wide");
  check(summary.too_wide_points == kHeight, "every row records the rejection");
  check(profile[0].too_wide && !profile[0].valid, "row flags the rejection");
  check(profile[0].width_px == 60U, "rejected candidate width is reported");
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
  laser::ExtractorConfig tracked{};
  tracked.prediction_window_px = 12.0F;
  tracked.initial_prediction_px = 40.0F;
  const auto summary = run(on, off, profile, tracked);
  check(summary.valid, "tracked scene valid");
  for (const auto& point : profile) {
    check(std::fabs(point.x_px - 40.0F) < 0.25F,
          "tracking holds the original stripe");
  }

  std::vector<laser::ProfilePoint> untracked_profile;
  laser::ExtractorConfig untracked{};
  run(on, off, untracked_profile, untracked);
  check(std::fabs(untracked_profile[50].x_px - 110.0F) < 0.25F,
        "without tracking the brighter intruder wins");
}

void testSeamCornerFit() {
  TestImage off;
  TestImage on = off;
  constexpr int kCornerY = 55;
  constexpr float kCornerX = 70.0F;
  constexpr float kFirstSlope = -0.40F;
  constexpr float kSecondSlope = 0.25F;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    const int offset = static_cast<int>(y) - kCornerY;
    const float slope = offset < 0 ? kFirstSlope : kSecondSlope;
    on.stripe(y, static_cast<int>(kCornerX + slope * offset + 0.5F));
  }
  std::vector<laser::ProfilePoint> profile;
  const auto summary = run(on, off, profile);
  check(summary.valid, "corner scene valid");

  laser::SeamCorner corner{};
  const laser::SeamFitConfig config{};
  check(laser::fitSeamCorner(profile.data(), profile.size(), config, &corner),
        "corner fit runs");
  check(corner.valid, "corner accepted");
  check(std::fabs(corner.y_px - static_cast<float>(kCornerY)) < 1.5F,
        "corner y within tolerance");
  check(std::fabs(corner.x_px - kCornerX) < 1.0F, "corner x within tolerance");
  check(corner.rms_px < 1.0F, "corner residual small");
  check(corner.first_leg_points > 20U && corner.second_leg_points > 20U,
        "both legs populated");

  // A straight profile has no corner to find.
  TestImage straight_on = off;
  for (std::uint16_t y = 0; y < kHeight; ++y) {
    straight_on.stripe(y, 60 + y / 8);
  }
  std::vector<laser::ProfilePoint> straight_profile;
  run(straight_on, off, straight_profile);
  laser::SeamCorner straight{};
  check(laser::fitSeamCorner(straight_profile.data(), straight_profile.size(),
                             config, &straight),
        "straight corner fit runs");
  check(!straight.valid, "straight profile reports no corner");
  check(straight.included_angle_deg > 170.0F, "straight legs stay near 180 deg");

  // Too few points must fail closed rather than extrapolate.
  laser::SeamCorner sparse{};
  check(!laser::fitSeamCorner(profile.data(), 4U, config, &sparse),
        "sparse profile rejected");
  check(!sparse.valid, "sparse corner not valid");
}

void testNoStripeAndErrors() {
  TestImage off;
  std::vector<laser::ProfilePoint> profile;
  const auto summary = run(off, off, profile);
  check(!summary.valid, "no stripe invalid");
  check(summary.invalid_reason == laser::InvalidReason::kNoStripe,
        "no stripe reason");

  laser::MeasurementSummary error{};
  laser::ExtractorConfig config{};
  const auto off_view = off.view();
  check(!laser::extractStripeRgb888(off_view, &off_view, config, profile.data(),
                                    1U, &error),
        "small output rejected");
  check(error.invalid_reason == laser::InvalidReason::kOutputTooSmall,
        "small output reason");

  auto wrong = off.view();
  wrong.width -= 1U;
  check(!laser::extractStripeRgb888(off.view(), &wrong, config, profile.data(),
                                    profile.size(), &error),
        "dimension mismatch rejected");
  check(error.invalid_reason == laser::InvalidReason::kDimensionMismatch,
        "dimension mismatch reason");
}

}  // namespace

int main() {
  testStraightAndSubpixel();
  testStep();
  testGhostAndGap();
  testClippedWhiteCoreBridgesOneStripe();
  testClippedWarmCoreBridgesOneStripe();
  testBroadBlobDoesNotSuppressStripe();
  testAllTooWideIsItsOwnReason();
  testPredictionWindowKeepsContinuity();
  testSeamCornerFit();
  testNoStripeAndErrors();

  // Regression for the uint16_t maximum: the scan loop must terminate and the
  // weighted centroid must not overflow at the far right of a wide frame.
  {
    constexpr std::uint16_t width = 65535U;
    std::vector<std::uint8_t> off(static_cast<std::size_t>(width) * 3U, 20U);
    auto on = off;
    for (std::uint32_t x = 65530U; x < 65535U; ++x) {
      on[static_cast<std::size_t>(x) * 3U] = 220U;
    }
    const laser::FrameViewRgb888 on_view{on.data(), width, 1U,
                                         static_cast<std::uint32_t>(width) * 3U};
    const laser::FrameViewRgb888 off_view{off.data(), width, 1U,
                                          static_cast<std::uint32_t>(width) * 3U};
    laser::ProfilePoint point{};
    laser::MeasurementSummary summary{};
    laser::ExtractorConfig config{};
    check(laser::extractStripeRgb888(on_view, &off_view, config, &point, 1U,
                                     &summary),
          "maximum-width extraction terminates");
    check(summary.valid && std::fabs(point.x_px - 65532.0F) < 0.25F,
          "maximum-width centroid");
  }

  if (failures != 0) {
    std::cerr << "TEST_RESULT FAIL failures=" << failures << '\n';
    return 1;
  }
  std::cout << "TEST_RESULT PASS\n";
  return 0;
}
