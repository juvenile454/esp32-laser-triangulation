# Agent instructions

Read this file first. Do not read all of `docs/`; open the one document the task
map points to. Do not load `docs/LAB_LOG.md` unless the task names a dated
capture or test.

## Invariants

- Keep measurements in pixels until a versioned rigid-fixture calibration exists.
- Classical extraction is the baseline. Do not add a model, TFLite or training
  pipeline. TinyML rules: `ml/README.md`.
- Do not write captures, datasets, firmware images, flash dumps or secrets into
  Git. Data root and Git boundary: `CONTRIBUTING.md`.
- Do not emit robot, welding-system or production-machine corrections.
  Boundary: `SECURITY.md`.
- Do not treat synthetic fixtures or paper-pair results as calibration.
- The manual switch is the laser interlock. The firmware watchdogs are
  compiled but not board-verified; do not rely on them. Never upload firmware or actuate
  hardware as part of an automated check. Hardware rules: `SECURITY.md` and
  `docs/LASER_DRIVER.md`.
- Python reference (`tools/laser_pipeline.py`) and C++ core
  (`lib/laser_core/`) change together; bump the contract version on behaviour
  changes.

## Current status

Snapshot: `docs/STATUS.md`. Do not reconstruct status from the README or the
lab log.

## Read by task

| Task | Read |
|---|---|
| Firmware UART, camera lock, pins | `docs/FIRMWARE_PROTOCOL.md`, then `src/main.cpp`, `include/board_camera_pins.h` |
| Stripe algorithm, `laser_core` | `lib/laser_core/src/laser_core.h`, `tests/test_laser_core.cpp` |
| Geometry, fixture, accuracy | `docs/GEOMETRY.md`, `tools/triangulation_budget.py` |
| Laser hardware, GPIO14 | `docs/LASER_DRIVER.md`, `SECURITY.md` |
| Capture or calibration series | `docs/CHECKLIST.md` |
| Live viewer | `tools/live_viewer.py`, `tools/serial_protocol.py`, `SECURITY.md` |
| TinyML | `ml/README.md` |
| Pipeline overview, teach-and-compare | `docs/ARCHITECTURE.md` |
| Verify, commit, artifacts | `CONTRIBUTING.md` |

Code and headers are the source of truth for contracts, pins and UART parsing.

## Verify

`make check` runs everything CI runs on the host; `make firmware` is the
ESP32-S3 cross-build. A firmware build is compile-only: it proves neither
runtime behaviour, PSRAM pressure, calibration nor optical accuracy.
