#include <Arduino.h>
#include <driver/gpio.h>
#include <esp_camera.h>
#include <esp_err.h>
#include <esp_flash.h>
#include <esp_heap_caps.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
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
// Must match kFrameSize. The driver's resolution table is a runtime array, so
// the pairing cannot be checked at compile time; frames are checked instead.
constexpr std::size_t kFrameWidth = 640U;
constexpr std::size_t kFrameHeight = 480U;
constexpr int kJpegQuality = 12;
constexpr unsigned long kSerialBaud = 921600UL;
constexpr std::uint8_t kTestLightPin = 48U;
constexpr std::uint8_t kTestLightDefaultLevel = 32U;
constexpr std::uint8_t kTestLightMaximumLevel = 64U;
constexpr std::uint32_t kHeartbeatIntervalMs = 5000U;

// Bring-up values carried over from the initial release. They are empirical
// project choices, not datasheet requirements.
constexpr int kSensorVflip = 1;
constexpr int kSensorBrightness = 1;
constexpr int kSensorSaturation = -2;
constexpr int kWarmupFrames = 3;
constexpr std::uint32_t kWarmupFrameDelayMs = 100U;
constexpr std::uint32_t kPixformatSettleMs = 120U;
constexpr int kPixformatSettleFrames = 2;
// Gives a host that opened the port during reset time to start reading
// before the bring-up report.
constexpr std::uint32_t kBootReportDelayMs = 1200U;
constexpr std::uint32_t kLoopIdleDelayMs = 5U;

// Serial input. The UART driver buffer is enlarged so bytes that arrive
// during a long command are not lost before the poller drains them.
constexpr std::size_t kSerialRxBufferBytes = 1024U;
// Bytes drained from the UART but not yet executed, in arrival order.
constexpr std::size_t kPendingCapacity = 256U;
// Per-byte wait for fixed-width arguments (Ennnn, Gnn, Xrrggbb); the same
// value Serial.setTimeout() used before.
constexpr std::uint32_t kArgumentByteTimeoutMs = 100U;
// Line commands: '@' + at most kLineCommandMaxChars characters + '\n'. The
// whole line must arrive within kLineCommandTimeoutMs of the '@'.
constexpr std::size_t kLineCommandMaxChars = 31U;
constexpr std::uint32_t kLineCommandTimeoutMs = 250U;
// Frames are written in chunks so the emergency poller and the loop task
// watchdog are serviced during a transfer (2 KiB is about 22 ms at
// 921600 baud).
constexpr std::size_t kTransferChunkBytes = 2048U;

// Low-side laser switch: GPIO -> 1 kOhm -> 2N2222 base, emitter to GND,
// collector to the laser's negative lead. HIGH turns the laser on.
//
// Hardware-verified on the GOOUUU V1.5 header as G14, immediately above 5V. It
// is not a camera pin, a strapping pin or the USB pair. A 10 kOhm resistor from
// base to emitter keeps the laser dark while the pin floats during reset.
// The manual switch in the laser supply is the authoritative disconnect;
// everything here is a second line of defence.
constexpr std::uint8_t kLaserPin = 14U;
// Frames discarded after a laser state change so no buffered frame from the
// previous state can reach the host.
constexpr int kLaserSettleFrames = 2;
constexpr std::uint32_t kLaserSettleMs = 80U;
// Maximum continuous on-time. The watchdog in loop() switches the laser off
// once it has been on this long, so a lost host or a forgotten `0` cannot keep
// it lit indefinitely. Repeating `@LASER ON` or `R` while on does not extend
// it; only an off -> on transition restarts the clock. It is checked between
// serial commands, never inside one, so it can overshoot by the duration of
// the longest single command (an RGB565 frame takes roughly 7 s) and it never
// splits a registered pair or a frame transfer. A `0` is still honoured
// during a command by the emergency poller.
constexpr std::uint32_t kLaserMaxOnMs = 60000U;
// Host-silence cut-off: a lit laser is switched off once no byte has arrived
// for this long. The timer also restarts when a transmission completes, so a
// long frame transfer does not count as host silence.
constexpr std::uint32_t kLaserHostSilenceMs = 10000U;

// Project limits chosen at bring-up, not values cited from the OV3660
// datasheet or the esp32-camera driver source.
constexpr int kAecValueMaximum = 1200;
constexpr int kAgcGainMaximum = 30;

bool gCameraReady = false;
bool gLaserOn = false;
std::uint32_t gLaserOnSinceMs = 0;
std::uint32_t gHostActivityMs = 0;
bool gLoopWatchdogEnabled = false;
std::uint8_t gTestLightRed = 0U;
std::uint8_t gTestLightGreen = 0U;
std::uint8_t gTestLightBlue = 0U;
std::uint32_t gLastHeartbeatMs = 0;
std::uint32_t gHeartbeat = 0;

