import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readStudioSource } from "./studio-source.mjs";

import { buildGenerationPayload } from "../app/studio/generationPayload.ts";
import { studioCreationFacts } from "../app/studio/viewModel.ts";
import {
  MAX_PRODUCT_DETAIL_IMAGES,
  MAX_VIDEO_DURATION_SECONDS,
  VIDEO_DURATION_PRESETS,
} from "../app/studio/constants.ts";
import { buildStudioDerivedViewState } from "../app/studio/viewModel.ts";
import {
  composePromptFromStructured,
  reverseVideoWorkspacePatch,
} from "../app/studio/helpers.ts";
import {
  PENDING_GENERATE_STORAGE_KEY,
  PENDING_REVERSE_STORAGE_KEY,
  clearPendingGenerateRequest,
  clearPendingReverseRequest,
  generateClientRequestId,
  generateReverseClientRequestId,
  shouldKeepPendingReverseRequest,
} from "../app/studio/generationRequestId.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readStudioSource(root);
const submitHookSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const submitWorkflowSource = readFileSync(join(root, "hooks/generationSubmitWorkflow.js"), "utf8");

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
  submitWorkflowSource,
  /buildGenerationPayload\(\{/,
  "generation submit workflow should own payload assembly",
);
assert.match(
  submitWorkflowSource,
  /generateClientRequestId\(/,
  "generation submit workflow should own idempotent request ids",
);
assert.equal(MAX_VIDEO_DURATION_SECONDS, 15);
assert.equal(MAX_PRODUCT_DETAIL_IMAGES, 10);
assert.deepEqual(
  VIDEO_DURATION_PRESETS.map((item) => item.seconds),
  [5, 8, 10, 15],
  "video duration presets should stop at the 15s generation limit",
);

const serumReverseAnalysis = {
  source: {
    width: 720,
    height: 960,
    ratio: "3:4",
    duration_seconds: 10.053991,
    fps: 23,
    has_audio: true,
    audio_analyzed: false,
  },
  sampled_frames: [
    { index: 1, timestamp_seconds: 0 },
    { index: 7, timestamp_seconds: 10.004 },
  ],
  shots: [
    { start_seconds: 0, end_seconds: 2.2, visual: "液滴入水" },
    { start_seconds: 2.2, end_seconds: 4.2, visual: "倒入手心" },
  ],
};

assert.deepEqual(
  reverseVideoWorkspacePatch({
    analysis: serumReverseAnalysis,
    current: {
      ratio: "9:16",
      vDuration: 5,
    },
    startedRatio: "9:16",
    startedDuration: 5,
    maxDuration: 15,
  }),
  {
    reverseVideoAnalysis: serumReverseAnalysis,
    ratio: "3:4",
    vDuration: 10,
  },
  "video reverse should inherit exact source ratio/duration without mutating hidden product-motion state",
);

assert.deepEqual(
  reverseVideoWorkspacePatch({
    analysis: serumReverseAnalysis,
    current: { ratio: "1:1", vDuration: 8, videoProductLockMode: "locked" },
    startedRatio: "9:16",
    startedDuration: 5,
    maxDuration: 15,
    productVideo: false,
  }),
  { reverseVideoAnalysis: serumReverseAnalysis },
  "late reverse responses must not overwrite ratio or duration changed by the user",
);

const canonicalReversePrompt = "参考图复刻：成年人物后仰斜坐，腰腿高度低机位向上约15度，35-45mm近距离透视；保持服装覆盖下较饱满胸廓、自然腰线、较宽胯部与近镜大腿，左前上方大面积柔光、冷紫轮廓光、抬升黑位和宽范围高光扩散。";
assert.equal(
  composePromptFromStructured(
    {
      "图像类型": "人物图",
      "主体": "成年女性模特坐姿",
      "人物比例": "纤细修长的7.5头身",
      "构图": "竖版9:16，主体占画幅82%",
      "光线": "左前上方大面积柔光",
    },
    canonicalReversePrompt,
    { preferFallback: true },
  ),
  canonicalReversePrompt,
  "reverse parsing should preserve the API final_text instead of rebuilding every structured field",
);

const reverseVideoFinalText = "高端日系个护广告。Shot 1：抽出洗脸巾。Shot 2：微距展开如意云纹。";
const reverseVideoPayload = buildGenerationPayload({
  stage: "preview",
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  subjectMode: "general",
  prompt: reverseVideoFinalText,
  promptDirty: false,
  promptSourceSignature: "video|https://cdn.example.com/reference.mp4|https://cdn.example.com/cover.jpg",
  selected: {
    type: "video",
    url: "https://cdn.example.com/reference.mp4",
    thumb: "https://cdn.example.com/cover.jpg",
    width: 720,
    height: 1280,
  },
  productAsset: null,
  structured: {
    "风格": "高端日系个护广告",
    "可迁移主体动作": "抽出洗脸巾后微距展开纹理",
    "主体动作": "未证实的顶层动作",
    "镜头运动": "未证实的顶层运镜",
    "剪辑节奏": "未证实的顶层剪辑",
    "时序分镜": "0-5s 抽出洗脸巾；5-10s 微距展开如意云纹",
    "字幕卖点": "干湿两用",
    "旁白": "模型幻觉旁白",
    "音效": "模型幻觉音效",
    "OCR证据": "不得下发的 OCR",
    "分析证据": "不得下发的证据",
    "未知字段": "不得下发的未知内容",
  },
  structuredSource: "video|https://cdn.example.com/reference.mp4|https://cdn.example.com/cover.jpg",
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  vDuration: 10,
  vResolution: "720p",
});
assert.equal(reverseVideoPayload.finalText, reverseVideoFinalText);
assert.doesNotMatch(reverseVideoPayload.finalText, /参考品牌|原品牌/);
assert.equal(reverseVideoPayload.payload.prompt.raw_text, reverseVideoPayload.finalText);
assert.equal(reverseVideoPayload.payload.prompt.assembled_text, reverseVideoPayload.finalText);
for (const field of [
  "可迁移主体动作", "主体动作", "镜头运动", "剪辑节奏", "时序分镜",
  "字幕卖点", "旁白", "音效", "OCR证据", "分析证据", "未知字段",
]) assert.equal(reverseVideoPayload.payload.prompt[field], undefined);
assert.doesNotMatch(
  JSON.stringify(reverseVideoPayload.payload.prompt),
  /未证实的顶层|模型幻觉|不得下发/,
  "audit-only video fields must not cross the final generation boundary",
);

const providerVideoEditPayload = buildGenerationPayload({
  stage: "preview",
  cfg: {
    video_duration_min_seconds: 1,
    video_duration_max_seconds: 15,
  },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  subjectMode: "general",
  prompt: "保持原片时序，将画面调整为低饱和护肤品广告",
  promptDirty: true,
  selected: {
    type: "video",
    url: "https://cdn.example.com/source-edit.mp4",
    width: 1080,
    height: 1920,
  },
  productAsset: null,
  structured: {},
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  vDuration: 15,
  vResolution: "1080p",
  providerVideoEditMode: true,
});
assert.equal(
  providerVideoEditPayload.payload.source_asset_url,
  "https://cdn.example.com/source-edit.mp4",
  "provider video editing must keep the source video",
);
assert.deepEqual(
  providerVideoEditPayload.payload.params,
  {},
  "provider video editing must not send generation-only duration, resolution, or ratio controls",
);
for (const field of ["duration", "resolution", "target_resolution", "ratio"]) {
  assert.equal(providerVideoEditPayload.payload.params[field], undefined);
}

const firstFrameAsset = {
  id: 701,
  type: "image",
  url: "https://cdn.example.com/first-frame.jpg",
  width: 1280,
  height: 720,
};
const lastFrameAsset = {
  id: 702,
  type: "image",
  url: "https://cdn.example.com/last-frame.jpg",
  width: 1280,
  height: 720,
};
const firstLastFrameVideoArgs = {
  stage: "preview",
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  subjectMode: "general",
  prompt: "镜头从首帧平稳过渡到尾帧",
  promptDirty: true,
  selected: firstFrameAsset,
  lastFrameAsset,
  firstLastFrameEnabled: true,
  productAsset: null,
  structured: {},
  ratio: "16:9",
  imageQuality: "1k",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
};
const firstLastFrameVideo = buildGenerationPayload(firstLastFrameVideoArgs);
assert.equal(firstLastFrameVideo.payload.params.first_frame_image, firstFrameAsset.url);
assert.equal(firstLastFrameVideo.payload.params.last_frame_image, lastFrameAsset.url);
assert.equal(firstLastFrameVideo.payload.params.reference_image_url, undefined);
assert.equal(
  firstLastFrameVideo.payload.source_asset_meta.last_frame.selected_url,
  lastFrameAsset.url,
  "the second image should retain source provenance as the video last frame",
);

const unsupportedFirstLastFrameVideo = buildGenerationPayload({
  ...firstLastFrameVideoArgs,
  firstLastFrameEnabled: false,
});
assert.equal(unsupportedFirstLastFrameVideo.payload.params.first_frame_image, undefined);
assert.equal(unsupportedFirstLastFrameVideo.payload.params.last_frame_image, undefined);
assert.equal(
  unsupportedFirstLastFrameVideo.payload.params.reference_image_url,
  firstFrameAsset.url,
  "models without the declared capability must keep the existing single-reference payload",
);

const productModeIgnoresLastFrame = buildGenerationPayload({
  ...firstLastFrameVideoArgs,
  subjectMode: "product",
  productAsset: firstFrameAsset,
});
assert.equal(productModeIgnoresLastFrame.payload.params.product_reference_image, firstFrameAsset.url);
assert.equal(productModeIgnoresLastFrame.payload.params.first_frame_image, undefined);
assert.equal(productModeIgnoresLastFrame.payload.params.last_frame_image, undefined);

assert.throws(
  () => buildGenerationPayload({
    ...firstLastFrameVideoArgs,
    lastFrameAsset: { ...lastFrameAsset, url: firstFrameAsset.url },
  }),
  /首帧和尾帧不能使用同一张图片/,
);
assert.throws(
  () => buildGenerationPayload({
    ...firstLastFrameVideoArgs,
    lastFrameAsset: { ...lastFrameAsset, type: "video" },
  }),
  /尾帧只支持图片素材/,
);

assert.equal(
  shouldKeepPendingReverseRequest({ status: 409, message: "反推请求仍在处理中,请稍后重试" }),
  true,
);
assert.equal(shouldKeepPendingReverseRequest({ status: 502, message: "网关错误" }), false);

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
    "负向": "Logo 变形、包装文字乱码、多余商品",
  },
  final_text: "上传产品是唯一商品主角：DAMAH 黑魔法全棉棉柔巾洗脸巾，正面包装文字包含黑魔法、全棉棉柔巾、200抽；黑白包装结构、抽取式开口、Logo和所有文字必须完整保留，用它替换参考素材原主体。",
};
const styleAsset = {
  type: "image",
  url: "http://localhost:8000/api/uploads/upload/style.jpg",
  width: 900,
  height: 1200,
};
const productDetails = [
  { type: "image", url: "http://localhost:8000/api/uploads/upload/detail-front.png" },
  { type: "image", url: "http://localhost:8000/api/uploads/upload/detail-back.png" },
];

