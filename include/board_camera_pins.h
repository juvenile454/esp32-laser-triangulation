#pragma once

namespace board_camera {
// GOOUUU ESP32-S3-CAM V1.5. Matches Espressif CAMERA_MODEL_ESP32S3_EYE.
constexpr int kPowerDown = -1;
constexpr int kReset = -1;
constexpr int kXclk = 15;
constexpr int kSccbSda = 4;
constexpr int kSccbScl = 5;
constexpr int kData0 = 11;
constexpr int kData1 = 9;
constexpr int kData2 = 8;
constexpr int kData3 = 10;
constexpr int kData4 = 12;
constexpr int kData5 = 18;
constexpr int kData6 = 17;
constexpr int kData7 = 16;
constexpr int kVsync = 6;
constexpr int kHref = 7;
constexpr int kPclk = 13;
}  // namespace board_camera
