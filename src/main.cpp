#include <Arduino.h>
#include <esp_camera.h>
#include <esp_err.h>
#include <esp_heap_caps.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>

#include "board_camera_pins.h"

namespace {
constexpr std::size_t kMiB = 1024U * 1024U;
constexpr std::size_t kExpectedFlashBytes = 16U * kMiB;
constexpr std::size_t kExpectedPsramBytes = 8U * kMiB;
constexpr std::size_t kAllowedPsramReserveBytes = 64U * 1024U;
constexpr std::size_t kPsramTestBytes = 1U * kMiB;
constexpr int kCameraXclkHz = 20000000;
constexpr framesize_t kFrameSize = FRAMESIZE_VGA;
constexpr int kJpegQuality = 12;
constexpr unsigned long kSerialBaud = 921600UL;
constexpr std::uint8_t kTestLightPin = 48U;
constexpr std::uint8_t kTestLightDefaultLevel = 32U;
constexpr std::uint8_t kTestLightMaximumLevel = 64U;
constexpr std::uint32_t kHeartbeatIntervalMs = 5000U;

// Low-side laser switch: GPIO -> 1 kOhm -> 2N2222 base, emitter to GND,
// collector to the laser's negative lead. HIGH turns the laser on.
//
// Hardware-verified on the GOOUUU V1.5 header as G14, immediately above 5V. It
// is not a camera pin, a strapping pin or the USB pair. A 10 kOhm resistor from
// base to emitter keeps the laser dark while the pin floats during reset.
constexpr std::uint8_t kLaserPin = 14U;
// Frames discarded after a laser state change so no buffered frame from the
// previous state can reach the host.
constexpr int kLaserSettleFrames = 2;
constexpr std::uint32_t kLaserSettleMs = 80U;
// Maximum continuous on-time. The watchdog in loop() switches the laser off
// once it has been on this long, so a lost host or a forgotten `0` cannot keep
// it lit indefinitely. Repeating `1` while on does not extend it; only an
// off -> on transition restarts the clock. It is checked between serial
// commands, never inside one, so it can overshoot by the duration of the
// longest single command (an RGB565 frame takes roughly 7 s) and it never
// splits a registered pair or a frame transfer.
constexpr std::uint32_t kLaserMaxOnMs = 60000U;

constexpr int kAecValueMaximum = 1200;
constexpr int kAgcGainMaximum = 30;

bool gCameraReady = false;
bool gLaserOn = false;
std::uint32_t gLaserOnSinceMs = 0;
std::uint8_t gTestLightRed = 0U;
std::uint8_t gTestLightGreen = 0U;
std::uint8_t gTestLightBlue = 0U;
std::uint32_t gLastHeartbeatMs = 0;
std::uint32_t gHeartbeat = 0;

void printCameraControls() {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("CAMERA_CONTROLS ERROR no_sensor");
    return;
  }
  if (sensor->init_status != nullptr) {
    sensor->init_status(sensor);
  }
  const camera_status_t& status = sensor->status;
  Serial.printf(
      "CAMERA_CONTROLS aec=%u aec_value=%u agc=%u agc_gain=%u awb=%u "
      "awb_gain=%u wb_mode=%u\n",
      static_cast<unsigned>(status.aec),
      static_cast<unsigned>(status.aec_value),
      static_cast<unsigned>(status.agc),
      static_cast<unsigned>(status.agc_gain),
      static_cast<unsigned>(status.awb),
      static_cast<unsigned>(status.awb_gain),
      static_cast<unsigned>(status.wb_mode));
}

void setCameraAutomation(bool enabled) {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("CAMERA_AUTOMATION ERROR no_sensor");
    return;
  }
  if (sensor->init_status != nullptr) {
    sensor->init_status(sensor);
  }
  const unsigned retainedAecValue =
      static_cast<unsigned>(sensor->status.aec_value);
  const unsigned retainedAgcGain =
      static_cast<unsigned>(sensor->status.agc_gain);
  const unsigned retainedWbMode =
      static_cast<unsigned>(sensor->status.wb_mode);
  const int value = enabled ? 1 : 0;
  const int exposureResult = sensor->set_exposure_ctrl(sensor, value);
  const int gainResult = sensor->set_gain_ctrl(sensor, value);
  const int whiteBalanceResult = sensor->set_whitebal(sensor, value);
  const int awbGainResult = sensor->set_awb_gain(sensor, value);
  const bool ok = exposureResult == 0 && gainResult == 0 &&
                  whiteBalanceResult == 0 && awbGainResult == 0;
  Serial.printf(
      "CAMERA_AUTOMATION %s %s retained_aec_value=%u retained_agc_gain=%u "
      "retained_wb_mode=%u results=%d/%d/%d/%d\n",
      enabled ? "AUTO" : "LOCKED", ok ? "PASS" : "FAIL", retainedAecValue,
      retainedAgcGain, retainedWbMode, exposureResult, gainResult,
      whiteBalanceResult, awbGainResult);
  printCameraControls();
}

