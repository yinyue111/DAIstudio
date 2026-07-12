import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { buildGenerationPayload } from "../app/studio/generationPayload.ts";
import { MAX_VIDEO_DURATION_SECONDS, VIDEO_DURATION_PRESETS } from "../app/studio/constants.ts";
import {
  PENDING_GENERATE_STORAGE_KEY,
  PENDING_REVERSE_STORAGE_KEY,
  clearPendingGenerateRequest,
  clearPendingReverseRequest,
  generateClientRequestId,
  generateReverseClientRequestId,
} from "../app/studio/generationRequestId.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const submitHookSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");

assert.doesNotMatch(
  pageSource,
  /buildGenerationPayload\(/,
  "main page should not own generation payload assembly",
);
assert.match(
  pageSource,
  /useGenerationSubmit\(\{/,
  "main page should delegate generation submit behavior to a hook",
);
assert.match(
  submitHookSource,
  /buildGenerationPayload\(\{/,
  "generation submit hook should own payload assembly",
);
assert.match(
  submitHookSource,
  /generateClientRequestId\(/,
  "generation submit hook should own idempotent request ids",
);
assert.equal(MAX_VIDEO_DURATION_SECONDS, 15);
assert.deepEqual(
  VIDEO_DURATION_PRESETS.map((item) => item.seconds),
  [5, 8, 10, 15],
  "video duration presets should stop at the 15s generation limit",
);

const productAsset = {
  type: "image",
  url: "http://localhost:8000/api/uploads/upload/product.png",
  width: 1200,
  height: 1600,
};
const productAssetSignature = "image|http://localhost:8000/api/uploads/upload/product.png|";
const productProfile = {
  structured: {
    "产品品类": "全棉棉柔巾洗脸巾",
    "品牌Logo": "DAMAH 黑魔法 Logo 位于包装正面",
    "包装文字": "黑魔法、全棉棉柔巾、200抽",
    "不可改项": "Logo、包装文字、黑白包装结构、抽取式开口、品牌色必须完整保留",
  },
  final_text: "上传产品是唯一商品主角：DAMAH 黑魔法全棉棉柔巾洗脸巾，正面包装文字包含黑魔法、全棉棉柔巾、200抽；黑白包装结构、抽取式开口、Logo和所有文字必须完整保留，用它替换参考素材原主体。",
};
const styleAsset = {
  type: "image",
  url: "http://localhost:8000/api/uploads/upload/style.jpg",
  width: 900,
  height: 1200,
};

const productEditArgs = {
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image_edit",
  isEditMode: true,
  isImageEditMode: true,
  subjectMode: "product",
  prompt: "浴室柔光广告场景",
  negative: "低清",
  promptDirty: true,
  selected: styleAsset,
  productAsset,
  productProfile,
  productProfileSource: productAssetSignature,
  structured: {
    "主体": "Estee Lauder Advanced Night Repair 棕色玻璃滴管瓶",
    "商品服装": "参考商品 Logo 和包装文字清晰",
    "材质纹理": "amber glass, dropper bottle",
    "场景背景": "高端浴室台面",
    "广告目标": "高端护肤品社媒广告",
    "光线": "柔和自然光",
    "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  ratio: "4:5",
  imageQuality: "2k",
  n: 4,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
};
const productEdit = buildGenerationPayload(productEditArgs);

assert.equal(productEdit.effCategory, "image");
assert.equal(productEdit.payload.source_asset_url, productAsset.url);
assert.equal(productEdit.payload.params.subject_mode, "product");
assert.equal(productEdit.payload.params.style_reference_image, styleAsset.url);
assert.equal(productEdit.payload.params.edit_mask_mode, "protect_subject");
assert.equal(productEdit.payload.params.product_pixel_lock, "auto");
assert.equal(productEdit.payload.params.n, 4);
assert.match(productEdit.payload.prompt["产品身份档案"], /DAMAH 黑魔法全棉棉柔巾/);
assert.match(productEdit.payload.prompt.final_text, /DAMAH 黑魔法全棉棉柔巾/);
assert.match(productEdit.payload.prompt.final_text, /200抽/);
assert.match(productEdit.payload.prompt.final_text, /唯一产品身份/);
assert.match(productEdit.payload.prompt.final_text, /包装上的品牌名/);
assert.equal(productEdit.payload.prompt["场景背景"], "高端浴室台面");
assert.equal(productEdit.payload.prompt["光线"], "柔和自然光");
assert.equal(productEdit.payload.prompt["主体"], undefined);
assert.equal(productEdit.payload.prompt["商品服装"], undefined);
assert.equal(productEdit.payload.prompt["材质纹理"], undefined);
assert.equal(productEdit.payload.prompt["标签"], undefined);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /Estee Lauder/i);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /Advanced Night Repair/i);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /dropper bottle/i);
assert.match(productEdit.payload.params.negative_prompt, /包装文字被改写/);
assert.match(productEdit.payload.params.negative_prompt, /低清/);
assert.match(productEdit.payload.params.negative_prompt, /产品残缺/);
assert.match(productEdit.payload.params.negative_prompt, /产品被裁切/);

const staleReversePromptArgs = {
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image",
  isEditMode: false,
  prompt: "旧参考 A 的反推提示词，不应该继续参与新素材生成",
  promptDirty: false,
  promptSourceSignature: "image|http://localhost:8000/api/uploads/upload/old-style.jpg|",
  selected: styleAsset,
  structured: {},
  ratio: "4:5",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
};
const staleReversePrompt = buildGenerationPayload(staleReversePromptArgs);
assert.doesNotMatch(staleReversePrompt.payload.prompt.final_text, /旧参考 A/);
assert.match(staleReversePrompt.payload.prompt.final_text, /生成同风格的新素材/);

const userEditedPrompt = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: "用户手写的新提示词需要保留",
  promptDirty: true,
});
assert.match(userEditedPrompt.payload.prompt.final_text, /用户手写的新提示词需要保留/);

const productEditMaskOff = buildGenerationPayload({
  ...productEditArgs,
  prompt: "整体换成插画风格",
  structured: {},
  imageQuality: "1k",
  n: 1,
  editMaskMode: "off",
});

assert.equal(productEditMaskOff.payload.params.edit_mask_mode, "off");
assert.equal(productEditMaskOff.payload.params.product_pixel_lock, "off");

const productEditCenterBox = buildGenerationPayload({
  ...productEditArgs,
  prompt: "主体居中，使用中心兼容保护",
  structured: {},
  imageQuality: "1k",
  n: 1,
  editMaskMode: "center_box",
  productPixelLockMode: "strict",
});

assert.equal(productEditCenterBox.payload.params.edit_mask_mode, "center_box");
assert.equal(productEditCenterBox.payload.params.product_pixel_lock, "strict");

const variationSource = {
  id: 991,
  type: "image",
  url: "http://localhost:8000/media/preview/variant-source.jpg",
  preview_url: "http://localhost:8000/media/preview/variant-source.jpg",
  width: 1024,
  height: 1024,
};
const variationEdit = buildGenerationPayload({
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image_edit",
  isEditMode: true,
  isImageEditMode: true,
  subjectMode: "general",
  prompt: "基于这张图生成同主体、同风格的近似变体",
  promptDirty: true,
  selected: null,
  productAsset: variationSource,
  variationSource,
  structured: {},
  ratio: "1:1",
  imageQuality: "1k",
  n: 2,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});

assert.equal(variationEdit.payload.params.variation_of_asset_id, 991);
assert.equal(variationEdit.payload.params.style_reference_image, variationSource.url);
assert.equal(variationEdit.payload.source_asset_meta.variation_of_asset_id, 991);
assert.equal(variationEdit.payload.source_asset_meta.variation_source.selected_url, variationSource.url);

const portraitEdit = buildGenerationPayload({
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image_edit",
  isEditMode: true,
  isImageEditMode: true,
  subjectMode: "portrait",
  prompt: "小红书写真光线",
  promptDirty: true,
  selected: styleAsset,
  productAsset,
  structured: {},
  ratio: "2:3",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});

assert.equal(portraitEdit.payload.params.subject_mode, "portrait");
assert.equal(portraitEdit.payload.params.character_reference_image, productAsset.url);
assert.match(portraitEdit.payload.prompt.final_text, /人像照片作为唯一人物身份/);
assert.match(portraitEdit.payload.prompt.final_text, /专业商业人像/);
assert.doesNotMatch(portraitEdit.payload.prompt.final_text, /严格按照用户提示词执行局部或整体编辑/);
assert.match(portraitEdit.payload.params.negative_prompt, /身份不一致/);
assert.match(portraitEdit.payload.params.negative_prompt, /幼态成人化/);
assert.match(portraitEdit.payload.params.negative_prompt, /低机位身体凝视/);

const finalVideo = buildGenerationPayload({
  stage: "final",
  task: {
    id: 42,
    category: "video",
    params: { target_ratio: "9:16", target_resolution: "1080p", target_duration: 30 },
  },
  cfg: { video_duration_max_seconds: 900 },
  category: "image",
  creationMode: "image",
  isEditMode: false,
  ratio: "1:1",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});

assert.equal(finalVideo.payload.category, "video");
assert.equal(finalVideo.payload.parent_task_id, 42);
assert.equal(finalVideo.payload.params.resolution, "1080p");
assert.equal(finalVideo.payload.params.duration, 15);
assert.equal(finalVideo.ratioOption.key, "9:16");

const directFinalVideo = buildGenerationPayload({
  stage: "final",
  task: null,
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  prompt: "竖屏短视频成片，产品居中展示，柔和棚拍光线",
  promptDirty: true,
  selected: null,
  structured: {},
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 10,
  vResolution: "1080p",
});

assert.equal(directFinalVideo.payload.category, "video");
assert.equal(directFinalVideo.payload.stage, "final");
assert.equal(directFinalVideo.payload.parent_task_id, null);
assert.equal(directFinalVideo.payload.params.duration, 10);
assert.equal(directFinalVideo.payload.params.resolution, "1080p");
assert.equal(directFinalVideo.payload.params.target_resolution, "1080p");
assert.equal(directFinalVideo.ratioOption.key, "9:16");

const productVideoEditArgs = {
  stage: "final",
  cfg: { video_duration_max_seconds: 900 },
  category: "video",
  creationMode: "video_edit",
  isEditMode: true,
  isImageEditMode: false,
  subjectMode: "product",
  prompt: "产品旋转展示，水花飞溅，镜头推进",
  promptDirty: true,
  selected: styleAsset,
  productAsset,
  productProfile,
  productProfileSource: productAssetSignature,
  structured: {
    "主体": "参考视频中的 Estee Lauder 棕色滴管瓶",
    "商品服装": "Advanced Night Repair bottle, gold cap",
    "材质纹理": "amber glass, dropper bottle",
    "场景背景": "暖棕色广告棚景和金色沙粒台面",
    "视角构图": "竖屏 9:16, 产品居中, 浅景深",
    "主体动作": "参考商品从画面左侧入场，缓慢旋转，水花飞溅后切到 Logo 特写",
    "镜头运动": "缓慢推进并轻微环绕",
    "光线": "柔和电影感主光和边缘高光",
    "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
  videoProductLockMode: "free",
  videoProductTemplate: "soft_splash",
};
const productVideoEditFree = buildGenerationPayload(productVideoEditArgs);

assert.equal(productVideoEditFree.payload.category, "video");
assert.equal(productVideoEditFree.payload.stage, "final");
assert.equal(productVideoEditFree.payload.parent_task_id, null);
assert.equal(productVideoEditFree.payload.params.subject_mode, "product");
assert.equal(productVideoEditFree.payload.params.product_lock_mode, "free");
assert.equal(productVideoEditFree.payload.params.product_video_template, "soft_splash");
assert.equal(productVideoEditFree.payload.params.reference_image_url, productAsset.url);
assert.equal(productVideoEditFree.payload.prompt["场景背景"], "暖棕色广告棚景和金色沙粒台面");
assert.match(productVideoEditFree.payload.prompt["主体动作"], /上传产品作为唯一视频主体/);
assert.equal(productVideoEditFree.payload.prompt["镜头运动"], "缓慢推进并轻微环绕");
assert.match(productVideoEditFree.payload.prompt["产品身份档案"], /DAMAH 黑魔法全棉棉柔巾/);
assert.equal(productVideoEditFree.payload.prompt["主体"], undefined);
assert.equal(productVideoEditFree.payload.prompt["商品服装"], undefined);
assert.equal(productVideoEditFree.payload.prompt["材质纹理"], undefined);
assert.equal(productVideoEditFree.payload.prompt["标签"], undefined);
assert.equal(productVideoEditFree.payload.prompt.user_instruction, "产品旋转展示，水花飞溅，镜头推进");
assert.match(productVideoEditFree.payload.prompt.final_text, /用上传产品替换参考视频/);
assert.match(productVideoEditFree.payload.prompt.final_text, /DAMAH 黑魔法全棉棉柔巾/);
assert.match(productVideoEditFree.payload.prompt.final_text, /200抽/);
assert.match(productVideoEditFree.payload.prompt.final_text, /可适配到产品的展示动作/);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /Estee Lauder/i);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /Advanced Night Repair/i);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /dropper bottle/i);
assert.match(productVideoEditFree.payload.params.negative_prompt, /产品正面文字被重排/);

