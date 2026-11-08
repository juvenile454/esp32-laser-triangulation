# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

### Security

- The live viewer rejects foreign `Host` headers (421), cross-origin and
  non-JSON `POST`s and requests without its per-start token. Before, any web
  page open in the same browser could switch the laser on. Responses carry a
  strict Content-Security-Policy and the page script moved to
  `tools/live_viewer.js`.
- The viewer switches the laser off after `--laser-max-on-s` (default 60 s)
  and when the page stops polling for 5 s. On shutdown it waits for the worker
  and reports `LASER STATE UNKNOWN` with exit code 1 if it cannot confirm off.
- `fit_calibration.py` validates the fixture revision before writing it into a
  C++ string literal.

### Changed

- **Contract v4.** The continuity prediction follows only tracked selections,
  so a one-row gap can no longer hand the window to a foreign blob. Non-finite
  or out-of-range `ghost_ratio`, `minimum_valid_ratio`, `prediction_window_px`
  and `initial_prediction_px` are rejected. The seam fit skips non-finite
  points and rejects profiles longer than 65535 points.
- `triangulation_budget.py --from-measured-shift` inverts the model of the
  selected `--layout`. It always used the tilted-laser model before, which put
  the focal length about a third too low in the default tilted-camera layout.
  The three focal-length sources are now mutually exclusive.
- `fit_calibration.py` samples candidate subsets at random (fixed seed), so an
  outlier at the start of the CSV is rejected. It writes the firmware header
  only with `--fixture-revision` (was `--version`, still accepted with a
  warning) and always emits valid float literals.
- `capture_pair.py` records `calibration_grade_real` only with
  `--calibration-grade` and a confirmed camera lock. It validates
  `--sample-id`, refuses to overwrite an existing sample without
  `--overwrite` and checks the written metadata against the schema.
- `validate_dataset.py` validates against the JSON schema first. The schema
  rejects unknown fields and enforces the calibration-grade fields.
- `live_viewer.py` uses `--port` like the capture tools (`--device` still
  works), supports `--bind ::1` and saves to `$LASER_DATA_DIR/live` or the
  repository's `data/live`.
- `analyze_laser_pair.py` exits with 3 for an invalid profile (was 2, which
  clashed with usage errors) and gained `--prediction-window` and
  `--initial-prediction`. `laser_cli` uses the same exit codes and parses
  its arguments strictly.
- All tools validate their numeric options and end expected errors with a
  one-line message instead of a traceback. JSON outputs never contain NaN.
- Python profile CSVs write booleans as `1`/`0`, like the C++ CLI.
- `make check` runs the C++ tests, sanitizers, a C++11 compile check, ruff,
  mypy and the Python tests; CI runs it on Python 3.10, 3.12 and 3.13.
  Requirements are split into host, dev and firmware files.
- README figures are regenerated from the code. The height-noise chart had
  a wrong title and a reference point that matched no curve.
- `docs/HARDWARE_RETURN_CHECKLIST.md` is now `docs/CHECKLIST.md`. New:
  `docs/STATUS.md`, `docs/FIRMWARE_PROTOCOL.md`, `examples/README.md` and a
  code of conduct.

### Fixed

- `docs/GEOMETRY.md` mixed the two layout models without saying so; every
  table now names its layout.
- The README headline quoted the 0.22 px floor as the working repeatability.
  Today's value with the clipped core is 2.7–3.5 px.

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
