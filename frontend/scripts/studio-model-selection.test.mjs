import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildGenerationPayload } from "../app/studio/generationPayload.ts";
import {
  filterModelOptions,
  modelOptionCostLabel,
  modelOptionName,
  normalizeModelOptions,
  readModelSelections,
  resolveModelConfigId,
  strictMultiReferenceLimit,
  validateMultiReferenceSelection,
  writeModelSelections,
} from "../app/studio/StudioModelSelector.jsx";
import { reverseOperationRequestSignature } from "../lib/reverseOperations.ts";

const options = normalizeModelOptions({
  model_options: {
    vision: [
      { id: 2, display_name: "Vision Video", is_default: false, capabilities: { video_analysis: true } },
      { id: 1, display_name: "Vision Image", is_default: true, capabilities: { video_analysis: false, image_analysis: true } },
      { id: 99, display_name: "Disabled", enabled: false },
    ],
    image: [
      { id: 4, display_name: "Edit", provider: "env", capabilities: { image_to_image: true }, cost_credits: 3 },
      { id: 3, display_name: "Generate", provider: "volcengine_ark", is_default: true, capabilities: { image_to_image: false }, cost_credits: 15 },
    ],
    video: [{ id: 5, display_name: "Video", is_default: true }],
    prompt: [{ id: 6, display_name: "Optimizer", is_default: true }],
  },
});

assert.deepEqual(options.vision.map((item) => item.id), [1, 2], "default model should sort first and disabled models must be hidden");
assert.equal(resolveModelConfigId(options.image, 4), 4);
assert.equal(resolveModelConfigId(options.image, 404), 3, "missing selection should fall back to the enabled default");
assert.equal(modelOptionName(options.image[0]), "Generate", "model controls should only expose the model name");
assert.doesNotMatch(modelOptionName(options.image[0]), /volcengine|env|积分/, "provider and price must stay out of model names");
assert.equal(modelOptionCostLabel(options.image[0], "image"), "单张 15 积分");
assert.deepEqual(
  filterModelOptions(options.image, { use: "image", creationMode: "image_edit", selected: null, productAsset: null }).map((item) => item.id),
  [4],
  "models declaring image edit unsupported must not appear in image edit mode",
);
assert.equal(strictMultiReferenceLimit({ capabilities: {} }), 0);
assert.equal(strictMultiReferenceLimit({ capabilities: { multi_reference: true } }), 0);
assert.equal(
  strictMultiReferenceLimit({ capabilities: { multi_reference: true, max_reference_images: "10" } }),
  0,
  "max_reference_images must be a real integer instead of a numeric string",
);
assert.equal(
  validateMultiReferenceSelection({ capabilities: {} }, 2, 1).ok,
  false,
  "missing multi-reference metadata must be treated as unsupported",
);
assert.equal(
  validateMultiReferenceSelection({ capabilities: { multi_reference: true, max_reference_images: 3 } }, 4, 2).ok,
  false,
  "reference counts above the declared model limit must be blocked",
);
assert.equal(
  validateMultiReferenceSelection({ capabilities: { multi_reference: true, max_reference_images: 10 } }, 4, 2).ok,
  true,
);
assert.deepEqual(
  filterModelOptions(options.vision, { use: "vision", selected: { type: "video" } }).map((item) => item.id),
  [2],
  "video reverse must only offer models that explicitly support video input",
);

const values = new Map();
const storage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
};
writeModelSelections(storage, "42", { vision: 2, image: 4, video: 5, prompt: 6 });
assert.deepEqual(readModelSelections(storage, "42"), { vision: 2, image: 4, video: 5, prompt: 6 });
assert.deepEqual(
  readModelSelections(storage, "43"),
  { vision: null, image: null, video: null, prompt: null },
  "model preferences must be scoped by user",
);

const generation = buildGenerationPayload({
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image",
  prompt: "clean product photo",
  promptDirty: true,
  ratio: "1:1",
  imageQuality: "2k",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
  modelConfigId: 4,
});
assert.equal(generation.payload.model_config_id, 4, "generation requests must carry the selected catalog id");

assert.notEqual(
  reverseOperationRequestSignature({ asset_url: "/asset.jpg", target: "image", model_config_id: 1 }),
  reverseOperationRequestSignature({ asset_url: "/asset.jpg", target: "image", model_config_id: 2 }),
  "reverse idempotency signatures must include the selected model",
);

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const reverseSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const submitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const uploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
assert.match(pageSource, /window\.addEventListener\("focus", onFocus\)/, "studio config should refresh when the page regains focus");
assert.match(pageSource, /optimizer_model_config_id: selectedPromptModelConfigId/, "prompt optimization must carry the selected optimizer id");
assert.match(reverseSource, /model_config_id: Number\(modelConfigId\)/, "reverse creation must carry the selected vision model id");
assert.match(submitSource, /modelConfigId,/, "generation submit must forward the selected generation model id");
assert.match(pageSource, /visionModelConfigId: selectedVisionModelConfigId/, "profile prefetch must receive the selected vision model id");
assert.match(uploadSource, /model_config_id: Number\(visionModelConfigId\)/, "profile prefetch must submit the selected vision model id");
assert.match(pageSource, /message: "请先选择产品主题图"/, "detail image controls must require a product theme image");
assert.match(uploadSource, /if \(!productAsset\?\.url\)/, "detail uploads must enforce the theme image gate internally");

console.log("studio model selection tests passed");
