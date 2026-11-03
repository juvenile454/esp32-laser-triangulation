# Lab log

A dated record of what was built, measured and verified. Entries describe the
state on their date; later entries supersede earlier numbers. Paths under
`$LASER_DATA_DIR` are local capture and analysis artifacts. They are kept out
of Git and are not published.

Started: 2026-08-08 · Last hardware update: 2026-08-18

## First optical smoke test (2026-08-08)

- The laser module is marked `CHT1230`, `650 nm`, `Max Power: 5mW`, `Class IIIa`.
  The seller specifies a stable 5 V supply.
- With the board powered over USB, its `5V` pin measured 4.99 V against `GND`.
- For the first smoke test the laser was wired directly to the board rail (red to
  `5V`, black to `GND`, polarity taken from the lead colours). It ran for about
  five minutes without observed heating or instability. This temporary
  connection was later replaced by the GPIO14 driver (see 2026-08-18 below).
- The first laser-on frame was captured at 640 × 480 at about 5 FPS
  (`$LASER_DATA_DIR/live/live_20260808_215611_000273.jpg`). The line was clearly
  visible and stepped at the target edge, which confirmed the basic optical
  geometry. Its core was saturated and the scene softly focused.
- A laser-off counterpart was taken by covering the laser aperture without moving
  camera or target (`$LASER_DATA_DIR/live/live_20260808_220047_001653.jpg`). Both
  laser traces vanished in the OFF frame. Global residual differences showed that
  automatic exposure, white balance and JPEG noise must be controlled before
  calibration-grade subtraction.

The verified factory-flash backup of the board is kept outside this repository.

## Scope completed without the chip (2026-08-13)

- versioned sample metadata schema;
- dependency-light Python reference extractor for registered laser ON/OFF RGB images;
- score image, profile CSV, overlay and JSON diagnostics;
- ten deterministic synthetic fixtures (twelve since contract v3) with pixel ground truth, including neutral-white and warm yellow-white clipped-core regressions;
- allocation-free portable C++ stripe core and PPM host CLI;
- native unit tests, Python/C++ parity tests and sanitizer run;
- metadata/hash/dimension validator for real and synthetic sample layouts;
- robust linear/quadratic calibration fitter with validation residuals, SVG plot and generated C++ header;
- provisional 69-float TinyML feature contract without pretending to have a useful model;
- ESP32 PlatformIO compile check of the actual portable extraction call;
- hardware-verified serial commands to report, lock and re-enable camera automation.

## Provisional real-pair result

Input is the previously captured temporary-geometry ON/OFF pair. With ROI `(180,0)..(420,480)`, score threshold 25 and maximum stripe width 18:

- 480 scan rows;
- 324 accepted points (67.5% coverage);
- 74 accepted rows marked saturated;
- 111 rows with a relevant secondary candidate;
- heuristic confidence 0.349.

Visual inspection confirms that the diagonal main stripe and its lower kink are traced. Background residuals remain because the camera controls changed globally between ON and OFF. This is an algorithm smoke test only, not a measurement result.

Artifacts:

- `$LASER_DATA_DIR/analysis/provisional-real-pair-v1/overlay.png`
- `$LASER_DATA_DIR/analysis/provisional-real-pair-v1/score.png`
- `$LASER_DATA_DIR/analysis/provisional-real-pair-v1/profile.csv`
- `$LASER_DATA_DIR/analysis/provisional-real-pair-v1/result.json`
- `$LASER_DATA_DIR/analysis/provisional-real-pair-v1/provisional_features.json`
- `$LASER_DATA_DIR/calibration/synthetic-smoke-v1/output/` (tool smoke test, not a real calibration)

## Executed verification (2026-08-13, before contract v3)

