import { reverseOperationResult } from "../../lib/reverseOperations";
import { normalizeProductVideoStrategyKey } from "./productVideoStrategy";
import { storyboardResultShots } from "./storyboard";
import {
  bindGenerationTaskToVideoCompositionShot,
  normalizeVideoComposition,
} from "./videoComposition";

// 反推应用冲突详情：workspace 字段 → 用户可读名称（仅用于冲突详情面板渲染）。
const REVERSE_APPLY_CONFLICT_FIELD_LABELS = {
  prompt: "提示词",
  negative: "负向提示词",
  structured: "结构化维度",
  structuredBaseline: "结构化维度基线",
  ratio: "画面比例",
  vDuration: "视频时长",
  vResolution: "视频分辨率",
  reverseVideoAnalysis: "视频分析",
  image_evidence: "图片区域证据",
};
const REVERSE_APPLY_CONFLICT_INTERNAL_FIELDS = new Set([
  "promptSourceSignature",
  "structuredSource",
  "promptDirty",
  "negativeTouched",
  "structuredDirty",
  "structuredBaseline",
]);

export function reverseApplyConflictFieldChips(fields) {
  const rows = [...new Set(Array.isArray(fields) ? fields : [])];
  const visible = rows.filter((field) => !REVERSE_APPLY_CONFLICT_INTERNAL_FIELDS.has(field));
  return (visible.length ? visible : rows).map((field) => ({
    field,
    label: REVERSE_APPLY_CONFLICT_FIELD_LABELS[field] || field,
  }));
}

export function positivePromptDraftInteger(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

export function normalizedPromptDraft(raw) {
  const reverseSnapshot = raw.reverse_snapshot_v3 || raw.reverse_snapshot_v2 || null;
  const structured = raw.structured && typeof raw.structured === "object" && !Array.isArray(raw.structured)
    ? raw.structured
    : {};
  const generation = raw.generation && typeof raw.generation === "object" && !Array.isArray(raw.generation)
    ? raw.generation
    : {};
  const snapshotRecipeId = reverseSnapshot?.creation_recipe_id;
  const snapshotRecipeVersion = reverseSnapshot?.creation_recipe_version;
  const snapshotShareSlug = reverseSnapshot?.creation_recipe_share_slug;
  const snapshotSource = reverseSnapshot?.creation_recipe_source;
  const requestedSource = String(raw.creationRecipeSource || snapshotSource || "");
  return {
    prompt: String(raw.prompt || ""),
    negative: String(raw.negative || ""),
    structured,
    generation,
    category: String(raw.category || ""),
    creationMode: String(raw.creationMode || ""),
    creationRecipeId: positivePromptDraftInteger(raw.creationRecipeId || snapshotRecipeId),
    creationRecipeVersion: positivePromptDraftInteger(raw.creationRecipeVersion || snapshotRecipeVersion),
    creationRecipeShareSlug: String(raw.creationRecipeShareSlug || snapshotShareSlug || ""),
    creationRecipeSource: ["owner", "public", "share"].includes(requestedSource) ? requestedSource : "",
    savedAt: Number(raw.savedAt || 0),
    reverseSnapshot,
    legacyReverse: Boolean(raw.legacy_reverse),
  };
}

export function reverseResultPayload(pending) {
  if (!pending || typeof pending !== "object") return null;
  if (pending.payload && typeof pending.payload === "object") return pending.payload;
  if (pending.result && typeof pending.result === "object") return pending.result;
  return pending;
}

export function replaceReverseResultPayload(pending, payload, dirty = true) {
  if (!pending || typeof pending !== "object") return payload;
  if (pending.payload && typeof pending.payload === "object") {
    return { ...pending, payload, dirty };
  }
  if (pending.result && typeof pending.result === "object") {
    return { ...pending, result: payload, dirty };
  }
  return { ...payload, dirty };
}

export function reverseResultEnvelope(operation, result = reverseOperationResult(operation), dirty = false) {
  if (!operation || !result) return null;
  const snapshot = operation.workspace_snapshot_v3 || operation.workspace_snapshot_v2 || {};
  const sourceSignature = operation.request_context?.source_signature || snapshot.source_signature || "";
  return {
    kind: "pending_reverse_review",
    operation_id: operation.id,
    category: operation.target === "video" ? "video" : "image",
    target: operation.target,
    source_signature: sourceSignature,
    received_at: new Date().toISOString(),
    dirty: Boolean(dirty),
    result,
  };
}

export function bindGenerationTaskToPendingReverseResult(pending, {
  operationId,
  shotId,
  task,
}) {
  const result = reverseResultPayload(pending);
  if (!result) return null;
  const composition = normalizeVideoComposition(
    result.video_composition,
    storyboardResultShots(result),
    operationId,
  );
  const binding = bindGenerationTaskToVideoCompositionShot(composition, {
    shotId,
    task,
  });
  return {
    binding,
    result: binding.changed
      ? { ...result, video_composition: binding.composition }
      : result,
  };
}

const REVERSE_APPLY_PARENT_SOURCES = new Set(["normalized", "user_edit"]);

export function positiveRevisionId(value) {
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : null;
}

export function findReverseApplyParentRevision(revisions, operationId) {
  const ownerOperationId = positiveRevisionId(operationId);
  return (Array.isArray(revisions) ? revisions : [])
    .filter((revision) => (
      REVERSE_APPLY_PARENT_SOURCES.has(String(revision?.source || ""))
      && (!ownerOperationId || Number(revision?.operation_id) === ownerOperationId)
    ))
    .sort((left, right) => Number(right.version || 0) - Number(left.version || 0))[0] || null;
}

export function mergeReverseResultRevisions(current, additions) {
  const merged = [...(Array.isArray(current) ? current : [])];
  for (const revision of additions || []) {
    if (revision?.id && !merged.some((item) => Number(item?.id) === Number(revision.id))) {
      merged.push(revision);
    }
  }
  return merged.sort((left, right) => Number(left.version || 0) - Number(right.version || 0));
}

export function clearsReviewedImageEvidence(parentRevision, payload) {
  const parentEvidence = parentRevision?.payload?.image_evidence;
  return Array.isArray(parentEvidence)
    && parentEvidence.length > 0
    && Array.isArray(payload?.image_evidence)
    && payload.image_evidence.length === 0;
}

export function promptDraftGenerationPatch(generation) {
  if (!generation || typeof generation !== "object" || Array.isArray(generation)) return {};
  const patch: Record<string, unknown> = {};
  if (generation.ratio) patch.ratio = String(generation.ratio);
  if (generation.image_quality) patch.imageQuality = String(generation.image_quality);
  const count = positivePromptDraftInteger(generation.count ?? generation.n);
  if (count) patch.n = count;
  if (generation.seed !== undefined && generation.seed !== null) patch.seed = String(generation.seed);
  const duration = positivePromptDraftInteger(generation.duration);
  if (duration) patch.vDuration = duration;
  if (generation.resolution) patch.vResolution = String(generation.resolution);
  const productVideoTemplate = normalizeProductVideoStrategyKey(
    generation.product_video_template ?? generation.productVideoTemplate,
  );
  if (productVideoTemplate) patch.productVideoTemplate = productVideoTemplate;
  return patch;
}

export function reverseSnapshotModelSelections(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const nested = snapshot.workspace_snapshot_v2 && typeof snapshot.workspace_snapshot_v2 === "object"
    ? snapshot.workspace_snapshot_v2
    : {};
  const selections = snapshot.model_selections || nested.model_selections;
  return selections && typeof selections === "object" ? selections : null;
}