const productVideoEditLocked = buildGenerationPayload({
  ...productVideoEditArgs,
  videoProductLockMode: "locked",
  videoProductTemplate: "slow_push",
});

assert.equal(productVideoEditLocked.payload.params.product_lock_mode, "locked");
assert.equal(productVideoEditLocked.payload.params.product_video_template, "slow_push");

const productVideoEditDefault = buildGenerationPayload({
  ...productVideoEditArgs,
  videoProductLockMode: undefined,
  videoProductTemplate: undefined,
});

assert.equal(productVideoEditDefault.payload.params.product_lock_mode, "locked");
assert.equal(productVideoEditDefault.payload.params.product_video_template, "stable_showcase");

const fakeStorage = new Map();
const storage = {
  getItem: (key) => fakeStorage.get(key) ?? null,
  setItem: (key, value) => fakeStorage.set(key, value),
  removeItem: (key) => fakeStorage.delete(key),
};
const ref = { current: null };
const firstId = generateClientRequestId(ref, "preview", "sig-a", {
  storage,
  now: () => 1000,
  randomId: () => "fixed",
});
assert.equal(firstId, "studio-preview-fixed");
const secondId = generateClientRequestId(ref, "preview", "sig-b", {
  storage,
  now: () => 1100,
  randomId: () => "second",
});
assert.equal(secondId, "studio-preview-second");
assert.equal(
  generateClientRequestId(ref, "preview", "sig-a", { storage, now: () => 1200, randomId: () => "retry-a" }),
  firstId,
  "retrying generation A after generation B starts should reuse A's original request id",
);
ref.current = null;
assert.equal(
  generateClientRequestId(ref, "preview", "sig-b", { storage, now: () => 1300, randomId: () => "stored-b" }),
  secondId,
  "generation B should remain reusable from session storage",
);
clearPendingGenerateRequest(ref, firstId, { storage, now: () => 1350 });
ref.current = null;
assert.equal(
  generateClientRequestId(ref, "preview", "sig-b", { storage, now: () => 1400, randomId: () => "after-clear-b" }),
  secondId,
  "clearing generation A should preserve generation B",
);
const replacementFirstId = generateClientRequestId(
  ref,
  "preview",
  "sig-a",
  { storage, now: () => 1500, randomId: () => "after-clear-a" },
);
assert.equal(
  replacementFirstId,
  "studio-preview-after-clear-a",
  "clearing generation A should remove only A",
);
clearPendingGenerateRequest(ref, secondId, { storage, now: () => 1600 });
clearPendingGenerateRequest(ref, replacementFirstId, { storage, now: () => 1650 });
assert.equal(fakeStorage.has(PENDING_GENERATE_STORAGE_KEY), false);