const directProductVideoFacts = studioCreationFacts({
  creationMode: "video",
  imageEditProductMode: false,
  editSubjectMode: "general",
  productAsset,
});
assert.equal(directProductVideoFacts.isEditMode, false);
assert.equal(directProductVideoFacts.subjectMode, "product");
assert.equal(directProductVideoFacts.productGenerationMode, true);

const directProductVideo = buildGenerationPayload({
  stage: "final",
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  isImageEditMode: false,
  subjectMode: "product",
  prompt: "产品放在浴室台面，镜头缓慢推进，水珠沿包装边缘滑落。",
  promptDirty: true,
  selected: styleAsset,
  productAsset,
  productDetailAssets: productDetails,
  productProfile,
  productProfileSource: productAssetSignature,
  structured: {},
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 10,
  vResolution: "1080p",
  productVideoTemplate: "stable_showcase",
});
assert.equal(directProductVideo.payload.source_asset_url, productAsset.url);
assert.equal(directProductVideo.payload.source_type, "image");
assert.equal(directProductVideo.payload.params.subject_mode, "product");
assert.equal(directProductVideo.payload.params.product_reference_image, productAsset.url);
assert.deepEqual(
  directProductVideo.payload.params.product_detail_images,
  productDetails.map((asset) => asset.url),
  "product detail images must preserve the user-defined order",
);
const tenProductDetails = [
  ...productDetails,
  { type: "image", url: "/d3.png" },
  { type: "image", url: "/d4.png" },
  { type: "image", url: "/d5.png" },
  { type: "image", url: "/d6.png" },
  { type: "image", url: "/d7.png" },
  { type: "image", url: "/d8.png" },
  { type: "image", url: "/d9.png" },
  { type: "image", url: "/d10.png" },
];
const tenDetailVideo = buildGenerationPayload({
  ...directProductVideo,
  category: "video",
  creationMode: "video",
  subjectMode: "product",
  productAsset,
  productDetailAssets: tenProductDetails,
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
});
assert.deepEqual(
  tenDetailVideo.payload.params.product_detail_images,
  tenProductDetails.map((asset) => asset.url),
  "ten product details must be accepted and preserve order",
);
assert.equal(directProductVideo.payload.params.reference_image_url, undefined);
assert.equal(directProductVideo.payload.params.first_frame_image, undefined);
assert.equal(directProductVideo.payload.params.last_frame_image, undefined);
assert.equal(directProductVideo.payload.params.style_reference_image, styleAsset.url);
assert.equal(directProductVideo.payload.params.product_lock_mode, "locked");
assert.equal(directProductVideo.payload.params.product_video_template, "stable_showcase");
assert.equal(directProductVideo.payload.source_asset_meta.product_generation_mode, true);
assert.equal(directProductVideo.payload.source_asset_meta.product_subject.selected_url, productAsset.url);
assert.equal(directProductVideo.payload.source_asset_meta.style_reference.selected_url, styleAsset.url);
assert.match(directProductVideo.payload.prompt["产品身份档案"], /DAMAH 黑魔法/);
assert.match(directProductVideo.payload.prompt["产品身份档案"], /全棉棉柔巾/);
assert.equal(
  directProductVideo.payload.prompt.final_text,
  "产品放在浴室台面，镜头缓慢推进，水珠沿包装边缘滑落。",
  "direct product video should retain text-to-video prompt semantics",
);
assert.equal(directProductVideo.payload.params.character_reference_image, undefined);
assert.throws(
  () => buildGenerationPayload({
    category: "video",
    creationMode: "video",
    subjectMode: "product",
    productAsset: null,
    productDetailAssets: [productDetails[0]],
    prompt: "x",
    ratio: "9:16",
    imageQuality: "1k",
    n: 1,
    vDuration: 5,
    vResolution: "720p",
  }),
  /请先选择产品主题图/,
  "product details must never be submitted without a theme image",
);
assert.throws(
  () => buildGenerationPayload({
    ...directProductVideo,
    category: "video",
    creationMode: "video",
    subjectMode: "product",
    productAsset,
    productDetailAssets: [productDetails[0], productDetails[0]],
    ratio: "9:16",
    imageQuality: "1k",
    n: 1,
    vDuration: 5,
    vResolution: "720p",
  }),
  /不能重复/,
  "duplicate product details must fail instead of being silently removed",
);
assert.throws(
  () => buildGenerationPayload({
    category: "video",
    creationMode: "video",
    subjectMode: "product",
    productAsset,
    productDetailAssets: [...tenProductDetails, { type: "image", url: "/d11.png" }],
    prompt: "x",
    ratio: "9:16",
    imageQuality: "1k",
    n: 1,
    vDuration: 5,
    vResolution: "720p",
  }),
  /最多 10 张/,
  "more than ten details must fail instead of being truncated",
);

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
assert.match(productEdit.payload.prompt["产品身份档案"], /DAMAH 黑魔法/);
assert.ok(productEdit.payload.prompt["产品身份档案"].length <= 420);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /DAMAH 黑魔法/);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /全棉棉柔巾/);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /200抽/);
assert.match(productEdit.payload.prompt.final_text, /唯一产品身份/);
assert.match(productEdit.payload.prompt.final_text, /Logo和可见文字/);
assert.ok(productEdit.payload.prompt.final_text.length <= 450);
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
assert.match(productEdit.payload.params.negative_prompt, /Logo 变形/);
assert.doesNotMatch(productEdit.payload.prompt.final_text, /Logo 变形/);

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

