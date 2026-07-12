import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const mediaUploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const workspaceStateSource = readFileSync(join(root, "hooks/useStudioWorkspaceState.js"), "utf8");
const promptWorkspaceSource = readFileSync(join(root, "app/studio/StudioPromptWorkspace.jsx"), "utf8");
const constantsSource = readFileSync(join(root, "app/studio/constants.ts"), "utf8");
const editPromptSource = readFileSync(join(root, "app/studio/editPrompt.ts"), "utf8");
const generationPayloadSource = readFileSync(join(root, "app/studio/generationPayload.ts"), "utf8");
const generationSubmitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const referenceParsingSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const viewModelSource = readFileSync(join(root, "app/studio/viewModel.ts"), "utf8");
const structuredEditorSource = readFileSync(join(root, "app/studio/StudioStructuredEditor.jsx"), "utf8");
const generationControlsSource = readFileSync(join(root, "components/StudioGenerationControls.jsx"), "utf8");
const studioSource = `${pageSource}\n${workspaceStateSource}\n${promptWorkspaceSource}\n${editPromptSource}\n${generationPayloadSource}\n${referenceParsingSource}\n${viewModelSource}\n${constantsSource}\n${generationControlsSource}`;
const subjectProtectionModule = await import("../lib/studioSubjectProtection.js").catch(() => ({}));
const { startSubjectProtectionPreview } = subjectProtectionModule;

