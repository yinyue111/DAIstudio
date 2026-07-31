import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { readStudioSourceFromUrl } from "./studio-source.mjs";

const catalog = readFileSync(new URL("../app/catalog/page.jsx", import.meta.url), "utf8");
const studio = readStudioSourceFromUrl(import.meta.url);
const deepLinkBootstrap = readFileSync(
  new URL("../hooks/useStudioDeepLinkBootstrap.js", import.meta.url),
  "utf8",
);

test("model catalog compares up to three models and deep-links exact selections", () => {
  assert.match(catalog, /function ModelComparison/);
  assert.match(catalog, /current\.slice\(-2\)/);
  assert.match(catalog, /catalogModelStudioHref/);
  assert.match(catalog, /price_version/);
  assert.match(catalog, /route_availability/);
  assert.match(catalog, /aspect_ratios/);
  assert.match(catalog, /resolutions/);
  assert.match(catalog, /durations/);
  assert.match(catalog, /max_reference_images/);
  assert.match(catalog, /video_reference: "视频参考"/);
  assert.match(catalog, /video_edit: "视频编辑"/);
  assert.match(catalog, /mask_edit: "蒙版编辑"/);
  assert.match(catalog, /audio_reference: "音频参考输入"/);
  assert.match(catalog, /generated_audio: "生成同步音频"/);
  assert.doesNotMatch(catalog, /video_to_video: "视频参考\/编辑"/);
  assert.match(catalog, /product_profile: "商品档案识别"/);
  assert.match(catalog, /portrait_profile: "人物档案识别"/);
  assert.match(catalog, /\["max_reference_images", "平台最多参考图"\]/);
  assert.match(catalog, /\["max_reference_videos", "平台最多视频输入"\]/);
  assert.match(catalog, /\["max_reference_audio", "平台最多音频输入"\]/);
  assert.match(catalog, /视频反推（平台抽帧分析）/);
  assert.match(catalog, /CapabilityState/);
  assert.match(catalog, />不支持</);
  assert.match(catalog, />未知</);
  assert.match(catalog, /在创作中使用/);
});

test("studio resolves server tool versions before mutating workflow state", () => {
  assert.match(studio, /useStudioDeepLinkBootstrap\(\{/);
  assert.match(deepLinkBootstrap, /api\.toolCatalogDetail\(slug\)/);
  assert.match(deepLinkBootstrap, /resolveStudioWorkflowPreset\(window\.location\.search, tool\)/);
  assert.match(deepLinkBootstrap, /resolution\.status !== "ready"/);
  assert.match(deepLinkBootstrap, /当前草稿未改变/);
});

test("studio validates catalog model capability and compatible mode before exact selection", () => {
  assert.match(studio, /catalogModelBootstrapRef/);
  assert.match(deepLinkBootstrap, /MODEL_SELECTION_USES\.includes\(modelUse\)/);
  assert.match(deepLinkBootstrap, /allModelOptions\[modelUse\]\?\.find/);
  assert.match(deepLinkBootstrap, /resolveCatalogModelIntent\(option, requestedMode\)/);
  assert.match(deepLinkBootstrap, /resolution\.creationMode && creationMode !== resolution\.creationMode/);
  assert.match(deepLinkBootstrap, /modelOptionsForSelection\[modelUse\]/);
  assert.match(deepLinkBootstrap, /changeModelSelection\(modelUse, modelConfigId\)/);
  assert.ok(!deepLinkBootstrap.includes("setModelSelections({ [modelUse]: modelConfigId })"));
});