const reverseRef = { current: null };
const reverseId = generateReverseClientRequestId(reverseRef, "image", "asset-a", {
  storage,
  now: () => 2000,
  randomId: () => "rev-fixed",
});
assert.equal(reverseId, "studio-reverse-image-rev-fixed");
const secondReverseId = generateReverseClientRequestId(reverseRef, "image", "asset-b", {
  storage,
  now: () => 2100,
  randomId: () => "rev-second",
});
assert.equal(secondReverseId, "studio-reverse-image-rev-second");
assert.equal(
  generateReverseClientRequestId(reverseRef, "image", "asset-a", { storage, now: () => 2200, randomId: () => "rev-retry-a" }),
  reverseId,
  "retrying reverse A after reverse B starts should reuse A's original request id",
);
reverseRef.current = null;
assert.equal(
  generateReverseClientRequestId(reverseRef, "image", "asset-b", { storage, now: () => 2300, randomId: () => "rev-stored-b" }),
  secondReverseId,
  "reverse B should remain reusable from session storage",
);
clearPendingReverseRequest(reverseRef, reverseId, { storage, now: () => 2350 });
reverseRef.current = null;
assert.equal(
  generateReverseClientRequestId(reverseRef, "image", "asset-b", { storage, now: () => 2400, randomId: () => "rev-after-clear-b" }),
  secondReverseId,
  "clearing reverse A should preserve reverse B",
);
const replacementReverseId = generateReverseClientRequestId(
  reverseRef,
  "image",
  "asset-a",
  { storage, now: () => 2500, randomId: () => "rev-after-clear-a" },
);
assert.equal(
  replacementReverseId,
  "studio-reverse-image-rev-after-clear-a",
  "clearing reverse A should remove only A",
);
clearPendingReverseRequest(reverseRef, secondReverseId, { storage, now: () => 2600 });
clearPendingReverseRequest(reverseRef, replacementReverseId, { storage, now: () => 2650 });
assert.equal(fakeStorage.has(PENDING_REVERSE_STORAGE_KEY), false);

