import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  appendUniqueReverseText,
  applyReverseResultAction,
  applyReverseResultApplication,
  confirmedImageEvidence,
  mergeConfirmedReverseResultApplication,
  normalizePendingReverseResult,
  resolveReverseResultApplicationSelection,
  selectedImageEvidenceForApplication,
  undoReverseResultApplication,
} from "../app/studio/reverseResultApplication.ts";
import {
  compareReverseResultRevisions,
  normalizeReverseRevisionContent,
} from "../app/studio/reverseResultRevisionDiff.ts";

function workspace(overrides = {}) {
  return {
    prompt: "原提示词",
    negative: "原负向词",
    structured: { 主体: "原主体" },
    structuredBaseline: { 主体: "原基线" },
    structuredDirty: true,
    ratio: "1:1",
    vDuration: 5,
    vResolution: "720p",
    promptSourceSignature: "old-prompt-source",
    structuredSource: "old-structure-source",
    promptDirty: true,
    negativeTouched: true,
    selected: { id: 7, type: "video" },
    unrelated: { keep: true },
    ...overrides,
  };
}

function videoPending(overrides = {}) {
  return {
    kind: "pending_reverse_result",
    mediaType: "video",
    prompt: "新视频提示词",
    negative: "闪烁，形变",
    structured: { 主体: "新主体", 镜头运动: "缓慢推近" },
    parameters: { ratio: "16:9", vDuration: 9, vResolution: "1080p" },
    sourceSignature: "video|source-9",
    ...overrides,
  };
}

function imageEvidence(overrides = {}) {
  return {
    evidence_id: "ocr-title",
    evidence_type: "ocr",
    bbox: { x: 0.1, y: 0.2, width: 0.4, height: 0.1 },
    field_key: "标题",
    evidence_text: "造梦 Studio",
    confidence: 0.94,
    source_index: 1,
    fact_status: "visible",
    protected: true,
    editable: false,
    review_status: "confirmed",
    ...overrides,
  };
}

test("normalizes image and video envelopes without leaking video parameters into images", () => {
  const rawImage = {
    source_type: "image",
    request_context: { source_signature: "image|source-1" },
    result: {
      final_text: "  商品棚拍  ",
      structured: { 主体: "杯子", 负向: "文字，水印" },
      parameters: { ratio: "4:5", duration: 12, resolution: "1080p" },
    },
  };
  const image = normalizePendingReverseResult(rawImage);
  assert.deepEqual(image, {
    kind: "pending_reverse_result",
    mediaType: "image",
    prompt: "商品棚拍",
    negative: "文字，水印",
    structured: { 主体: "杯子", 负向: "文字，水印" },
    imageEvidence: [],
    videoAnalysis: null,
    parameters: { ratio: "4:5", vDuration: null, vResolution: null },
    sourceSignature: "image|source-1",
  });

  const video = normalizePendingReverseResult({
    target: "video",
    normalized_result: {
      generation_prompt: " 分镜生成稿 ",
      negative_constraints: ["闪烁", "身份漂移"],
      structured: { shots: [{ start_seconds: 0, end_seconds: 3 }] },
      generation_parameters: { resolution: "1080p" },
      video_analysis: { source: { ratio: "9:16", duration_seconds: 7.6 } },
    },
  }, { sourceSignature: "video|source-2" });
  assert.equal(video.mediaType, "video");
  assert.equal(video.prompt, "分镜生成稿");
  assert.equal(video.negative, "闪烁，身份漂移");
  assert.deepEqual(video.parameters, { ratio: "9:16", vDuration: 8, vResolution: "1080p" });
  assert.equal(video.sourceSignature, "video|source-2");

  rawImage.result.structured.主体 = "外部突变";
  assert.equal(image.structured.主体, "杯子", "normalization must detach pending structured data");
});