const structuredPortraitReference = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: canonicalReversePrompt,
  promptSourceSignature: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  selected: styleAsset,
  structured: {
    "图像类型": "人物图",
    "主体": "成年人物后仰斜坐",
    "人物比例": "服装覆盖下胸廓较饱满，腰线自然，近镜大腿受透视放大",
    "构图": "竖版9:16，主体占画幅82%，前景占25%",
    "视角镜头": "腰腿高度低机位向上约15度，35-45mm近距离透视",
    "光线": "左前上方大面积柔光，右后冷紫轮廓光",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  ratio: "9:16",
});

assert.equal(structuredPortraitReference.payload.prompt.final_text, canonicalReversePrompt);
assert.equal(structuredPortraitReference.payload.prompt.instruction, canonicalReversePrompt);
assert.equal(structuredPortraitReference.payload.params.reference_image_url, styleAsset.url);
assert.equal(structuredPortraitReference.payload.params.subject_mode, undefined);

const structuredGeneralReference = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: canonicalReversePrompt,
  promptSourceSignature: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  selected: styleAsset,
  structured: {
    "图像类型": "产品图",
    "主体": "白色包装居中直立",
    "场景背景": "半透明蓝色冰块框景",
    "构图": "竖版3:4中心构图",
    "光线": "左后方橙金暖光",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  ratio: "3:4",
});
assert.equal(structuredGeneralReference.payload.params.reference_image_url, undefined);

