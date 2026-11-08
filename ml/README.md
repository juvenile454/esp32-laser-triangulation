# TinyML decision and feature contract

Status: **preparation only; no production model exists**.

The deterministic stripe extractor remains the baseline. A neural model will only be justified after rigid-fixture captures expose a measured failure class that the classical pipeline cannot handle robustly.

## Provisional input contract

`tools/build_profile_features.py` converts an extracted profile to exactly 69 finite float features:

- 64 normalised, uniformly resampled centreline values;
- valid-point ratio;
- normalised mean peak score;
- normalised mean stripe width;
- saturation ratio;
- secondary-reflection ratio.

The matching firmware contract is `lib/laser_core/src/inference_contract.h`. The input shape is deliberately compact and provisional. It must be reassessed after real captures establish the required field of view and profile detail.

## Deliberately not done yet

- no final labels or classes;
- no train/validation/test split from real workpieces;
- no TensorFlow/TFLite dependency installed;
- no synthetic-only model presented as useful for seam correction;
- no quantisation, target latency, tensor arena, or MCU memory claim.

When representative data exists, compare held-out performance against the classical baseline before adding a TFLite Micro adapter.
