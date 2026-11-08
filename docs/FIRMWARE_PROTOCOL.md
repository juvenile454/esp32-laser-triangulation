# Firmware UART protocol

Implementation: [`src/main.cpp`](../src/main.cpp). Pins:
[`include/board_camera_pins.h`](../include/board_camera_pins.h). Laser wiring
and boot behaviour: [LASER_DRIVER.md](LASER_DRIVER.md). The code is the source
of truth; this page explains it.

The firmware talks over the CH340C USB-UART at **921600 baud, 8N1**. It uses no
Wi-Fi and no SD card. Commands are single ASCII bytes (letters in either case)
or, for switching the laser, framed line commands. Every text reply ends in
`\r\n`; the host tools also accept `\n`.

> [!IMPORTANT]
> The framed laser command, the watchdogs and the RGB565 re-initialisation
> are cross-compiled but **not yet verified on hardware**. Run the supervised
> test in [CHECKLIST.md](CHECKLIST.md#2c-firmware-laser-watchdogs) before
> relying on them.

## Laser commands

| Command | Action | Reply |
|---|---|---|
| `@LASER ON\n` | The **only** way to switch the laser on | `LASER ON pin=14` |
| `@LASER OFF\n` | Laser off | `LASER OFF pin=14` |
| `0` | Laser off, immediately; also honoured while a frame is being sent | `LASER OFF pin=14` |
| `1` | Refused, laser forced off | `LASER ERROR framed_on_required` |

Line commands start with `@`, are matched exactly and case-sensitively, may be
at most 31 characters long and must end in `\n` within 250 ms; `\r` is ignored.
Otherwise the firmware replies `ERROR line_timeout`, `ERROR line_too_long`,
`ERROR line_invalid_char` or `ERROR unknown_line_command`, and forces the laser
off. Everything ambiguous ends with the laser off.

## Other commands

| Command | Action | Reply |
|---|---|---|
| `P` | Camera readiness and laser state | `CAMERA_STATUS READY\|FAILED`, then `LASER ON\|OFF pin=14` |
| `C` | One VGA JPEG frame | `FRAME_BEGIN <len> <w> <h> <fmt> <fnv1a>`, `<len>` raw bytes, `FRAME_END` |
| `R` | Registered laser OFF/ON pair; only the laser state changes between the frames | `PAIR_BEGIN laser_pin=14 settle_ms=80 settle_frames=2`, `PAIR_STATE OFF`, frame, `PAIR_STATE ON`, frame, `PAIR_END ok=1` |
| `M` | One raw RGB565 frame; the camera driver is re-initialised for RGB565 and then back to JPEG | `RGB_BEGIN …` (same header as `FRAME_BEGIN`), 614400 bytes, `RGB_END` |
| `S` | AEC/AGC/AWB status | `CAMERA_CONTROLS aec= aec_value= agc= agc_gain= awb= awb_gain= wb_mode=` |
| `K` / `A` | Lock / re-enable exposure, gain and white-balance automation | `CAMERA_AUTOMATION LOCKED\|AUTO PASS\|FAIL …`, then `CAMERA_CONTROLS …` |
| `Ennnn` | Exposure (`aec_value`, project limit 1200); locks automation first | `CAMERA_EXPOSURE PASS\|FAIL requested= applied= readback= …`, then `CAMERA_CONTROLS …` |
| `Gnn` | Gain (`agc_gain`, project limit 30); locks automation first | `CAMERA_GAIN PASS\|FAIL …`, then `CAMERA_CONTROLS …` |
| `L` / `O` | WS2812 test light on GPIO 48 on (32/255) / off | `TEST_LIGHT RGB r= g= b= pin=48 max=64` |
| `Xrrggbb` | Test-light colour, each channel clamped to 64/255 | as above |
| CR, LF, space, tab | Ignored | — |
| any other byte | Laser forced off | `ERROR unknown_command 0xNN` |

`<fnv1a>` is the 32-bit FNV-1a hash of the payload in hexadecimal; the host
rejects a frame whose length, end marker or hash does not match. Numeric
arguments end at the first non-digit, which is then executed as the next
command (`E12\nC` gives `invalid_argument`, then a frame); each argument byte
has a 100 ms timeout.

Errors:

- `C`: `FRAME_ERROR CAMERA_NOT_READY`, `FRAME_ERROR INVALID_JPEG`, possibly
  `FRAME_WRITE_WARNING n/len`.
- `R`: `PAIR_ERROR CAMERA_NOT_READY | SETTLE_FRAME_FAILED | FRAME_FAILED |
  LASER_OFF_REQUESTED`. After an error the laser is always off.
- `M`: `RGB_ERROR CAMERA_NOT_READY | NO_SENSOR | DEINIT_FAILED |
  RGB565_INIT_FAILED | SETTLE_FRAME_FAILED | INVALID_FRAME | WRITE_FAILED |
  RESTORE_JPEG_FAILED`, possibly `RGB_WARNING CONTROLS_NOT_RESTORED`. Any error
  forces the laser off.
- Camera commands on a camera that failed bring-up: `<PREFIX> ERROR
  CAMERA_NOT_READY`; invalid arguments: `<PREFIX> ERROR invalid_argument`
  (laser off).

## Unsolicited lines

| Line | Meaning |
|---|---|
| `LASER OFF pin=14 reason=timeout on_ms=<n> max_on_ms=60000` | The laser was on for 60 s; repeating `@LASER ON` or `R` does not extend it |
| `LASER OFF pin=14 reason=host_silence silent_ms=<n> max_silent_ms=10000` | The laser was on and no byte arrived for 10 s |
| `LASER OFF pin=14 reason=protocol_error \| camera_error \| pair_error` | Switched off by an error path |
| `LASER OFF pin=14 reason=off_while_busy` | A `0` arrived during `C`/`R`/`M`; printed after the command |
| `ERROR rx_overflow dropped=<n>` | The 256-byte command queue overflowed; queue cleared, laser off |
| `ALIVE <n> \| camera= \| laser= \| free_heap= \| free_psram=` | Status every 5 s |

`reason=` lines appear only if the laser was actually on. The 60 s and 10 s
checks run between commands, so they can overshoot by the duration of one
command; a `0` is honoured at any time.

While `C`, `R` or `M` runs, the firmware drains the serial input between
2 KiB chunks. **Any `0` byte in that input switches the laser off**, even if it
was meant as an argument digit (`E0100`). This is the intended fail-safe
direction; hosts send arguments only between commands.

## Robustness

- GPIO14 is driven low and then set as output before anything else in
  `setup()`; the 10 kΩ base pull-down keeps the stage off while the pin floats.
- A shutdown handler drives GPIO14 low on `esp_restart()`, and the loop task
  watchdog (5 s) resets a hung loop.
- The serial receive buffer is 1 KiB.
- Boot reports include `LASER_SILENCE_MS:`, `LASER_SHUTDOWN_HOOK:`,
  `LOOP_WDT:` and both the physical and the configured flash size.

## Camera lock (OV3660)

The lock is fail-closed. Verify it with `S`:

- locked: `aec=0 agc=0 awb=0 awb_gain=0`
- automatic: `aec=1 agc=1 awb=1 awb_gain=0 wb_mode=0`

On this sensor, bit 0 of register `0x3406` selects **manual** white-balance
gains, so automatic mode correctly reads `awb_gain=0`. Expecting `awb_gain=1`
in automatic mode would be wrong. `M` saves these settings and writes them back
after each driver re-initialisation.

## Bring-up checks

At boot the firmware checks for an ESP32-S3, 16 MiB of flash, 8 MiB of PSRAM
and a successful 1 MiB PSRAM allocation. It then starts the OV3660 at 20 MHz
XCLK, verifies sensor PID `0x3660`, captures three warm-up frames and a test
frame, and reports `CAMERA_RESULT: PASS|FAIL`. Error paths after a successful
camera init release the driver again.

## Build

The firmware is built as C++17 (`build_unflags = -std=gnu++11`), and `src/` is
compiled with `-Wall -Wextra -Werror`; see `platformio.ini`.

## Pins

The GOOUUU V1.5 mapping matches Espressif's `CAMERA_MODEL_ESP32S3_EYE`. It was
verified on hardware by SCCB sensor detection and valid VGA frame capture.
GPIO 14 is not a camera, strapping or USB pin.

Pinout sources: [profharris/GOOUUU_ESP32-S3-CAM](https://github.com/profharris/GOOUUU_ESP32-S3-CAM),
[zhuhai-esp/ESP32-S3-Goouuu-Cam](https://github.com/zhuhai-esp/ESP32-S3-Goouuu-Cam),
and Espressif's Arduino `CameraWebServer` example, profile
`CAMERA_MODEL_ESP32S3_EYE`.