// Exposure and gain can only be written while their automatic controller is
// off, so both setters lock first and say so. Without this the OV3660 silently
// overwrites the value on the next frame.
void setExposureValue(int requested) {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("CAMERA_EXPOSURE ERROR no_sensor");
    return;
  }
  const int value = std::min(std::max(requested, 0), kAecValueMaximum);
  const int lockResult = sensor->set_exposure_ctrl(sensor, 0);
  const int setResult = sensor->set_aec_value(sensor, value);
  if (sensor->init_status != nullptr) {
    sensor->init_status(sensor);
  }
  Serial.printf(
      "CAMERA_EXPOSURE %s requested=%d applied=%d readback=%u "
      "results=%d/%d\n",
      lockResult == 0 && setResult == 0 ? "PASS" : "FAIL", requested, value,
      static_cast<unsigned>(sensor->status.aec_value), lockResult, setResult);
  printCameraControls();
}

void setGainValue(int requested) {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("CAMERA_GAIN ERROR no_sensor");
    return;
  }
  const int value = std::min(std::max(requested, 0), kAgcGainMaximum);
  const int lockResult = sensor->set_gain_ctrl(sensor, 0);
  const int setResult = sensor->set_agc_gain(sensor, value);
  if (sensor->init_status != nullptr) {
    sensor->init_status(sensor);
  }
  Serial.printf(
      "CAMERA_GAIN %s requested=%d applied=%d readback=%u results=%d/%d\n",
      lockResult == 0 && setResult == 0 ? "PASS" : "FAIL", requested, value,
      static_cast<unsigned>(sensor->status.agc_gain), lockResult, setResult);
  printCameraControls();
}

void setLaser(bool on) {
  if (on && !gLaserOn) {
    gLaserOnSinceMs = millis();
  }
  gLaserOn = on;
  digitalWrite(kLaserPin, on ? HIGH : LOW);
}

void reportLaser() {
  Serial.printf("LASER %s pin=%u\n", gLaserOn ? "ON" : "OFF",
                static_cast<unsigned>(kLaserPin));
}

// Switches the laser off once it has exceeded kLaserMaxOnMs and says why, so
// the host can tell a timeout from a commanded off. Unsigned subtraction keeps
// this correct across the 49-day millis() wrap.
void enforceLaserMaxOnTime() {
  if (!gLaserOn) {
    return;
  }
  const std::uint32_t onMs = millis() - gLaserOnSinceMs;
  if (onMs < kLaserMaxOnMs) {
    return;
  }
  setLaser(false);
  Serial.printf("LASER OFF pin=%u reason=timeout on_ms=%lu max_on_ms=%lu\n",
                static_cast<unsigned>(kLaserPin),
                static_cast<unsigned long>(onMs),
                static_cast<unsigned long>(kLaserMaxOnMs));
}

// Reads a fixed-width unsigned decimal argument. Returns -1 on any malformed
// input so a truncated command can never be read as a valid value.
int readDecimalArgument(std::size_t digits) {
  char encoded[4]{};
  if (digits > sizeof(encoded) ||
      Serial.readBytes(encoded, digits) != digits) {
    return -1;
  }
  int value = 0;
  for (std::size_t index = 0; index < digits; ++index) {
    if (encoded[index] < '0' || encoded[index] > '9') {
      return -1;
    }
    value = value * 10 + (encoded[index] - '0');
  }
  return value;
}

std::uint8_t limitLightChannel(std::uint8_t value) {
  return value > kTestLightMaximumLevel ? kTestLightMaximumLevel : value;
}

void setTestLight(std::uint8_t red, std::uint8_t green, std::uint8_t blue) {
  gTestLightRed = limitLightChannel(red);
  gTestLightGreen = limitLightChannel(green);
  gTestLightBlue = limitLightChannel(blue);
  neopixelWrite(kTestLightPin, gTestLightRed, gTestLightGreen,
                gTestLightBlue);
}