const sharedReverseStorageData = new Map();
const sharedReverseStorage = {
  getItem: (key) => sharedReverseStorageData.get(key) ?? null,
  setItem: (key, value) => sharedReverseStorageData.set(key, value),
  removeItem: (key) => sharedReverseStorageData.delete(key),
};
const firstSharedReverseRef = { current: null };
const staleSharedReverseRef = { current: null };
const clearedSharedReverseId = generateReverseClientRequestId(
  firstSharedReverseRef,
  "image",
  "shared-clear-a",
  { storage: sharedReverseStorage, now: () => 2700, randomId: () => "shared-clear-a" },
);
generateReverseClientRequestId(
  staleSharedReverseRef,
  "image",
  "shared-keep-b",
  { storage: sharedReverseStorage, now: () => 2800, randomId: () => "shared-keep-b" },
);
clearPendingReverseRequest(firstSharedReverseRef, clearedSharedReverseId, {
  storage: sharedReverseStorage,
  now: () => 2900,
});
generateReverseClientRequestId(
  staleSharedReverseRef,
  "image",
  "shared-new-c",
  { storage: sharedReverseStorage, now: () => 3000, randomId: () => "shared-new-c" },
);
assert.equal(
  generateReverseClientRequestId(
    { current: null },
    "image",
    "shared-clear-a",
    { storage: sharedReverseStorage, now: () => 3100, randomId: () => "shared-clear-a-replacement" },
  ),
  "studio-reverse-image-shared-clear-a-replacement",
  "a stale ref must not write a request id back after another ref clears it from shared storage",
);