std::uint8_t gPending[kPendingCapacity]{};
std::size_t gPendingHead = 0U;
std::size_t gPendingCount = 0U;
// Bytes the poller had to drop because the pending queue was full.
std::size_t gPendingDropped = 0U;
// Set when the poller switched a lit laser off; reported once it is safe to
// print, i.e. not in the middle of a binary payload.
bool gBusyOffUnreported = false;

// ---------------------------------------------------------------- laser

void setLaser(bool on) {
  if (on && !gLaserOn) {
    gLaserOnSinceMs = millis();
  }
  gLaserOn = on;
  digitalWrite(kLaserPin, on ? HIGH : LOW);
}

void reportLaser() {
  Serial.printf("LASER %s pin=%u\r\n", gLaserOn ? "ON" : "OFF",
                static_cast<unsigned>(kLaserPin));
}

// Drives the laser off. If it was on, says so with a reason so the host
// state never silently diverges from the pin.
void forceLaserOff(const char* reason) {
  const bool wasOn = gLaserOn;
  setLaser(false);
  if (wasOn) {
    Serial.printf("LASER OFF pin=%u reason=%s\r\n",
                  static_cast<unsigned>(kLaserPin), reason);
  }
}

// Runs from esp_restart() and other orderly shutdowns. A panic or watchdog
// reset skips it; then the pin returns to its reset state (input) and the
// 10 kOhm base pull-down holds the transistor off.
void laserShutdownHandler() {
  gpio_set_level(static_cast<gpio_num_t>(kLaserPin), 0);
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
  Serial.printf("LASER OFF pin=%u reason=timeout on_ms=%lu max_on_ms=%lu\r\n",
                static_cast<unsigned>(kLaserPin),
                static_cast<unsigned long>(onMs),
                static_cast<unsigned long>(kLaserMaxOnMs));
}

void enforceLaserHostSilence() {
  if (!gLaserOn) {
    return;
  }
  const std::uint32_t silentMs = millis() - gHostActivityMs;
  if (silentMs < kLaserHostSilenceMs) {
    return;
  }
  setLaser(false);
  Serial.printf(
      "LASER OFF pin=%u reason=host_silence silent_ms=%lu "
      "max_silent_ms=%lu\r\n",
      static_cast<unsigned>(kLaserPin), static_cast<unsigned long>(silentMs),
      static_cast<unsigned long>(kLaserHostSilenceMs));
}

void enforceLaserLimits() {
  enforceLaserMaxOnTime();
  enforceLaserHostSilence();
}

// ---------------------------------------------------------- serial input

bool pendingPush(std::uint8_t value) {
  if (gPendingCount >= kPendingCapacity) {
    return false;
  }
  gPending[(gPendingHead + gPendingCount) % kPendingCapacity] = value;
  ++gPendingCount;
  return true;
}

int pendingPeek() {
  return gPendingCount == 0U ? -1 : gPending[gPendingHead];
}

int pendingPop() {
  if (gPendingCount == 0U) {
    return -1;
  }
  const int value = gPending[gPendingHead];
  gPendingHead = (gPendingHead + 1U) % kPendingCapacity;
  --gPendingCount;
  return value;
}

void pendingClear() {
  gPendingHead = 0U;
  gPendingCount = 0U;
}

bool pendingContainsOff() {
  for (std::size_t index = 0; index < gPendingCount; ++index) {
    if (gPending[(gPendingHead + index) % kPendingCapacity] == '0') {
      return true;
    }
  }
  return false;
}

// Normal intake between commands: moves bytes into the pending queue until
// it is full and leaves the rest in the UART buffer.
void receiveSerial() {
  while (gPendingCount < kPendingCapacity && Serial.available() > 0) {
    const int value = Serial.read();
    if (value < 0) {
      break;
    }
    gHostActivityMs = millis();
    pendingPush(static_cast<std::uint8_t>(value));
  }
}

// Emergency intake during long commands. Drains the UART completely so a `0`
// can never hide behind a full buffer and keeps every byte for loop() in
// arrival order. The laser goes off immediately if any `0` byte is waiting
// (newly drained or queued before the command started, also one that may turn
// out to be an argument digit) or if a byte had to be dropped, because the
// command stream can then no longer be trusted. It never switches the laser
// on.
void pollEmergencyOff() {
  while (Serial.available() > 0) {
    const int value = Serial.read();
    if (value < 0) {
      break;
    }
    gHostActivityMs = millis();
    if (!pendingPush(static_cast<std::uint8_t>(value))) {
      ++gPendingDropped;
    }
  }
  if (gLaserOn && (gPendingDropped > 0U || pendingContainsOff())) {
    setLaser(false);
    gBusyOffUnreported = true;
  }
}

void feedWatchdog() {
  if (gLoopWatchdogEnabled) {
    feedLoopWDT();
  }
}

// Called between the steps of every long operation.
void serviceBusy() {
  pollEmergencyOff();
  feedWatchdog();
}

void busyDelay(std::uint32_t durationMs) {
  const std::uint32_t start = millis();
  for (;;) {
    serviceBusy();
    const std::uint32_t elapsed = millis() - start;
    if (elapsed >= durationMs) {
      return;
    }
    delay(std::min<std::uint32_t>(kLoopIdleDelayMs, durationMs - elapsed));
  }
}