test("recompiles legacy image prompts with analysis scaffolding without overwriting user edits", () => {
  const legacyPrompt = [
    "直接可见事实：参考图1中白色纸盒居中直立",
    "视觉估计：参考图1为高质量、广告级品质、UHD、award-winning的产品摄影",
    "可迁移为小红书、抖音及品牌网页广告素材",
    "未知：真实拍摄地点不确定",
  ].join("；");
  const input = {
    source_type: "image",
    dirty: false,
    final_text: legacyPrompt,
    structured: {
      "主体": "直接可见事实：参考图1中白色纸盒居中直立。",
      "场景背景": "视觉估计：半透明蓝色冰块环绕中央冰台。",
      "风格": "高质量、广告级品质、UHD、award-winning的商业产品摄影；可迁移为小红书和抖音素材。",
    },
  };

  const normalized = normalizePendingReverseResult(input);
  assert.match(normalized.prompt, /白色纸盒居中直立/);
  assert.match(normalized.prompt, /半透明蓝色冰块环绕中央冰台/);
  assert.doesNotMatch(
    normalized.prompt,
    /直接可见事实|视觉估计|未知|不确定|参考图1|高质量|广告级品质|UHD|award-winning|小红书|抖音|品牌网页广告/,
  );

  assert.equal(
    normalizePendingReverseResult({ ...input, dirty: true }).prompt,
    legacyPrompt,
    "a user-edited pending prompt must be preserved verbatim",
  );
});

