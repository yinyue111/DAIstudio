import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readStudioSource } from "./studio-source.mjs";
import {
  composeEvidenceBackedVideoTransferPrompt,
  composePromptFromStructured,
  visualStructuredFields,
} from "../app/studio/helpers.ts";
import {
  buildReverseOperationRequestSnapshotV2,
  buildReverseSnapshotV2,
  reverseSnapshotFromHistory,
  workspacePatchFromReverseSnapshot,
} from "../app/studio/reverseSnapshot.ts";
import {
  isActiveReverseOperation,
  isTerminalReverseOperation,
  normalizeReverseOperation,
  normalizeReverseOperationList,
  normalizeReverseResultRevisionList,
  reverseOperationRequestSignature,
  reverseOperationResumeCandidates,
  reverseOperationTrackingLost,
} from "../lib/reverseOperations.ts";
import {
  analysisModeLabel,
  videoAnalysisEvidenceData,
} from "../app/studio/StudioReferencePanel.jsx";
import { buildStudioDerivedViewState } from "../app/studio/viewModel.ts";
import { cancelStaleReverseOperation } from "../hooks/useReferenceParsing.js";
import { buildReverseHistoryWorkspacePatch } from "../hooks/useReverseHistoryActions.js";
import { resolvePendingReverseOperation } from "../hooks/studio/useStudioReverseDomain.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");
const apiTypeSource = readFileSync(join(root, "lib/api.d.ts"), "utf8");
const trackingSource = readFileSync(join(root, "hooks/useReverseOperationTracking.js"), "utf8");
const parsingSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const uploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const reverseHistoryActionsSource = readFileSync(join(root, "hooks/useReverseHistoryActions.js"), "utf8");
const deepLinkBootstrapSource = readFileSync(join(root, "hooks/useStudioDeepLinkBootstrap.js"), "utf8");
const reverseResultActionsSource = readFileSync(join(root, "hooks/useReverseResultActions.js"), "utf8");
const shotGenerationSource = readFileSync(join(root, "hooks/useShotGeneration.js"), "utf8");
const ownerSessionSource = [
  readFileSync(join(root, "hooks/useStudioOwnerSession.js"), "utf8"),
  readFileSync(join(root, "hooks/studioOwnerRestore.js"), "utf8"),
].join("\n");
const recipeActionsSource = readFileSync(join(root, "hooks/useStudioRecipeActions.js"), "utf8");
const workspaceActionsSource = readFileSync(join(root, "hooks/useStudioWorkspaceActions.js"), "utf8");
const pageSource = readStudioSource(root);
const editorSource = readFileSync(join(root, "app/studio/StudioStructuredEditor.jsx"), "utf8");
const referencePanelSource = readFileSync(join(root, "app/studio/StudioReferencePanel.jsx"), "utf8");
const submitSource = [
  readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8"),
  readFileSync(join(root, "hooks/generationSubmitWorkflow.js"), "utf8"),
].join("\n");

const normalized = normalizeReverseOperation({
  operation: {
    id: 17,
    status: "running",
    percent: 130,
    frozen_credits: 8,
    charged_credits: 2,
    cancel_requested: 1,
  },
});
assert.equal(normalized.progress, 100);
assert.equal(normalized.cost_frozen, 8);
assert.equal(normalized.cost_settled, 2);
assert.equal(normalized.cancel_requested, true);
assert.equal(isActiveReverseOperation(normalized), true);
assert.equal(isTerminalReverseOperation(normalized), false);
const trackingLost = reverseOperationTrackingLost(normalized);
assert.equal(trackingLost.status, "running", "a client tracking outage must not forge a server failure");
assert.equal(trackingLost.error_code, "tracking_lost");
assert.equal(isActiveReverseOperation(trackingLost), true);
assert.equal(normalizeReverseOperationList({ items: [{ id: 18, status: "canceled" }] }).length, 1);
assert.equal(isTerminalReverseOperation({ id: 18, status: "canceled" }), true);
assert.deepEqual(
  normalizeReverseResultRevisionList([
    { id: 31, operation_id: 17, version: 3, source: "model_compiled", payload: { final_text: "compiled" } },
    { id: 32, operation_id: 17, version: 4, source: "generation", payload: { final_text: "generated" } },
  ]).map((revision) => revision.source),
  ["model_compiled", "generation"],
);
const resumeCandidates = reverseOperationResumeCandidates({
  image: {
    selected: { type: "image", url: "/source.jpg" },
    reverseOperation: { id: 21, status: "running" },
  },
  video_edit: {
    productAsset: { type: "image", url: "/portrait.jpg" },
    profileOperation: { id: 22, status: "needs_confirmation" },
  },
  video: {
    reverseOperation: { id: 23, status: "succeeded" },
  },
});
assert.deepEqual(resumeCandidates.map(({ mode, trackingKey, operation }) => (
  [mode, trackingKey, operation.id]
)), [
  ["image", "image", 21],
  ["video_edit", "video_edit::profile", 22],
]);
assert.equal(
  resolvePendingReverseOperation({
    pendingReverseResult: { operation_id: 78 },
    trackedOperation: { id: 81, status: "failed" },
    workspaceOperation: { id: 81, status: "failed" },
  }),
  null,
  "a stale result must never be rendered with a newer operation's status or settlement",
);
assert.equal(
  resolvePendingReverseOperation({
    pendingReverseResult: { operation_id: 80 },
    trackedOperation: { id: 80, status: "succeeded" },
    workspaceOperation: { id: 81, status: "failed" },
  })?.id,
  80,
  "a pending result should resolve only to its own operation",
);
assert.equal(
  reverseOperationRequestSignature({
    target: "image",
    asset_url: "/source.jpg",
    workspace_snapshot_v2: { final_text: "A", structured: { b: "2", a: "1" } },
  }),
  reverseOperationRequestSignature({
    workspace_snapshot_v2: { structured: { a: "1", b: "2" }, final_text: "A" },
    asset_url: "/source.jpg",
    target: "image",
  }),
  "equivalent request bodies must reuse the same pending id regardless of key order",
);
assert.notEqual(
  reverseOperationRequestSignature({ asset_url: "/source.jpg", workspace_snapshot_v2: { final_text: "A" } }),
  reverseOperationRequestSignature({ asset_url: "/source.jpg", workspace_snapshot_v2: { final_text: "B" } }),
  "every effective workspace input must participate in the pending request signature",
);