// Returns the next unconsumed byte without consuming it, waiting at most
// timeoutMs for one to arrive, or -1 on timeout.
int peekByteWithin(std::uint32_t timeoutMs) {
  const std::uint32_t start = millis();
  for (;;) {
    if (gPendingCount == 0U) {
      receiveSerial();
    }
    if (gPendingCount > 0U) {
      return pendingPeek();
    }
    if (millis() - start >= timeoutMs) {
      return -1;
    }
    delay(1);
  }
}

// Reports a malformed or unknown command and drives the laser off: anything
// ambiguous ends dark.
void protocolError(const char* line) {
  Serial.println(line);
  forceLaserOff("protocol_error");
}

// Reads a fixed-width unsigned decimal argument. Returns -1 on any malformed
// input so a truncated command can never be read as a valid value. A
// non-digit ends the argument without being consumed, so it is executed as
// the next command.
int readDecimalArgument(std::size_t digits) {
  int value = 0;
  for (std::size_t index = 0; index < digits; ++index) {
    const int next = peekByteWithin(kArgumentByteTimeoutMs);
    if (next < '0' || next > '9') {
      return -1;
    }
    pendingPop();
    value = value * 10 + (next - '0');
  }
  return value;
}

int hexValue(int value) {
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

// Same rules as readDecimalArgument: six hex digits, a non-hex byte ends the
// argument unconsumed.
bool readTestLightColor(std::uint8_t& red, std::uint8_t& green,
                        std::uint8_t& blue) {
  std::uint8_t nibbles[6]{};
  for (std::uint8_t& nibble : nibbles) {
    const int decoded = hexValue(peekByteWithin(kArgumentByteTimeoutMs));
    if (decoded < 0) {
      return false;
    }
    pendingPop();
    nibble = static_cast<std::uint8_t>(decoded);
  }
  red = static_cast<std::uint8_t>((nibbles[0] << 4) | nibbles[1]);
  green = static_cast<std::uint8_t>((nibbles[2] << 4) | nibbles[3]);
  blue = static_cast<std::uint8_t>((nibbles[4] << 4) | nibbles[5]);
  return true;
}

// Handles a line command after its '@'. The laser can only be switched on
// here: a complete, exact `@LASER ON\n` within the deadline.
void handleLineCommand() {
  char line[kLineCommandMaxChars + 1U]{};
  std::size_t length = 0U;
  bool tooLong = false;
  bool invalidChar = false;
  const std::uint32_t start = millis();
  for (;;) {
    const std::uint32_t elapsed = millis() - start;
    if (elapsed >= kLineCommandTimeoutMs) {
      protocolError("ERROR line_timeout");
      return;
    }
    const int value = peekByteWithin(kLineCommandTimeoutMs - elapsed);
    if (value < 0) {
      continue;
    }
    pendingPop();
    if (value == '\n') {
      break;
    }
    if (value == '\r') {
      continue;
    }
    if (value < 0x20 || value > 0x7E) {
      invalidChar = true;
    } else if (length >= kLineCommandMaxChars) {
      tooLong = true;
    } else {
      line[length++] = static_cast<char>(value);
    }
  }

  if (tooLong) {
    protocolError("ERROR line_too_long");
  } else if (invalidChar) {
    protocolError("ERROR line_invalid_char");
  } else if (std::strcmp(line, "LASER ON") == 0) {
    setLaser(true);
    reportLaser();
  } else if (std::strcmp(line, "LASER OFF") == 0) {
    setLaser(false);
    reportLaser();
  } else {
    protocolError("ERROR unknown_line_command");
  }
}

// Prints what the poller did while output was blocked by a command.
void reportBusyEvents() {
  if (gPendingDropped > 0U) {
    Serial.printf("ERROR rx_overflow dropped=%u\r\n",
                  static_cast<unsigned>(gPendingDropped));
    gPendingDropped = 0U;
    // The queue has a gap; its tail no longer forms valid commands.
    pendingClear();
    forceLaserOff("protocol_error");
  }
  if (gBusyOffUnreported) {
    gBusyOffUnreported = false;
    Serial.printf("LASER OFF pin=%u reason=off_while_busy\r\n",
                  static_cast<unsigned>(kLaserPin));
  }
}

// --------------------------------------------------------------- camera

void printCameraControls() {
  if (!gCameraReady) {
    Serial.println("CAMERA_CONTROLS ERROR CAMERA_NOT_READY");
    return;
  }
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
      "awb_gain=%u wb_mode=%u\r\n",
      static_cast<unsigned>(status.aec),
      static_cast<unsigned>(status.aec_value),
      static_cast<unsigned>(status.agc),
      static_cast<unsigned>(status.agc_gain),
      static_cast<unsigned>(status.awb),
      static_cast<unsigned>(status.awb_gain),
      static_cast<unsigned>(status.wb_mode));
}