```text
make clean test sanitize
  native C++ tests: PASS
  AddressSanitizer/UndefinedBehaviorSanitizer: PASS

.venv/bin/python -m unittest discover -s tests -v
  13 tests: PASS
  Python/C++ parity exercised on 8 synthetic scenarios
  exact-truth scenarios within declared bounds
  gap, ghost, broad, saturation, neutral/warm clipped-core and no-stripe behavior exercised
  viewer camera-control acknowledgement, OV3660 automatic-mode readback and fail-closed lock verification exercised
  calibration outlier/validation, metadata/hash checks and 69-feature contract exercised

.venv/bin/python tools/validate_dataset.py --data-root "$LASER_DATA_DIR" \
  "$LASER_DATA_DIR/synthetic/v2" \
  examples/provisional_real_pair_metadata.json \
  examples/provisional_paper_pair_20260813_metadata.json \
  examples/locked_paper_pair_20260813_metadata.json
  13/13 metadata records, image hashes and dimensions: PASS

.venv/bin/python -m compileall -q tools tests
  PASS

nice -n 15 ionice -c3 .venv/bin/pio run -j 1
  PASS
  RAM 23,460 / 327,680 bytes (7.2%)
  Flash 341,689 / 6,553,600 bytes (5.2%)
```

The PlatformIO result proves compilation only. The extraction core has not run on the ESP32, and the current firmware still transports JPEG rather than an RGB888 analysis frame.

## Provisional board-return result (2026-08-13)

The returning board was identified read-only through its CH340C interface on `/dev/ttyUSB0`. The installed firmware captured checksum-valid 640x480 JPEG frames and sustained approximately 5 FPS in the localhost viewer. No firmware was uploaded.

A temporary carrier placed the 650 nm stripe diagonally across white paper. The line crossed within approximately 3 px of the viewer crosshair centre. A registered pair was captured by covering only the laser aperture:

- OFF: `$LASER_DATA_DIR/live/live_20260813_175100_004676.jpg`;
- ON: `$LASER_DATA_DIR/live/live_20260813_175146_004906.jpg`;
- metadata: `examples/provisional_paper_pair_20260813_metadata.json`;
- current analysis: `$LASER_DATA_DIR/analysis/provisional-paper-pair-20260813-clipped-core-v2/`.

Contract v1 showed that the full-frame provisional baseline accepted 316/480 rows, but it split the broad white-clipped core into two red flanks and therefore reported 208 false secondary candidates. Contract v2 bridges at most 12 intervening pixels only when every pixel is bright, neutral and at least 25 luma levels brighter than the registered OFF image. With `threshold=30` and `max_width=48`, it still accepts 316/480 rows, follows the primary centreline without visible outliers, marks 285 rows saturated and reports zero secondary candidates. Missing rows remain mainly outside the diagonal stripe's in-frame span.

The v2 rule is covered by a ninth synthetic fixture. A separate 38 px ghost remains detected as a secondary line. On the real 480-row pair, Python and native C++ agree on every validity, width, saturation and secondary flag; the maximum centre difference is 0.00003 px.

The pair has no detected integer translation at two-pixel sampling. After excluding the stripe neighbourhood, the ON background is still brighter than OFF by approximately 6.15/5.85/3.75 RGB levels. The geometry is therefore registered enough for an algorithm smoke test but the camera automation is not stable enough for calibration-grade subtraction.

On 2026-08-13 the prepared firmware was uploaded through the identified CH340C `/dev/ttyUSB0` path after rechecking the protected 16 MiB factory-flash backup. Esptool identified ESP32-S3 revision 0.2 with 8 MiB embedded PSRAM and verified the hashes of all four written regions before the hard reset.

Real `S/K/A` validation then passed:

