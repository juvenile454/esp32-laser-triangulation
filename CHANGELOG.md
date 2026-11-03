# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

## [0.1.0] - 2026-11-03

First public release of the research prototype.

### Added

- ESP32-S3 firmware for the GOOUUU ESP32-S3-CAM V1.5 with OV3660: framed JPEG
  transport, registered laser ON/OFF pair capture, RGB565 measurement frame,
  exposure/gain setters and AEC/AGC/AWB lock.
- GPIO14 / `2N2222` laser driver, verified on hardware.
- Portable, allocation-free C++17 stripe extractor (contract v3) with continuity
  tracking, over-wide rejection before ranking and a two-leg seam-corner fit.
- Python reference implementation with C++/Python parity tests, sanitizer runs
  and twelve synthetic regression fixtures.
- Host tools for capture, live viewing, offline analysis, dataset validation,
  calibration fitting, triangulation budgeting and part-locating analysis.
- Versioned capture-metadata schema and real-capture example metadata.
- Documentation: architecture, measurement geometry, laser driver, hardware
  checklist and a dated lab log.

### Changed

- The localhost viewer interface is now in English, and its page lives in
  `tools/live_viewer.html`.
- Data-writing tools use `$LASER_DATA_DIR` (default `./data`) instead of a fixed
  local path. `validate_dataset.py` gained `--data-root` for relative image paths.
- `capture_pair.py` records image file names relative to their metadata file.
- Serial ports are no longer fixed in `platformio.ini`; pass `--upload-port`.

[Unreleased]: https://github.com/juvenile454/esp32-laser-triangulation/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/juvenile454/esp32-laser-triangulation/releases/tag/v0.1.0