const unassignedClearStorageData = new Map();
const unassignedClearStorage = {
  getItem: (key) => unassignedClearStorageData.get(key) ?? null,
  setItem: (key, value) => unassignedClearStorageData.set(key, value),
  removeItem: (key) => unassignedClearStorageData.delete(key),
};
const unassignedClearRef = { current: null };
const preservedGenerateId = generateClientRequestId(unassignedClearRef, "preview", "preserve-existing", {
  storage: unassignedClearStorage,
  now: () => 3200,
  randomId: () => "preserve-existing",
});
clearPendingGenerateRequest(unassignedClearRef, null, { storage: unassignedClearStorage, now: () => 3300 });
assert.equal(
  generateClientRequestId(unassignedClearRef, "preview", "preserve-existing", {
    storage: unassignedClearStorage,
    now: () => 3400,
    randomId: () => "preserve-existing-replacement",
  }),
  preservedGenerateId,
  "clearing an unassigned request id must not erase unrelated pending generation ids",
);

const staleStorageData = new Map();
let staleStorageWritesBlocked = false;
const staleStorage = {
  getItem: (key) => staleStorageData.get(key) ?? null,
  setItem: (key, value) => {
    if (staleStorageWritesBlocked) throw new Error("storage writes blocked");
    staleStorageData.set(key, value);
  },
  removeItem: (key) => {
    if (staleStorageWritesBlocked) throw new Error("storage removes blocked");
    staleStorageData.delete(key);
  },
};
const staleStorageRef = { current: null };
const staleStorageA = generateClientRequestId(staleStorageRef, "preview", "stale-a", {
  storage: staleStorage,
  now: () => 3000,
  randomId: () => "stale-a",
});
const staleStorageB = generateClientRequestId(staleStorageRef, "preview", "stale-b", {
  storage: staleStorage,
  now: () => 3100,
  randomId: () => "stale-b",
});
staleStorageWritesBlocked = true;
clearPendingGenerateRequest(staleStorageRef, staleStorageA, { storage: staleStorage, now: () => 3200 });
const replacementStaleStorageA = generateClientRequestId(staleStorageRef, "preview", "stale-a", {
  storage: staleStorage,
  now: () => 3300,
  randomId: () => "replacement-stale-a",
});
assert.equal(
  replacementStaleStorageA,
  "studio-preview-replacement-stale-a",
  "a failed storage write must not let stale storage resurrect a cleared request",
);
assert.equal(
  generateClientRequestId(staleStorageRef, "preview", "stale-b", {
    storage: staleStorage,
    now: () => 3400,
    randomId: () => "replacement-stale-b",
  }),
  staleStorageB,
  "disabling stale storage should preserve other in-memory pending requests",
);
staleStorageWritesBlocked = false;
assert.equal(
  generateClientRequestId({ current: null }, "preview", "stale-a", {
    storage: staleStorage,
    now: () => 3450,
    randomId: () => "recovered-stale-a",
  }),
  replacementStaleStorageA,
  "a new ref must reuse the in-memory replacement instead of reviving stale storage after a write failure",
);
staleStorageWritesBlocked = true;
clearPendingGenerateRequest(staleStorageRef, staleStorageB, { storage: staleStorage, now: () => 3500 });
assert.equal(
  generateClientRequestId(staleStorageRef, "preview", "stale-b", {
    storage: staleStorage,
    now: () => 3600,
    randomId: () => "after-failed-remove",
  }),
  "studio-preview-after-failed-remove",
  "a failed storage remove must keep stale storage disabled for the current ref",
);