int hexValue(char value) {
  if (value >= '0' && value <= '9') {
    return value - '0';
  }
  if (value >= 'A' && value <= 'F') {
    return value - 'A' + 10;
  }
  if (value >= 'a' && value <= 'f') {
    return value - 'a' + 10;
  }
  return -1;
}

bool readTestLightColor(std::uint8_t& red, std::uint8_t& green,
                        std::uint8_t& blue) {
  char encoded[6]{};
  if (Serial.readBytes(encoded, sizeof(encoded)) != sizeof(encoded)) {
    return false;
  }
  std::uint8_t decoded[3]{};
  for (std::size_t index = 0; index < 3U; ++index) {
    const int high = hexValue(encoded[index * 2U]);
    const int low = hexValue(encoded[index * 2U + 1U]);
    if (high < 0 || low < 0) {
      return false;
    }
    decoded[index] = static_cast<std::uint8_t>((high << 4) | low);
  }
  red = decoded[0];
  green = decoded[1];
  blue = decoded[2];
  return true;
}

void printBytes(const char* label, std::size_t bytes) {
  const auto wholeMiB = static_cast<unsigned>(bytes / kMiB);
  const auto hundredthsMiB =
      static_cast<unsigned>(((bytes % kMiB) * 100U) / kMiB);
  Serial.printf("%-20s %u bytes (%u.%02u MiB)\n", label,
                static_cast<unsigned>(bytes), wholeMiB, hundredthsMiB);
}

std::uint32_t fnv1a32(const std::uint8_t* data, std::size_t length) {
  std::uint32_t hash = 2166136261UL;
  for (std::size_t i = 0; i < length; ++i) {
    hash ^= data[i];
    hash *= 16777619UL;
  }
  return hash;
}

