import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { readStudioSource } from "./studio-source.mjs";

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
import StudioSubmitBar from "../app/studio/StudioSubmitBar.jsx";
import { buildStudioModelContext } from "../app/studio/studioModelContext.ts";
import { reverseOperationRequestSignature } from "../lib/reverseOperations.ts";
import { validateGenerationSubmission } from "../hooks/generationSubmitWorkflow.js";

globalThis.React = React;

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
const seedance15Option = {
  id: 15,
  display_name: "Seedance 1.5 Pro",
  model_id: "doubao-seedance-1-5-pro-251215",
  capabilities: {
    image_to_video: true,
    reference_image: false,
    first_last_frame: true,
    multi_reference: false,
  },
};
assert.equal(strictMultiReferenceLimit(seedance15Option), 0);
assert.equal(
  validateMultiReferenceSelection(seedance15Option, 2, 1).ok,
  false,
  "Seedance 1.5 Pro must reject product detail references",
);
assert.deepEqual(
  filterModelOptions([seedance15Option], {
    use: "video",
    creationMode: "video",
    selected: null,
    productAsset: { type: "image", url: "/product.png" },
  }),
  [],
  "Seedance 1.5 Pro must not be offered for independent product references",
);
assert.deepEqual(
  filterModelOptions([seedance15Option], {
    use: "video",
    creationMode: "video",
    selected: { type: "image", url: "/first.png" },
    productAsset: null,
  }).map((item) => item.id),
  [15],
  "Seedance 1.5 Pro must remain available for first-frame image-to-video",
);
const seedanceProductPreflight = validateGenerationSubmission({
  uploading: false,
  parsing: false,
  reversing: false,
  productProfiling: false,
  structuredDirty: false,
  modelOption: seedance15Option,
  productVideoTemplate: "prompt_driven",
  category: "video",
  creationMode: "video_edit",
  subjectMode: "product",
  firstLastFrameEnabled: false,
  lastFrameAsset: null,
  selected: { type: "video", url: "/style.mp4" },
  productAsset: { type: "image", url: "/product.png" },
  productDetailAssets: [],
  task: null,
  isEditMode: true,
  isImageEditMode: false,
  prompt: "产品广告",
  structured: {},
});
assert.equal(seedanceProductPreflight.ok, false);
assert.match(seedanceProductPreflight.message, /不支持独立产品主题图/);
const seedance15ProductContext = buildStudioModelContext({
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "product",
  productGenerationMode: true,
  selected: null,
  lastFrameAsset: null,
  productAsset: { type: "image", url: "/product.png" },
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [seedance15Option],
  selectedGenerationModel: seedance15Option,
});
assert.equal(seedance15ProductContext.productReferenceUnsupported, true);
assert.equal(seedance15ProductContext.videoModelSwitchRequired, true);
assert.match(seedance15ProductContext.videoModelSwitchMessage, /Seedance 1\.5 Pro 不支持独立产品主题图/);

