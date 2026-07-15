import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  clearAllWorkspaceContent,
  clearWorkspaceContent,
} from "../app/studio/workspaceReset.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const mediaUploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const workspaceStateSource = readFileSync(join(root, "hooks/useStudioWorkspaceState.js"), "utf8");
const promptWorkspaceSource = readFileSync(join(root, "app/studio/StudioPromptWorkspace.jsx"), "utf8");
const referencePanelSource = readFileSync(join(root, "app/studio/StudioReferencePanel.jsx"), "utf8");
const constantsSource = readFileSync(join(root, "app/studio/constants.ts"), "utf8");
const editPromptSource = readFileSync(join(root, "app/studio/editPrompt.ts"), "utf8");
const generationPayloadSource = readFileSync(join(root, "app/studio/generationPayload.ts"), "utf8");
const generationSubmitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const referenceParsingSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const viewModelSource = readFileSync(join(root, "app/studio/viewModel.ts"), "utf8");
const structuredEditorSource = readFileSync(join(root, "app/studio/StudioStructuredEditor.jsx"), "utf8");
const generationControlsSource = readFileSync(join(root, "components/StudioGenerationControls.jsx"), "utf8");
const studioSource = `${pageSource}\n${workspaceStateSource}\n${promptWorkspaceSource}\n${editPromptSource}\n${generationPayloadSource}\n${referenceParsingSource}\n${viewModelSource}\n${constantsSource}\n${generationControlsSource}`;
assert.doesNotMatch(
  generationControlsSource,
  /applyVideoProductTemplate/,
  "removed product-template controls should not keep template handlers",
);
assert.doesNotMatch(
  generationControlsSource,
  /applyVideoProductMode/,
  "removed product-motion controls should not keep mode handlers",
);
const subjectProtectionModule = await import("../lib/studioSubjectProtection.js").catch(() => ({}));
const { startSubjectProtectionPreview } = subjectProtectionModule;

const clearedWorkspace = clearWorkspaceContent({
  prompt: "旧提示词",
  negative: "旧负向词",
  assets: [{ id: 1 }],
  selected: { id: 1 },
  productAsset: { id: 2 },
  productProfile: { final_text: "旧主体档案" },
  structured: { 主体: "旧反推主体" },
  reverseVideoAnalysis: { source: { duration_seconds: 10 } },
  ratio: "3:4",
});
assert.equal(clearedWorkspace.prompt, "");
assert.equal(clearedWorkspace.negative, "");
assert.deepEqual(clearedWorkspace.assets, []);
assert.equal(clearedWorkspace.selected, null);
assert.equal(clearedWorkspace.productAsset, null);
assert.equal(clearedWorkspace.productProfile, null);
assert.deepEqual(clearedWorkspace.structured, {});
assert.equal(clearedWorkspace.reverseVideoAnalysis, null);
assert.equal(clearedWorkspace.ratio, "3:4", "clear should preserve generation settings");

const clearedWorkspaces = clearAllWorkspaceContent({
  image: {
    prompt: "文生图旧提示词",
    assets: [{ id: 1 }],
    selected: { id: 1 },
    ratio: "16:9",
  },
  video_edit: {
    prompt: "图生视频旧提示词",
    productAsset: { id: 2 },
    productProfile: { final_text: "旧主体档案" },
    structured: { 主体: "旧反推主体" },
    vDuration: 10,
  },
});
assert.equal(clearedWorkspaces.image.prompt, "");
assert.deepEqual(clearedWorkspaces.image.assets, []);
assert.equal(clearedWorkspaces.image.selected, null);
assert.equal(clearedWorkspaces.image.ratio, "16:9", "clear all should preserve image settings");
assert.equal(clearedWorkspaces.video_edit.prompt, "");
assert.equal(clearedWorkspaces.video_edit.productAsset, null);
assert.equal(clearedWorkspaces.video_edit.productProfile, null);
assert.deepEqual(clearedWorkspaces.video_edit.structured, {});
assert.equal(clearedWorkspaces.video_edit.vDuration, 10, "clear all should preserve video settings");
assert.match(
  pageSource,
  /function clearCurrentWorkspace\(\)[\s\S]*resetOwnerReferenceParsing\(\)[\s\S]*resetOwnerMediaUpload\(\)[\s\S]*setWorkspaces\(clearAllWorkspaceContent\)/,
  "clear should cancel all mode requests, revoke local previews, and clear every workspace",
);

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

