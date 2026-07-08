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

const productEditCenterBox = buildGenerationPayload({
  ...productEditArgs,
  prompt: "主体居中，使用中心兼容保护",
  structured: {},
  imageQuality: "1k",
  n: 1,
  editMaskMode: "center_box",
});

assert.equal(productEditCenterBox.payload.params.edit_mask_mode, "center_box");

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
};
const productVideoEditFree = buildGenerationPayload(productVideoEditArgs);

assert.equal(productVideoEditFree.payload.category, "video");
assert.equal(productVideoEditFree.payload.stage, "final");
assert.equal(productVideoEditFree.payload.parent_task_id, null);
assert.equal(productVideoEditFree.payload.params.subject_mode, "product");
assert.equal(productVideoEditFree.payload.params.product_lock_mode, "free");
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
});

assert.equal(productVideoEditLocked.payload.params.product_lock_mode, "locked");

const productVideoEditDefault = buildGenerationPayload({
  ...productVideoEditArgs,
  videoProductLockMode: undefined,
});

assert.equal(productVideoEditDefault.payload.params.product_lock_mode, "locked");

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
assert.equal(generateClientRequestId(ref, "preview", "sig-a", { storage, now: () => 1100, randomId: () => "other" }), firstId);
ref.current = null;
assert.equal(generateClientRequestId(ref, "preview", "sig-a", { storage, now: () => 1200, randomId: () => "other" }), firstId);
clearPendingGenerateRequest(ref, firstId, { storage });
assert.equal(fakeStorage.has(PENDING_GENERATE_STORAGE_KEY), false);

const reverseRef = { current: null };
const reverseId = generateReverseClientRequestId(reverseRef, "image", "asset-a", {
  storage,
  now: () => 1300,
  randomId: () => "rev-fixed",
});
assert.equal(reverseId, "studio-reverse-image-rev-fixed");
assert.equal(
  generateReverseClientRequestId(reverseRef, "image", "asset-a", { storage, now: () => 1400, randomId: () => "rev-other" }),
  reverseId,
);
reverseRef.current = null;
assert.equal(
  generateReverseClientRequestId(reverseRef, "image", "asset-a", { storage, now: () => 1500, randomId: () => "rev-other" }),
  reverseId,
);
clearPendingReverseRequest(reverseRef, reverseId, { storage });
assert.equal(fakeStorage.has(PENDING_REVERSE_STORAGE_KEY), false);

console.log("generation payload tests passed");
