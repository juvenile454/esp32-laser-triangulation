// Host command line front end for the portable extractor.
//
// Exit codes:
//   0  extraction produced a valid profile
//   1  I/O error (unreadable or malformed PPM, CSV cannot be written)
//   2  usage or argument error
//   3  extraction produced no valid profile (EXTRACTION_ERROR from the core,
//      or a scanned frame whose summary is invalid; the CSV is still written
//      in the latter case)

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "laser_core.h"

namespace {

constexpr int kExitValid = 0;
constexpr int kExitIoError = 1;
constexpr int kExitUsage = 2;
constexpr int kExitInvalidProfile = 3;

// Bad command line values; everything else thrown is treated as I/O.
class UsageError : public std::runtime_error {
 public:
  explicit UsageError(const std::string& message)
      : std::runtime_error(message) {}
};

struct Image {
  std::uint16_t width = 0U;
  std::uint16_t height = 0U;
  std::vector<std::uint8_t> rgb;
};

// Strict decimal parse: digits only, no sign, no trailing characters, and
// no value above maximum (which itself must not exceed 65535).
bool parseUnsigned(const char* text, unsigned maximum, unsigned* value) {
  if (text == nullptr || *text < '0' || *text > '9') {
    return false;
  }
  char* end = nullptr;
  errno = 0;
  const unsigned long parsed = std::strtoul(text, &end, 10);
  if (errno != 0 || end == text || *end != '\0' ||
      parsed > static_cast<unsigned long>(maximum)) {
    return false;
  }
  *value = static_cast<unsigned>(parsed);
  return true;
}

bool parseFloat(const char* text, float* value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }
  char* end = nullptr;
  errno = 0;
  const float parsed = std::strtof(text, &end);
  if (errno != 0 || end == text || *end != '\0' || !std::isfinite(parsed)) {
    return false;
  }
  *value = parsed;
  return true;
}

unsigned argumentUnsigned(const char* text, unsigned minimum,
                          unsigned maximum, const char* label) {
  unsigned value = 0U;
  if (!parseUnsigned(text, maximum, &value) || value < minimum) {
    throw UsageError(std::string("invalid ") + label + " (" +
                     std::to_string(minimum) + ".." +
                     std::to_string(maximum) + "): " + text);
  }
  return value;
}

float argumentFloat(const char* text, const char* label) {
  float value = 0.0F;
  if (!parseFloat(text, &value)) {
    throw UsageError(std::string("invalid ") + label +
                     " (finite number): " + text);
  }
  return value;
}

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

unsigned headerValue(std::istream& input, const std::string& path,
                     const char* label) {
  const std::string token = readToken(input);
  unsigned value = 0U;
  if (!parseUnsigned(token.c_str(), 65535U, &value)) {
    throw std::runtime_error(path + ": invalid PPM " + label + ": " + token);
  }
  return value;
}

Image loadPpm(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open " + path);
  }
  if (readToken(input) != "P6") {
    throw std::runtime_error(path + ": only binary P6 PPM is supported");
  }
  const unsigned width = headerValue(input, path, "width");
  const unsigned height = headerValue(input, path, "height");
  const unsigned maximum = headerValue(input, path, "max value");
  if (width == 0U || height == 0U || maximum != 255U) {
    throw std::runtime_error(path + ": unsupported PPM dimensions or max value");
  }
  input.get();
  Image image;
  image.width = static_cast<std::uint16_t>(width);
  image.height = static_cast<std::uint16_t>(height);
  image.rgb.resize(static_cast<std::size_t>(width) * height * 3U);
  input.read(reinterpret_cast<char*>(image.rgb.data()),
             static_cast<std::streamsize>(image.rgb.size()));
  if (input.gcount() != static_cast<std::streamsize>(image.rgb.size())) {
    throw std::runtime_error(path + ": truncated PPM payload");
  }
  return image;
}

laser::FrameViewRgb888 viewOf(const Image& image) {
  laser::FrameViewRgb888 view;
  view.data = image.rgb.data();
  view.width = image.width;
  view.height = image.height;
  view.stride_bytes = static_cast<std::uint32_t>(image.width) * 3U;
  return view;
}

void writeProfileCsv(const char* path,
                     const std::vector<laser::ProfilePoint>& profile) {
  std::ofstream output(path);
  if (!output) {
    throw std::runtime_error(std::string("cannot create ") + path);
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
  output.flush();
  if (!output) {
    throw std::runtime_error(std::string("cannot write ") + path);
  }
  output.close();
  if (!output) {
    throw std::runtime_error(std::string("cannot close ") + path);
  }
}

void usage(const char* executable) {
  std::cerr << "Usage: " << executable
            << " ON.ppm OFF.ppm|- OUTPUT.csv [threshold [max_width "
               "[prediction_window [initial_prediction]]]]\n"
               "  OFF.ppm '-'          no OFF frame (ON-only scoring)\n"
               "  threshold            1..255 (default 20)\n"
               "  max_width            1..255 (default 16)\n"
               "  prediction_window    finite >= 0 px, 0 disables tracking\n"
               "  initial_prediction   finite px, negative means no seed\n"
               "Exit: 0 valid, 1 I/O error, 2 usage error, 3 invalid profile\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 4 || argc > 8) {
    usage(argv[0]);
    return kExitUsage;
  }
  try {
    // Parse every argument before touching the file system so that usage
    // errors are reported as such even when an input is also missing.
    laser::ExtractorConfig config{};
    if (argc >= 5) {
      config.score_threshold = static_cast<std::uint8_t>(
          argumentUnsigned(argv[4], 1U, 255U, "threshold"));
    }
    if (argc >= 6) {
      config.max_stripe_width_px = static_cast<std::uint8_t>(
          argumentUnsigned(argv[5], 1U, 255U, "max_width"));
    }
    if (argc >= 7) {
      config.prediction_window_px = argumentFloat(argv[6], "prediction_window");
      if (config.prediction_window_px < 0.0F) {
        throw UsageError(std::string("invalid prediction_window (>= 0): ") +
                         argv[6]);
      }
    }
    if (argc >= 8) {
      config.initial_prediction_px =
          argumentFloat(argv[7], "initial_prediction");
    }

    const Image on = loadPpm(argv[1]);
    const bool have_off = std::strcmp(argv[2], "-") != 0;
    Image off;
    if (have_off) {
      off = loadPpm(argv[2]);
    }
    const laser::FrameViewRgb888 on_view = viewOf(on);
    const laser::FrameViewRgb888 off_view = viewOf(off);
    std::vector<laser::ProfilePoint> profile(on.height);
    laser::MeasurementSummary summary{};
    if (!laser::extractStripeRgb888(on_view, have_off ? &off_view : nullptr,
                                    config, profile.data(), profile.size(),
                                    &summary)) {
      std::cerr << "EXTRACTION_ERROR "
                << laser::invalidReasonName(summary.invalid_reason) << "\n";
      return kExitInvalidProfile;
    }

    writeProfileCsv(argv[3], profile);
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
    std::cout.flush();
    if (!std::cout) {
      return kExitIoError;
    }
    return summary.valid ? kExitValid : kExitInvalidProfile;
  } catch (const UsageError& error) {
    std::cerr << "ERROR " << error.what() << '\n';
    usage(argv[0]);
    return kExitUsage;
  } catch (const std::exception& exception) {
    std::cerr << "ERROR " << exception.what() << '\n';
    return kExitIoError;
  }
}