bool testPsram() {
  auto* buffer = static_cast<std::uint8_t*>(
      heap_caps_malloc(kPsramTestBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (buffer == nullptr) {
    Serial.println("PSRAM_TEST:          FAIL (allocation)");
    return false;
  }

  for (std::size_t i = 0; i < kPsramTestBytes; ++i) {
    buffer[i] = static_cast<std::uint8_t>((i ^ (i >> 8U) ^ 0xA5U) & 0xFFU);
  }

  bool valid = true;
  for (std::size_t i = 0; i < kPsramTestBytes; ++i) {
    const auto expected =
        static_cast<std::uint8_t>((i ^ (i >> 8U) ^ 0xA5U) & 0xFFU);
    if (buffer[i] != expected) {
      Serial.printf("PSRAM_TEST:          FAIL (offset=%u)\n",
                    static_cast<unsigned>(i));
      valid = false;
      break;
    }
  }

  heap_caps_free(buffer);
  if (valid) {
    Serial.printf("PSRAM_TEST:          PASS (%u bytes)\n",
                  static_cast<unsigned>(kPsramTestBytes));
  }
  return valid;
}

bool frameIsValidJpeg(const camera_fb_t* frame) {
  return frame != nullptr && frame->format == PIXFORMAT_JPEG &&
         frame->width == 640U && frame->height == 480U && frame->len >= 4U &&
         frame->buf[0] == 0xFFU && frame->buf[1] == 0xD8U &&
         frame->buf[frame->len - 2U] == 0xFFU &&
         frame->buf[frame->len - 1U] == 0xD9U;
}

bool discardWarmupFrames() {
  for (int index = 0; index < 3; ++index) {
    camera_fb_t* frame = esp_camera_fb_get();
    if (frame == nullptr) {
      Serial.printf("CAMERA_WARMUP:       FAIL (frame %d)\n", index + 1);
      return false;
    }
    esp_camera_fb_return(frame);
    delay(100);
  }
  Serial.println("CAMERA_WARMUP:       PASS (3 frames)");
  return true;
}

bool initializeCamera() {
  camera_config_t config{};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = board_camera::kData0;
  config.pin_d1 = board_camera::kData1;
  config.pin_d2 = board_camera::kData2;
  config.pin_d3 = board_camera::kData3;
  config.pin_d4 = board_camera::kData4;
  config.pin_d5 = board_camera::kData5;
  config.pin_d6 = board_camera::kData6;
  config.pin_d7 = board_camera::kData7;
  config.pin_xclk = board_camera::kXclk;
  config.pin_pclk = board_camera::kPclk;
  config.pin_vsync = board_camera::kVsync;
  config.pin_href = board_camera::kHref;
  config.pin_sccb_sda = board_camera::kSccbSda;
  config.pin_sccb_scl = board_camera::kSccbScl;
  config.pin_pwdn = board_camera::kPowerDown;
  config.pin_reset = board_camera::kReset;
  config.xclk_freq_hz = kCameraXclkHz;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = kFrameSize;
  config.jpeg_quality = kJpegQuality;
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

  Serial.println("CAMERA_PIN_PROFILE:  GOOUUU V1.5 / ESP32S3_EYE");
  Serial.printf("CAMERA_CONFIG:       VGA JPEG, XCLK=%d Hz, FB=PSRAM\n",
                kCameraXclkHz);

  const esp_err_t error = esp_camera_init(&config);
  if (error != ESP_OK) {
    Serial.printf("CAMERA_INIT:         FAIL (0x%08X, %s)\n",
                  static_cast<unsigned>(error), esp_err_to_name(error));
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("CAMERA_SENSOR:       FAIL (no sensor handle)");
    return false;
  }

  Serial.printf("CAMERA_SENSOR_ID:    PID=0x%04X VER=0x%02X MID=0x%02X%02X\n",
                sensor->id.PID, sensor->id.VER, sensor->id.MIDH,
                sensor->id.MIDL);
  if (sensor->id.PID != OV3660_PID) {
    Serial.printf("CAMERA_SENSOR:       FAIL (expected OV3660 PID=0x%04X)\n",
                  OV3660_PID);
    return false;
  }
  Serial.println("CAMERA_SENSOR:       PASS (OV3660)");

  const bool tuningOk = sensor->set_vflip(sensor, 1) == 0 &&
                        sensor->set_brightness(sensor, 1) == 0 &&
                        sensor->set_saturation(sensor, -2) == 0;
  Serial.printf("CAMERA_TUNING:       %s\n", tuningOk ? "PASS" : "WARN");

  if (!discardWarmupFrames()) {
    return false;
  }

  camera_fb_t* frame = esp_camera_fb_get();
  if (frame == nullptr) {
    Serial.println("CAMERA_FRAME_TEST:   FAIL (no frame)");
    return false;
  }

  const bool valid = frameIsValidJpeg(frame);
  const std::uint32_t hash = fnv1a32(frame->buf, frame->len);
  Serial.printf(
      "CAMERA_FRAME_TEST:   %s (%ux%u, %u bytes, FNV1A=%08lX)\n",
      valid ? "PASS" : "FAIL", static_cast<unsigned>(frame->width),
      static_cast<unsigned>(frame->height), static_cast<unsigned>(frame->len),
      static_cast<unsigned long>(hash));
  esp_camera_fb_return(frame);
  return valid;
}

void sendFrame() {
  if (!gCameraReady) {
    Serial.println("FRAME_ERROR CAMERA_NOT_READY");
    return;
  }

  camera_fb_t* frame = esp_camera_fb_get();
  if (!frameIsValidJpeg(frame)) {
    Serial.println("FRAME_ERROR INVALID_JPEG");
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
    return;
  }

  const std::size_t frameLength = frame->len;
  const std::uint32_t hash = fnv1a32(frame->buf, frameLength);
  Serial.printf("FRAME_BEGIN %u %u %u %u %08lX\r\n",
                static_cast<unsigned>(frameLength),
                static_cast<unsigned>(frame->width),
                static_cast<unsigned>(frame->height),
                static_cast<unsigned>(frame->format),
                static_cast<unsigned long>(hash));
  const std::size_t written = Serial.write(frame->buf, frameLength);
  Serial.print("\r\nFRAME_END\r\n");
  Serial.flush();
  esp_camera_fb_return(frame);

  if (written != frameLength) {
    Serial.printf("FRAME_WRITE_WARNING %u/%u\r\n",
                  static_cast<unsigned>(written),
                  static_cast<unsigned>(frameLength));
  }
}

void discardFrames(int count) {
  for (int index = 0; index < count; ++index) {
    camera_fb_t* frame = esp_camera_fb_get();
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
  }
}

// Captures one registered ON/OFF pair with only the laser state changing
// between the two frames. Doing this on the device removes the hand-covered
// aperture and the seconds of drift that came with it.
void sendRegisteredPair() {
  if (!gCameraReady) {
    Serial.println("PAIR_ERROR CAMERA_NOT_READY");
    return;
  }
  const bool restore = gLaserOn;
  const std::uint32_t restoreOnSinceMs = gLaserOnSinceMs;
  Serial.printf("PAIR_BEGIN laser_pin=%u settle_ms=%u settle_frames=%d\r\n",
                static_cast<unsigned>(kLaserPin),
                static_cast<unsigned>(kLaserSettleMs), kLaserSettleFrames);

  setLaser(false);
  delay(kLaserSettleMs);
  discardFrames(kLaserSettleFrames);
  Serial.println("PAIR_STATE OFF");
  sendFrame();

  setLaser(true);
  delay(kLaserSettleMs);
  discardFrames(kLaserSettleFrames);
  Serial.println("PAIR_STATE ON");
  sendFrame();

  setLaser(restore);
  // A pair taken while the laser was on must not restart its on-time clock,
  // or repeated `R` commands could keep it lit indefinitely. The watchdog only
  // runs between commands, so the pair itself is never cut short; if the limit
  // passed during the pair, the laser goes off right after PAIR_END.
  if (restore) {
    gLaserOnSinceMs = restoreOnSinceMs;
  }
  Serial.println("PAIR_END");
}

// Raw RGB565 measurement frame. JPEG stays the viewing path; measurement needs
// pixels the codec has not touched, because 4:2:2 chroma subsampling halves
// red resolution along the disparity axis.
//
// NOT YET VERIFIED ON HARDWARE. Runtime pixel-format switching re-allocates the
// frame buffer; every step is checked and JPEG is always restored.
void sendRgb565Frame() {
  if (!gCameraReady) {
    Serial.println("RGB_ERROR CAMERA_NOT_READY");
    return;
  }
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("RGB_ERROR NO_SENSOR");
    return;
  }
  if (sensor->set_pixformat(sensor, PIXFORMAT_RGB565) != 0) {
    Serial.println("RGB_ERROR SET_RGB565_FAILED");
    sensor->set_pixformat(sensor, PIXFORMAT_JPEG);
    return;
  }
  delay(120);
  discardFrames(2);

  camera_fb_t* frame = esp_camera_fb_get();
  if (frame == nullptr || frame->format != PIXFORMAT_RGB565 ||
      frame->len != static_cast<std::size_t>(frame->width) * frame->height * 2U) {
    Serial.println("RGB_ERROR INVALID_FRAME");
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
    sensor->set_pixformat(sensor, PIXFORMAT_JPEG);
    delay(120);
    discardFrames(2);
    return;
  }

  const std::size_t frameLength = frame->len;
  const std::uint32_t hash = fnv1a32(frame->buf, frameLength);
  Serial.printf("RGB_BEGIN %u %u %u %u %08lX\r\n",
                static_cast<unsigned>(frameLength),
                static_cast<unsigned>(frame->width),
                static_cast<unsigned>(frame->height),
                static_cast<unsigned>(frame->format),
                static_cast<unsigned long>(hash));
  const std::size_t written = Serial.write(frame->buf, frameLength);
  Serial.print("\r\nRGB_END\r\n");
  Serial.flush();
  esp_camera_fb_return(frame);

  if (written != frameLength) {
    Serial.printf("RGB_WRITE_WARNING %u/%u\r\n",
                  static_cast<unsigned>(written),
                  static_cast<unsigned>(frameLength));
  }

  if (sensor->set_pixformat(sensor, PIXFORMAT_JPEG) != 0) {
    Serial.println("RGB_ERROR RESTORE_JPEG_FAILED");
    gCameraReady = false;
    return;
  }
  delay(120);
  discardFrames(2);
}
}  // namespace