void setCameraAutomation(bool enabled) {
  if (!gCameraReady) {
    Serial.println("CAMERA_AUTOMATION ERROR CAMERA_NOT_READY");
    return;
  }
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
      "retained_wb_mode=%u results=%d/%d/%d/%d\r\n",
      enabled ? "AUTO" : "LOCKED", ok ? "PASS" : "FAIL", retainedAecValue,
      retainedAgcGain, retainedWbMode, exposureResult, gainResult,
      whiteBalanceResult, awbGainResult);
  printCameraControls();
}

// Exposure and gain can only be written while their automatic controller is
// off, so both setters lock first and say so. Without this the OV3660 silently
// overwrites the value on the next frame.
void setExposureValue(int requested) {
  if (!gCameraReady) {
    Serial.println("CAMERA_EXPOSURE ERROR CAMERA_NOT_READY");
    return;
  }
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
      "results=%d/%d\r\n",
      lockResult == 0 && setResult == 0 ? "PASS" : "FAIL", requested, value,
      static_cast<unsigned>(sensor->status.aec_value), lockResult, setResult);
  printCameraControls();
}

void setGainValue(int requested) {
  if (!gCameraReady) {
    Serial.println("CAMERA_GAIN ERROR CAMERA_NOT_READY");
    return;
  }
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
      "CAMERA_GAIN %s requested=%d applied=%d readback=%u results=%d/%d\r\n",
      lockResult == 0 && setResult == 0 ? "PASS" : "FAIL", requested, value,
      static_cast<unsigned>(sensor->status.agc_gain), lockResult, setResult);
  printCameraControls();
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

void reportTestLight() {
  Serial.printf("TEST_LIGHT RGB r=%u g=%u b=%u pin=%u max=%u\r\n",
                gTestLightRed, gTestLightGreen, gTestLightBlue, kTestLightPin,
                kTestLightMaximumLevel);
}

void printBytes(const char* label, std::size_t bytes) {
  const auto wholeMiB = static_cast<unsigned>(bytes / kMiB);
  const auto hundredthsMiB =
      static_cast<unsigned>(((bytes % kMiB) * 100U) / kMiB);
  Serial.printf("%-20s %u bytes (%u.%02u MiB)\r\n", label,
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
      Serial.printf("PSRAM_TEST:          FAIL (offset=%u)\r\n",
                    static_cast<unsigned>(i));
      valid = false;
      break;
    }
  }

  heap_caps_free(buffer);
  if (valid) {
    Serial.printf("PSRAM_TEST:          PASS (%u bytes)\r\n",
                  static_cast<unsigned>(kPsramTestBytes));
  }
  return valid;
}

bool frameHasExpectedSize(const camera_fb_t* frame) {
  return frame->width == kFrameWidth && frame->height == kFrameHeight;
}

bool frameIsValidJpeg(const camera_fb_t* frame) {
  return frame != nullptr && frame->format == PIXFORMAT_JPEG &&
         frameHasExpectedSize(frame) && frame->len >= 4U &&
         frame->buf[0] == 0xFFU && frame->buf[1] == 0xD8U &&
         frame->buf[frame->len - 2U] == 0xFFU &&
         frame->buf[frame->len - 1U] == 0xD9U;
}

bool frameIsValidRgb565(const camera_fb_t* frame) {
  return frame != nullptr && frame->format == PIXFORMAT_RGB565 &&
         frameHasExpectedSize(frame) &&
         frame->len == kFrameWidth * kFrameHeight * 2U;
}

bool frameIsValid(const camera_fb_t* frame, pixformat_t format) {
  return format == PIXFORMAT_JPEG ? frameIsValidJpeg(frame)
                                  : frameIsValidRgb565(frame);
}

const char* pixformatName(pixformat_t format) {
  return format == PIXFORMAT_JPEG ? "JPEG" : "RGB565";
}

bool discardWarmupFrames(bool report) {
  for (int index = 0; index < kWarmupFrames; ++index) {
    serviceBusy();
    camera_fb_t* frame = esp_camera_fb_get();
    if (frame == nullptr) {
      if (report) {
        Serial.printf("CAMERA_WARMUP:       FAIL (frame %d)\r\n", index + 1);
      }
      return false;
    }
    esp_camera_fb_return(frame);
    busyDelay(kWarmupFrameDelayMs);
  }
  if (report) {
    Serial.printf("CAMERA_WARMUP:       PASS (%d frames)\r\n", kWarmupFrames);
  }
  return true;
}