const structuredReplicaReference = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: canonicalReversePrompt,
  promptSourceSignature: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  selected: styleAsset,
  structured: structuredGeneralReference.effectiveStructured,
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  analysisFocus: "replica",
  ratio: "3:4",
});
assert.equal(structuredReplicaReference.payload.params.reference_image_url, styleAsset.url);

const portraitReferencePricing = buildStudioDerivedViewState({
  cfg: {
    image_size_max_dim: 2048,
    image_n_max: 8,
    models: { image: { cost_credits: 15 } },
    pricing: {
      image: {
        unit_costs: { "1k": 15 },
        edit_unit_costs: { "1k": 20 },
      },
    },
  },
  creationMode: "image",
  category: "image",
  isEditMode: false,
  isImageEditMode: false,
  subjectMode: "general",
  productGenerationMode: false,
  portraitGenerationMode: false,
  task: null,
  submitting: false,
  selected: styleAsset,
  productAsset: null,
  structured: structuredPortraitReference.effectiveStructured,
  prompt: canonicalReversePrompt,
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
  videoAnalysisPreset: "standard",
});

assert.equal(
  portraitReferencePricing.estCost,
  20,
  "a structured portrait reference should display image-edit pricing before submit",
);

const userEditedPrompt = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: "用户手写的新提示词需要保留",
  promptDirty: true,
});
assert.match(userEditedPrompt.payload.prompt.final_text, /用户手写的新提示词需要保留/);

