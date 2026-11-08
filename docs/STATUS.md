# Status

Snapshot of what is verified, blocked and open. Last hardware session:
2026-08-18. Prototype; not production metrology or robot control.

## Verified on hardware

- ESP32-S3 rev 0.2, 16 MiB flash, 8 MiB OPI PSRAM, CH340C USB-UART, OV3660
  sensor PID `0x3660`.
- OV3660 `S`/`K`/`A` camera lock (exposure, gain, white balance).
- GPIO14 / `2N2222` laser switch, driven from the localhost viewer.
- Static stripe-centre floor of **0.22 px** in unsaturated columns. Today's
  working repeatability is 2.7–3.5 px because the core clips.

## Verified in software (CI)

- C++ core and Python reference, contract v4, with row-by-row parity on twelve
  synthetic fixtures, sanitizers and a C++11 compile check.
- Host tools on Python 3.10, 3.12 and 3.13, with ruff and mypy.
- ESP32-S3 firmware cross-build. A cross-build proves neither runtime
  behaviour, PSRAM pressure nor optical accuracy.

## Blocked until the rigid fixture exists

- Pixel-to-millimetre calibration and a measured focal length.
- Final ROI, optical thresholds, on-device extraction runtime and PSRAM
  pressure.
- An unsaturated laser core (clipping costs about 10× in centre precision).
- Any production or machine correction output (never in scope; see
  [SECURITY.md](../SECURITY.md)).

## Open

- Firmware maximum laser-on time and framed laser-on command: draft PR #7,
  compile-only, needs a supervised hardware test.
- RGB565 frame (`M`): fails with the current firmware's pixel-format switch;
  PR #7 re-initialises the driver, not yet run on hardware.
- Registered pair `R` in the final fixture, and a supervised thermal soak.