// Brings the camera up in the given pixel format: driver init, OV3660 check,
// tuning, warm-up and one validated test frame. Any failure after a
// successful esp_camera_init() deinitialises the driver again, so the caller
// always gets either a working camera or none. report=true prints the boot
// bring-up lines; runtime re-initialisation stays silent and the caller
// reports failures in its own reply format.
bool initializeCamera(pixformat_t format, bool report) {
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
  config.pixel_format = format;
  config.frame_size = kFrameSize;
  config.jpeg_quality = kJpegQuality;
  // One buffer in PSRAM for both formats: VGA RGB565 needs 614400 bytes, and
  // WHEN_EMPTY with a single buffer avoids queueing a stale frame.
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

  if (report) {
    Serial.println("CAMERA_PIN_PROFILE:  GOOUUU V1.5 / ESP32S3_EYE");
    Serial.printf("CAMERA_CONFIG:       VGA %s, XCLK=%d Hz, FB=PSRAM\r\n",
                  pixformatName(format), kCameraXclkHz);
  }

  feedWatchdog();
  const esp_err_t error = esp_camera_init(&config);
  feedWatchdog();
  if (error != ESP_OK) {
    if (report) {
      Serial.printf("CAMERA_INIT:         FAIL (0x%08X, %s)\r\n",
                    static_cast<unsigned>(error), esp_err_to_name(error));
    }
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    if (report) {
      Serial.println("CAMERA_SENSOR:       FAIL (no sensor handle)");
    }
    esp_camera_deinit();
    return false;
  }

  if (report) {
    Serial.printf(
        "CAMERA_SENSOR_ID:    PID=0x%04X VER=0x%02X MID=0x%02X%02X\r\n",
        sensor->id.PID, sensor->id.VER, sensor->id.MIDH, sensor->id.MIDL);
  }
  if (sensor->id.PID != OV3660_PID) {
    if (report) {
      Serial.printf(
          "CAMERA_SENSOR:       FAIL (expected OV3660 PID=0x%04X)\r\n",
          OV3660_PID);
    }
    esp_camera_deinit();
    return false;
  }
  if (report) {
    Serial.println("CAMERA_SENSOR:       PASS (OV3660)");
  }

  const bool tuningOk =
      sensor->set_vflip(sensor, kSensorVflip) == 0 &&
      sensor->set_brightness(sensor, kSensorBrightness) == 0 &&
      sensor->set_saturation(sensor, kSensorSaturation) == 0;
  if (report) {
    Serial.printf("CAMERA_TUNING:       %s\r\n", tuningOk ? "PASS" : "WARN");
  }

  if (!discardWarmupFrames(report)) {
    esp_camera_deinit();
    return false;
  }

  serviceBusy();
  camera_fb_t* frame = esp_camera_fb_get();
  if (frame == nullptr) {
    if (report) {
      Serial.println("CAMERA_FRAME_TEST:   FAIL (no frame)");
    }
    esp_camera_deinit();
    return false;
  }

  const bool valid = frameIsValid(frame, format);
  if (report) {
    const std::uint32_t hash = fnv1a32(frame->buf, frame->len);
    Serial.printf(
        "CAMERA_FRAME_TEST:   %s (%ux%u, %u bytes, FNV1A=%08lX)\r\n",
        valid ? "PASS" : "FAIL", static_cast<unsigned>(frame->width),
        static_cast<unsigned>(frame->height),
        static_cast<unsigned>(frame->len), static_cast<unsigned long>(hash));
  }
  esp_camera_fb_return(frame);
  if (!valid) {
    esp_camera_deinit();
  }
  return valid;
}

// Exposure, gain and white-balance state set with K/A/E/G. Re-initialising
// the driver resets the sensor, so it is captured before a format switch and
// written back afterwards.
struct SensorControls {
  int aec = 1;
  int aecValue = 0;
  int agc = 1;
  int agcGain = 0;
  int awb = 1;
  int awbGain = 1;
  int wbMode = 0;
};

bool captureSensorControls(SensorControls& controls) {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    return false;
  }
  if (sensor->init_status != nullptr) {
    sensor->init_status(sensor);
  }
  const camera_status_t& status = sensor->status;
  controls.aec = status.aec;
  controls.aecValue = status.aec_value;
  controls.agc = status.agc;
  controls.agcGain = status.agc_gain;
  controls.awb = status.awb;
  controls.awbGain = status.awb_gain;
  controls.wbMode = status.wb_mode;
  return true;
}

bool applySensorControls(const SensorControls& controls) {
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    return false;
  }
  bool ok = sensor->set_exposure_ctrl(sensor, controls.aec) == 0;
  if (controls.aec == 0) {
    ok = sensor->set_aec_value(sensor, controls.aecValue) == 0 && ok;
  }
  ok = sensor->set_gain_ctrl(sensor, controls.agc) == 0 && ok;
  if (controls.agc == 0) {
    ok = sensor->set_agc_gain(sensor, controls.agcGain) == 0 && ok;
  }
  ok = sensor->set_whitebal(sensor, controls.awb) == 0 && ok;
  ok = sensor->set_awb_gain(sensor, controls.awbGain) == 0 && ok;
  ok = sensor->set_wb_mode(sensor, controls.wbMode) == 0 && ok;
  return ok;
}

// Writes a payload in chunks and services the emergency poller and the
// watchdog in between. Returns the number of bytes written.
std::size_t writePayload(const std::uint8_t* data, std::size_t length) {
  std::size_t written = 0U;
  while (written < length) {
    const std::size_t chunk = std::min(kTransferChunkBytes, length - written);
    const std::size_t accepted = Serial.write(data + written, chunk);
    written += accepted;
    serviceBusy();
    if (accepted != chunk) {
      break;
    }
  }
  return written;
}