- automatic state: `aec=1`, `agc=1`, `awb=1`, `awb_gain=0`, `wb_mode=0`;
- locked state: `aec=0`, `agc=0`, `awb=0`, `awb_gain=0`;
- retained values: `aec_value=784`, `agc_gain=16`, `wb_mode=0`;
- all four setter return codes: `0/0/0/0`;
- lock remained active after a 30-frame VGA JPEG burst;
- 30/30 frames passed transport framing, JPEG markers, device/host checksum and decode at 640x480;
- burst throughput: approximately 5.56 FPS;
- after automatic settling and lock, per-channel whole-frame mean ranges across the burst were approximately 1.49/0.99/1.76 RGB levels.

The hardware test corrected one host-side contract error. In the OV3660 driver, register `0x3406` bit 0 selects manual white-balance gains. Therefore automatic mode correctly reads back as `awb_gain=0`, `wb_mode=0`; the previous generic expectation of `awb_gain=1` was wrong. Lock verification remains fail-closed and still requires all four disabled flags. The localhost viewer was restarted, relocked and verified at approximately 5 FPS with the test RGB LED off and no capture error.

## Locked provisional paper pair

With the controls locked at `aec_value=784` and `agc_gain=16`, a new registered paper pair was captured by covering only the directly powered laser aperture:

- ON/OFF and hashes: `examples/locked_paper_pair_20260813_metadata.json`;
- analysis: `$LASER_DATA_DIR/analysis/locked-paper-pair-20260813-v2-tuned/`;
- 311/480 rows accepted;
- 293 accepted rows saturated;
- zero secondary candidates;
- zero integer translation was the best two-pixel-sampled registration;
- background ON-minus-OFF residual outside a broad stripe band: approximately 3.72/4.22/2.20 RGB levels;
- Python/native C++ parity over all 480 rows, maximum centre difference 0.00003 px.

The initial v2 limits still split 230 rows because the locked camera rendered the clipped core warm yellow-white rather than neutral white. Measured gap pixels had median chroma 52 and a maximum of 77 while remaining strongly laser-induced. The bounded bridge was therefore widened only to chroma 80 and minimum luma 145. A tenth synthetic warm-core regression passes in Python and C++, while the separate 38 px ghost remains detected on all 240 rows.

## Vertical-stripe session (2026-08-13, evening)

The laser module was rotated so the stripe images vertically and passes through
the frame centre. Measurements were taken through the running localhost viewer;
no firmware was uploaded.

Orientation, same hardware and same algorithm:

| | Morning (+29.3°) | Evening (+89.5°) |
|---|---:|---:|
| Row coverage | 64.8 % | 98.3 % |
| Mean stripe width | 14.5 px | 8.8 px |
| Contract | v2, width 64, threshold 30 | v1 frozen, width 16, threshold 20 |

The frozen v1 contract now outperforms the tuned v2 contract from the morning.
Every parameter loosened during that session was compensating for projection:
a shallow crossing angle inflates apparent width by `1/sin(angle)`, which is
2.04 at 29.3°. Analysis output is under
`$LASER_DATA_DIR/analysis/vertical-line-20260813-v1-frozen/`.

Repeatability over 20 consecutive frames of a static scene:

- luma centroid over the stripe span: **0.22 px** median, 0.74 px p90;
- red-excess centroid on columns whose core does not split: **0.20 px**;
- red-excess centroid on saturated columns: 2.7–3.5 px, bimodal;
- frame-to-frame drift: 0.003 px, i.e. none.

The large figure is flank flipping, not noise. The core reads `R = 255` across
the line and `G = 255` in its middle, so the extraction score collapses to zero
exactly where the signal is strongest.

Continuity tracking, added in contract v3, does not repair that:

| Configuration | Median centroid spread |
|---|---:|
| No tracking | 3.47 px |
| Tracking, 4 px window | 3.74 px |
| Tracking, 3 px window | 2.70 px |

Tracking is the right tool against spatially separated candidates — ghosts and
specular blobs — and the synthetic fixtures prove it there. Two flanks of one
clipped core are too close for any window to separate. Fixing this is optical:
reduce gain and exposure until the core peaks near 200, then add a 650 nm
bandpass filter.