void setup() {
  Serial.begin(kSerialBaud);
  Serial.setTimeout(100U);
  // The laser is driven dark before anything else can take time.
  pinMode(kLaserPin, OUTPUT);
  setLaser(false);
  setTestLight(0U, 0U, 0U);
  delay(1200);

  Serial.println();
  Serial.println("=== GOOUUU ESP32-S3-CAM V1.5 CAMERA BRING-UP ===");
  Serial.printf("Chip:                %s rev %u, %u cores, %u MHz\n",
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
                ESP.getCpuFreqMHz());

  const std::size_t flashBytes = ESP.getFlashChipSize();
  const std::size_t psramBytes = ESP.getPsramSize();
  printBytes("Flash:", flashBytes);
  printBytes("PSRAM heap:", psramBytes);

  const bool chipOk = std::strcmp(ESP.getChipModel(), "ESP32-S3") == 0;
  const bool flashOk = flashBytes == kExpectedFlashBytes;
  const bool psramSizeOk =
      psramFound() && psramBytes <= kExpectedPsramBytes &&
      psramBytes >= (kExpectedPsramBytes - kAllowedPsramReserveBytes);
  const bool psramTestOk = psramSizeOk && testPsram();
  Serial.printf("BASE_HARDWARE_CHECK: %s\n",
                chipOk && flashOk && psramSizeOk && psramTestOk ? "PASS"
                                                                  : "FAIL");

  if (chipOk && flashOk && psramSizeOk && psramTestOk) {
    gCameraReady = initializeCamera();
  }
  Serial.printf("CAMERA_RESULT:       %s\n", gCameraReady ? "PASS" : "FAIL");
  Serial.printf("LASER_PIN:           GPIO %u (off at boot)\n",
                static_cast<unsigned>(kLaserPin));
  Serial.printf("LASER_MAX_ON_MS:     %lu\n",
                static_cast<unsigned long>(kLaserMaxOnMs));
  Serial.println(
      "COMMANDS:            C=frame R=registered pair M=rgb565 frame "
      "P=status S=controls");
  Serial.println(
      "                     K=lock A=auto Ennnn=exposure Gnn=gain "
      "1=laser on 0=laser off");
  Serial.println(
      "                     L=white O=off Xrrggbb=color");
  gLastHeartbeatMs = millis();
}

