# Example metadata

These files are the metadata of real captures from the provisional bench
setup. The images they reference stay local, in line with the data policy in
[CONTRIBUTING.md](../CONTRIBUTING.md), so the hash and dimension checks need the
original capture directory.

| File | What it describes |
|---|---|
| `provisional_real_pair_metadata.json` | First real ON/OFF pair from the viewer, before the camera lock |
| `provisional_paper_pair_20260813_metadata.json` | Paper target, automatic camera controls |
| `locked_paper_pair_20260813_metadata.json` | Paper target with locked exposure, gain and white balance |
| `calibration_measurements_template.csv` | Column layout expected by `tools/fit_calibration.py` |

None of them is a calibration: all three are `reusable_real_evidence`,
recorded without the rigid fixture.

Schema only (works anywhere):

```bash
.venv/bin/python -c "import json, jsonschema, pathlib; s = json.load(open('schemas/sample.schema.json')); [jsonschema.validate(json.load(open(p)), s) for p in pathlib.Path('examples').glob('*metadata.json')]; print('schema OK')"
```

Full check, with the original captures:

```bash
.venv/bin/python tools/validate_dataset.py --data-root "$LASER_DATA_DIR" examples/
```

Without the images, `validate_dataset.py` reports `image not found` for each
file. That is expected.