let generationSubmitCount = 0;
let modelSwitchWarningCount = 0;
const incompatibleSubmitBar = StudioSubmitBar({
  category: "video",
  videoFinalCost: 10,
  estCost: 10,
  imageCount: 1,
  videoDuration: 5,
  vResolution: "720p",
  submit: () => { generationSubmitCount += 1; },
  missingRequiredSource: false,
  missingRequiredSourceLabel: "请先上传产品",
  videoModelSwitchRequired: true,
  videoModelSwitchMessage: seedance15ProductContext.videoModelSwitchMessage,
  onVideoModelSwitchRequired: () => { modelSwitchWarningCount += 1; },
  structuredDirty: false,
  submitting: false,
  parsing: false,
  uploading: false,
  reversing: false,
  productProfiling: false,
  task: null,
  currentModelEnabled: true,
  running: false,
  submitLabel: "生成成片",
});
const incompatibleSubmitButton = incompatibleSubmitBar.props.children[1];
assert.equal(incompatibleSubmitButton.props.disabled, false, "model conflicts must remain clickable so the UI can explain them");
assert.equal(incompatibleSubmitButton.props.children.at(-1), "请切换视频模型");
incompatibleSubmitButton.props.onClick();
assert.equal(modelSwitchWarningCount, 1, "clicking an incompatible model should show the switch-model warning");
assert.equal(generationSubmitCount, 0, "an incompatible model must never submit or silently switch generation models");
const grokReferenceOption = {
  id: 20,
  model_id: "grok-imagine-video",
  capabilities: {
    image_to_video: true,
    reference_image: true,
    multi_reference: true,
    max_reference_images: 3,
  },
};
const grok15Option = {
  id: 21,
  model_id: "grok-imagine-video-1.5",
  capabilities: {
    image_to_video: true,
    reference_image: false,
    multi_reference: false,
  },
};
assert.deepEqual(
  filterModelOptions([grokReferenceOption, grok15Option], {
    use: "video",
    creationMode: "video",
    selected: null,
    productAsset: { type: "image", url: "/product.png" },
  }).map((item) => item.id),
  [20],
  "product reference mode must offer grok-imagine-video and hide Grok 1.5",
);
const seedance20Option = {
  display_name: "Seedance 2.0",
  model_id: "doubao-seedance-2-0-mini-260615",
  capabilities: { multi_reference: true, max_reference_images: 10 },
};
assert.equal(strictMultiReferenceLimit(seedance20Option), 10);
assert.equal(
  validateMultiReferenceSelection(seedance20Option, 10, 9).ok,
  true,
  "Seedance 2.0 must allow one theme image plus nine detail images",
);
assert.equal(
  validateMultiReferenceSelection(seedance20Option, 11, 10).ok,
  false,
  "Seedance 2.0 must reject more than ten total reference images",
);
const seedance20ProductContext = buildStudioModelContext({
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "product",
  productGenerationMode: true,
  selected: null,
  lastFrameAsset: null,
  productAsset: { type: "image", url: "/product.png" },
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [seedance20Option],
  selectedGenerationModel: seedance20Option,
});
assert.equal(seedance20ProductContext.videoModelSwitchRequired, false);
assert.equal(seedance20ProductContext.videoModelSwitchMessage, "");
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
const pageSource = readStudioSource(root);
const runtimeBootstrapSource = readFileSync(join(root, "hooks/useStudioRuntimeBootstrap.js"), "utf8");
const modelContextSource = readFileSync(join(root, "app/studio/studioModelContext.ts"), "utf8");
const promptOptimizationSource = readFileSync(join(root, "hooks/usePromptOptimization.js"), "utf8");
const reverseSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const submitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const uploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const modelSelectionSource = readFileSync(join(root, "hooks/useStudioModelSelection.js"), "utf8");
assert.match(
  runtimeBootstrapSource,
  /window\.addEventListener\("focus", onFocus\)/,
  "studio config should refresh when the page regains focus",
);
assert.match(promptOptimizationSource, /optimizer_model_config_id: selectedPromptModelConfigId/, "prompt optimization must carry the selected optimizer id");
assert.match(reverseSource, /model_config_id: Number\(modelConfigId\)/, "reverse creation must carry the selected vision model id");
assert.match(submitSource, /modelConfigId,/, "generation submit must forward the selected generation model id");
assert.match(
  pageSource,
  /visionModelConfigId: (?:model\.)?selectedVisionModelConfigId/,
  "profile prefetch must receive the selected vision model id",
);
assert.match(uploadSource, /model_config_id: Number\(visionModelConfigId\)/, "profile prefetch must submit the selected vision model id");
assert.match(
  modelContextSource,
  /message: "请先选择产品主题图"/,
  "detail image controls must require a product theme image",
);
assert.match(uploadSource, /if \(!productAsset\?\.url\)/, "detail uploads must enforce the theme image gate internally");
assert.match(
  modelSelectionSource,
  /const generationModelOptions = allModelOptions\[category\]/,
  "generation model selection must remain stable when a new reference is incompatible",
);
assert.match(
  modelContextSource,
  /productDetailModelLimit[\s\S]*?- \(productAsset \? 0 : 1\)/,
  "detail capacity must reserve one model reference slot for the required theme image",
);
assert.match(
  uploadSource,
  /productDetailAssets\.length \+ files\.length > effectiveProductDetailLimit/,
  "detail uploads must enforce the selected model's remaining reference limit before upload",
);

console.log("studio model selection tests passed");