const ttlStorageData = new Map();
const ttlStorage = {
  getItem: (key) => ttlStorageData.get(key) ?? null,
  setItem: (key, value) => ttlStorageData.set(key, value),
  removeItem: (key) => ttlStorageData.delete(key),
};
const ttlRef = { current: null };
const ttlStart = 10_000;
const ttlRequestA = generateClientRequestId(ttlRef, "preview", "ttl-a", {
  storage: ttlStorage,
  now: () => ttlStart,
  randomId: () => "ttl-a",
});
const ttlRequestB = generateClientRequestId(ttlRef, "preview", "ttl-b", {
  storage: ttlStorage,
  now: () => ttlStart + 60 * 60 * 1000,
  randomId: () => "ttl-b",
});
ttlRef.current = null;
assert.equal(
  generateClientRequestId(ttlRef, "preview", "ttl-a", {
    storage: ttlStorage,
    now: () => ttlStart + 2 * 60 * 60 * 1000 + 1,
    randomId: () => "ttl-a-expired",
  }),
  "studio-preview-ttl-a-expired",
  "expired pending request ids should not be reused",
);
assert.equal(
  generateClientRequestId(ttlRef, "preview", "ttl-b", {
    storage: ttlStorage,
    now: () => ttlStart + 2 * 60 * 60 * 1000 + 2,
    randomId: () => "ttl-b-replacement",
  }),
  ttlRequestB,
  "unexpired pending request ids should survive cleanup of expired peers",
);
assert.notEqual(ttlRequestA, "studio-preview-ttl-a-expired");