The measured geometry (B ≈ 70 mm, Z ≈ 400 mm, both axes vertical) yields a
9.9° triangulation angle and 1.34 mm of height noise. See
[GEOMETRY.md](GEOMETRY.md) for the budget and the alternatives.

## Contract v3 changes

- Over-wide clusters are rejected **before** ranking, matching the documented
  order. A constructed 40 px specular field beside a 5 px stripe previously lost
  the row entirely in both Python and native C++, reported as `no_stripe` with
  no ambiguity flag; it now returns the stripe centre exactly.
- `all_clusters_too_wide` separates a glare field from an unlit scene.
- Optional continuity tracking, off by default.
- `fitSeamCorner` fits two legs and intersects them. On the synthetic fillet
  fixture the corner is recovered to 0.001 px of truth; the straight and sparse
  cases fail closed.
- Two new synthetic fixtures: `fillet_corner` with exact corner truth and
  `blob_and_stripe` as the regression for the selection order.
- Firmware gained exposure and gain setters, laser switching, registered-pair
  capture and an RGB565 measurement frame.
- The sample schema now requires `capture.timestamp_utc` and accepts
  `capture.robot_pose`.

Verification: 24 host tests pass, native C++ tests and the
AddressSanitizer/UndefinedBehaviorSanitizer run pass, Python and native C++
agree on all flags across all twelve fixtures, and the ESP32-S3 cross-build
succeeds at RAM 7.2 % and Flash 5.3 %.

## GPIO14 laser-driver validation (2026-08-18)

The 5 V line-laser module was measured in series at 13.0 mA and moved from the
temporary direct connection to a `2N2222` low-side switch. GPIO14 drives the base
through 1 kOhm; 10 kOhm from base to emitter holds it off while the pin floats;
the emitter and ESP32 share GND; the manual switch remains in the positive laser
lead. The supplier-confirmed positional transistor pinout, flat marked face
forward/leads down, is emitter/base/collector from left to right.

The open control wire kept the laser off. A manual 3.3 V signal through the
installed 1 kOhm resistor turned it on and removal turned it off. GPIO14 was then
connected to the board header hole marked `G14`, second from the bottom of the
left header immediately above `5V`.

The attached board was positively identified through CH340C `/dev/ttyUSB0`.
The current tree built successfully at RAM 23,460/327,680 bytes (7.2 %) and Flash
344,129/6,553,600 bytes (5.3 %), uploaded at 921600 baud, and passed esptool hash
verification for every written region before reset. The localhost viewer then
received these acknowledgements from the real firmware:

```text
LASER OFF pin=14
LASER ON pin=14
LASER OFF pin=14
```

With the manual switch enabled, the three-second ON command visibly lit the
physical laser, which the operator confirmed visually. Camera capture
continued at approximately 5 FPS without error, and the final viewer state
was off.

This validates manual off, floating-input off, direct 3.3 V base control,
firmware GPIO14 control and the viewer's complete USB-UART command path. It does
not yet validate a rigid-fixture `R` pair or prolonged operation. No multi-hour
soak has run, the laser's primary continuous-duty rating remains unknown, and
firmware has no maximum-on-time watchdog. The exact circuit and operating
boundary are in [LASER_DRIVER.md](LASER_DRIVER.md).

## Explicitly blocked until rigid fixture completion

- final ROI and optical thresholds;
- real pixel-to-mm coefficients and accuracy;
- target runtime, frame rate, PSRAM pressure and camera/inference coexistence;
- final labels, representative training data, model architecture and generalization;
- any FANUC or production-machine correction limits.

## First hardware handoff

1. Record fixture and hardware revisions.
2. Set geometry and focus; lock camera automation.
3. Capture registered ON/OFF pairs at measured reference positions.
4. Run the existing offline pipeline unchanged first.
5. Tune only evidenced failures, fit calibration with held-out validation points, then run the same core on-device.
