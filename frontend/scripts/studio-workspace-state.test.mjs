import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");

for (const field of [
  "prompt",
  "negative",
  "imageEditProductMode",
  "editSubjectMode",
  "url",
  "assets",
  "selected",
  "productAsset",
  "ratio",
  "imageQuality",
  "n",
  "seed",
  "vDuration",
  "vResolution",
  "videoAnalysisPreset",
  "structured",
  "structuredSource",
  "parsing",
  "uploading",
  "reversing",
]) {
  assert.match(
    pageSource,
    new RegExp(`${field}:`),
    `${field} should live inside the per-mode workspace state`,
  );
  assert.doesNotMatch(
    pageSource,
    new RegExp(`const \\[${field},\\s*set${field[0].toUpperCase()}${field.slice(1)}\\] = useState`),
    `${field} should not be a top-level shared state across creation tabs`,
  );
}

assert.match(
  pageSource,
  /const \[workspaces,\s*setWorkspaces\] = useState\(createModeWorkspaces\)/,
  "studio should initialize one workspace per creation mode",
);
assert.match(
  pageSource,
  /selectedByModeRef = useRef\(\{\}\)/,
  "selected reference guards should be keyed by creation mode",
);
assert.match(
  pageSource,
  /refVersionRef = useRef\(\{\}\)/,
  "parse/reverse version guards should be keyed by creation mode",
);
assert.match(
  pageSource,
  /isRequestCurrent\(reverseRequestRef,\s*mode,\s*reqId\)/,
  "late reverse responses should be ignored for stale mode-local requests",
);
assert.match(
  pageSource,
  /setWorkspacePatch\(\{\s*structured:\s*s,[\s\S]*prompt:\s*composePromptFromStructured/,
  "reverse output should write into the initiating mode workspace",
);
assert.match(
  pageSource,
  /const subjectModeParam = effectivePortraitMode \|\| effectiveProductMode \? effectiveSubjectMode : ""/,
  "plain image editing should not send subject_mode=general to the backend",
);
assert.doesNotMatch(
  pageSource,
  /\.\.\.\(effectiveSubjectMode \? \{ subject_mode: effectiveSubjectMode \} : \{\}\)/,
  "only product/portrait edit modes should include subject_mode in generation params",
);
assert.match(
  pageSource,
  /productUploadRequestRef = useRef\(\{\}\)/,
  "product uploads should have a dedicated stale-response guard",
);
assert.match(
  pageSource,
  /isRequestCurrent\(productUploadRequestRef,\s*mode,\s*productReqId\)/,
  "late product upload responses should be ignored after clear or replace",
);
assert.match(
  pageSource,
  /bumpRequest\(productUploadRequestRef,\s*creationMode\)/,
  "clearing a product source should invalidate in-flight product uploads",
);
assert.match(
  pageSource,
  /updateWorkspaceField\("ratio",\s*nearestRatio\(dims\.width,\s*dims\.height,\s*videoRatioOptions\(\)\),\s*targetMode\)/,
  "video asset ratio should be written into the target video workspace even when selected from another tab",
);
assert.doesNotMatch(
  pageSource,
  /targetMode === creationMode[\s\S]{0,180}setRatio\(nearestRatio/,
  "asset ratio updates should not depend on the currently rendered tab",
);
assert.match(
  pageSource,
  /label: "图生视频"/,
  "video edit tab should describe the actual image-to-video reconstruction capability",
);
assert.match(
  pageSource,
  /"生成重构视频 ▶"/,
  "video reconstruction submit button should not promise direct video editing",
);
assert.doesNotMatch(
  pageSource,
  /label: "视频编辑"/,
  "video reconstruction mode should not be labeled as direct video editing",
);

for (const phrase of [
  "包装文字逐字保留",
  "产品表面像素视为锁定图层",
  "逐字逐形保持原图",
  "不得翻译、改写、补写、删减、重排、风格化、模糊或替换",
  "若风格迁移和产品保真冲突，优先保证产品主体与包装文字完全不变",
  "产品正面文字被重排",
  "顶部文字被改写",
  "保留人像身份",
  "人像照片作为唯一人物身份",
  "人物重构",
  "当前为人物参考驱动重构，非逐帧换脸",
]) {
  assert.match(
    pageSource,
    new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
    `product image edit prompt should include fidelity guard: ${phrase}`,
  );
}

console.log("studio workspace state test passed");