const boundedRef = { current: null };
const boundedIds = [];
for (let index = 0; index < 17; index += 1) {
  boundedIds.push(generateClientRequestId(boundedRef, "preview", `bounded-${index}`, {
    storage: null,
    now: () => 20_000 + index,
    randomId: () => `bounded-${index}`,
  }));
}
assert.equal(
  generateClientRequestId(boundedRef, "preview", "bounded-1", {
    storage: null,
    now: () => 21_000,
    randomId: () => "bounded-1-replacement",
  }),
  boundedIds[1],
  "the newest 16 pending request ids should remain reusable",
);
assert.equal(
  generateClientRequestId(boundedRef, "preview", "bounded-0", {
    storage: null,
    now: () => 21_001,
    randomId: () => "bounded-0-replacement",
  }),
  "studio-preview-bounded-0-replacement",
  "the oldest pending request id should be evicted after the collection reaches 16 entries",
);

const legacyStorageData = new Map([
  [PENDING_GENERATE_STORAGE_KEY, JSON.stringify({
    scope: "preview",
    signature: "legacy-single",
    id: "studio-preview-legacy-single",
    createdAt: 30_000,
  })],
]);
const legacyStorage = {
  getItem: (key) => legacyStorageData.get(key) ?? null,
  setItem: (key, value) => legacyStorageData.set(key, value),
  removeItem: (key) => legacyStorageData.delete(key),
};
assert.equal(
  generateClientRequestId({ current: null }, "preview", "legacy-single", {
    storage: legacyStorage,
    now: () => 30_100,
    randomId: () => "legacy-replacement",
  }),
  "studio-preview-legacy-single",
  "legacy single-record JSON should remain reusable",
);

const throwingStorage = {
  getItem: () => { throw new Error("storage reads blocked"); },
  setItem: () => { throw new Error("storage writes blocked"); },
  removeItem: () => { throw new Error("storage removes blocked"); },
};
const throwingStorageRef = { current: null };
const throwingStorageA = generateReverseClientRequestId(throwingStorageRef, "image", "throw-a", {
  storage: throwingStorage,
  now: () => 40_000,
  randomId: () => "throw-a",
});
const throwingStorageB = generateReverseClientRequestId(throwingStorageRef, "image", "throw-b", {
  storage: throwingStorage,
  now: () => 40_100,
  randomId: () => "throw-b",
});
assert.equal(
  generateReverseClientRequestId(throwingStorageRef, "image", "throw-a", {
    storage: throwingStorage,
    now: () => 40_200,
    randomId: () => "throw-a-retry",
  }),
  throwingStorageA,
  "request ids should remain reusable in memory when storage reads and writes throw",
);
clearPendingReverseRequest(throwingStorageRef, throwingStorageA, { storage: throwingStorage, now: () => 40_300 });
assert.equal(
  generateReverseClientRequestId(throwingStorageRef, "image", "throw-a", {
    storage: throwingStorage,
    now: () => 40_400,
    randomId: () => "throw-a-replacement",
  }),
  "studio-reverse-image-throw-a-replacement",
);
assert.equal(
  generateReverseClientRequestId(throwingStorageRef, "image", "throw-b", {
    storage: throwingStorage,
    now: () => 40_500,
    randomId: () => "throw-b-replacement",
  }),
  throwingStorageB,
);
clearPendingReverseRequest(throwingStorageRef, throwingStorageB, { storage: throwingStorage, now: () => 40_600 });

const removeFailureStorageData = new Map();
const removeFailureStorage = {
  getItem: (key) => removeFailureStorageData.get(key) ?? null,
  setItem: (key, value) => removeFailureStorageData.set(key, value),
  removeItem: () => { throw new Error("storage removes blocked"); },
};
const removeFailureRef = { current: null };
const removeFailureId = generateClientRequestId(removeFailureRef, "preview", "remove-failure", {
  storage: removeFailureStorage,
  now: () => 50_000,
  randomId: () => "remove-failure",
});
clearPendingGenerateRequest(removeFailureRef, removeFailureId, { storage: removeFailureStorage, now: () => 50_100 });
assert.equal(
  generateClientRequestId(removeFailureRef, "preview", "remove-failure", {
    storage: removeFailureStorage,
    now: () => 50_200,
    randomId: () => "after-remove-failure",
  }),
  "studio-preview-after-remove-failure",
  "a failed storage remove must not resurrect the cleared in-memory request",
);
assert.notEqual(removeFailureId, "studio-preview-after-remove-failure");

console.log("generation payload tests passed");