{
  let attempts = 0;
  const writes = [];
  const busy = Object.assign(new Error("主体保护处理繁忙"), { status: 429 });
  const request = startSubjectProtectionPreview({
    load: () => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(busy) : Promise.resolve("recovered");
    },
    onSuccess: (value) => writes.push(`success:${value}`),
    onError: (error) => writes.push(`error:${error.message}`),
    retryDelayMs: 0,
  });

  await request.settled;
  assert.equal(attempts, 2, "a transient subject-protection failure should retry once");
  assert.deepEqual(writes, ["success:recovered"]);
}

{
  const pending = deferred();
  let aborted = false;
  const abortablePromise = pending.promise;
  abortablePromise.cancel = () => {
    aborted = true;
    pending.reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
  };
  const request = startSubjectProtectionPreview({
    load: () => abortablePromise,
    onSuccess: () => assert.fail("a canceled preview must not report success"),
    onError: () => assert.fail("a canceled preview must not report an error"),
  });

  await Promise.resolve();
  request.cancel();
  assert.equal(aborted, true, "canceling a preview should abort its active request");
  await request.settled;
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
  /const transferPrompt = composeSafeVideoTransferPrompt\(structured,\s*subjectMode\)[\s\S]*const reversePrompt = isVideo[\s\S]*isEditMode[\s\S]*transferPrompt[\s\S]*composeStyleTransferPrompt[\s\S]*result\.final_text[\s\S]*composePromptFromStructured/,
  "video reverse should separate authoritative replication text from identity-safe transfer prompts",
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
  generationSubmitSource,
  /subjectMode === "portrait"[\s\S]*请先上传人物照片[\s\S]*subjectMode === "product"[\s\S]*请先上传产品主体图片/,
  "missing edit sources should produce subject-specific guidance",
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
assert.doesNotMatch(
  generationSubmitSource,
  /videoProductLockMode,/,
  "removed video product lock state should not be submitted",
);
assert.match(
  generationSubmitSource,
  /productPixelLockMode,/,
  "product pixel lock mode should be submitted with the generation payload",
);
assert.doesNotMatch(
  generationSubmitSource,
  /videoProductTemplate,/,
  "removed video product template state should not be submitted",
);
assert.match(
  generationPayloadSource,
  /product_lock_mode:\s*"locked"/,
  "product video requests should always preserve product identity",
);
assert.match(
  generationPayloadSource,
  /product_video_template:\s*"prompt_driven"/,
  "product video requests should always preserve user-authored motion",
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
assert.doesNotMatch(
  generationControlsSource,
  /自由运动/,
  "video product generation controls should not expose a free-motion module",
);
assert.doesNotMatch(
  generationControlsSource,
  /产品运动/,
  "video product generation controls should not expose a product-motion module",
);
assert.doesNotMatch(
  generationControlsSource,
  /产品模板/,
  "video product generation controls should not expose product templates",
);
assert.doesNotMatch(
  constantsSource,
  /VIDEO_PRODUCT_TEMPLATES/,
  "removed product video templates should not remain in studio constants",
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
assert.doesNotMatch(
  workspaceStateSource,
  /videoProductLockMode/,
  "removed product motion mode should not remain in workspace state",
);
assert.doesNotMatch(
  workspaceStateSource,
  /videoProductTemplate/,
  "removed product video template should not remain in workspace state",
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
  generationControlsSource,
  /!subjectProtection\s*\?\s*"待上传"/,
  "an empty subject-protection state should be shown as waiting for upload",
);
assert.match(
  referencePanelSource,
  /const imageUploadTargetsProduct = isImageEditMode && productGenerationMode && !productAsset/,
  "the generic image action should target the product slot until a product exists",
);
assert.match(
  referencePanelSource,
  /imageUploadTargetsProduct \? productUploadInputRef : imageUploadInputRef/,
  "the product-first image action should open the product upload input",
);
assert.match(
  referencePanelSource,
  /const imageUploadLabel = imageUploadTargetsProduct[\s\S]*\? "上传产品主体"[\s\S]*isImageEditMode[\s\S]*\? "上传风格参考图"[\s\S]*directProductVideoMode[\s\S]*\? "上传图片参考"/,
  "product and style-reference upload actions should have explicit labels",
);
assert.match(
  referencePanelSource,
  /const directProductVideoMode = creationMode === "video"[\s\S]*\{\(isImageEditMode \|\| directProductVideoMode\) &&/,
  "text-to-video should expose a dedicated product-subject upload panel",
);
assert.match(
  referencePanelSource,
  /作为视频唯一产品主体，不是风格参考/,
  "text-to-video should explain that the product image is authoritative, not a style reference",
);
assert.match(
  referencePanelSource,
  /\{\(isEditMode \|\| directProductVideoMode\) && \([\s\S]*ref=\{productUploadInputRef\}/,
  "text-to-video should render the product file input used by its upload control",
);
assert.match(
  viewModelSource,
  /creationMode === "video" && productAsset[\s\S]*\? "product"/,
  "an uploaded text-to-video product image should activate product subject mode",
);
assert.match(
  generationPayloadSource,
  /const directProductVideo = Boolean\([\s\S]*creationMode === "video"[\s\S]*subjectMode === "product"[\s\S]*productAsset[\s\S]*sourceAsset = isFinal \? null : \(isEditMode \|\| directProductVideo \? productAsset : selected\)/,
  "direct product video should use the product asset as its source instead of the style reference",
);
assert.match(
  mediaUploadSource,
  /const profileSubjectMode = creationMode === "video" \? "product" : subjectMode/,
  "direct product video uploads should prefetch the authoritative product profile",
);
assert.match(
  generationSubmitSource,
  /const directProductVideo = creationMode === "video" && subjectMode === "product"[\s\S]*\(isEditMode \|\| directProductVideo\)[\s\S]*productAsset\?\.url/,
  "direct product video submission should resolve a product identity profile before generation",
);
const productUploadPatch = mediaUploadSource.match(
  /setWorkspacePatch\(\{\s*productAsset: asset,([\s\S]*?)\}, mode\);/,
);
assert.ok(productUploadPatch, "product upload should update the product workspace");
for (const staleField of [
  "subjectProtection: null",
  "subjectProtectionLoading: true",
  'subjectProtectionSource: ""',
]) {
  assert.ok(
    productUploadPatch[1].includes(staleField),
    `product replacement should reset ${staleField}`,
  );
}
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");
assert.match(
  apiSource,
  /subjectProtectionPreview:[\s\S]*?new AbortController\(\)[\s\S]*?promise\.cancel = \(\) => controller\.abort\(\)/,
  "subject-protection API requests should expose real AbortController cancellation",
);
assert.doesNotMatch(
  pageSource,
  /videoProductLockMode:\s*current\.videoProductLockMode/,
  "draft restore should not revive removed product motion state",
);
assert.match(
  pageSource,
  /const missingRequiredSource = isEditMode && !productAsset/,
  "edit generation should treat its source image as a required input",
);
assert.match(
  pageSource,
  /disabled=\{missingRequiredSource \|\| generationSubmitDisabled/,
  "generation must stay disabled until the required product or portrait source is uploaded",
);
assert.match(
  pageSource,
  /missingRequiredSource \? missingRequiredSourceLabel : submitLabel/,
  "the disabled submit button should explain which source image is missing",
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
  "Logo和可见文字",
  "产品与包装文字保真优先",
  "产品正面文字被重排",
  "顶部文字被改写",
  "保留人像身份",
  "上传人像是唯一人物身份",
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