const shortEditedPortraitPrompt = buildGenerationPayload({
  ...staleReversePromptArgs,
  prompt: "保持低机位后仰坐姿和原图柔雾光影",
  promptDirty: true,
  promptSourceSignature: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  structured: structuredPortraitReference.effectiveStructured,
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
});
assert.equal(
  shortEditedPortraitPrompt.payload.prompt.final_text,
  "保持低机位后仰坐姿和原图柔雾光影",
  "a user-edited canonical prompt should not be prefixed with every structured field",
);
assert.equal(
  shortEditedPortraitPrompt.payload.prompt.instruction,
  "保持低机位后仰坐姿和原图柔雾光影",
);
assert.equal(
  shortEditedPortraitPrompt.payload.prompt.user_instruction,
  "保持低机位后仰坐姿和原图柔雾光影",
  "a dirty prompt should explicitly mark the user's canonical override",
);

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
  productProfile: {
    structured: {
      "年龄语境": "成年",
      "脸型五官": "椭圆脸，深棕色眼睛",
      "身份稳定特征": "保持眼型、鼻梁和下颌轮廓稳定",
      "负向": "换脸、五官漂移、年龄突变",
    },
    final_text: "这段未经白名单的模型文本不应直接使用",
  },
  productProfileSource: productAssetSignature,
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
assert.equal(portraitEdit.payload.params.reference_image_url, productAsset.url);
assert.match(portraitEdit.payload.prompt.final_text, /上传人像是唯一人物身份/);
assert.ok(portraitEdit.payload.prompt.final_text.length <= 450);
assert.match(portraitEdit.payload.prompt.final_text, /专业商业人像/);
assert.doesNotMatch(portraitEdit.payload.prompt.final_text, /严格按照用户提示词执行局部或整体编辑/);
assert.match(portraitEdit.payload.params.negative_prompt, /身份不一致/);
assert.match(portraitEdit.payload.params.negative_prompt, /幼态成人化/);
assert.match(portraitEdit.payload.params.negative_prompt, /低机位身体凝视/);
assert.match(portraitEdit.payload.params.negative_prompt, /五官漂移/);
assert.doesNotMatch(portraitEdit.payload.prompt.final_text, /未经白名单/);

const portraitVideo = buildGenerationPayload({
  stage: "preview",
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video_edit",
  isEditMode: true,
  isImageEditMode: false,
  subjectMode: "portrait",
  prompt: "人物保持身份一致，缓慢转头看向镜头",
  promptDirty: true,
  selected: null,
  productAsset,
  structured: {},
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});

assert.equal(portraitVideo.payload.params.character_reference_image, productAsset.url);
assert.equal(portraitVideo.payload.params.reference_image_url, undefined);
assert.equal(portraitVideo.payload.params.first_frame_image, undefined);
assert.equal(portraitVideo.payload.params.last_frame_image, undefined);

