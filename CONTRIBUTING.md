# Contributing

This repository is an experimental engineering project. Small, evidence-backed
changes are preferred over broad rewrites. By taking part you agree to the
[code of conduct](CODE_OF_CONDUCT.md).

## Before changing code

- Check [docs/STATUS.md](docs/STATUS.md) for what is verified and what is
  blocked, then read the document that matches your change:
  [ARCHITECTURE](docs/ARCHITECTURE.md) for the algorithm,
  [FIRMWARE_PROTOCOL](docs/FIRMWARE_PROTOCOL.md) for the UART protocol,
  [GEOMETRY](docs/GEOMETRY.md) for the accuracy model, or
  [CHECKLIST](docs/CHECKLIST.md) for captures and calibration.
- Code and headers are the source of truth for contracts, pins and parsing.
- Keep optical measurements in pixels unless a real, versioned calibration
  applies.
- Do not add generated captures, datasets, model binaries, firmware images,
  flash backups, credentials or device-specific secrets.
- Do not convert a synthetic tool check into a real-world accuracy claim.
- A change to the extraction algorithm must change the Python reference and
  the C++ core together, and bump the contract version if behaviour changes.

## Verification

Create the environment as described in the [README](README.md#quick-start),
then run:

```bash
make check      # C++ tests, sanitizers, C++11 check, ruff, mypy, Python tests
make firmware   # ESP32-S3 cross-build
```

These are the same commands CI runs. If a change cannot be exercised without
the board, make the boundary explicit and at least cross-compile the affected
firmware path. Record the missing hardware verification in the relevant
documentation. Any change to how or when the laser switches needs a
supervised hardware test with the manual switch within reach before it is
merged.

## Data and calibration changes

- Store large or generated artifacts under `$LASER_DATA_DIR` (default: the
  repository's `data/` directory, ignored by Git), not in Git.
- Version schemas, metadata templates and generators instead of generated
  datasets.
- Preserve original inputs and exact commands for A/B comparisons.
- Keep calibration fit and validation positions separate.
- Record fixture revision, camera controls, geometry, surface and lighting
  conditions.

## TinyML changes

A model proposal must include a frozen classical baseline, grouped real-data
train/validation/test splits, failure-mode analysis, quantised on-device
measurements and a documented advantage. Synthetic fixtures alone are not
evidence of production performance.

## Style

- Python: `ruff` and `mypy` as configured in `pyproject.toml`; every CLI option
  has help text, and expected errors end with a one-line message instead of a
  traceback.
- C++: the portable core stays allocation-free and compiles warning-free as
  C++11 and C++17 with `-Wconversion`. The firmware uses camelCase and a `g`
  prefix for globals, the core uses snake_case; keep each file's convention.
- Prose: British English (`centre`, `behaviour`, `quantisation`).

## Commit style

Use focused imperative commit subjects, for example:

```text
Add row-wise stripe ambiguity flag
Document rigid-fixture capture procedure
```

Do not include unrelated generated files or local environment changes.
