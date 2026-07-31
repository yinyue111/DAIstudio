import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { readStudioSource } from "./studio-source.mjs";

import { buildGenerationPayload } from "../app/studio/generationPayload.ts";
import {
  filterModelOptions,
  markGenerationModelCompatibility,
  modelRequirements,
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
import StudioGenerationControls from "../components/StudioGenerationControls.jsx";
import {
  buildStudioModelContext,
  videoModelRequiresFirstFrame,
} from "../app/studio/studioModelContext.ts";
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
assert.deepEqual(
  filterModelOptions(
    [{ id: 41, capabilities: { image_to_image: "true" } }],
    { use: "image", creationMode: "image_edit", selected: null, productAsset: null },
  ),
  [],
  "capability values must be the boolean true instead of truthy strings",
);
assert.deepEqual(
  modelRequirements({
    use: "image",
    creationMode: "image",
    selected: { type: "image", url: "/reference.png" },
    productAsset: null,
  }),
  [
    ["image_to_image", "image_edit", "edit"],
    ["reference_image", "image_reference", "image_input"],
  ],
  "image generation with a reference must require edit and reference-image capabilities",
);
assert.deepEqual(
  filterModelOptions(
    [
      {
        id: 31,
        capabilities: {
          text_to_image: true,
          image_to_image: false,
          reference_image: true,
        },
      },
      {
        id: 32,
        capabilities: {
          text_to_image: true,
          image_to_image: true,
          reference_image: true,
        },
      },
    ],
    {
      use: "image",
      creationMode: "image",
      selected: { type: "image", url: "/reference.png" },
      productAsset: null,
    },
  ).map((item) => item.id),
  [32],
  "text-to-image-only models must not be offered once an image reference is selected",
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
const annotatedImageOptions = markGenerationModelCompatibility(
  [
    {
      id: 51,
      capabilities: { image_to_image: true, reference_image: true, multi_reference: false },
    },
    {
      id: 52,
      capabilities: {
        image_to_image: true,
        reference_image: true,
        multi_reference: true,
        max_reference_images: 2,
      },
    },
  ],
  {
    use: "image",
    creationMode: "image_edit",
    selected: { type: "image", url: "/style.png" },
    productAsset: { type: "image", url: "/product.png" },
    productDetailAssets: [],
    subjectMode: "product",
  },
);
assert.equal(annotatedImageOptions[0].selection_disabled, true);
assert.match(annotatedImageOptions[0].selection_disabled_reason, /2 张参考图/);
assert.equal(annotatedImageOptions[1].selection_disabled, undefined);
const limitedImageContext = buildStudioModelContext({
  cfg: {},
  category: "image",
  creationMode: "image_edit",
  subjectMode: "product",
  productGenerationMode: true,
  selected: { type: "image", url: "/style.png" },
  lastFrameAsset: null,
  productAsset: { type: "image", url: "/product.png" },
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: annotatedImageOptions,
  selectedGenerationModel: annotatedImageOptions[0],
});
assert.equal(limitedImageContext.declaredReferenceLimitUnsupported, true);
assert.equal(limitedImageContext.modelSwitchRequired, true);
assert.match(limitedImageContext.modelSwitchMessage, /当前素材需要 2 张参考图/);
assert.match(limitedImageContext.modelSwitchMessage, /请切换图片模型/);
const seedance15Option = {
  id: 15,
  display_name: "Seedance 1.5 Pro",
  model_id: "doubao-seedance-1-5-pro-251215",
  capabilities: {
    text_to_video: true,
    image_to_video: true,
    reference_image: false,
    first_last_frame: true,
    multi_reference: false,
    min_duration_seconds: 4,
    max_duration_seconds: 12,
    resolutions: ["480p", "720p", "1080p"],
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
assert.equal(seedance15ProductContext.modelSwitchRequired, true);
assert.equal(seedance15ProductContext.minVideoDuration, 4);
assert.equal(seedance15ProductContext.maxVideoDuration, 12);
assert.deepEqual(seedance15ProductContext.videoResolutions, ["480p", "720p", "1080p"]);
assert.match(seedance15ProductContext.modelSwitchMessage, /Seedance 1\.5 Pro 不支持独立产品主题图/);

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
  modelSwitchRequired: true,
  modelSwitchMessage: seedance15ProductContext.modelSwitchMessage,
  onModelSwitchRequired: () => { modelSwitchWarningCount += 1; },
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
    text_to_video: true,
    image_to_video: true,
    reference_image: true,
    multi_reference: true,
    max_reference_images: 7,
    max_reference_duration_seconds: 10,
    min_duration_seconds: 1,
    max_duration_seconds: 15,
    reference_image_mode_exclusive: true,
    resolutions: ["480p", "720p"],
    video_to_video: true,
    video_reference: false,
    video_edit: true,
    audio_reference: false,
    max_reference_videos: 1,
    max_reference_audio: 0,
  },
};
const grok15Option = {
  id: 21,
  model_id: "grok-imagine-video-1.5",
  capabilities: {
    text_to_video: false,
    image_to_video: true,
    reference_image: false,
    multi_reference: false,
    min_duration_seconds: 1,
    max_duration_seconds: 15,
    resolutions: ["480p", "720p", "1080p"],
  },
};
const seedance20VideoOption = {
  id: 22,
  model_id: "doubao-seedance-2-0-260128",
  capabilities: {
    text_to_video: true,
    image_to_video: true,
    reference_image: true,
    multi_reference: true,
    max_reference_images: 9,
    min_duration_seconds: 4,
    max_duration_seconds: 15,
    resolutions: ["480p", "720p", "1080p"],
    video_to_video: true,
    video_reference: true,
    video_edit: false,
    audio_reference: false,
    max_reference_videos: 1,
    max_reference_audio: 0,
  },
};
const videoSubmission = (overrides = {}) => validateGenerationSubmission({
  uploading: false,
  parsing: false,
  reversing: false,
  productProfiling: false,
  structuredDirty: false,
  productVideoTemplate: "prompt_driven",
  category: "video",
  creationMode: "video",
  subjectMode: "general",
  firstLastFrameEnabled: false,
  lastFrameAsset: null,
  selected: null,
  productAsset: null,
  productDetailAssets: [],
  task: null,
  isEditMode: false,
  isImageEditMode: false,
  prompt: "生成护肤品广告",
  structured: {},
  ...overrides,
});
const imageEditSubmission = (overrides = {}) => validateGenerationSubmission({
  uploading: false,
  parsing: false,
  reversing: false,
  productProfiling: false,
  structuredDirty: false,
  productVideoTemplate: "prompt_driven",
  category: "image",
  creationMode: "image_edit",
  subjectMode: "general",
  firstLastFrameEnabled: false,
  lastFrameAsset: null,
  selected: { type: "image", url: "/style.png" },
  productAsset: { type: "image", url: "/source.png" },
  productDetailAssets: [],
  task: null,
  isEditMode: true,
  isImageEditMode: true,
  prompt: "保留主体并迁移风格",
  structured: {},
  ...overrides,
});
const singleReferenceOnlyImageModel = {
  id: 30,
  capabilities: {
    image_to_image: true,
    reference_image: true,
    multi_reference: false,
  },
};
const maskUnsupportedImageContext = buildStudioModelContext({
  cfg: {},
  category: "image",
  creationMode: "image_edit",
  subjectMode: "product",
  productGenerationMode: true,
  selected: { type: "image", url: "/style.png" },
  lastFrameAsset: null,
  productAsset: { type: "image", url: "/source.png" },
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [singleReferenceOnlyImageModel],
  selectedGenerationModel: {
    ...singleReferenceOnlyImageModel,
    capabilities: { ...singleReferenceOnlyImageModel.capabilities, mask_edit: false },
  },
});
assert.equal(maskUnsupportedImageContext.maskEditSupported, false);
assert.equal(
  buildStudioModelContext({
    cfg: {},
    category: "image",
    creationMode: "image_edit",
    subjectMode: "product",
    productGenerationMode: true,
    selected: { type: "image", url: "/style.png" },
    lastFrameAsset: null,
    productAsset: { type: "image", url: "/source.png" },
    productDetailAssets: [],
    productVideoTemplate: "prompt_driven",
    generationModelOptions: [],
    selectedGenerationModel: null,
  }).maskEditSupported,
  true,
  "legacy configurations without a selected catalog row must preserve mask editing",
);
const twoReferenceImageModel = {
  id: 31,
  capabilities: {
    image_to_image: true,
    reference_image: true,
    multi_reference: true,
    max_reference_images: 2,
  },
};
const twoImageReferencesWithoutDetails = imageEditSubmission({
  modelOption: singleReferenceOnlyImageModel,
});
assert.equal(twoImageReferencesWithoutDetails.ok, false);
assert.match(twoImageReferencesWithoutDetails.message, /未明确支持多张参考图/);
assert.equal(
  imageEditSubmission({ modelOption: twoReferenceImageModel }).ok,
  true,
  "all deduplicated image references should respect the declared two-image limit",
);
assert.equal(
  imageEditSubmission({
    modelOption: singleReferenceOnlyImageModel,
    selected: { type: "image", url: "/source.png" },
  }).ok,
  true,
  "duplicate image URLs should count once before enforcing reference limits",
);
assert.equal(videoModelRequiresFirstFrame(grok15Option), true);
assert.equal(videoModelRequiresFirstFrame(seedance15Option), false);
const grok15WithoutFirstFrame = videoSubmission({ modelOption: grok15Option });
assert.equal(grok15WithoutFirstFrame.ok, false);
assert.match(grok15WithoutFirstFrame.message, /上传视频首帧或切换模型/);
assert.equal(
  videoSubmission({
    modelOption: grok15Option,
    selected: { type: "image", url: "/first-frame.png" },
  }).ok,
  true,
  "Grok Imagine Video 1.5 should submit once an image first frame is selected",
);
const seedanceDirectVideo = videoSubmission({
  modelOption: seedance15Option,
  selected: { type: "video", url: "/source.mp4" },
});
assert.equal(seedanceDirectVideo.ok, false);
assert.match(seedanceDirectVideo.message, /不支持视频输入/);
assert.equal(
  videoSubmission({
    modelOption: seedance15Option,
    selected: { type: "video", url: "/source.mp4" },
    reverseOperationId: 91,
    reverseRevisionId: 92,
    reverseSourceSignature: "video|/source.mp4|",
  }).ok,
  true,
  "an applied reverse version should make its source video analysis provenance, not video-to-video input",
);
assert.equal(
  videoSubmission({
    modelOption: seedance15Option,
    firstLastFrameEnabled: true,
    selected: { type: "image", url: "/first.png" },
    lastFrameAsset: { type: "image", url: "/last.png" },
  }).ok,
  true,
  "a pure first/last-frame pair should use the dedicated capability instead of generic multi-reference",
);
const seedancePortraitReference = videoSubmission({
  modelOption: seedance15Option,
  creationMode: "video_edit",
  subjectMode: "portrait",
  isEditMode: true,
  productAsset: { type: "image", url: "/portrait.png" },
});
assert.equal(seedancePortraitReference.ok, false);
assert.match(seedancePortraitReference.message, /不支持独立人物参考图/);
const grok15Context = buildStudioModelContext({
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "general",
  productGenerationMode: false,
  selected: null,
  lastFrameAsset: null,
  productAsset: null,
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [grok15Option],
  selectedGenerationModel: grok15Option,
});
assert.equal(grok15Context.videoRequiresFirstFrame, true);
assert.equal(grok15Context.modelSwitchRequired, false);
const seedanceDirectVideoContext = buildStudioModelContext({
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "general",
  productGenerationMode: false,
  selected: { type: "video", url: "/source.mp4" },
  lastFrameAsset: null,
  productAsset: null,
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [seedance15Option],
  selectedGenerationModel: seedance15Option,
});
assert.equal(seedanceDirectVideoContext.generationInputUnsupported, true);
assert.equal(seedanceDirectVideoContext.modelSwitchRequired, true);
assert.match(seedanceDirectVideoContext.modelSwitchMessage, /不支持视频输入/);
const seedanceReverseBoundVideoContext = buildStudioModelContext({
  ...seedanceDirectVideoContext,
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "general",
  productGenerationMode: false,
  selected: { type: "video", url: "/source.mp4" },
  lastFrameAsset: null,
  productAsset: null,
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [seedance15Option],
  selectedGenerationModel: seedance15Option,
  reverseOperationId: 91,
  reverseRevisionId: 92,
  reverseSourceSignature: "video|/source.mp4|",
});
assert.equal(seedanceReverseBoundVideoContext.analysisOnlySourceVideo, true);
assert.equal(seedanceReverseBoundVideoContext.generationInputUnsupported, false);
assert.equal(seedanceReverseBoundVideoContext.modelSwitchRequired, false);
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
assert.deepEqual(
  filterModelOptions(
    [grokReferenceOption, grok15Option, seedance15Option, seedance20VideoOption],
    {
      use: "video",
      creationMode: "video_edit",
      selected: { type: "video", url: "/source.mp4" },
      productAsset: null,
    },
  ).map((item) => item.id),
  [20, 22],
  "video sources must only offer models with explicit video-to-video support",
);
assert.deepEqual(
  modelRequirements({
    use: "video",
    creationMode: "video",
    selected: { type: "video", url: "/source.mp4" },
    productAsset: null,
  }),
  [["video_reference", "video_edit", "video_to_video", "reference_video"]],
  "video input selection must accept either reference-generation or edit capability",
);
assert.deepEqual(
  modelRequirements({
    use: "video",
    creationMode: "video",
    selected: { type: "video", url: "/source.mp4" },
    productAsset: null,
    analysisOnlySourceVideo: true,
  }),
  [["video_generation", "text_to_video", "video"]],
  "a reverse-bound source video is analysis provenance rather than a provider video-to-video input",
);
assert.deepEqual(
  filterModelOptions(
    [grokReferenceOption, grok15Option, seedance15Option, seedance20VideoOption],
    {
      use: "video",
      creationMode: "video",
      selected: { type: "video", url: "/source.mp4" },
      productAsset: null,
      analysisOnlySourceVideo: true,
    },
  ).map((item) => item.id),
  [20, 15, 22],
  "reverse-bound video generation must use text-to-video capability instead of video-to-video capability",
);
const seedance20Option = {
  display_name: "Seedance 2.0",
  model_id: "doubao-seedance-2-0-mini-260615",
  capabilities: {
    text_to_video: true,
    image_to_video: true,
    reference_image: true,
    multi_reference: true,
    max_reference_images: 9,
    min_duration_seconds: 4,
    max_duration_seconds: 15,
    resolutions: ["480p", "720p"],
  },
};
assert.equal(strictMultiReferenceLimit(seedance20Option), 9);
assert.equal(
  validateMultiReferenceSelection(seedance20Option, 9, 8).ok,
  true,
  "Seedance 2.0 must allow at most nine total reference images",
);
assert.equal(
  validateMultiReferenceSelection(seedance20Option, 10, 9).ok,
  false,
  "Seedance 2.0 must reject more than nine total reference images",
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
assert.equal(seedance20ProductContext.modelSwitchRequired, false);
assert.equal(seedance20ProductContext.modelSwitchMessage, "");
assert.equal(seedance20ProductContext.minVideoDuration, 4);
assert.equal(seedance20ProductContext.maxVideoDuration, 15);
assert.deepEqual(seedance20ProductContext.videoResolutions, ["480p", "720p"]);
const grokProductContext = buildStudioModelContext({
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
  generationModelOptions: [grokReferenceOption],
  selectedGenerationModel: grokReferenceOption,
});
assert.equal(grokProductContext.maxVideoDuration, 10);
assert.equal(grokProductContext.minVideoDuration, 1);
assert.deepEqual(grokProductContext.videoResolutions, ["480p", "720p"]);
const grokVideoEditContext = buildStudioModelContext({
  cfg: {},
  category: "video",
  creationMode: "video",
  subjectMode: "general",
  productGenerationMode: false,
  selected: { type: "video", url: "/source.mp4" },
  lastFrameAsset: null,
  productAsset: null,
  productDetailAssets: [],
  productVideoTemplate: "prompt_driven",
  generationModelOptions: [grokReferenceOption],
  selectedGenerationModel: grokReferenceOption,
});
assert.equal(
  grokVideoEditContext.providerVideoEditMode,
  true,
  "a source video with edit-only provider semantics must enter provider video-edit mode",
);

function collectElementText(node) {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(collectElementText).join(" ");
  return collectElementText(node.props?.children);
}

const grokVideoEditControls = StudioGenerationControls({
  category: "video",
  ratioOptions: [{ key: "9:16", label: "9:16", w: 9, h: 16 }],
  ratio: "9:16",
  onRatioChange: () => {},
  minVideoDuration: 1,
  maxVideoDuration: 15,
  videoDuration: 15,
  vDuration: 15,
  onVideoDurationChange: () => {},
  vResolution: "720p",
  onVideoResolutionChange: () => {},
  providerVideoEditMode: true,
  showNegative: false,
  onToggleNegative: () => {},
  negative: "",
  onNegativeChange: () => {},
  onNegativeTouched: () => {},
  submitBar: null,
});
const grokVideoEditControlText = collectElementText(grokVideoEditControls);
assert.doesNotMatch(grokVideoEditControlText, /比例|时长|质量/);
assert.match(grokVideoEditControlText, /负向词/);
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
const creationConsoleSource = readFileSync(join(root, "app/studio/StudioCreationConsole.jsx"), "utf8");
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
  /const generationModelOptions = markGenerationModelCompatibility\(allModelOptions\[category\],/,
  "generation model selection must remain stable while incompatible choices are marked explicitly",
);
assert.match(
  submitSource,
  /analysisOnlySourceVideo:[\s\S]*?reverseOperationId[\s\S]*?reverseRevisionId/,
  "generation preflight must distinguish a reverse-bound source video from provider video-to-video input",
);
assert.match(
  creationConsoleSource,
  /mediaType: category === "video" && !videoRequiresFirstFrame \? "all" : "image"/,
  "video reverse asset picker must expose both image and video references",
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
