# Contributing

This repository is currently an experimental engineering project. Small, evidence-backed changes are preferred over broad rewrites.

## Before changing code

- Read `docs/ARCHITECTURE.md` and the relevant part of `docs/LAB_LOG.md` or `docs/HARDWARE_RETURN_CHECKLIST.md`.
- Keep optical measurements in pixels unless a real, versioned calibration applies.
- Do not add generated captures, datasets, model binaries, firmware images, flash backups, credentials or device-specific secrets.
- Do not convert a synthetic tool check into a real-world accuracy claim.

## Verification

Create the environment as described in `README.md`, then run:

```bash
make clean test sanitize
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip install ruff==0.16.9
.venv/bin/ruff check tools tests
.venv/bin/pio run -j 1
```

If a change cannot be exercised without the board, make the boundary explicit and at least cross-compile the affected firmware path. Record the missing hardware verification in the relevant documentation.

## Data and calibration changes

- Store large or generated artifacts under `$LASER_DATA_DIR` (default `./data`, ignored by Git), not in Git.
- Version schemas, metadata templates and generators instead of generated datasets.
- Preserve original inputs and exact commands for A/B comparisons.
- Keep calibration fit and validation positions separate.
- Record fixture revision, camera controls, geometry, surface and lighting conditions.

## TinyML changes

A model proposal must include a frozen classical baseline, grouped real-data train/validation/test splits, failure-mode analysis, quantized on-device measurements and a documented advantage. Synthetic fixtures alone are not evidence of production performance.

## Commit style

Use focused imperative commit subjects, for example:

```text
Add row-wise stripe ambiguity flag
Document rigid-fixture capture procedure
```

Do not include unrelated generated files or local environment changes.
