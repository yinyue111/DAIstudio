import assert from "node:assert/strict";

import {
  PRODUCT_VIDEO_STRATEGIES,
  normalizeProductVideoStrategyCapabilities,
  normalizeProductVideoStrategyKey,
  resolveProductVideoStrategySelection,
} from "../app/studio/productVideoStrategy.ts";

assert.equal(PRODUCT_VIDEO_STRATEGIES.length, 8);
assert.equal(normalizeProductVideoStrategyKey(" Slow_Push "), "slow_push");
assert.equal(normalizeProductVideoStrategyKey("wild_spin"), null);

const legacy = normalizeProductVideoStrategyCapabilities({ image_to_video: true });
assert.equal(legacy.declared, false);
assert.equal(legacy.legacyFallback, true);
assert.equal(legacy.supported, true);
assert.deepEqual(legacy.strategies, ["prompt_driven"]);

for (const capabilities of [
  { product_video_templates: [] },
  { product_video_templates: null },
  { product_video_templates: "prompt_driven" },
  { product_video_templates: ["unknown_only"] },
]) {
  const normalized = normalizeProductVideoStrategyCapabilities(capabilities);
  assert.equal(normalized.declared, true);
  assert.equal(normalized.supported, false);
  assert.deepEqual(normalized.strategies, []);
}

const filtered = normalizeProductVideoStrategyCapabilities({
  product_video_templates: [
    "slow_push",
    "unknown",
    "slow_push",
    "single_clip_action",
    null,
  ],
});
assert.deepEqual(filtered.strategies, ["slow_push", "single_clip_action"]);
assert.deepEqual(filtered.options.map((option) => option.key), filtered.strategies);

const selected = resolveProductVideoStrategySelection(
  { product_video_templates: ["stable_showcase", "slow_push"] },
  "slow_push",
);
assert.equal(selected.effectiveValue, "slow_push");
assert.equal(selected.fellBack, false);

const fallback = resolveProductVideoStrategySelection(
  { product_video_templates: ["stable_showcase", "slow_push"] },
  "soft_splash",
);
assert.equal(fallback.effectiveValue, "stable_showcase");
assert.equal(fallback.fellBack, true);

const unsupported = resolveProductVideoStrategySelection(
  { product_video_templates: [] },
  "prompt_driven",
);
assert.equal(unsupported.supported, false);
assert.equal(unsupported.effectiveValue, null);

console.log("product video strategy tests passed");