const explicitlyOpenedResult = {
  structured: { "主体": "历史任务主体" },
  final_text: "历史压缩稿",
  video_analysis: {
    sampled_frames: Array.from({ length: 12 }, (_, index) => ({
      index: index + 1,
      timestamp_seconds: index,
    })),
    shots: Array.from({ length: 6 }, (_, index) => ({
      id: index + 1,
      start_seconds: index * 2,
      end_seconds: index * 2 + 2,
      visual: `历史镜头${index + 1}`,
      action: `历史动作${index + 1}`,
      evidence_frame_indices: [index * 2 + 1, index * 2 + 2],
      confidence: 0.9,
    })),
  },
};
const explicitlyOpenedOperation = {
  id: 76,
  target: "video",
  result_schema_version: "v3",
  applied_result_version: 4,
};
const explicitRestorePatch = buildReverseHistoryWorkspacePatch({
  operation: explicitlyOpenedOperation,
  result: explicitlyOpenedResult,
  restored: {
    subjectMode: "general",
    workspace: {
      pendingReverseResult: { operation_id: 74, result: { video_analysis: { shots: [] } } },
      reverseOperation: { id: 74, target: "video" },
    },
  },
  revisions: [{ id: 145, operation_id: 76 }],
  feedback: null,
  appliedRevision: null,
});
assert.equal(explicitRestorePatch.pendingReverseResult.operation_id, 76);
assert.equal(explicitRestorePatch.pendingReverseResult.result.video_analysis.shots.length, 6);
assert.match(explicitRestorePatch.pendingReverseResult.result.final_text, /镜头6/);
assert.doesNotMatch(explicitRestorePatch.pendingReverseResult.result.final_text, /历史压缩稿/);
assert.equal(explicitRestorePatch.reverseOperation.id, 76);
assert.match(deepLinkBootstrapSource, /cloudDraftLoadedRef\.current[\s\S]*?parseUnifiedTaskKey[\s\S]*?task\.kind !== "reverse"/);
assert.match(deepLinkBootstrapSource, /openRecentReverseOperation\(\{ id: task\.id \}\)/);