void loop() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 'C' || command == 'c') {
      sendFrame();
    } else if (command == 'R' || command == 'r') {
      sendRegisteredPair();
    } else if (command == 'M' || command == 'm') {
      sendRgb565Frame();
    } else if (command == '1') {
      setLaser(true);
      reportLaser();
    } else if (command == '0') {
      setLaser(false);
      reportLaser();
    } else if (command == 'E' || command == 'e') {
      const int value = readDecimalArgument(4U);
      if (value < 0) {
        Serial.println("CAMERA_EXPOSURE ERROR invalid_argument");
      } else {
        setExposureValue(value);
      }
    } else if (command == 'G' || command == 'g') {
      const int value = readDecimalArgument(2U);
      if (value < 0) {
        Serial.println("CAMERA_GAIN ERROR invalid_argument");
      } else {
        setGainValue(value);
      }
    } else if (command == 'P' || command == 'p') {
      Serial.printf("CAMERA_STATUS %s\n", gCameraReady ? "READY" : "FAILED");
      reportLaser();
    } else if (command == 'S' || command == 's') {
      printCameraControls();
    } else if (command == 'K' || command == 'k') {
      setCameraAutomation(false);
    } else if (command == 'A' || command == 'a') {
      setCameraAutomation(true);
    } else if (command == 'L' || command == 'l') {
      setTestLight(kTestLightDefaultLevel, kTestLightDefaultLevel,
                   kTestLightDefaultLevel);
      Serial.printf("TEST_LIGHT RGB r=%u g=%u b=%u pin=%u max=%u\n",
                    gTestLightRed, gTestLightGreen, gTestLightBlue,
                    kTestLightPin, kTestLightMaximumLevel);
    } else if (command == 'O' || command == 'o') {
      setTestLight(0U, 0U, 0U);
      Serial.printf("TEST_LIGHT RGB r=0 g=0 b=0 pin=%u max=%u\n",
                    kTestLightPin, kTestLightMaximumLevel);
    } else if (command == 'X' || command == 'x') {
      std::uint8_t red = 0U;
      std::uint8_t green = 0U;
      std::uint8_t blue = 0U;
      if (readTestLightColor(red, green, blue)) {
        setTestLight(red, green, blue);
        Serial.printf("TEST_LIGHT RGB r=%u g=%u b=%u pin=%u max=%u\n",
                      gTestLightRed, gTestLightGreen, gTestLightBlue,
                      kTestLightPin, kTestLightMaximumLevel);
      } else {
        Serial.println("TEST_LIGHT ERROR invalid_rgb_command");
      }
    }
    // Between commands only: a command's own output is never interrupted.
    enforceLaserMaxOnTime();
  }
  enforceLaserMaxOnTime();

  const std::uint32_t now = millis();
  if (now - gLastHeartbeatMs >= kHeartbeatIntervalMs) {
    gLastHeartbeatMs = now;
    Serial.printf(
        "ALIVE %lu | camera=%s | laser=%s | free_heap=%u | free_psram=%u\n",
        static_cast<unsigned long>(++gHeartbeat),
        gCameraReady ? "READY" : "FAILED", gLaserOn ? "ON" : "OFF",
        ESP.getFreeHeap(), ESP.getFreePsram());
  }
  delay(5);
}
