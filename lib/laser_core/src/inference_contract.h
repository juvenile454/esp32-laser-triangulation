#pragma once

#include <cstddef>
#include <cstdint>

namespace laser {

// Provisional adapter contract only. No production model is supplied while
// representative, rigid-fixture data is unavailable.
constexpr std::uint16_t kInferenceContractVersion = 1U;
constexpr std::size_t kProfileFeatureCount = 64U;
constexpr std::size_t kQualityFeatureCount = 5U;
constexpr std::size_t kInferenceFeatureCount =
    kProfileFeatureCount + kQualityFeatureCount;

struct InferenceInput {
  float values[kInferenceFeatureCount]{};
};

struct InferenceResult {
  bool valid = false;
  std::uint16_t class_index = 0U;
  float confidence = 0.0F;
};

using InferenceFunction = bool (*)(const InferenceInput&, InferenceResult&);

}  // namespace laser