// Sends one framed payload: <tag>_BEGIN header, raw bytes, <tag>_END.
// Returns false if the UART did not accept the whole payload.
bool transmitFrame(const char* tag, const camera_fb_t* frame) {
  const std::size_t frameLength = frame->len;
  const std::uint32_t hash = fnv1a32(frame->buf, frameLength);
  Serial.printf("%s_BEGIN %u %u %u %u %08lX\r\n", tag,
                static_cast<unsigned>(frameLength),
                static_cast<unsigned>(frame->width),
                static_cast<unsigned>(frame->height),
                static_cast<unsigned>(frame->format),
                static_cast<unsigned long>(hash));
  const std::size_t written = writePayload(frame->buf, frameLength);
  Serial.printf("\r\n%s_END\r\n", tag);
  Serial.flush();
  serviceBusy();
  // A completed transmission restarts the host-silence timer: the host could
  // not have been expected to talk while it was receiving.
  gHostActivityMs = millis();
  if (written != frameLength) {
    Serial.printf("%s_WRITE_WARNING %u/%u\r\n", tag,
                  static_cast<unsigned>(written),
                  static_cast<unsigned>(frameLength));
    return false;
  }
  return true;
}

bool sendFrame() {
  if (!gCameraReady) {
    Serial.println("FRAME_ERROR CAMERA_NOT_READY");
    return false;
  }

  serviceBusy();
  camera_fb_t* frame = esp_camera_fb_get();
  if (!frameIsValidJpeg(frame)) {
    Serial.println("FRAME_ERROR INVALID_JPEG");
    if (frame != nullptr) {
      esp_camera_fb_return(frame);
    }
    return false;
  }
  const bool ok = transmitFrame("FRAME", frame);
  esp_camera_fb_return(frame);
  return ok;
}

// Fetches and drops frames. Returns false as soon as one cannot be fetched,
// so a stalled camera is reported instead of being mistaken for settling.
bool discardFrames(int count) {
  for (int index = 0; index < count; ++index) {
    serviceBusy();
    camera_fb_t* frame = esp_camera_fb_get();
    if (frame == nullptr) {
      return false;
    }
    esp_camera_fb_return(frame);
  }
  serviceBusy();
  return true;
}

// True once a `0` was received during the current command. Bytes drained by
// the poller wait in the pending queue, so a queued `0` (or a dropped byte
// that might have been one) means the host asked for the laser to be off.
bool laserOffRequested() {
  return pendingContainsOff() || gPendingDropped > 0U;
}