for (const endpoint of [
  "/api/prompt/reverse-operations",
  "/confirm-cover",
  "/cancel",
  "/ws-ticket",
  "/revisions",
  "/apply",
  "/feedback",
  "/retry",
  "/api/recipes",
]) assert.match(apiSource, new RegExp(endpoint.replaceAll("/", "\\/")));
assert.match(trackingSource, /reverseOperationWsTicket\(tracker\.id\)/);
assert.match(trackingSource, /new WebSocket\(wsUrl\(/);
assert.match(trackingSource, /ws\.onclose[\s\S]*fallbackToPolling\(\)/);
assert.match(trackingSource, /tracker\.polling = true/);
assert.match(
  trackingSource,
  /connect\(\);\s*tracker\.timer = setTimeout\(poll, WS_WATCHDOG_INTERVAL_MS\)/,
  "a connected websocket must retain a low-frequency poll so missed terminal events cannot freeze the UI",
);
assert.match(
  trackingSource,
  /tracker\.polling \? POLL_INTERVAL_MS : WS_WATCHDOG_INTERVAL_MS/,
  "websocket watchdog polling should stay slower than full fallback polling",
);
assert.doesNotMatch(
  trackingSource,
  /ws\.onopen\s*=\s*\(\)\s*=>\s*\{\s*tracker\.wsAttempts\s*=\s*0/,
  "an opened socket that immediately closes must eventually fall back to polling",
);
assert.match(trackingSource, /const merged = \{ \.\.\.tracker\.operation,/);
assert.match(trackingSource, /api\.reverseOperation\(tracker\.id\)/);
assert.match(trackingSource, /api\.reverseOperations\(\{ limit: 100 \}\)/);
assert.match(trackingSource, /operationTrackingKey[\s\S]*`\$\{mode\}::profile`/);
assert.match(trackingSource, /operation\.status === "needs_confirmation"/);
const restoreSource = trackingSource.match(
  /const restore = async \(\) => \{[\s\S]*?(?=\n    restore\(\);)/,
)?.[0] || "";
assert.ok(
  restoreSource.indexOf("api.reverseOperation(operationId)")
    < restoreSource.indexOf("api.reverseOperations({ limit: 100 })"),
  "saved operation ids must be refreshed before the active-operation list fallback",
);
assert.match(restoreSource, /startTracking\(operation, \{[\s\S]*recoveredFromDraft: true/);
assert.match(
  restoreSource,
  /catch \(error\) \{[\s\S]*startTracking\(candidate\.operation,[\s\S]*recoveredFromDraft: true/,
  "a transient direct-refresh failure must keep the saved operation id recoverable",
);
assert.match(
  restoreSource,
  /for \(const operation of normalizeReverseOperationList\(payload\)\)[\s\S]*if \(!isActiveReverseOperation\(operation\)\) continue/,
  "the list fallback must never replay unrelated successful history",
);
assert.match(ownerSessionSource, /reverseOperationResumeCandidates\(restoredWorkspaces\)/);
assert.match(ownerSessionSource, /reverseResumeOperationsRef\.current = \[\]/);
assert.match(ownerSessionSource, /const ACTIVE_REVERSE_STATUSES = new Set\(\["queued", "running", "needs_confirmation"\]\)/);
assert.match(
  ownerSessionSource,
  /hasActiveReverseOperations\(workspaces\)[\s\S]*saveStudioSessionDraft\(active \? "reverse_operation_active" : "reverse_operation_settled", \{[\s\S]*persistCloud: false/,
  "active reverse operations must be persisted locally immediately instead of waiting for the cloud debounce",
);
assert.match(
  trackingSource,
  /if \(isTerminalReverseOperation\(operation\)\)[\s\S]*if \(isActiveReverseOperation\(operation\)\)[\s\S]*publish\(operation/,
  "cooperative cancellation must keep tracking running responses until a real terminal state",
);
assert.match(
  trackingSource,
  /if \(currentOperation && !isActiveReverseOperation\(currentOperation\)\) return currentOperation/,
  "terminal operations must not be canceled again while an explicit saved id remains cancellable",
);
assert.match(
  trackingSource,
  /reverseOperationTrackingLost\(tracker\.operation \|\| initial\)[\s\S]*publish\(operation, mode, context, trackingKey\)/,
  "tracking loss must retain the active operation for refresh recovery",
);

assert.match(parsingSource, /profileOperation: operation/);
assert.match(parsingSource, /operationTarget === "portrait_profile"[\s\S]*portraitProfile: resolvedProfile/);
const recoveredBindingSource = parsingSource.match(
  /function bindRecoveredOperationWorkspace\([\s\S]*?(?=\n  function applyReverseOperationResult)/,
)?.[0] || "";
assert.match(recoveredBindingSource, /workspacePatchFromReverseSnapshot\(snapshot\)/);
assert.match(recoveredBindingSource, /recoveredSourceAsset\(operation, restoredWorkspace, operationTarget\)/);
assert.match(
  recoveredBindingSource,
  /\(profileTarget \? profileAssetByModeRef : selectedByModeRef\)\.current\[mode\] = sourceAsset/,
  "recovered profile and prompt operations must seed separate source references",
);
assert.match(recoveredBindingSource, /productAsset: current\.productAsset \|\| sourceAsset/);
const applyReverseResultSource = parsingSource.match(
  /function applyReverseOperationResult\([\s\S]*?(?=\n  function handleReverseOperationUpdate)/,
)?.[0] || "";
assert.match(applyReverseResultSource, /kind: "pending_reverse_review"/);
assert.match(applyReverseResultSource, /pendingReverseResult: pendingResult/);
assert.doesNotMatch(
  applyReverseResultSource,
  /setWorkspacePatch\(\{[\s\S]{0,500}\bprompt:\s*reversePrompt/,
  "reverse completion must wait for explicit review instead of overwriting the workspace prompt",
);
assert.match(parsingSource, /workspace_snapshot_v3: workspaceSnapshot/);
assert.match(parsingSource, /const sources = \[/);
for (const field of [
  "analysis_focus",
  "analysis_precision",
  "output_purpose",
  "custom_instruction",
  "source_range",
  "custom_keyframes",
  "include_audio",
]) assert.match(parsingSource, new RegExp(`${field}:`), `v3 reverse request should include ${field}`);
assert.match(parsingSource, /assetSignature\(selectedByModeRef\.current\[mode\]\) !== targetSignature/);
assert.match(parsingSource, /cancelTrackedReverseOperation\(trackingKey, savedOperationId\(trackingKey\)\)[\s\S]*stopReverseTracker\(trackingKey\)/);
const reverseSubmitSource = parsingSource.match(
  /async function doReverse\(\)[\s\S]*?(?=\n  function savedOperationId)/,
)?.[0] || "";
assert.match(
  reverseSubmitSource,
  /if \(!isCurrent\(\)\) \{[\s\S]*await cancelStaleReverseOperation\(operation\)/,
  "input changes after confirmed execution must cancel the newly created reverse operation",
);
assert.match(parsingSource, /const profileAssetByModeRef = useRef\(\{\}\)/);
assert.match(
  parsingSource,
  /if \(!Object\.prototype\.hasOwnProperty\.call\(targetRef\.current, candidate\.mode\)\)[\s\S]*targetRef\.current\[candidate\.mode\] = candidate\.sourceAsset/,
  "refresh recovery must not overwrite a newly selected source on every render",
);
assert.match(
  parsingSource,
  /appliedUrl: String\(asset\.source_page_url \|\| ""\)\.trim\(\)/,
  "selecting a fetched asset must preserve its successfully applied source URL",
);
assert.match(
  parsingSource,
  /isProfileOperation[\s\S]*\? \{ productProfiling: false, profileOperation: null \}[\s\S]*: \{ reversing: false, reverseOperation: null \}/,
  "a settled operation for a replaced source must clear the correct busy state",
);
assert.match(uploadSource, /cancelRecoveredProfileOperation\?\.\(mode\)/);
assert.match(uploadSource, /await cancelProfileOperation\(mode\)/);
assert.match(uploadSource, /import \{ buildReverseOperationRequestSnapshotV2 \} from "\.\.\/app\/studio\/reverseSnapshot"/);
const profilePrefetchSource = uploadSource.match(
  /async function prefetchProductProfile\([\s\S]*?(?=\n  async function cancelProfileOperation)/,
)?.[0] || "";
assert.match(
  profilePrefetchSource,
  /workspace_snapshot_v2:\s*\{[\s\S]*buildReverseOperationRequestSnapshotV2\(\{[\s\S]*productAsset:\s*asset,[\s\S]*source_signature:\s*signature/,
  "profile prefetch must persist the current product asset in its v2 workspace snapshot",
);
assert.match(
  profilePrefetchSource,
  /requestQuoteConfirmation\(\{[\s\S]*kind: "reverse"[\s\S]*execute: \(\{ request \}\) => api\.createReverseOperation\(request\)/,
  "profile prefetch must quote before creating the paid reverse operation",
);
assert.match(profilePrefetchSource, /normalizeReverseOperation\(profileConfirmation\.result\)/);
assert.match(profilePrefetchSource, /waitForTrackedProfileOperation\(createdOperation,[\s\S]*trackProfileReverseOperation/);
assert.doesNotMatch(profilePrefetchSource, /createAndWaitForReverseOperation/);
assert.match(
  profilePrefetchSource,
  /if \(keepTracking\) \{[\s\S]*productProfiling: true,[\s\S]*profileOperation: activeOperation/,
  "profile prefetch tracking loss must retain the active operation in the workspace",
);
assert.match(submitSource, /import \{ buildReverseOperationRequestSnapshotV2 \} from "\.\.\/app\/studio\/reverseSnapshot"/);
assert.match(
  submitSource,
  /workspace_snapshot_v2:\s*\{[\s\S]*buildReverseOperationRequestSnapshotV2\(\{[\s\S]*productAsset,[\s\S]*source_signature:\s*productSignature/,
  "generation-time profile requests must persist the current product asset in their v2 workspace snapshot",
);
assert.match(
  submitSource,
  /requestQuoteConfirmation\(\{[\s\S]*kind: "reverse"[\s\S]*execute: \(\{ request \}\) => api\.createReverseOperation\(request\)/,
  "generation-time profile fallback must quote before creating the paid reverse operation",
);
assert.match(submitSource, /normalizeReverseOperation\(profileConfirmation\.result\)/);
assert.match(submitSource, /waitForTrackedProfileOperation\(createdOperation,[\s\S]*trackProfileReverseOperation/);
assert.doesNotMatch(submitSource, /createAndWaitForReverseOperation/);
assert.match(
  submitSource,
  /if \(keepTracking\) \{[\s\S]*productProfiling: true,[\s\S]*profileOperation: activeOperation/,
  "generation-time profile tracking loss must remain refresh-recoverable",
);
assert.equal(
  (pageSource.match(/trackProfileReverseOperation:\s*reverse\.trackProfileReverseOperation/g) || []).length,
  2,
  "the reference and generation domains must connect both profile creation paths to the shared reverse tracker",
);
assert.match(pageSource, /<StudioReverseResultPanel/);
assert.match(pageSource, /<StudioRecentReversePanel/);
assert.match(
  pageSource,
  /const pendingReverseOperation = reverseOperationForPendingResult\(\)[\s\S]*pending=\{pendingReverseResult\}[\s\S]*operation=\{pendingReverseOperation\}/,
  "the result panel must restore persisted portable results even when no online operation is available",
);
assert.match(reverseResultActionsSource, /applyReverseResultApplication\(latestWorkspace, pending, mode\)/);
assert.match(reverseResultActionsSource, /undoReverseResultApplication\(workspace, reverseUndoSnapshot\)/);
assert.match(reverseResultActionsSource, /api\.createReverseOperationRevision\(operation\.id/);
assert.match(reverseResultActionsSource, /api\.applyReverseOperationResult\(operation\.id/);
assert.match(reverseResultActionsSource, /api\.updateReverseOperationFeedback\(operation\.id/);
assert.match(reverseHistoryActionsSource, /api\.retryReverseOperation\(operation\.id/);
assert.match(recipeActionsSource, /api\.createCreationRecipe\(/);
assert.match(recipeActionsSource, /api\.createCreationRecipeVersion\(/);
assert.match(pageSource, /onGenerationSubmitted: (?:task\.)?handleGenerationSubmitted/);
assert.match(
  submitSource,
  /onGenerationSubmitted\?\.\(\{ task: nextTask, payload: confirmation\.request, stage \}\)/,
  "generation lineage must bind the normalized request that was actually confirmed and submitted",
);
for (const lineageField of ["source_revision_id", "compiled_revision_id", "generation_revision_id"]) {
  assert.match(shotGenerationSource, new RegExp(`submittedTask\\?\\.${lineageField}`));
}
assert.match(shotGenerationSource, /reverseAppliedRevisionId:\s*sourceRevisionId/);
assert.doesNotMatch(shotGenerationSource, /source:\s*["']generation["']/);
assert.match(
  apiTypeSource,
  /interface ReverseResultRevisionCreate \{\s*source:\s*"user_edit" \| "applied";/,
  "the browser contract must not allow server-owned revision sources",
);
assert.match(apiTypeSource, /interface ReverseResultApplyInput/);
assert.match(apiTypeSource, /applyReverseOperationResult/);
const clearProductSource = workspaceActionsSource.match(
  /async function clearProductAsset\(\)[\s\S]*?(?=\n  async function clearRef)/,
)?.[0] || "";
assert.match(clearProductSource, /cancelRecoveredProfileOperationForMode\(creationMode\)/);
assert.match(clearProductSource, /cancelProfileOperation\(creationMode\)/);
assert.ok(
  clearProductSource.indexOf("await cancelProfileOperation(creationMode)")
    < clearProductSource.indexOf("productAsset: null"),
  "clearing a subject must not destroy local state before remote cancellation succeeds",
);
const selectAssetSource = parsingSource.match(
  /async function selectAssetForMode\([\s\S]*?(?=\n  async function waitForParseResult)/,
)?.[0] || "";
assert.ok(
  selectAssetSource.indexOf("await cancelReverseOperationForMode(targetMode)")
    < selectAssetSource.indexOf("selectedByModeRef.current[targetMode] = asset"),
  "replacing a reference must preserve the old local selection when cancellation fails",
);
const productUploadSource = uploadSource.match(
  /async function doUploadProductImage\([\s\S]*?(?=\n  async function doUploadVideo)/,
)?.[0] || "";
assert.ok(
  productUploadSource.indexOf("await cancelProfileOperation(mode)")
    < productUploadSource.indexOf("const productReqId"),
  "subject replacement must not begin uploading before cancellation succeeds",
);

const imageVisual = visualStructuredFields({
  "主体": "红色瓶子",
  "妆发五官": "自然妆容",
  "人像意图": "无",
  "广告目标": "未见明确卖点",
  "包装文字": "OCR 文字",
  "旁白": "不得进入",
  "final_text": "完整文本",
}, "image");
assert.deepEqual(imageVisual, { "主体": "红色瓶子", "妆发五官": "自然妆容" });
const imageLayoutVisual = visualStructuredFields({
  "主体": "红色瓶子",
  "文字版式": "顶部白色无衬线标题，底部小号说明文字",
  "负向": "乱码与水印",
}, "image");
assert.match(composePromptFromStructured(imageLayoutVisual), /顶部白色无衬线标题/);
assert.doesNotMatch(composePromptFromStructured(imageLayoutVisual), /乱码与水印/);
const videoVisual = visualStructuredFields({
  "主体": "产品",
  "主体运动设计": "慢速旋转",
  "镜头运动设计": "缓慢推近",
  "主体动作": "未证实顶层动作",
  "镜头运动": "未证实顶层运镜",
  "剪辑节奏": "未证实顶层剪辑",
  "时序分镜": "未证实顶层时序",
  "迁移生成指令": "未证实顶层迁移指令",
  "字幕卖点": "新品上市",
  "音效": "水滴声",
  "观察事实": "分析证据",
  "源视频规格": "960x540，5.016秒，约24fps",
}, "video");
assert.equal(composePromptFromStructured(videoVisual, "", { target: "video" }), "产品");
assert.doesNotMatch(JSON.stringify(videoVisual), /动作|运镜|剪辑|时序|迁移|字幕|音效|证据/);
const imageMotionVisual = visualStructuredFields({
  "静态观察": "产品置于白色台面",
  "主体运动设计": "新设计为慢速旋转",
  "镜头运动设计": "新设计为缓慢推近",
  "时序设计": "0-3s 保持，3-5s 新设计运动",
  "字幕卖点": "不得进入",
  "旁白": "不得进入",
}, "image_to_video");
assert.match(
  composePromptFromStructured(imageMotionVisual, "", { target: "image_to_video" }),
  /新设计为慢速旋转/,
);
assert.doesNotMatch(JSON.stringify(imageMotionVisual), /字幕|旁白/);
const evidenceTransfer = composeEvidenceBackedVideoTransferPrompt({
  "场景背景": "白色棚拍台",
  "主体动作": "未证实顶层动作",
  "迁移生成指令": "未证实顶层迁移指令",
}, {
  sampled_frames: [
    { index: 1, timestamp_seconds: 0 },
    { index: 2, timestamp_seconds: 2.5 },
  ],
  shots: [{
    start_seconds: 0,
    end_seconds: 2.5,
    visual: "产品居中",
    action: "缓慢旋转",
    camera: "镜头缓慢推近",
    ocr: "新品上市",
    audio_cue: "水滴声",
    evidence_frame_indices: [1, 2],
    confidence: 0.9,
  }],
}, "product");
assert.match(evidenceTransfer, /镜头1：画面：产品居中；动作：缓慢旋转；运镜：镜头缓慢推近/);
assert.doesNotMatch(evidenceTransfer, /未证实|新品上市|水滴声|evidence/i);
assert.ok(evidenceTransfer.length > 0);
const compressedLongTransfer = composeEvidenceBackedVideoTransferPrompt({}, {
  sampled_frames: [
    { index: 1, timestamp_seconds: 0 },
    { index: 2, timestamp_seconds: 25.408 },
  ],
  shots: [{
    start_seconds: 0,
    end_seconds: 25.408,
    visual: "深蓝玻璃精华瓶依次展示滴管、液滴和瓶身折射",
    evidence_frame_indices: [1, 2],
    confidence: 0.9,
  }],
}, "");
assert.match(compressedLongTransfer, /参考片时间线较长/);
assert.match(compressedLongTransfer, /逐镜分别生成后按顺序合成/);
assert.match(compressedLongTransfer, /镜头1：/);
assert.doesNotMatch(compressedLongTransfer, /25\.41 秒|镜头1（/);
const singleFrameTransfer = composeEvidenceBackedVideoTransferPrompt({}, {
  sampled_frames: [
    { index: 1, timestamp_seconds: 1 },
    { index: 2, timestamp_seconds: 1 },
  ],
  shots: [{
    start_seconds: 0.5,
    end_seconds: 1.5,
    visual: "产品居中",
    action: "幻觉旋转",
    camera: "幻觉环绕",
    lighting: "柔和侧光",
    transition: "幻觉闪白",
    evidence_frame_indices: [1, 2],
    confidence: 0.95,
  }],
}, "portrait");
assert.match(singleFrameTransfer, /画面：产品居中；光线：柔和侧光/);
assert.doesNotMatch(singleFrameTransfer, /幻觉旋转|幻觉环绕|幻觉闪白/);
const missingEvidenceTransfer = composeEvidenceBackedVideoTransferPrompt({}, {
  sampled_frames: [{ index: 1, timestamp_seconds: 0 }],
  shots: [{
    start_seconds: 0,
    end_seconds: 1,
    visual: "无证据视觉",
    action: "无证据动作",
    evidence_frame_indices: [],
    confidence: 0.9,
  }],
}, "product");
assert.doesNotMatch(missingEvidenceTransfer, /无证据视觉|无证据动作/);
assert.deepEqual(visualStructuredFields({
  "档案类型": "产品档案",
  "产品品类": "棉柔巾",
  "主色材质": "黑白纸盒",
  "关键图案": "白色 Logo",
  "可迁移项": "光线和镜头",
  "负向": "乱码",
}, "product_profile"), {
  "产品品类": "棉柔巾",
  "主色材质": "黑白纸盒",
  "关键图案": "白色 Logo",
  "可迁移项": "光线和镜头",
});
assert.deepEqual(visualStructuredFields({
  "年龄语境": "成年",
  "脸型五官": "椭圆脸",
  "姿态表情": "正面微笑",
  "可调整项": "服装与场景",
  "产品品类": "不得进入",
  "负向": "换脸",
}, "portrait_profile"), {
  "年龄语境": "成年",
  "脸型五官": "椭圆脸",
  "姿态表情": "正面微笑",
  "可调整项": "服装与场景",
});

const future = new Date(Date.now() + 60_000).toISOString();
const snapshot = buildReverseSnapshotV2({
  creationMode: "video_edit",
  subjectMode: "portrait",
  target: "portrait_profile",
  selected: { type: "video", url: "/video.mp4", retention_expires_at: future },
  productAsset: { type: "image", url: "/portrait.jpg", retention_expires_at: future },
  assets: [{ type: "image", url: "/alternate.jpg", retention_expires_at: future }],
  structured: { "脸型五官": "椭圆脸" },
  finalText: "人物提示词",
  promptDirty: true,
  videoAnalysisPreset: "quality",
  videoAnalysis: { analysis_mode: "image_motion", source: { audio_analyzed: false } },
  subjectProfile: { structured: { "脸型五官": "椭圆脸" } },
  appliedVersion: 4,
  appliedRevisionId: 44,
  resultRevisions: [
    { id: 44, operation_id: 12, version: 4, source: "applied", payload: { final_text: "人物提示词" } },
    { id: 45, operation_id: 12, version: 5, source: "model_compiled", payload: { final_text: "compiled" } },
    { id: 46, operation_id: 12, version: 6, source: "generation", payload: { final_text: "generated" } },
  ],
});
const requestSnapshot = buildReverseOperationRequestSnapshotV2({
  creationMode: "video_edit",
  subjectMode: "portrait",
  target: "portrait_profile",
  selected: { type: "video", url: "/video.mp4", retention_expires_at: future },
  productAsset: { type: "image", url: "/portrait.jpg", retention_expires_at: future },
  assets: [{ type: "image", url: "/alternate.jpg", retention_expires_at: future }],
  videoAnalysisPreset: "fine",
  subjectProfile: { structured: { "脸型五官": "椭圆脸" } },
});
assert.equal(requestSnapshot.product_asset.url, "/portrait.jpg");
assert.equal(requestSnapshot.video_analysis_preset, "fine");
assert.equal(requestSnapshot.subject_profile.structured["脸型五官"], "椭圆脸");
assert.equal("structured" in requestSnapshot, false);
assert.equal("final_text" in requestSnapshot, false);
assert.equal("video_analysis" in requestSnapshot, false);
assert.equal(snapshot.selected.retention_expires_at, future);
assert.equal(snapshot.prompt_dirty, true);
const rootRestore = workspacePatchFromReverseSnapshot(snapshot);
assert.equal(rootRestore.subjectMode, "portrait");
assert.equal(rootRestore.workspace.portraitProfile.structured["脸型五官"], "椭圆脸");
assert.equal(rootRestore.workspace.productProfile, null);
assert.equal(rootRestore.workspace.assets[0].url, "/alternate.jpg");
assert.equal(rootRestore.workspace.videoAnalysisPreset, "standard");
assert.equal(rootRestore.workspace.reverseVideoAnalysis.analysis_mode, "image_motion");
assert.equal(snapshot.reverse_applied_revision_id, 44);
assert.equal(rootRestore.workspace.reverseAppliedRevisionId, 44);
assert.equal(rootRestore.workspace.productVideoTemplate, "prompt_driven");
assert.equal(rootRestore.workspace.promptDirty, true);
assert.deepEqual(
  rootRestore.workspace.reverseResultRevisions.map((revision) => revision.source),
  ["applied", "model_compiled", "generation"],
);
assert.equal(workspacePatchFromReverseSnapshot({ version: 2, workspace_snapshot_v2: snapshot }).workspace.prompt, "人物提示词");
assert.equal(reverseSnapshotFromHistory({ params: { reverse_snapshot_v2: snapshot } }).version, 3);
assert.equal(reverseSnapshotFromHistory({ params: { prompt: "v1 only" } }), null);
const rawProductProfileRestore = workspacePatchFromReverseSnapshot({
  version: 2,
  target: "product_profile",
  subject_mode: "product",
  final_text: "锁定黑白纸盒产品身份",
  product_asset: { type: "image", url: "/product.jpg", retention_expires_at: future },
  subject_profile: { "产品品类": "棉柔巾", "主色材质": "黑白纸盒" },
});
assert.deepEqual(rawProductProfileRestore.workspace.productProfile, {
  structured: { "产品品类": "棉柔巾", "主色材质": "黑白纸盒" },
  final_text: "锁定黑白纸盒产品身份",
});
const rawPortraitProfileRestore = workspacePatchFromReverseSnapshot({
  version: 2,
  target: "portrait_profile",
  final_text: "锁定人物身份",
  portrait_profile: { "脸型五官": "椭圆脸", "妆发": "黑色长发" },
});
assert.equal(rawPortraitProfileRestore.subjectMode, "portrait");
assert.deepEqual(rawPortraitProfileRestore.workspace.portraitProfile, {
  structured: { "脸型五官": "椭圆脸", "妆发": "黑色长发" },
  final_text: "锁定人物身份",
});
const expiredRestore = workspacePatchFromReverseSnapshot({
  ...snapshot,
  selected: { type: "video", url: "/expired.mp4", retention_expires_at: "2000-01-01T00:00:00Z" },
});
assert.equal(expiredRestore.workspace.selected, null);
assert.equal(expiredRestore.workspace.prompt, "人物提示词");
assert.equal(expiredRestore.expiredAssetsSkipped, true);
assert.equal(workspacePatchFromReverseSnapshot({ ...snapshot, creation_mode: "invalid", target: "video" }).creationMode, "video");
assert.equal(workspacePatchFromReverseSnapshot({ ...snapshot, subject_mode: "invalid" }).subjectMode, "general");
const legacyNoisyImageRestore = workspacePatchFromReverseSnapshot({
  version: 3,
  target: "image",
  structured: {
    "主体": "直接可见事实：参考1中白色纸盒居中直立。",
    "场景背景": "视觉估计：半透明蓝色冰块环绕中央冰台。",
  },
  final_text: "直接可见事实：参考1中白色纸盒居中直立；未知：地点不确定。",
});
assert.match(legacyNoisyImageRestore.workspace.prompt, /白色纸盒居中直立/);
assert.match(legacyNoisyImageRestore.workspace.prompt, /半透明蓝色冰块环绕中央冰台/);
assert.doesNotMatch(legacyNoisyImageRestore.workspace.prompt, /直接可见事实|视觉估计|未知|参考1/);
assert.equal(workspacePatchFromReverseSnapshot({
  ...snapshot,
  generation: { product_video_template: "single_clip_action" },
}).workspace.productVideoTemplate, "single_clip_action");
assert.equal(workspacePatchFromReverseSnapshot({
  ...snapshot,
  generation: { product_video_template: "unknown_strategy" },
}).workspace.productVideoTemplate, "prompt_driven");
assert.match(
  ownerSessionSource,
  /prompt:\s*snapshotRestore\s*\?\s*\(restoredWorkspace\.prompt\s*\|\|\s*parsedDraft\.prompt\s*\|\|\s*""\)/,
);
assert.match(
  ownerSessionSource,
  /promptDirty:\s*snapshotRestore\s*\?\s*Boolean\(restoredWorkspace\.promptDirty\)\s*:\s*true/,
);

const imageMotionPricing = buildStudioDerivedViewState({
  cfg: {
    reverse: {
      image_cost: 5,
      video_presets: [
        { key: "fast", max_cost: 5, max_frames: 3 },
        { key: "standard", max_cost: 5, max_frames: 6 },
        { key: "fine", max_cost: 5, max_frames: 10 },
      ],
    },
  },
  creationMode: "video",
  category: "video",
  isEditMode: false,
  isImageEditMode: false,
  subjectMode: "general",
  productGenerationMode: false,
  portraitGenerationMode: false,
  task: null,
  submitting: false,
  selected: { type: "image", url: "/motion-source.jpg" },
  productAsset: null,
  structured: {},
  prompt: "",
  ratio: "16:9",
  imageQuality: "standard",
  n: 1,
  vDuration: 5,
  vResolution: "720p",
  videoAnalysisPreset: "fine",
});
assert.equal(imageMotionPricing.selectedReverseCost, 5);
assert.equal(imageMotionPricing.reverseImageCost, 5);
assert.equal(imageMotionPricing.selectedReverseCostLabel, "5积分");

const videoAudioPricing = buildStudioDerivedViewState({
  ...{
    cfg: {
      reverse: {
        image_cost: 5,
        audio_surcharge: 2,
        video_presets: [{ key: "fine", max_cost: 5, max_frames: 22 }],
      },
    },
    creationMode: "video",
    category: "video",
    isEditMode: false,
    isImageEditMode: false,
    subjectMode: "general",
    productGenerationMode: false,
    portraitGenerationMode: false,
    task: null,
    submitting: false,
    selected: { type: "video", url: "/reference.mp4" },
    productAsset: null,
    structured: {},
    prompt: "",
    ratio: "9:16",
    imageQuality: "standard",
    n: 1,
    vDuration: 10,
    vResolution: "720p",
    videoAnalysisPreset: "fine",
    reverseConfig: { include_audio: true },
  },
});
assert.equal(videoAudioPricing.selectedReverseCost, 7);
assert.equal(videoAudioPricing.selectedReverseCostLabel, "冻结7积分(最多22帧)");
assert.match(parsingSource, /video_analysis_preset: targetVideoPreset/);
assert.match(referencePanelSource, /单图运动设计成功后按图片反推结算 \{reverseImageCost\} 积分/);

assert.equal(analysisModeLabel("keyframes"), "多帧分析");
assert.equal(analysisModeLabel("cover_fallback"), "封面单帧");
assert.equal(analysisModeLabel("image_motion"), "单图运动设计");
const evidence = videoAnalysisEvidenceData({
  analysis_mode: "cover_fallback",
  degraded_reason: "抽帧失败",
  source: { audio_analyzed: false },
  sampled_frames: [
    { timestamp_seconds: 1.25 },
    { timestamp_seconds: 6.75 },
  ],
  analysis_gaps: [{ message: "6.8s 后无视觉证据" }],
});
assert.equal(evidence.degradedReason, "抽帧失败");
assert.equal(evidence.coverageStart, 1.25);
assert.equal(evidence.coverageEnd, 6.75);
assert.deepEqual(evidence.gapTexts, ["6.8s 后无视觉证据"]);
const rangedEvidence = videoAnalysisEvidenceData({
  analysis_gaps: [
    { start_seconds: 2.2, end_seconds: 10.1 },
    { start_seconds: 11 },
    { end_seconds: 1.5 },
    { code: "unknown_gap" },
  ],
});
assert.deepEqual(rangedEvidence.gapTexts, [
  "2.2s - 10.1s 未覆盖",
  "11.0s 后未覆盖",
  "1.5s 前未覆盖",
  "存在未标注时间的分析缺口",
]);
assert.match(
  referencePanelSource,
  /\{reverseVideoAnalysis && \([\s\S]*<VideoAnalysisEvidence analysis=\{reverseVideoAnalysis\}/,
  "single-image video motion evidence must render even when the selected source is not a video",
);

const doParseSource = parsingSource.match(/async function doParse\(\)[\s\S]*?(?=\n  function pickAsset)/)?.[0] || "";
assert.ok(doParseSource.indexOf('result.status !== "done"') < doParseSource.indexOf("appliedUrl: targetUrl"));
assert.ok(
  doParseSource.indexOf("if (!parsedAssets.length)") < doParseSource.indexOf("cancelReverseOperationForMode(mode)"),
  "a successful fetch with no usable assets must preserve the existing workspace and operation",
);
assert.doesNotMatch(doParseSource.slice(0, doParseSource.indexOf('result.status !== "done"')), /assets:\s*\[\]|selected:\s*null/);
assert.match(workspaceActionsSource, /function updateReferenceUrl\(value\)[\s\S]*setWorkspacePatch\(\{ url: value, parsing: false \}/);
assert.match(
  parsingSource,
  /const reverseBody = \{[\s\S]*workspace_snapshot_v3: workspaceSnapshot[\s\S]*reverseOperationRequestSignature\(reverseBody\)/,
  "the pending id signature must cover the exact request body including the v3 workspace snapshot",
);
assert.match(
  parsingSource,
  /buildReverseOperationRequestSnapshotV3\(\{[\s\S]*source_signature: targetSignature/,
  "ordinary image and video reverse requests must persist only the minimal request snapshot",
);
assert.doesNotMatch(
  parsingSource,
  /buildReverseSnapshotV2\(/,
  "the full result/history snapshot must not be sent with an operation creation request",
);
const doReverseSource = parsingSource.match(/async function doReverse\(\)[\s\S]*?(?=\n  function savedOperationId)/)?.[0] || "";
assert.match(
  doReverseSource,
  /setWorkspacePatch\(\{[\s\S]*reverseOperation: operation,[\s\S]*pendingReverseResult: null,[\s\S]*reverseResultRevisions: \[\],[\s\S]*reverseAppliedVersion: null,[\s\S]*reverseAppliedRevisionId: null,[\s\S]*reverseFeedback: null/,
  "starting a confirmed reverse operation must clear the previous result and its review metadata",
);
const settledReverseSource = parsingSource.match(
  /function handleReverseOperationSettled\([\s\S]*?(?=\n  const \{)/,
)?.[0] || "";
assert.match(
  settledReverseSource,
  /String\(current\.reverseOperation\.id\) !== String\(operation\.id\)/,
  "a stale terminal update must not replace the current reverse operation",
);
assert.match(
  settledReverseSource,
  /pendingReverseResult: null[\s\S]*reverseResultRevisions: \[\][\s\S]*reverseAppliedVersion: null[\s\S]*reverseAppliedRevisionId: null[\s\S]*reverseFeedback: null/,
  "a failed reverse operation must not retain an older result or its review metadata",
);
const staleOperationBranch = doReverseSource.slice(
  doReverseSource.indexOf("if (!isCurrent())"),
  doReverseSource.indexOf("setWorkspacePatch({ reverseOperation: operation"),
);
assert.match(staleOperationBranch, /await cancelStaleReverseOperation\(operation\)/);
assert.match(staleOperationBranch, /return;/);
assert.doesNotMatch(
  staleOperationBranch,
  /setWorkspacePatch/,
  "a stale confirmed operation must never overwrite the current workspace",
);

const unchangedWorkspace = {
  selected: { id: 202, url: "/current-source.jpg" },
  prompt: "current workspace prompt",
  reverseOperation: null,
};
const cancelCalls = [];
const canceled = await cancelStaleReverseOperation(
  { id: 901, status: "queued" },
  async (operationId) => { cancelCalls.push(operationId); },
  () => assert.fail("successful stale cancellation must not report an error"),
);
assert.equal(canceled, true);
assert.deepEqual(cancelCalls, [901]);
assert.deepEqual(unchangedWorkspace, {
  selected: { id: 202, url: "/current-source.jpg" },
  prompt: "current workspace prompt",
  reverseOperation: null,
});

const cancelFailure = new Error("cancel unavailable");
const reportedCancelErrors = [];
const canceledAfterFailure = await cancelStaleReverseOperation(
  { id: 902, status: "running" },
  async () => { throw cancelFailure; },
  (error, context) => { reportedCancelErrors.push({ error, context }); },
);
assert.equal(canceledAfterFailure, false);
assert.deepEqual(reportedCancelErrors, [{
  error: cancelFailure,
  context: "cancel stale reverse operation after confirmed execution",
}]);
assert.deepEqual(unchangedWorkspace, {
  selected: { id: 202, url: "/current-source.jpg" },
  prompt: "current workspace prompt",
  reverseOperation: null,
});

assert.ok(
  doReverseSource.indexOf('existingOperation?.status === "needs_confirmation"')
    < doReverseSource.indexOf("bumpReverseRequest(mode)"),
  "a cover-confirmation task must block ordinary reverse resubmission before any request id is advanced",
);
assert.match(referencePanelSource, /disabled=\{!selected \|\| !reverseEnabled \|\| reversing \|\| reverseNeedsConfirmation\}/);
assert.match(referencePanelSource, /请先处理封面确认/);
const confirmCoverSource = parsingSource.match(
  /async function confirmReverseCover\([\s\S]*?(?=\n  function cancelReverseOperationForMode)/,
)?.[0] || "";
assert.match(confirmCoverSource, /api\.uploadImage\(fallbackFile\)/);
assert.match(confirmCoverSource, /fallbackImage = uploaded\?\.url \|\| uploaded\?\.preview_url \|\| null/);
assert.match(confirmCoverSource, /confirmTrackedReverseCover\([\s\S]*fallbackImage/);
assert.match(referencePanelSource, /onConfirmCover\?\.\(fallbackCoverFile\)/);

assert.match(pageSource, /if \(promptDirty\)[\s\S]*structuredDirty: true[\s\S]*composePromptFromStructured/);
assert.match(workspaceActionsSource, /function recompose\(\)[\s\S]*structuredDirty: false/);
assert.match(workspaceActionsSource, /function undoStructuredChanges\(\)[\s\S]*structuredBaseline[\s\S]*structuredDirty: false/);
assert.match(editorSource, />\s*应用结构修改\s*</);
assert.match(editorSource, />\s*撤销结构修改\s*</);
assert.match(submitSource, /if \(structuredDirty\)[\s\S]*请先应用结构修改或撤销结构修改/);

const clearCurrentSource = workspaceActionsSource.match(
  /async function clearCurrentWorkspace\(\)[\s\S]*?(?=\n  async function clearAllWorkspaces)/,
)?.[0] || "";
const clearAllSource = workspaceActionsSource.match(
  /async function clearAllWorkspaces\(\)[\s\S]*?(?=\n\n  return \{)/,
)?.[0] || "";
assert.match(clearCurrentSource, /clearWorkspaceContent\(current\)/);
assert.doesNotMatch(clearCurrentSource, /clearAllWorkspaceContent/);
assert.match(clearAllSource, /window\.confirm\(/);
assert.match(clearAllSource, /clearAllWorkspaceContent/);

console.log("reverse operations frontend test passed");