test("restoring a user-edit revision marks its prompt as authored content", () => {
  const actionsSource = readFileSync(new URL("../hooks/useReverseResultActions.js", import.meta.url), "utf8");
  assert.match(
    actionsSource,
    /preserveStored:\s*revision\.source === "user_edit"/,
  );
  assert.match(
    actionsSource,
    /reverseResultEnvelope\([\s\S]*restoredPayload[\s\S]*revision\.source === "user_edit"/,
  );
});

test("normalizes null and invalid pending input to one predictable empty image result", () => {
  assert.deepEqual(normalizePendingReverseResult(null), {
    kind: "pending_reverse_result",
    mediaType: "image",
    prompt: "",
    negative: "",
    structured: {},
    imageEvidence: [],
    videoAnalysis: null,
    parameters: { ratio: null, vDuration: null, vResolution: null },
    sourceSignature: "",
  });
  assert.deepEqual(
    normalizePendingReverseResult({ target: "video", duration: -3, structured: [] }).parameters,
    { ratio: null, vDuration: null, vResolution: null },
  );
});

test("image evidence application carries only explicitly selected confirmed rows", () => {
  const pending = normalizePendingReverseResult({
    source_type: "image",
    final_text: "商品棚拍",
    image_evidence: [
      imageEvidence(),
      imageEvidence({ evidence_id: "pending-copy", evidence_text: "待审文字", review_status: "pending" }),
      imageEvidence({ evidence_id: "rejected-copy", evidence_text: "错误文字", review_status: "rejected" }),
    ],
  });
  assert.equal(pending.imageEvidence.length, 3);
  assert.deepEqual(confirmedImageEvidence(pending.imageEvidence).map((item) => item.evidence_id), ["ocr-title"]);

  const fullSelection = resolveReverseResultApplicationSelection(pending);
  assert.equal(fullSelection.imageEvidence, true);
  assert.deepEqual(
    selectedImageEvidenceForApplication(pending, fullSelection).map((item) => item.evidence_id),
    ["ocr-title"],
  );

  const promptOnly = resolveReverseResultApplicationSelection(pending, {
    prompt: true,
    imageEvidence: false,
  });
  assert.equal(promptOnly.imageEvidence, false);
  assert.deepEqual(selectedImageEvidenceForApplication(pending, promptOnly), []);

  const pendingOnly = normalizePendingReverseResult({
    source_type: "image",
    image_evidence: [imageEvidence({ review_status: "pending" })],
  });
  assert.equal(resolveReverseResultApplicationSelection(pendingOnly).imageEvidence, false);
});

test("replace applies every available result group and undo restores the exact workspace", () => {
  const original = workspace();
  const applied = applyReverseResultApplication(original, videoPending(), "replace");

  assert.deepEqual(applied.workspace, {
    ...original,
    prompt: "新视频提示词",
    negative: "闪烁，形变",
    structured: { 主体: "新主体", 镜头运动: "缓慢推近" },
    structuredBaseline: { 主体: "新主体", 镜头运动: "缓慢推近" },
    structuredDirty: false,
    ratio: "16:9",
    vDuration: 9,
    vResolution: "1080p",
    promptSourceSignature: "video|source-9",
    structuredSource: "video|source-9",
    promptDirty: false,
    negativeTouched: false,
  });
  assert.equal(applied.workspace.unrelated, original.unrelated, "unselected objects must be preserved");
  assert.deepEqual(original, workspace(), "application must not mutate the input workspace");

  applied.workspace.unrelated = { changedAfterApply: true };
  const undone = undoReverseResultApplication(applied.workspace, applied.undo);
  assert.deepEqual(
    undone.workspace,
    { ...original, unrelated: { changedAfterApply: true } },
    "undo restores selected fields while retaining unrelated changes made later",
  );
});

test("replace treats empty groups as unavailable and image results never alter video controls", () => {
  const original = workspace();
  const pending = normalizePendingReverseResult({
    source_type: "image",
    final_text: "新图片提示词",
    negative: "",
    structured: {},
    parameters: { ratio: "3:4", duration: 14, resolution: "4k" },
  });
  const applied = applyReverseResultApplication(original, pending, "replace");
  assert.equal(applied.workspace.prompt, "新图片提示词");
  assert.equal(applied.workspace.ratio, "3:4");
  assert.equal(applied.workspace.negative, original.negative);
  assert.equal(applied.workspace.structured, original.structured);
  assert.equal(applied.workspace.vDuration, original.vDuration);
  assert.equal(applied.workspace.vResolution, original.vResolution);
  assert.equal(applied.workspace.negativeTouched, original.negativeTouched);
  assert.equal(applied.workspace.structuredDirty, original.structuredDirty);
});

test("append adds only new clauses, marks mixed text, and leaves structure and parameters untouched", () => {
  const original = workspace({
    prompt: "红色杯子，白色背景",
    negative: "模糊, watermark",
    promptDirty: false,
    negativeTouched: false,
  });
  const pending = videoPending({
    prompt: "白色背景；棚拍柔光；棚拍柔光",
    negative: "WATERMARK，文字，文字",
  });
  const applied = applyReverseResultApplication(original, pending, "append");
  assert.equal(applied.workspace.prompt, "红色杯子，白色背景，棚拍柔光");
  assert.equal(applied.workspace.negative, "模糊, watermark，文字");
  assert.equal(applied.workspace.promptDirty, true);
  assert.equal(applied.workspace.negativeTouched, true);
  assert.equal(applied.workspace.promptSourceSignature, "");
  for (const field of [
    "structured", "structuredBaseline", "structuredDirty", "structuredSource",
    "ratio", "vDuration", "vResolution",
  ]) assert.equal(applied.workspace[field], original[field], `${field} is not selected by append`);
  assert.deepEqual(undoReverseResultApplication(applied.workspace, applied.undo).workspace, original);
});

test("empty and duplicate append is a true no-op", () => {
  assert.equal(appendUniqueReverseText("A，B", " b ; A "), "A，B");
  assert.equal(appendUniqueReverseText("A，B", ""), "A，B");
  const original = workspace({ prompt: "A，B", negative: "C" });
  const applied = applyReverseResultApplication(
    original,
    videoPending({ prompt: "B;A", negative: "C", structured: {}, parameters: { ratio: null, vDuration: null, vResolution: null } }),
    "append",
  );
  assert.equal(applied.workspace, original);
  assert.deepEqual(applied.changedFields, []);
  assert.equal(applied.undo, null);
});

test("structure_only changes only structure state and its source", () => {
  const original = workspace();
  const applied = applyReverseResultApplication(original, videoPending(), "structure_only");
  assert.deepEqual(applied.workspace.structured, { 主体: "新主体", 镜头运动: "缓慢推近" });
  assert.deepEqual(applied.workspace.structuredBaseline, applied.workspace.structured);
  assert.equal(applied.workspace.structuredDirty, false);
  assert.equal(applied.workspace.structuredSource, "video|source-9");
  for (const field of [
    "prompt", "negative", "ratio", "vDuration", "vResolution",
    "promptSourceSignature", "promptDirty", "negativeTouched",
  ]) assert.equal(applied.workspace[field], original[field], `${field} is not selected by structure_only`);
  assert.deepEqual(undoReverseResultApplication(applied.workspace, applied.undo).workspace, original);

  const empty = applyReverseResultApplication(
    original,
    videoPending({ structured: {} }),
    "structure_only",
  );
  assert.equal(empty.workspace, original);
  assert.equal(empty.undo, null);

  const evidenceOnly = applyReverseResultApplication(
    original,
    videoPending({ structured: {}, videoAnalysis: { sampled_frames: [{ index: 1 }] } }),
    "structure_only",
    { videoAnalysis: true },
  );
  assert.deepEqual(evidenceOnly.workspace.reverseVideoAnalysis, {
    sampled_frames: [{ index: 1 }],
  });
  assert.deepEqual(
    undoReverseResultApplication(evidenceOnly.workspace, evidenceOnly.undo).workspace,
    original,
  );
});

test("parameters_only changes valid media parameters and no text or dirty state", () => {
  const original = workspace();
  const applied = applyReverseResultApplication(original, videoPending(), "parameters_only");
  assert.deepEqual(
    { ratio: applied.workspace.ratio, duration: applied.workspace.vDuration, resolution: applied.workspace.vResolution },
    { ratio: "16:9", duration: 9, resolution: "1080p" },
  );
  for (const field of [
    "prompt", "negative", "structured", "structuredBaseline", "structuredDirty",
    "promptSourceSignature", "structuredSource", "promptDirty", "negativeTouched",
  ]) assert.equal(applied.workspace[field], original[field], `${field} is not selected by parameters_only`);
  assert.deepEqual(undoReverseResultApplication(applied.workspace, applied.undo).workspace, original);

  const image = normalizePendingReverseResult({
    source_type: "image",
    parameters: { ratio: "4:5", duration: 20, resolution: "4k" },
  });
  const imageApplied = applyReverseResultApplication(original, image, "parameters_only");
  assert.equal(imageApplied.workspace.ratio, "4:5");
  assert.equal(imageApplied.workspace.vDuration, original.vDuration);
  assert.equal(imageApplied.workspace.vResolution, original.vResolution);
});

test("undo action removes fields that did not exist before application", () => {
  const original = { unrelated: "kept" };
  const applied = applyReverseResultAction(original, {
    type: "replace",
    pending: videoPending({ structured: {}, parameters: { ratio: null, vDuration: null, vResolution: null } }),
  });
  assert.equal(applied.workspace.prompt, "新视频提示词");
  assert.equal(Object.hasOwn(applied.workspace, "promptDirty"), true);

  const undone = applyReverseResultAction(applied.workspace, { type: "undo", undo: applied.undo });
  assert.deepEqual(undone.workspace, original);
  assert.equal(Object.hasOwn(undone.workspace, "prompt"), false);
  assert.equal(Object.hasOwn(undone.workspace, "promptDirty"), false);
});

test("field selection applies only prompt, one structured field, and ratio", () => {
  const original = workspace({
    structured: { 主体: "原主体", 镜头运动: "保留运镜", 环境: "原环境" },
    structuredBaseline: { 主体: "原基线", 镜头运动: "基线运镜", 环境: "基线环境" },
    reverseVideoAnalysis: { source: "existing" },
  });
  const pending = videoPending({
    structured: { 主体: "新主体", 镜头运动: "新运镜", 环境: "新环境" },
    videoAnalysis: { source: "incoming" },
  });
  const applied = applyReverseResultApplication(original, pending, "replace", {
    prompt: true,
    negative: false,
    structuredKeys: ["主体"],
    parameterFields: ["ratio"],
    videoAnalysis: false,
  });

  assert.equal(applied.workspace.prompt, "新视频提示词");
  assert.equal(applied.workspace.negative, "原负向词");
  assert.deepEqual(applied.workspace.structured, {
    主体: "新主体",
    镜头运动: "保留运镜",
    环境: "原环境",
  });
  assert.deepEqual(applied.workspace.structuredBaseline, {
    主体: "新主体",
    镜头运动: "基线运镜",
    环境: "基线环境",
  });
  assert.equal(applied.workspace.structuredDirty, true);
  assert.equal(applied.workspace.structuredSource, "");
  assert.equal(applied.workspace.ratio, "16:9");
  assert.equal(applied.workspace.vDuration, 5);
  assert.equal(applied.workspace.vResolution, "720p");
  assert.deepEqual(applied.workspace.reverseVideoAnalysis, { source: "existing" });
  assert.deepEqual(undoReverseResultApplication(applied.workspace, applied.undo).workspace, original);
});

test("confirmed application merges only actual changed fields into the latest workspace", () => {
  const requestWorkspace = workspace({
    prompt: "请求发出时的提示词",
    promptDirty: true,
  });
  const application = applyReverseResultApplication(
    requestWorkspace,
    videoPending(),
    "replace",
    { prompt: true, parameterFields: ["ratio"] },
  );
  const latestWorkspace = {
    ...requestWorkspace,
    negative: "等待期间新负向词",
    selected: { id: 99, type: "video" },
    unrelated: { newer: true },
  };

  const merged = mergeConfirmedReverseResultApplication(
    latestWorkspace,
    requestWorkspace,
    application,
  );

  assert.equal(merged.status, "applied");
  assert.equal(merged.pending, false);
  assert.deepEqual(merged.conflictFields, []);
  assert.deepEqual(merged.pendingFields, []);
  assert.equal(merged.workspace.prompt, "新视频提示词");
  assert.equal(merged.workspace.ratio, "16:9");
  assert.equal(merged.workspace.negative, "等待期间新负向词");
  assert.deepEqual(merged.workspace.selected, { id: 99, type: "video" });
  assert.equal(merged.workspace.unrelated, latestWorkspace.unrelated);
  assert.deepEqual(merged.changedFields, application.changedFields);
  assert.deepEqual(
    Object.keys(merged.undo.before).sort(),
    [...merged.changedFields].sort(),
    "undo must cover exactly the fields committed after confirmation",
  );
  assert.equal(merged.undo.before.prompt.value, "请求发出时的提示词");
  assert.equal(merged.undo.after.prompt.value, "新视频提示词");

  const undone = undoReverseResultApplication(merged.workspace, merged.undo);
  assert.equal(undone.workspace.prompt, "请求发出时的提示词");
  assert.equal(undone.workspace.ratio, "1:1");
  assert.equal(undone.workspace.negative, "等待期间新负向词");
  assert.deepEqual(undone.workspace.unrelated, { newer: true });
});

test("confirmed application stays pending when a selected field changed during the request", () => {
  const requestWorkspace = workspace({ prompt: "请求发出时的提示词" });
  const application = applyReverseResultApplication(
    requestWorkspace,
    videoPending(),
    "replace",
    { prompt: true, parameterFields: ["ratio"] },
  );
  const latestWorkspace = {
    ...requestWorkspace,
    prompt: "用户在等待期间编辑的提示词",
    unrelated: { newer: true },
  };

  const merged = mergeConfirmedReverseResultApplication(
    latestWorkspace,
    requestWorkspace,
    application,
  );

  assert.equal(merged.status, "conflict");
  assert.equal(merged.pending, true);
  assert.deepEqual(merged.conflictFields, ["prompt"]);
  assert.ok(merged.pendingFields.includes("prompt"));
  assert.ok(merged.pendingFields.includes("ratio"), "safe fields remain pending because confirmation is atomic");
  assert.equal(merged.workspace, latestWorkspace);
  assert.deepEqual(merged.changedFields, []);
  assert.equal(merged.undo, null);
  assert.equal(merged.workspace.prompt, "用户在等待期间编辑的提示词");
  assert.equal(merged.workspace.ratio, "1:1");
});

test("confirmed application ignores stale changedFields and precise undo protects later edits", () => {
  const requestWorkspace = workspace({ prompt: "请求发出时的提示词" });
  const application = applyReverseResultApplication(
    requestWorkspace,
    videoPending(),
    "replace",
    { prompt: true, parameterFields: ["ratio"] },
  );
  application.changedFields.push("negative");
  const merged = mergeConfirmedReverseResultApplication(
    requestWorkspace,
    requestWorkspace,
    application,
  );

  assert.equal(merged.status, "applied");
  assert.ok(!merged.changedFields.includes("negative"), "unchanged fields are not committed or added to undo");

  const editedAfterApply = {
    ...merged.workspace,
    prompt: "应用后的新手工提示词",
    unrelated: { afterApply: true },
  };
  const undone = undoReverseResultApplication(editedAfterApply, merged.undo);
  assert.equal(undone.workspace.prompt, "应用后的新手工提示词");
  assert.equal(
    undone.workspace.promptSourceSignature,
    merged.workspace.promptSourceSignature,
    "a newer prompt edit blocks undo for its source/dirty metadata group",
  );
  assert.equal(undone.workspace.ratio, "1:1", "unmodified confirmed fields can still be undone");
  assert.deepEqual(undone.workspace.unrelated, { afterApply: true });
});

test("an explicitly empty selection is a strict no-op in every application mode", () => {
  const original = workspace();
  for (const mode of ["replace", "append", "structure_only", "parameters_only"]) {
    const applied = applyReverseResultApplication(original, videoPending(), mode, {});
    assert.equal(applied.workspace, original, `${mode} must preserve workspace identity`);
    assert.deepEqual(applied.changedFields, []);
    assert.equal(applied.undo, null);
  }
  assert.deepEqual(resolveReverseResultApplicationSelection(videoPending(), {}), {
    prompt: false,
    negative: false,
    structuredKeys: [],
    parameterFields: [],
    imageEvidence: false,
    videoAnalysis: false,
  });
});

test("omitting selection preserves full replacement semantics", () => {
  const original = workspace({ structured: { 主体: "原主体", 应删除: "旧字段" } });
  const applied = applyReverseResultApplication(original, videoPending(), "replace");
  assert.deepEqual(applied.workspace.structured, { 主体: "新主体", 镜头运动: "缓慢推近" });
  assert.equal(Object.hasOwn(applied.workspace.structured, "应删除"), false);
  assert.deepEqual(resolveReverseResultApplicationSelection(videoPending()), {
    prompt: true,
    negative: true,
    structuredKeys: ["主体", "镜头运动"],
    parameterFields: ["ratio", "vDuration", "vResolution"],
    imageEvidence: false,
    videoAnalysis: false,
  });
});

test("revision comparison covers prompt, negative, structured fields, and parameters", () => {
  const before = {
    id: 1,
    version: 2,
    payload: {
      target: "video",
      final_text: "老生成稿",
      negative: "模糊",
      structured: { 主体: "白色杯子", 环境: "影棚" },
      parameters: { ratio: "1:1", vDuration: 5, vResolution: "720p" },
    },
  };
  const after = {
    id: 2,
    version: 3,
    payload: {
      target: "video",
      final_text: "新生成稿",
      negative: "",
      structured: { 主体: "红色杯子", 环境: "影棚", 光线: "柔光" },
      parameters: { ratio: "16:9", vDuration: 5, vResolution: "1080p" },
    },
  };

  const comparison = compareReverseResultRevisions(before, after);
  assert.deepEqual(comparison.diffs.map((diff) => diff.key), [
    "prompt",
    "negative",
    "structured.光线",
    "structured.主体",
    "parameters.ratio",
    "parameters.vResolution",
  ]);
  assert.deepEqual(comparison.summary, {
    total: 6,
    added: 1,
    removed: 1,
    changed: 4,
    groups: { prompt: 1, negative: 1, structured: 2, parameters: 2 },
  });
  assert.equal(comparison.diffs.find((diff) => diff.key === "structured.环境"), undefined);
  assert.deepEqual(normalizeReverseRevisionContent(after).parameters, {
    ratio: "16:9",
    vDuration: 5,
    vResolution: "1080p",
  });
});

test("reverse result hook persists the atomic lineage before changing the workspace", () => {
  const hookSource = readFileSync(new URL("../hooks/useReverseResultActions.js", import.meta.url), "utf8");
  const handlerStart = hookSource.indexOf("async function applyPendingReverseResult");
  const handlerEnd = hookSource.indexOf("function undoAppliedReverseResult", handlerStart);
  const handler = hookSource.slice(handlerStart, handlerEnd);
  assert.match(handler, /applyPendingReverseResult\(mode, selection\)/);
  assert.match(handler, /resolveReverseResultApplicationSelection\(pending, selection\)/);
  assert.match(handler, /applyReverseResultApplication\(latestWorkspace, pending, mode, resolvedSelection\)/);
  assert.match(handler, /application_selection: resolvedSelection/);
  assert.match(handler, /selectedImageEvidenceForApplication\(pending, resolvedSelection\)/);
  assert.match(handler, /key !== "image_evidence"/);
  assert.match(handler, /image_evidence: selectedImageEvidence/);
  assert.match(handler, /clear_image_evidence: clearsReviewedImageEvidence/);
  assert.match(handler, /final_text: String\(applied\.workspace\.prompt/);
  assert.match(handler, /api\.applyReverseOperationResult/);
  assert.match(handler, /response\.user_edit/);
  assert.match(handler, /response\.applied/);
  assert.match(handler, /parent_revision_id: parentRevision\.id/);
  assert.match(
    handler,
    /const confirmedApplication = mergeConfirmedReverseResultApplication\(\s*currentWorkspace,\s*latestWorkspace,\s*applied,?\s*\)/,
    "the confirmed response must be merged against the workspace that is current after the request",
  );
  assert.match(
    handler,
    /const latestMerge = mergeConfirmedReverseResultApplication\(\s*current,\s*latestWorkspace,\s*applied,?\s*\)/,
    "the state updater must re-run the merge against its latest workspace value",
  );
  assert.match(
    handler,
    /reverseUndoSnapshot:\s*latestMerge\.status === "unchanged"\s*\?\s*current\.reverseUndoSnapshot\s*:\s*latestMerge\.undo/,
    "an unchanged confirmation must retain the existing undo snapshot",
  );
  assert.match(handler, /应用失败，工作区未修改/);
  assert.ok(
    handler.indexOf("if (!applied.changedFields.length)")
      < handler.indexOf("api.applyReverseOperationResult"),
    "empty selection must return before creating a revision",
  );
  assert.ok(
    handler.indexOf("await api.applyReverseOperationResult")
      < handler.indexOf("setWorkspacePatch"),
    "workspace changes must wait for the atomic server transaction",
  );
});

test("storyboard application uses the same server-first atomic path", () => {
  const actionsSource = readFileSync(new URL("../hooks/useStoryboardActions.js", import.meta.url), "utf8");
  const handlerStart = actionsSource.indexOf("async function applyStoryboardShot");
  const handlerEnd = actionsSource.indexOf("return { compileStoryboardShot", handlerStart);
  const handler = actionsSource.slice(handlerStart, handlerEnd);
  assert.match(handler, /api\.applyReverseOperationResult/);
  assert.match(handler, /response\.user_edit/);
  assert.match(handler, /response\.applied/);
  assert.match(handler, /镜头应用失败，工作区未修改/);
  assert.ok(
    handler.indexOf("await api.applyReverseOperationResult")
      < handler.indexOf("setWorkspacePatch"),
    "storyboard workspace changes must wait for the atomic server transaction",
  );
});

test("generation lineage discovery accepts applied revisions only", () => {
  const draftSource = readFileSync(new URL("../app/studio/promptDraftUtils.ts", import.meta.url), "utf8");
  assert.match(draftSource, /GENERATION_SOURCE_REVISION_SOURCES = new Set\(\["applied"\]\)/);
  assert.doesNotMatch(
    draftSource,
    /GENERATION_SOURCE_REVISION_SOURCES = new Set\(\[[^\]]*"normalized"/,
  );
});