// Aborts a registered pair: the laser ends off and the host is told why.
// laserWasOn is the state the host saw before the pair.
void abortPair(const char* reason, bool laserWasOn) {
  setLaser(false);
  Serial.printf("PAIR_ERROR %s\r\n", reason);
  if (laserWasOn && !gBusyOffUnreported) {
    Serial.printf("LASER OFF pin=%u reason=pair_error\r\n",
                  static_cast<unsigned>(kLaserPin));
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
  busyDelay(kLaserSettleMs);
  if (!discardFrames(kLaserSettleFrames)) {
    abortPair("SETTLE_FRAME_FAILED", restore);
    return;
  }
  Serial.println("PAIR_STATE OFF");
  if (!sendFrame()) {
    abortPair("FRAME_FAILED", restore);
    return;
  }

  // Never switch the laser on after the host asked for it to be off.
  if (laserOffRequested()) {
    abortPair("LASER_OFF_REQUESTED", restore);
    return;
  }
  setLaser(true);
  busyDelay(kLaserSettleMs);
  if (!discardFrames(kLaserSettleFrames)) {
    abortPair("SETTLE_FRAME_FAILED", restore);
    return;
  }
  if (!gLaserOn) {
    // The poller switched it off during settling; an ON frame now would be
    // dark.
    abortPair("LASER_OFF_REQUESTED", restore);
    return;
  }
  Serial.println("PAIR_STATE ON");
  if (!sendFrame()) {
    abortPair("FRAME_FAILED", restore);
    return;
  }

  // A pair taken while the laser was on must not restart its on-time clock,
  // or repeated `R` commands could keep it lit indefinitely. The watchdog only
  // runs between commands, so the pair itself is never cut short; if the limit
  // passed during the pair, the laser goes off right after PAIR_END. A `0`
  // received during the pair wins over the restore.
  if (restore && gLaserOn && !laserOffRequested()) {
    gLaserOnSinceMs = restoreOnSinceMs;
  } else {
    setLaser(false);
  }
  Serial.println("PAIR_END ok=1");
}

// Raw RGB565 measurement frame. JPEG stays the viewing path; measurement needs
// pixels the codec has not touched, because 4:2:2 chroma subsampling halves
// red resolution along the disparity axis.
//
// sensor->set_pixformat() alone cannot do this: the driver keeps its JPEG
// frame-buffer sizing and reports fb->format as JPEG. The driver is therefore
// deinitialised and brought up again in RGB565, then in JPEG. Exposure, gain
// and white balance are carried across both re-inits.
//
// NOT YET VERIFIED ON HARDWARE (compile-only). On any failure the laser is
// forced off and JPEG is restored; if that fails too the camera reports
// CAMERA_NOT_READY until reset.
void sendRgb565Frame() {
  if (!gCameraReady) {
    Serial.println("RGB_ERROR CAMERA_NOT_READY");
    return;
  }
  SensorControls controls;
  if (!captureSensorControls(controls)) {
    Serial.println("RGB_ERROR NO_SENSOR");
    return;
  }

  const char* failure = nullptr;
  gCameraReady = false;
  if (esp_camera_deinit() != ESP_OK) {
    failure = "DEINIT_FAILED";
  } else if (!initializeCamera(PIXFORMAT_RGB565, false)) {
    failure = "RGB565_INIT_FAILED";
  } else {
    if (!applySensorControls(controls)) {
      Serial.println("RGB_WARNING CONTROLS_NOT_RESTORED");
    }
    // Lets the restored exposure take effect before the measured frame.
    busyDelay(kPixformatSettleMs);
    if (!discardFrames(kPixformatSettleFrames)) {
      failure = "SETTLE_FRAME_FAILED";
    } else {
      serviceBusy();
      camera_fb_t* frame = esp_camera_fb_get();
      if (!frameIsValidRgb565(frame)) {
        failure = "INVALID_FRAME";
      } else if (!transmitFrame("RGB", frame)) {
        // transmitFrame already printed RGB_WRITE_WARNING.
        failure = "WRITE_FAILED";
      }
      if (frame != nullptr) {
        esp_camera_fb_return(frame);
      }
    }
    esp_camera_deinit();
  }

  if (failure != nullptr) {
    Serial.printf("RGB_ERROR %s\r\n", failure);
    forceLaserOff("camera_error");
  }

  gCameraReady = initializeCamera(PIXFORMAT_JPEG, false);
  if (!gCameraReady) {
    Serial.println("RGB_ERROR RESTORE_JPEG_FAILED");
    forceLaserOff("camera_error");
    return;
  }
  if (!applySensorControls(controls)) {
    Serial.println("RGB_WARNING CONTROLS_NOT_RESTORED");
  }
}

// ------------------------------------------------------------- commands

bool isIgnoredByte(int command) {
  return command == '\r' || command == '\n' || command == ' ' ||
         command == '\t';
}

void executeCommand(int command) {
  if (isIgnoredByte(command)) {
    return;
  }
  switch (command) {
    case 'C':
    case 'c':
      sendFrame();
      break;
    case 'R':
    case 'r':
      sendRegisteredPair();
      break;
    case 'M':
    case 'm':
      sendRgb565Frame();
      break;
    case '@':
      handleLineCommand();
      break;
    case '1':
      // A single byte is too easy to produce by accident (line noise, echo,
      // a desynchronised argument). Switching on needs `@LASER ON\n`.
      Serial.println("LASER ERROR framed_on_required");
      forceLaserOff("protocol_error");
      break;
    case '0':
      setLaser(false);
      reportLaser();
      break;
    case 'E':
    case 'e': {
      const int value = readDecimalArgument(4U);
      if (value < 0) {
        protocolError("CAMERA_EXPOSURE ERROR invalid_argument");
      } else {
        setExposureValue(value);
      }
      break;
    }
    case 'G':
    case 'g': {
      const int value = readDecimalArgument(2U);
      if (value < 0) {
        protocolError("CAMERA_GAIN ERROR invalid_argument");
      } else {
        setGainValue(value);
      }
      break;
    }
    case 'P':
    case 'p':
      Serial.printf("CAMERA_STATUS %s\r\n", gCameraReady ? "READY" : "FAILED");
      reportLaser();
      break;
    case 'S':
    case 's':
      printCameraControls();
      break;
    case 'K':
    case 'k':
      setCameraAutomation(false);
      break;
    case 'A':
    case 'a':
      setCameraAutomation(true);
      break;
    case 'L':
    case 'l':
      setTestLight(kTestLightDefaultLevel, kTestLightDefaultLevel,
                   kTestLightDefaultLevel);
      reportTestLight();
      break;
    case 'O':
    case 'o':
      setTestLight(0U, 0U, 0U);
      reportTestLight();
      break;
    case 'X':
    case 'x': {
      std::uint8_t red = 0U;
      std::uint8_t green = 0U;
      std::uint8_t blue = 0U;
      if (readTestLightColor(red, green, blue)) {
        setTestLight(red, green, blue);
        reportTestLight();
      } else {
        protocolError("TEST_LIGHT ERROR invalid_rgb_command");
      }
      break;
    }
    default: {
      char line[32]{};
      std::snprintf(line, sizeof(line), "ERROR unknown_command 0x%02X",
                    static_cast<unsigned>(command & 0xFF));
      protocolError(line);
      break;
    }
  }
}
}  // namespace

