import assert from "node:assert/strict";
import React from "react";

import { generationRequiresPendingUpload } from "../app/studio/generationUploadPolicy.ts";
import StudioSubmitBar from "../app/studio/StudioSubmitBar.jsx";
import { buildGenerationPayload } from "../app/studio/generationPayload.ts";
import { validateGenerationSubmission } from "../hooks/generationSubmitWorkflow.js";

globalThis.React = React;

const baseSubmission = {
  uploading: true,
  parsing: false,
  reversing: false,
  productProfiling: false,
  structuredDirty: false,
  modelOption: { capabilities: {} },
  productVideoTemplate: "prompt_driven",
  category: "image",
  creationMode: "image",
  subjectMode: "general",
  firstLastFrameEnabled: false,
  lastFrameAsset: null,
  selected: null,
  productAsset: null,
  productDetailAssets: [],
  task: null,
  isEditMode: false,
  isImageEditMode: false,
  prompt: "高端日系美妆广告",
  structured: {},
};

assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "reference", selected: null }),
  false,
  "a first optional reference upload must not block prompt-only generation",
);
assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "video_reference", selected: null }),
  false,
  "a first optional video reference upload must not block prompt-only generation",
);
assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "reference", selected: { id: 1 } }),
  true,
  "replacing an existing reference must wait instead of submitting the old asset",
);
assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "product", selected: null }),
  true,
  "a pending product subject is required once the user starts uploading it",
);
assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "last_frame", selected: null }),
  true,
  "an explicit last frame upload must finish before generation",
);
assert.equal(
  generationRequiresPendingUpload({ uploading: true, uploadingRole: "product_detail", selected: null }),
  false,
  "optional product details should not hold up a request that can use completed assets",
);

assert.equal(
  validateGenerationSubmission({ ...baseSubmission, uploadingRole: "reference" }).ok,
  true,
  "submission preflight should allow generation without a completed optional reference",
);
assert.equal(
  validateGenerationSubmission({
    ...baseSubmission,
    uploadingRole: "video_reference",
    category: "video",
    creationMode: "video",
  }).ok,
  true,
  "video generation should also proceed without a completed optional reference",
);
assert.match(
  validateGenerationSubmission({ ...baseSubmission, uploadingRole: "product" }).message,
  /需要的素材仍在上传中/,
  "submission preflight must still block required uploads",
);
assert.match(
  validateGenerationSubmission({
    ...baseSubmission,
    uploadingRole: "reference",
    selected: { type: "image", url: "/old-reference.png" },
  }).message,
  /需要的素材仍在上传中/,
  "submission preflight must not submit an old reference while its replacement uploads",
);

const promptOnlyPayload = buildGenerationPayload({
  category: "image",
  creationMode: "image",
  prompt: "高端日系美妆广告",
  ratio: "1:1",
  imageQuality: "1k",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
});
assert.equal(promptOnlyPayload.payload.source_asset_url, null);
assert.equal(promptOnlyPayload.payload.params.reference_image_url, undefined);

let submissions = 0;
const submitBarInput = {
  category: "image",
  videoFinalCost: 0,
  estCost: 1,
  imageCount: 1,
  videoDuration: 5,
  vResolution: "720p",
  submit: () => { submissions += 1; },
  missingRequiredSource: false,
  missingRequiredSourceLabel: "请先上传图片",
  videoModelSwitchRequired: false,
  structuredDirty: false,
  submitting: false,
  parsing: false,
  uploadBlocked: false,
  reversing: false,
  productProfiling: false,
  task: null,
  currentModelEnabled: true,
  running: false,
  submitLabel: "生成图片",
};
const optionalUploadBar = StudioSubmitBar(submitBarInput);
const optionalUploadButton = optionalUploadBar.props.children[1];
assert.equal(optionalUploadButton.props.disabled, false);
optionalUploadButton.props.onClick();
assert.equal(submissions, 1, "the enabled submit button should start prompt-only generation immediately");

const requiredUploadBar = StudioSubmitBar({
  ...submitBarInput,
  uploadBlocked: true,
});
const requiredUploadButton = requiredUploadBar.props.children[1];
assert.equal(requiredUploadButton.props.disabled, true);
assert.equal(requiredUploadButton.props.children.at(-1), "素材上传中");
