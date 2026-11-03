# Hardware return and calibration checklist

This checklist starts only when the target board, camera, laser and rigid fixture are available. It prevents temporary bench geometry from becoming an undocumented calibration.

## 1. Record the configuration

Create one metadata record per capture series and record:

- board, camera, lens, laser and fixture revision;
- image resolution and pixel format;
- lens-to-work-plane distance;
- camera/laser baseline and angle;
- target material, surface state and ambient light;
- measured reference displacement and its measurement method;
- firmware commit and analysis command.

Do not reuse calibration coefficients after changing focus, camera/laser pose, lens, resolution or ROI.

## 2. Bring up without changing production systems

1. Power only the bench assembly.
2. Verify board startup and camera PID.
3. Check `P` camera readiness and `S` camera-control status over UART.
4. Capture an unmodified baseline frame.
5. Do not upload firmware until the intended device and port are positively identified.

## 2b. Laser switch — completed on 2026-08-18

The assembled circuit, measurements and runtime boundary are recorded in
[LASER_DRIVER.md](LASER_DRIVER.md). Completed checks:

- [x] GPIO14 was confirmed on the broken-out `G14` header and against the camera,
  strapping and USB pin assignments.
- [x] Low-side stage: manual switch from `5V` to laser positive, laser negative
  to `2N2222` collector, emitter to common `GND`, and GPIO14 through 1 kOhm to
  the base.
- [x] A 10 kOhm base-emitter pull-down keeps the transistor off while GPIO14
  floats during reset.
- [x] The exact installed transistor orientation is flat marked face forward,
  leads down: emitter left, base middle, collector right.
- [x] Laser-module current was measured in series as 13.0 mA at approximately
  5 V.
- [x] With the control wire open the laser stayed off; applying 3.3 V through
  the installed 1 kOhm resistor switched it on; removing 3.3 V switched it off.
- [x] Current firmware was uploaded only after identifying the CH340C board on
  `/dev/ttyUSB0`; esptool verified all written hashes.
- [x] Viewer commands produced `LASER OFF/ON/OFF pin=14`; a three-second GPIO14
  pulse visibly switched the physical laser, camera capture stayed near 5 FPS,
  and the final reported state was off.

Still required after mechanical installation:

- [ ] Verify laser-off at boot, clean viewer exit and USB reconnect in the final
  cable routing.
- [ ] Validate firmware command `R` with a real software-switched registered
  pair in the rigid fixture.
- [ ] Run a supervised staged thermal/connection soak before any prolonged use.
- [ ] Add a maximum-on-time policy before considering unattended operation.

## 3. Set focus and camera controls

1. Place a flat, high-contrast target in the intended measurement plane.
2. Set working distance and field of view mechanically. Choose them from
   [GEOMETRY.md](GEOMETRY.md); aim for a 30°–45° triangulation angle.
3. Rotate the laser until the stripe images **vertically**. The extractor scans
   rows along x, so a shallow crossing angle inflates the measured stripe width
   by `1/sin(angle)` and costs coverage.
4. Focus for the target plane, and focus the laser line itself; record both.
5. Let auto exposure/gain/white balance settle without the laser.
6. Send `K` to lock the current camera controls and verify with `S`.
7. Capture repeated OFF frames and compare brightness stability.
8. Switch the laser on and **verify the core is not clipped**. Drive gain down
   with `G00` and exposure down with `Ennnn` until the peak sits near 200 of
   255. A clipped core collapses the extraction score exactly where the signal
   is strongest and costs roughly an order of magnitude of precision. Change
   optics or exposure only in a documented new series.

`K`, `S` and `A` were hardware-verified on the OV3660 after the approved upload on 2026-08-13. Lock requires readback of `aec=0`, `agc=0`, `awb=0` and `awb_gain=0`. Automatic OV3660 mode correctly reads back `aec=1`, `agc=1`, `awb=1`, `awb_gain=0`, `wb_mode=0`; register `0x3406` bit 0 denotes manual gain mode.

## 4. Capture registered ON/OFF pairs

With the laser switch built, `tools/capture_pair.py` does this in one command and
writes schema-valid metadata including the UTC capture time:

```bash
.venv/bin/python tools/capture_pair.py \
  "$LASER_DATA_DIR/captures" \
  --lock --gain 0 --exposure 120 \
  --fixture-revision rigid-v1 \
  --working-distance-mm 120 --baseline-mm 70
```

For each mechanical reference position:

1. Keep camera, target and illumination fixed.
2. Capture the pair; only the laser state changes between the two frames.
3. Repeat enough pairs to quantify short-term variation.
4. Save images under `$LASER_DATA_DIR`, never in Git.
5. Validate with `tools/validate_dataset.py`.

Before any calibration series, measure the focal length rather than assuming it.
Place a block of known thickness under the target, capture before and after, and
feed the observed stripe shift to `tools/triangulation_budget.py
--from-measured-shift`.

If ON/OFF alignment or exposure changed, reject the pair rather than silently tuning around it.

## 5. Run the frozen offline baseline

Start with the existing algorithm and record the complete command. Inspect:

- overlay versus the visible primary stripe;
- score map and background residuals;
- valid coverage;
- saturated and missing rows;
- secondary-candidate rows;
- failure reason and confidence.

Only change ROI or thresholds to address observed, repeatable failure modes. Keep the old result for A/B comparison.

## 6. Acquire calibration and validation points

- Cover the full intended range in both directions.
- Include repeat measurements and approach selected points from both directions if fixture backlash is possible.
- Reserve validation positions that are not used for fitting.
- Keep side-offset and height calibration series distinguishable.
- Record reference-instrument resolution and uncertainty.

Fit with `tools/fit_calibration.py`; never judge fit residual alone. Acceptance is based on held-out real validation.

## 7. Integrate and measure on ESP32

Before claiming embedded readiness, measure on the target:

- extraction time and sustainable frame rate;
- internal RAM and PSRAM high-water marks;
- camera-buffer and extraction-buffer coexistence;
- repeated output parity against saved host fixtures;
- behavior for missing, broad, saturated and ambiguous lines;
- watchdog/reset behavior during prolonged capture.

## 8. Decide classical processing versus TinyML

Freeze the classical baseline first. Evaluate an optional model only with separated train/validation/test capture groups, preferably split by part or capture session. Compare:

- held-out error and invalid-rate;
- reflection and occlusion robustness;
- runtime, RAM, flash and quantization loss;
- explainability and safe rejection behavior.

Keep the classical method if TinyML has no reproducible operational advantage.

## 9. Production boundary

No FANUC, laser-welding-system or production correction output is authorized by this repository. Production integration needs a separate interface specification, bounds, stale-data behavior, independent enable state and machine-side validation.