assert.equal(
  typeof startSubjectProtectionPreview,
  "function",
  "subject-protection preview requests should use a cancellable runner",
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

{
  const pending = deferred();
  const failure = new Error("preview failed");
  const errors = [];
  const request = startSubjectProtectionPreview({
    load: () => pending.promise,
    onSuccess: () => assert.fail("a rejected preview must not report success"),
    onError: (error) => errors.push(error),
  });

  await Promise.resolve();
  pending.reject(failure);
  await request.settled;
  assert.deepEqual(errors, [failure], "a delayed preview rejection should reach onError");
}

{
  const first = deferred();
  const second = deferred();
  const writes = [];
  const firstRequest = startSubjectProtectionPreview({
    load: () => first.promise,
    onSuccess: (value) => writes.push(`first:${value}`),
    onError: (error) => writes.push(`first-error:${error.message}`),
  });
  await Promise.resolve();
  firstRequest.cancel();
  const secondRequest = startSubjectProtectionPreview({
    load: () => second.promise,
    onSuccess: (value) => writes.push(`second:${value}`),
    onError: (error) => writes.push(`second-error:${error.message}`),
  });

  await Promise.resolve();
  first.resolve("stale");
  second.resolve("current");
  await Promise.all([firstRequest.settled, secondRequest.settled]);
  assert.deepEqual(writes, ["second:current"], "canceling request A should not suppress request B");
}

{
  const pending = deferred();
  const writes = [];
  const request = startSubjectProtectionPreview({
    load: () => pending.promise,
    onSuccess: (value) => writes.push(value),
    onError: (error) => writes.push(error.message),
  });

  await Promise.resolve();
  request.cancel();
  pending.reject(new Error("disposed"));
  await request.settled;
  assert.deepEqual(writes, [], "a disposed preview request should not write success or error state");
}

{
  const values = [];
  const successfulRequest = startSubjectProtectionPreview({
    load: () => "sync success",
    onSuccess: (value) => values.push(value),
    onError: (error) => assert.fail(error),
  });
  await successfulRequest.settled;

  const failure = new Error("sync failure");
  const errors = [];
  const failedRequest = startSubjectProtectionPreview({
    load: () => { throw failure; },
    onSuccess: () => assert.fail("a synchronously thrown load must not report success"),
    onError: (error) => errors.push(error),
  });
  await failedRequest.settled;

  assert.deepEqual(values, ["sync success"], "a synchronous preview result should reach onSuccess");
  assert.deepEqual(errors, [failure], "a synchronous preview failure should reach onError");
}

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
  "editMaskMode",
  "productPixelLockMode",
  "videoProductLockMode",
  "videoProductTemplate",
  "videoAnalysisPreset",
  "subjectProtection",
  "subjectProtectionLoading",
  "subjectProtectionSource",
  "structured",
  "structuredSource",
  "promptSourceSignature",
  "parsing",
  "uploading",
  "reversing",
]) {
  assert.match(
    workspaceStateSource,
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
  workspaceStateSource,
  /const \[workspaces,\s*setWorkspaces\] = useState\(\(\) => createModeWorkspaces\(modes\)\)/,
  "studio should initialize one workspace per creation mode",
);
assert.match(
  pageSource,
  /useStudioWorkspaceState\(\{\s*creationMode,\s*modes: CREATION_MODES\s*\}\)/,
  "main page should delegate per-mode workspace state to the workspace hook",
);
assert.match(
  referenceParsingSource,
  /selectedByModeRef = useRef\(\{\}\)/,
  "selected reference guards should be keyed by creation mode",
);
assert.match(
  referenceParsingSource,
  /refVersionRef = useRef\(\{\}\)/,
  "parse/reverse version guards should be keyed by creation mode",
);
assert.match(
  referenceParsingSource,
  /urlByModeRef = useRef\(\{\}\)/,
  "parse guards should also track the URL that produced the pending result",
);
assert.match(
  referenceParsingSource,
  /isParseStillCurrent\(mode,\s*refVersion,\s*targetUrl\)/,
  "late parse responses should be ignored after the URL changes",
);
assert.match(
  referenceParsingSource,
  /isRequestCurrent\(reverseRequestRef,\s*mode,\s*reqId\)/,
  "late reverse responses should be ignored for stale mode-local requests",
);
assert.match(
  referenceParsingSource,
  /const reversePrompt = isEditMode[\s\S]*composeStyleTransferPrompt[\s\S]*composePromptFromStructured/,
  "reverse output should choose style-transfer prompts for edit modes and full prompts otherwise",
);
assert.match(
  referenceParsingSource,
  /setWorkspacePatch\(\(current\) => \{[\s\S]*promptUnchanged[\s\S]*promptSourceSignature:\s*targetSignature/,
  "reverse output should not overwrite manual prompt edits and should tag the prompt source",
);
assert.match(
  referenceParsingSource,
  /current\.promptSourceSignature[\s\S]*current\.promptSourceSignature !== nextSignature[\s\S]*prompt:\s*""/,
  "selecting a different reference should clear stale reverse prompts that still belong to the old reference",
);
assert.match(
  generationPayloadSource,
  /promptSourceStale[\s\S]*promptSourceSignature[\s\S]*styleSignature[\s\S]*promptText = promptSourceStale \? ""/,
  "payload assembly should defensively drop stale reverse prompts whose source no longer matches the selected reference",
);
assert.match(
  generationPayloadSource,
  /subjectModeParam:\s*portraitMode \|\| productMode \? effectiveSubjectMode : ""/,
  "plain image editing should not send subject_mode=general to the backend",
);
assert.doesNotMatch(
  generationPayloadSource,
  /\.\.\.\(effectiveSubjectMode \? \{ subject_mode: effectiveSubjectMode \} : \{\}\)/,
  "only product/portrait edit modes should include subject_mode in generation params",
);
assert.match(
  generationSubmitSource,
  /buildGenerationPayload\(\{/,
  "generation submit hook should delegate payload assembly to a focused module",
);
assert.match(
  generationSubmitSource,
  /productAssetSignatureByModeRef = useRef\(\{\}\)/,
  "generation submit should snapshot the current product asset by mode",
);
assert.match(
  generationSubmitSource,
  /isProductAssetStillCurrent/,
  "generation submit should abort if product profiling returns for a replaced subject image",
);
assert.match(
  pageSource,
  /setUrl=\{updateReferenceUrl\}/,
  "URL edits should invalidate stale parse/reverse results instead of only changing input text",
);
assert.match(
  pageSource,
  /productBusy=\{submitting \|\| productProfiling\}/,
  "subject replacement controls should be disabled while submit-time profiling is running",
);
assert.match(
  generationSubmitSource,
  /videoProductLockMode,/,
  "video product lock mode should be submitted with the generation payload",
);
assert.match(
  generationSubmitSource,
  /productPixelLockMode,/,
  "product pixel lock mode should be submitted with the generation payload",
);
assert.match(
  generationSubmitSource,
  /videoProductTemplate,/,
  "video product template should be submitted with the generation payload",
);
assert.match(
  generationPayloadSource,
  /product_lock_mode:\s*videoProductLockMode === "locked" \? "locked" : "free"/,
  "video product lock mode should be explicit in image-to-video edit params",
);
assert.match(
  generationPayloadSource,
  /product_video_template:\s*videoProductTemplate \|\| "stable_showcase"/,
  "video product template should be explicit in product image-to-video params",
);
assert.match(
  generationPayloadSource,
  /edit_mask_mode:\s*editMaskMode \|\| "protect_subject"/,
  "product image editing should explicitly send the selected inpaint mask mode",
);
assert.match(
  generationPayloadSource,
  /product_pixel_lock:\s*editMaskMode === "off" \? "off" : \(productPixelLockMode \|\| "auto"\)/,
  "product image editing should explicitly send the selected pixel-lock mode",
);
assert.match(
  generationControlsSource,
  /主体保护/,
  "product image editing controls should expose automatic mask protection",
);
assert.match(
  generationControlsSource,
  /中心保护/,
  "product image editing controls should expose the center-box compatibility mode",
);
assert.match(
  generationControlsSource,
  /整图编辑/,
  "product image editing controls should allow disabling mask protection",
);
assert.match(
  generationControlsSource,
  /像素锁/,
  "product image editing controls should expose pixel-lock options",
);
assert.match(
  generationControlsSource,
  /强制像素锁/,
  "product image editing controls should expose strict pixel-lock mode",
);
assert.match(
  generationControlsSource,
  /自由运动/,
  "video product generation controls should expose a dynamic free-motion option",
);
assert.match(
  generationControlsSource,
  /文字保真/,
  "video product generation controls should expose a text-fidelity lock option",
);
assert.match(
  generationControlsSource,
  /产品模板/,
  "video product generation controls should expose product-safe video templates",
);
assert.match(
  constantsSource,
  /VIDEO_PRODUCT_TEMPLATES/,
  "product-safe video templates should live in studio constants",
);
assert.match(
  workspaceStateSource,
  /productPixelLockMode:\s*"auto"/,
  "product image editing should default to automatic pixel lock",
);
assert.match(
  pageSource,
  /productPixelLockMode:\s*current\.productPixelLockMode \|\| "auto"/,
  "restored workspaces without a product pixel lock mode should use automatic pixel lock",
);
assert.match(
  workspaceStateSource,
  /videoProductLockMode:\s*"locked"/,
  "video product generation should default to text-fidelity lock mode",
);
assert.match(
  workspaceStateSource,
  /videoProductTemplate:\s*"stable_showcase"/,
  "video product generation should default to the stable showcase template",
);
assert.match(
  pageSource,
  /subjectProtectionPreview\(productAsset\.url,\s*requestedMode\)/,
  "product image editing should preflight subject-protection before generation",
);
const subjectProtectionEffect = pageSource.match(
  /useEffect\(\(\) => \{\s*const mode = creationMode;\s*const shouldPreview = \([\s\S]*?\n  \}, \[([\s\S]*?)\]\);/,
);
assert.ok(subjectProtectionEffect, "subject-protection preview should be managed by a focused effect");
for (const outputState of ["subjectProtection", "subjectProtectionLoading", "subjectProtectionSource"]) {
  assert.doesNotMatch(
    subjectProtectionEffect[1],
    new RegExp(`\\b${outputState}\\b`),
    `${outputState} should not restart and cancel the request that produced it`,
  );
}
assert.match(
  generationControlsSource,
  /主体保护预检/,
  "product image editing controls should show subject-protection preflight status",
);
assert.match(
  pageSource,
  /videoProductLockMode:\s*current\.videoProductLockMode \|\| "locked"/,
  "restored workspaces without a lock mode should use text-fidelity lock mode",
);
assert.doesNotMatch(
  pageSource,
  /buildGenerationPayload\(\{/,
  "main page should not own generation payload assembly",
);
assert.match(
  viewModelSource,
  /buildStudioDerivedViewState/,
  "derived studio display state should live outside the main page",
);
assert.match(
  pageSource,
  /buildStudioDerivedViewState\(\{/,
  "main page should consume the derived view model",
);
assert.match(
  pageSource,
  /<StudioStructuredEditor/,
  "main page should delegate structured reverse dimension editing to a focused component",
);
assert.match(
  structuredEditorSource,
  /反推维度/,
  "structured reverse dimension editor should own the dimension editing UI",
);
assert.doesNotMatch(
  pageSource,
  /<StudioStructuredEditor[\s\S]{0,500}setPromptDirty\(false\)/,
  "editing reverse dimensions must not clear manual prompt dirty state",
);
assert.match(
  generationPayloadSource,
  /generalEdit:\s*isImageEditMode && effectiveSubjectMode === "general"/,
  "portrait image editing should use portrait identity guards instead of generic edit guards",
);
assert.match(
  mediaUploadSource,
  /productUploadRequestRef = useRef\(\{\}\)/,
  "product uploads should have a dedicated stale-response guard",
);
assert.match(
  mediaUploadSource,
  /isRequestCurrent\(productUploadRequestRef,\s*mode,\s*productReqId\)/,
  "late product upload responses should be ignored after clear or replace",
);
assert.match(
  pageSource,
  /bumpProductUploadRequest\(creationMode\)/,
  "clearing a product source should invalidate in-flight product uploads",
);
assert.match(
  referenceParsingSource,
  /setRatio\(nearestRatio\(dims\.width,\s*dims\.height,\s*videoRatioOptions\(\)\),\s*targetMode\)/,
  "video asset ratio should be written into the target video workspace even when selected from another tab",
);
assert.doesNotMatch(
  referenceParsingSource,
  /targetMode === creationMode[\s\S]{0,180}setRatio\(nearestRatio/,
  "asset ratio updates should not depend on the currently rendered tab",
);
assert.match(
  constantsSource,
  /label: "图生视频"/,
  "video edit tab should describe the actual image-to-video reconstruction capability",
);
assert.match(
  viewModelSource,
  /"生成成片 ▶"/,
  "video submit button should create the final clip directly",
);
assert.doesNotMatch(
  constantsSource,
  /label: "视频编辑"/,
  "video reconstruction mode should not be labeled as direct video editing",
);
assert.match(
  pageSource,
  /createImageVariation/,
  "studio should expose an image variation workflow from generated results",
);
assert.match(
  pageSource,
  /setCreationMode\("image_edit"\)/,
  "image variation workflow should reuse image editing instead of bypassing the normal generate path",
);
assert.match(
  pageSource,
  /基于这张图生成同主体、同风格的近似变体/,
  "image variation workflow should prefill a same-style variation prompt",
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
    studioSource,
    new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
    `product image edit prompt should include fidelity guard: ${phrase}`,
  );
}

console.log("studio workspace state test passed");