const dirtyPortraitCanonical = "保持低机位后仰坐姿，使用硬质影棚主光、高锐度 HDR，明确无柔雾、无光晕。";
const dirtyPortraitEdit = buildGenerationPayload({
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image_edit",
  isEditMode: true,
  isImageEditMode: true,
  subjectMode: "portrait",
  prompt: dirtyPortraitCanonical,
  promptDirty: true,
  selected: styleAsset,
  productAsset,
  structured: {
    "图像类型": "人物图",
    "光线": "旧解析：左上大面积柔光、非 HDR",
    "后期质感": "旧解析：低对比柔雾与宽泛光晕",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  ratio: "2:3",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});

assert.match(dirtyPortraitEdit.payload.prompt.final_text, /硬质影棚主光、高锐度 HDR/);
assert.doesNotMatch(dirtyPortraitEdit.payload.prompt.final_text, /旧解析/);
assert.doesNotMatch(dirtyPortraitEdit.payload.prompt.final_text, /左上大面积柔光、非 HDR/);
assert.equal(dirtyPortraitEdit.payload.prompt.user_instruction, dirtyPortraitCanonical);

const finalVideo = buildGenerationPayload({
  stage: "final",
  task: {
    id: 42,
    category: "video",
    params: {
      target_ratio: "9:16",
      target_resolution: "1080p",
      target_duration: 30,
      product_reference_image: productAsset.url,
      product_detail_images: productDetails.map((asset) => asset.url),
      product_lock_mode: "free",
      product_video_template: "background_motion",
    },
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
assert.deepEqual(finalVideo.payload.params.product_detail_images, productDetails.map((asset) => asset.url));
assert.equal(finalVideo.payload.params.subject_mode, "product");
assert.equal(finalVideo.payload.params.product_lock_mode, "free");
assert.equal(finalVideo.payload.params.product_video_template, "background_motion");
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
assert.equal(directFinalVideo.payload.prompt.raw_text, "竖屏短视频成片，产品居中展示，柔和棚拍光线");
assert.equal(directFinalVideo.payload.prompt.assembled_text, directFinalVideo.payload.prompt.final_text);
assert.equal(directFinalVideo.payload.prompt.optimized_text, undefined);
assert.equal(directFinalVideo.payload.prompt.input_mode, "direct_input");

const optimizedDirectVideo = buildGenerationPayload({
  ...directFinalVideo.payload,
  stage: "final",
  cfg: { video_duration_max_seconds: 15 },
  category: "video",
  creationMode: "video",
  isEditMode: false,
  prompt: {
    text: (
      "风格设定：高端日系个护广告，柔和自然光。\n"
      + "场景脚本：\n"
      + "Shot 1：抽出洗脸巾。\n"
      + "Shot 2：微距展开，不再浸水。\n"
      + "技术约束：总时长10秒；画幅9:16；分辨率1080p。"
    ),
    raw_text: "女主抽出洗脸巾，展开后浸水。",
    optimized_text: (
      "风格设定：高端日系个护广告，柔和自然光。\n"
      + "场景脚本：\n"
      + "Shot 1：抽出洗脸巾。\n"
      + "Shot 2：微距展开洗脸巾。\n"
      + "技术约束：总时长10秒；画幅9:16；分辨率1080p。"
    ),
    optimizer_model_id: "gemini-3.5-flash-low",
    optimization_direction: "model_adaptation",
    optimization_kind: "model_compile",
    compiler_metadata: { version: "prompt-optimizer-v3", direction: "model_adaptation" },
    change_summary: ["已针对目标模型编译"],
    warnings: ["检查品牌文字"],
  },
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
assert.equal(optimizedDirectVideo.payload.prompt.raw_text, "女主抽出洗脸巾，展开后浸水。");
assert.match(optimizedDirectVideo.payload.prompt.optimized_text, /^风格设定：/);
assert.match(optimizedDirectVideo.payload.prompt.optimized_text, /\n场景脚本：\nShot 1：/);
assert.match(optimizedDirectVideo.payload.prompt.optimized_text, /\n技术约束：/);
assert.equal(optimizedDirectVideo.payload.prompt.optimizer_model_id, "gemini-3.5-flash-low");
assert.match(optimizedDirectVideo.payload.prompt.assembled_text, /^风格设定：/);
assert.match(optimizedDirectVideo.payload.prompt.final_text, /\n技术约束：/);
assert.equal(optimizedDirectVideo.payload.prompt.input_mode, "optimized");
assert.equal(optimizedDirectVideo.payload.prompt.optimization_direction, "model_adaptation");
assert.equal(optimizedDirectVideo.payload.prompt.optimization_kind, "model_compile");
assert.equal(optimizedDirectVideo.payload.prompt.compiler_metadata.version, "prompt-optimizer-v3");
assert.deepEqual(optimizedDirectVideo.payload.prompt.optimization_change_summary, ["已针对目标模型编译"]);
assert.deepEqual(optimizedDirectVideo.payload.prompt.optimization_warnings, ["检查品牌文字"]);

const optimizedDirectImage = buildGenerationPayload({
  stage: "preview",
  cfg: { image_size_max_dim: 2048, image_n_max: 8 },
  category: "image",
  creationMode: "image",
  isEditMode: false,
  prompt: {
    text: "高端商业产品图，ACME SKU-7 包装保持不变。",
    raw_text: "ACME SKU-7 产品图，包装不能变。",
    optimized_text: "高端商业产品图，ACME SKU-7 包装保持不变。",
    optimizer_model_id: "gemini-3.5-flash-low",
    optimization_direction: "commercial",
    optimization_kind: "rewrite",
    compiler_metadata: { version: "prompt-optimizer-v2", direction: "commercial" },
  },
  promptDirty: true,
  selected: null,
  structured: {},
  ratio: "1:1",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
});
assert.equal(optimizedDirectImage.payload.prompt.input_mode, "optimized");
assert.equal(optimizedDirectImage.payload.prompt.raw_text, "ACME SKU-7 产品图，包装不能变。");
assert.equal(optimizedDirectImage.payload.prompt.optimized_text, "高端商业产品图，ACME SKU-7 包装保持不变。");
assert.equal(optimizedDirectImage.payload.prompt.optimization_direction, "commercial");
assert.equal(optimizedDirectImage.payload.prompt.compiler_metadata.version, "prompt-optimizer-v2");

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
    "迁移生成指令": "暖棕色广告棚景，上传产品从画面左侧入场，缓慢旋转后切到包装特写，镜头低速推进并轻微环绕，柔和主光勾勒边缘高光。",
    "标签": "Estee Lauder, Advanced Night Repair, skincare bottle",
  },
  structuredSource: "image|http://localhost:8000/api/uploads/upload/style.jpg|",
  reverseVideoAnalysis: {
    analysis_mode: "multi_frame",
    sampled_frames: [
      { index: 1, timestamp_seconds: 0 },
      { index: 2, timestamp_seconds: 5 },
    ],
    shots: [{
      start_seconds: 0,
      end_seconds: 5,
      visual: "产品居中展示",
      action: "产品从画面左侧入场，慢速旋转后推近包装特写",
      camera: "镜头低速推进",
      ocr: "不得进入的字幕",
      audio_cue: "不得进入的音效",
      evidence_frame_indices: [1, 2],
      confidence: 0.9,
    }],
  },
  ratio: "9:16",
  imageQuality: "1k",
  n: 1,
  seed: "",
  vDuration: 5,
  vResolution: "720p",
  productVideoTemplate: "soft_splash",
};
const productVideoEditFree = buildGenerationPayload(productVideoEditArgs);

assert.equal(productVideoEditFree.payload.category, "video");
assert.equal(productVideoEditFree.payload.stage, "final");
assert.equal(productVideoEditFree.payload.parent_task_id, null);
assert.equal(productVideoEditFree.payload.params.subject_mode, "product");
assert.equal(productVideoEditFree.payload.params.product_lock_mode, "locked");
assert.equal(productVideoEditFree.payload.params.product_video_template, "soft_splash");
assert.equal(productVideoEditFree.payload.params.product_reference_image, productAsset.url);
assert.equal(productVideoEditFree.payload.params.reference_image_url, undefined);
assert.equal(productVideoEditFree.payload.params.first_frame_image, undefined);
assert.equal(productVideoEditFree.payload.params.last_frame_image, undefined);
assert.equal(productVideoEditFree.payload.prompt["场景背景"], "暖棕色广告棚景和金色沙粒台面");
assert.equal(productVideoEditFree.payload.prompt["主体动作"], undefined);
assert.equal(productVideoEditFree.payload.prompt["镜头运动"], undefined);
assert.match(productVideoEditFree.payload.prompt["产品身份档案"], /DAMAH 黑魔法/);
assert.match(productVideoEditFree.payload.prompt["产品身份档案"], /全棉棉柔巾/);
assert.equal(productVideoEditFree.payload.prompt["主体"], undefined);
assert.equal(productVideoEditFree.payload.prompt["商品服装"], undefined);
assert.equal(productVideoEditFree.payload.prompt["材质纹理"], undefined);
assert.equal(productVideoEditFree.payload.prompt["标签"], undefined);
assert.equal(productVideoEditFree.payload.prompt.user_instruction, "产品旋转展示，水花飞溅，镜头推进");
assert.equal(productVideoEditFree.payload.prompt.raw_text, "产品旋转展示，水花飞溅，镜头推进");
assert.equal(productVideoEditFree.payload.prompt.assembled_text, "产品旋转展示，水花飞溅，镜头推进");
assert.equal(productVideoEditFree.payload.prompt.final_text, "产品旋转展示，水花飞溅，镜头推进");
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /产品与包装文字保真优先/);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /Estee Lauder/i);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /Advanced Night Repair/i);
assert.doesNotMatch(productVideoEditFree.payload.prompt.final_text, /dropper bottle/i);
assert.match(productVideoEditFree.payload.params.negative_prompt, /Logo 变形/);

const reversedProductVideo = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
});
assert.match(reversedProductVideo.payload.prompt.final_text, /产品从画面左侧入场/);
assert.equal(reversedProductVideo.payload.prompt.user_instruction, reversedProductVideo.payload.prompt.final_text);
assert.equal(reversedProductVideo.payload.prompt.input_mode, "structured_reverse");
assert.match(reversedProductVideo.payload.prompt.final_text, /上传产品作为唯一视频主体/);
assert.doesNotMatch(reversedProductVideo.payload.prompt.final_text, /优先保持完整包装/);
assert.doesNotMatch(reversedProductVideo.payload.prompt.final_text, /不得进入的字幕|不得进入的音效/);

const leakedReverseVideoPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "Estee Lauder Advanced Night Repair 棕色滴管瓶旋转展示",
  promptDirty: false,
});
assert.doesNotMatch(leakedReverseVideoPayload.payload.prompt.final_text, /Estee Lauder/i);
assert.doesNotMatch(leakedReverseVideoPayload.payload.prompt.final_text, /Advanced Night Repair/i);
assert.match(leakedReverseVideoPayload.payload.prompt.final_text, /产品从画面左侧入场/);

const poisonedTransferPromptPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
  structured: {
    ...productVideoEditArgs.structured,
    "迁移生成指令": "Estee Lauder Advanced Night Repair 原商品继续旋转，最后展示品牌 Logo。",
  },
});
assert.doesNotMatch(poisonedTransferPromptPayload.payload.prompt.final_text, /Estee Lauder/i);
assert.doesNotMatch(poisonedTransferPromptPayload.payload.prompt.final_text, /Advanced Night Repair/i);
assert.match(poisonedTransferPromptPayload.payload.prompt.final_text, /上传产品作为唯一视频主体/);

const sparsePoisonedTransferPromptPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
  structured: {
    "场景背景": "暖棕色广告棚景",
    "可迁移主体动作": "慢速旋转后推近特写",
    "迁移生成指令": "Reference product keeps the original brand and original product identity.",
  },
});
assert.doesNotMatch(sparsePoisonedTransferPromptPayload.payload.prompt.final_text, /original brand/i);
assert.doesNotMatch(sparsePoisonedTransferPromptPayload.payload.prompt.final_text, /original product/i);
assert.match(sparsePoisonedTransferPromptPayload.payload.prompt.final_text, /上传产品作为唯一视频主体/);
assert.match(sparsePoisonedTransferPromptPayload.payload.prompt.final_text, /慢速旋转后推近(?:包装)?特写/);

const sparseChinesePoisonedTransferPromptPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
  structured: {
    "场景背景": "暖棕色广告棚景",
    "可迁移主体动作": "慢速旋转后推近特写",
    "迁移生成指令": "参考品牌和原商品继续旋转，最后展示原包装文字。",
  },
});
assert.doesNotMatch(
  sparseChinesePoisonedTransferPromptPayload.payload.prompt.final_text,
  /参考品牌和原商品继续旋转/,
);
assert.match(sparseChinesePoisonedTransferPromptPayload.payload.prompt.final_text, /上传产品作为唯一视频主体/);

const postProductionTransferPromptPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
  structured: {
    "场景背景": "暖棕色广告棚景",
    "可迁移主体动作": "慢速旋转后推近特写",
    "迁移生成指令": "暖棕色广告棚景；慢速旋转后推近特写；字幕：新品上市；温柔女声旁白：安心每一天；SFX：水滴声。",
  },
});
assert.match(postProductionTransferPromptPayload.payload.prompt.final_text, /暖棕色广告棚景/);
assert.match(postProductionTransferPromptPayload.payload.prompt.final_text, /慢速旋转后推近(?:包装)?特写/);
assert.doesNotMatch(postProductionTransferPromptPayload.payload.prompt.final_text, /字幕|新品上市/);
assert.doesNotMatch(postProductionTransferPromptPayload.payload.prompt.final_text, /旁白|安心每一天/);
assert.doesNotMatch(postProductionTransferPromptPayload.payload.prompt.final_text, /SFX|水滴声/i);

const chineseIdentityPoisonedTransferPromptPayload = buildGenerationPayload({
  ...productVideoEditArgs,
  prompt: "",
  promptDirty: false,
  structured: {
    "主体": "正面包装文字包含黑魔法全棉棉柔巾",
    "场景背景": "暖棕色广告棚景",
    "可迁移主体动作": "慢速旋转后推近包装特写",
    "迁移生成指令": "黑魔法全棉棉柔巾慢速旋转展示",
  },
});
assert.doesNotMatch(
  chineseIdentityPoisonedTransferPromptPayload.payload.prompt.final_text,
  /黑魔法全棉棉柔巾/,
);
assert.match(
  chineseIdentityPoisonedTransferPromptPayload.payload.prompt.final_text,
  /慢速旋转后推近包装特写/,
);

const productVideoEditLocked = buildGenerationPayload({
  ...productVideoEditArgs,
  productVideoTemplate: "slow_push",
});

assert.equal(productVideoEditLocked.payload.params.product_lock_mode, "locked");
assert.equal(productVideoEditLocked.payload.params.product_video_template, "slow_push");

const productVideoSingleClip = buildGenerationPayload({
  ...productVideoEditArgs,
  productVideoTemplate: "single_clip_action",
});
assert.equal(productVideoSingleClip.payload.params.product_video_template, "single_clip_action");

const productVideoEditDefault = buildGenerationPayload({
  ...productVideoEditArgs,
  productVideoTemplate: undefined,
});

assert.equal(productVideoEditDefault.payload.params.product_lock_mode, "locked");
assert.equal(productVideoEditDefault.payload.params.product_video_template, "prompt_driven");

assert.throws(
  () => buildGenerationPayload({
    ...productVideoEditArgs,
    productVideoTemplate: "wild_spin",
  }),
  /产品视频策略无效/,
  "unknown product video strategies must fail before reaching the API",
);

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