void setup() {
  // The laser is driven dark before anything else can take time: level
  // first, then direction, so the pin never drives HIGH.
  digitalWrite(kLaserPin, LOW);
  pinMode(kLaserPin, OUTPUT);
  gLaserOn = false;
  // Belt and braces next to the external 10 kOhm: keeps the base low should
  // the pin ever be reconfigured as an input. pinMode() clears pulls, so this
  // must follow it.
  gpio_pulldown_en(static_cast<gpio_num_t>(kLaserPin));
  const esp_err_t shutdownResult =
      esp_register_shutdown_handler(laserShutdownHandler);

  Serial.setRxBufferSize(kSerialRxBufferBytes);
  Serial.begin(kSerialBaud);
  setTestLight(0U, 0U, 0U);
  delay(kBootReportDelayMs);

  Serial.println();
  Serial.println("=== GOOUUU ESP32-S3-CAM V1.5 CAMERA BRING-UP ===");
  Serial.printf("Chip:                %s rev %u, %u cores, %u MHz\r\n",
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
                ESP.getCpuFreqMHz());

  // The physical size comes from the flash ID; ESP.getFlashChipSize() only
  // reports the size written into the image header.
  std::uint32_t physicalFlashBytes = 0U;
  const bool flashSizeRead =
      esp_flash_get_physical_size(esp_flash_default_chip,
                                  &physicalFlashBytes) == ESP_OK;
  const std::size_t configuredFlashBytes = ESP.getFlashChipSize();
  const std::size_t psramBytes = ESP.getPsramSize();
  printBytes("Flash (physical):", physicalFlashBytes);
  printBytes("Flash (configured):", configuredFlashBytes);
  printBytes("PSRAM heap:", psramBytes);

  const bool chipOk = std::strcmp(ESP.getChipModel(), "ESP32-S3") == 0;
  const bool flashOk =
      flashSizeRead && physicalFlashBytes == kExpectedFlashBytes;
  const bool psramSizeOk =
      psramFound() && psramBytes <= kExpectedPsramBytes &&
      psramBytes >= (kExpectedPsramBytes - kAllowedPsramReserveBytes);
  const bool psramTestOk = psramSizeOk && testPsram();
  Serial.printf("BASE_HARDWARE_CHECK: %s\r\n",
                chipOk && flashOk && psramSizeOk && psramTestOk ? "PASS"
                                                                : "FAIL");

  if (chipOk && flashOk && psramSizeOk && psramTestOk) {
    gCameraReady = initializeCamera(PIXFORMAT_JPEG, true);
  }
  Serial.printf("CAMERA_RESULT:       %s\r\n", gCameraReady ? "PASS" : "FAIL");
  Serial.printf("LASER_PIN:           GPIO %u (off at boot)\r\n",
                static_cast<unsigned>(kLaserPin));
  Serial.printf("LASER_MAX_ON_MS:     %lu\r\n",
                static_cast<unsigned long>(kLaserMaxOnMs));
  Serial.printf("LASER_SILENCE_MS:    %lu\r\n",
                static_cast<unsigned long>(kLaserHostSilenceMs));
  Serial.printf("LASER_SHUTDOWN_HOOK: %s\r\n",
                shutdownResult == ESP_OK ? "PASS" : "FAIL");
  Serial.println(
      "COMMANDS:            C=frame R=registered pair M=rgb565 frame "
      "P=status S=controls");
  Serial.println(
      "                     K=lock A=auto Ennnn=exposure Gnn=gain "
      "@LASER ON=laser on 0|@LASER OFF=laser off");
  Serial.println(
      "                     L=white O=off Xrrggbb=color");

  // The loop task watchdog starts last so the bring-up above cannot trip
  // it. Long commands feed it between chunks and camera steps.
  enableLoopWDT();
  gLoopWatchdogEnabled = esp_task_wdt_status(nullptr) == ESP_OK;
  Serial.printf("LOOP_WDT:            %s (%d s)\r\n",
                gLoopWatchdogEnabled ? "PASS" : "FAIL",
                CONFIG_ESP_TASK_WDT_TIMEOUT_S);

  gHostActivityMs = millis();
  gLastHeartbeatMs = millis();
}

void loop() {
  reportBusyEvents();
  receiveSerial();
  while (gPendingCount > 0U) {
    executeCommand(pendingPop());
    reportBusyEvents();
    // Between commands only: a command's own output is never interrupted.
    enforceLaserLimits();
    feedWatchdog();
    receiveSerial();
  }
  enforceLaserLimits();

  const std::uint32_t now = millis();
  if (now - gLastHeartbeatMs >= kHeartbeatIntervalMs) {
    gLastHeartbeatMs = now;
    Serial.printf(
        "ALIVE %lu | camera=%s | laser=%s | free_heap=%u | free_psram=%u\r\n",
        static_cast<unsigned long>(++gHeartbeat),
        gCameraReady ? "READY" : "FAILED", gLaserOn ? "ON" : "OFF",
        ESP.getFreeHeap(), ESP.getFreePsram());
  }
  delay(kLoopIdleDelayMs);
}
