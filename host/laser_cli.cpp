#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "laser_core.h"

namespace {

struct Image {
  std::uint16_t width = 0U;
  std::uint16_t height = 0U;
  std::vector<std::uint8_t> rgb;
};

std::string readToken(std::istream& input) {
  std::string token;
  while (input >> token) {
    if (!token.empty() && token[0] == '#') {
      input.ignore(std::numeric_limits<std::streamsize>::max(), '\n');
      continue;
    }
    return token;
  }
  throw std::runtime_error("unexpected end of PPM header");
}

Image loadPpm(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open " + path);
  }
  if (readToken(input) != "P6") {
    throw std::runtime_error("only binary P6 PPM is supported");
  }
  const unsigned width = std::stoul(readToken(input));
  const unsigned height = std::stoul(readToken(input));
  const unsigned maximum = std::stoul(readToken(input));
  if (width == 0U || height == 0U || width > 65535U || height > 65535U ||
      maximum != 255U) {
    throw std::runtime_error("unsupported PPM dimensions or max value");
  }
  input.get();
  Image image;
  image.width = static_cast<std::uint16_t>(width);
  image.height = static_cast<std::uint16_t>(height);
  image.rgb.resize(static_cast<std::size_t>(width) * height * 3U);
  input.read(reinterpret_cast<char*>(image.rgb.data()),
             static_cast<std::streamsize>(image.rgb.size()));
  if (input.gcount() != static_cast<std::streamsize>(image.rgb.size())) {
    throw std::runtime_error("truncated PPM payload");
  }
  return image;
}

unsigned parseUnsigned(const char* value, const char* label) {
  char* end = nullptr;
  errno = 0;
  const unsigned long parsed = std::strtoul(value, &end, 10);
  if (errno != 0 || end == value || *end != '\0' || parsed > 65535UL) {
    throw std::runtime_error(std::string("invalid ") + label + ": " + value);
  }
  return static_cast<unsigned>(parsed);
}

void usage(const char* executable) {
  std::cerr << "Usage: " << executable
            << " ON.ppm OFF.ppm OUTPUT.csv [threshold [max_width "
               "[prediction_window [initial_prediction]]]]\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 4 || argc > 8) {
    usage(argv[0]);
    return 64;
  }
  try {
    const Image on = loadPpm(argv[1]);
    const Image off = loadPpm(argv[2]);
    laser::ExtractorConfig config{};
    if (argc >= 5) {
      config.score_threshold = static_cast<std::uint8_t>(
          std::min(255U, parseUnsigned(argv[4], "threshold")));
    }
    if (argc >= 6) {
      config.max_stripe_width_px = static_cast<std::uint8_t>(
          std::min(255U, parseUnsigned(argv[5], "max_width")));
    }
    if (argc >= 7) {
      config.prediction_window_px =
          static_cast<float>(parseUnsigned(argv[6], "prediction_window"));
    }
    if (argc >= 8) {
      config.initial_prediction_px =
          static_cast<float>(parseUnsigned(argv[7], "initial_prediction"));
    }
    config.minimum_valid_ratio = 0.10F;
    const laser::FrameViewRgb888 on_view{
        on.rgb.data(), on.width, on.height,
        static_cast<std::uint32_t>(on.width) * 3U};
    const laser::FrameViewRgb888 off_view{
        off.rgb.data(), off.width, off.height,
        static_cast<std::uint32_t>(off.width) * 3U};
    std::vector<laser::ProfilePoint> profile(on.height);
    laser::MeasurementSummary summary{};
    if (!laser::extractStripeRgb888(on_view, &off_view, config, profile.data(),
                                    profile.size(), &summary)) {
      std::cerr << "EXTRACTION_ERROR "
                << laser::invalidReasonName(summary.invalid_reason) << "\n";
      return 2;
    }

    std::ofstream output(argv[3]);
    if (!output) {
      throw std::runtime_error(std::string("cannot create ") + argv[3]);
    }
    output << "x_px,y_px,peak_score,width_px,valid,saturated,"
              "secondary_candidate,too_wide\n";
    output << std::fixed << std::setprecision(6);
    for (const auto& point : profile) {
      output << point.x_px << ',' << point.y_px << ','
             << static_cast<unsigned>(point.peak_score) << ','
             << static_cast<unsigned>(point.width_px) << ',' << point.valid << ','
             << point.saturated << ',' << point.secondary_candidate << ','
             << point.too_wide << '\n';
    }
    std::cout << std::fixed << std::setprecision(6)
              << "CPP_EXTRACTION_RESULT valid=" << summary.valid
              << " reason=" << laser::invalidReasonName(summary.invalid_reason)
              << " points=" << summary.profile_points
              << " valid_points=" << summary.valid_points
              << " too_wide_points=" << summary.too_wide_points
              << " valid_ratio=" << summary.valid_ratio
              << " mean_peak=" << summary.mean_peak_score
              << " confidence=" << summary.confidence << '\n';

    laser::SeamCorner corner{};
    const laser::SeamFitConfig seam_config{};
    if (laser::fitSeamCorner(profile.data(), profile.size(), seam_config,
                             &corner)) {
      std::cout << "CPP_SEAM_CORNER valid=" << corner.valid
                << " x_px=" << corner.x_px << " y_px=" << corner.y_px
                << " included_angle_deg=" << corner.included_angle_deg
                << " rms_px=" << corner.rms_px
                << " first_leg_points=" << corner.first_leg_points
                << " second_leg_points=" << corner.second_leg_points << '\n';
    } else {
      std::cout << "CPP_SEAM_CORNER valid=0 reason=insufficient_profile\n";
    }
    return summary.valid ? 0 : 2;
  } catch (const std::exception& exception) {
    std::cerr << "ERROR " << exception.what() << '\n';
    return 1;
  }
}
