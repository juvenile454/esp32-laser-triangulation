# Firmware UART protocol

Implementation: [`src/main.cpp`](../src/main.cpp). Pins:
[`include/board_camera_pins.h`](../include/board_camera_pins.h). Laser wiring
and boot behaviour: [LASER_DRIVER.md](LASER_DRIVER.md). The code is the source
of truth; this page explains it.

The firmware talks over the CH340C USB-UART at **921600 baud, 8N1**. It uses no
Wi-Fi and no SD card. Commands are single ASCII bytes, and letters are accepted
in either case. Text replies are line-based; the host tools accept both `\n`
and `\r\n` line endings.

## Commands

| Command | Action | Reply |
|---|---|---|
| `P` | Camera readiness and laser state | `CAMERA_STATUS READY\|FAILED`, then `LASER ON\|OFF pin=14` |
| `C` | One VGA JPEG frame | `FRAME_BEGIN <len> <w> <h> <fmt> <fnv1a>`, `<len>` raw bytes, `FRAME_END` |
| `R` | Registered laser OFF/ON pair; only the laser state changes between the two frames | `PAIR_BEGIN laser_pin=14 settle_ms=80 settle_frames=2`, `PAIR_STATE OFF`, frame, `PAIR_STATE ON`, frame, `PAIR_END` |
| `M` | One raw RGB565 frame, then back to JPEG *(compiled, not run on hardware)* | `RGB_BEGIN …`, 614400 bytes, `RGB_END` |
| `1` / `0` | Laser on / off on GPIO14 | `LASER ON pin=14` / `LASER OFF pin=14` |
| `S` | AEC/AGC/AWB status | `CAMERA_CONTROLS aec= aec_value= agc= agc_gain= awb= awb_gain= wb_mode=` |
| `K` / `A` | Lock / re-enable exposure, gain and white-balance automation | `CAMERA_AUTOMATION LOCKED\|AUTO PASS\|FAIL …` |
| `Ennnn` | Exposure (`aec_value`, project limit 1200); locks automation first | `CAMERA_EXPOSURE PASS\|FAIL …` |
| `Gnn` | Gain (`agc_gain`, project limit 30); locks automation first | `CAMERA_GAIN PASS\|FAIL …` |
| `L` / `O` | WS2812 test light on GPIO 48 on (32/255) / off | `TEST_LIGHT RGB r= g= b= pin=48 max=64` |
| `Xrrggbb` | Test-light colour, every channel clamped to 64/255 | as above |

The firmware also prints an `ALIVE …` status line every 5 s.

`<fnv1a>` is the 32-bit FNV-1a hash of the payload, in hexadecimal. The host
rejects a frame whose length, end marker or hash does not match.

Error replies start with the command's prefix, for example
`FRAME_ERROR CAMERA_NOT_READY`, `PAIR_ERROR CAMERA_NOT_READY`,
`RGB_ERROR INVALID_FRAME` or `CAMERA_EXPOSURE ERROR invalid_argument`.

## Laser behaviour

- GPIO14 is driven low early in `setup()`. While the pin floats during reset,
  the 10 kΩ base pull-down keeps the transistor off.
- `1` keeps the laser on until `0`, a reset or power removal. **This firmware
  has no maximum on-time.** Draft PR #7 adds one and has not yet been tested on
  hardware.
- The live viewer enforces its own 60 s limit and switches the laser off when
  the browser page stops polling (see [SECURITY.md](../SECURITY.md)). This is a
  host-side mitigation, not an interlock.
- The manual switch in the laser's 5 V lead is the only authoritative
  disconnect.

## Camera lock (OV3660)

The lock is fail-closed. Verify it with `S`:

- locked: `aec=0 agc=0 awb=0 awb_gain=0`
- automatic: `aec=1 agc=1 awb=1 awb_gain=0 wb_mode=0`

On this sensor, bit 0 of register `0x3406` selects **manual** white-balance
gains, so automatic mode correctly reads `awb_gain=0`. Expecting `awb_gain=1`
in automatic mode would be wrong.

## Bring-up checks

At boot the firmware checks for an ESP32-S3, 16 MiB of flash, 8 MiB of PSRAM
and a successful 1 MiB PSRAM allocation. It then starts the OV3660 at 20 MHz
XCLK, verifies sensor PID `0x3660`, captures three warm-up frames and a test
frame, and reports `CAMERA_RESULT: PASS|FAIL`.

## Pins

The GOOUUU V1.5 mapping matches Espressif's `CAMERA_MODEL_ESP32S3_EYE`. It was
verified on hardware by SCCB sensor detection and valid VGA frame capture.
GPIO 14 is not a camera, strapping or USB pin.

Pinout sources: [profharris/GOOUUU_ESP32-S3-CAM](https://github.com/profharris/GOOUUU_ESP32-S3-CAM),
[zhuhai-esp/ESP32-S3-Goouuu-Cam](https://github.com/zhuhai-esp/ESP32-S3-Goouuu-Cam),
and Espressif's Arduino `CameraWebServer` example, profile
`CAMERA_MODEL_ESP32S3_EYE`.
