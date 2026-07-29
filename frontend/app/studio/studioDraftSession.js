import { MAX_REVERSE_BATCH_ITEMS } from "../../lib/reverseBatches";
import { MAX_PRODUCT_DETAIL_IMAGES } from "./constants";
import { normalizeProductVideoStrategyKey } from "./productVideoStrategy";
import {
  compactPendingReverseResultForStudioDraft,
  compactReverseOperationForStudioDraft,
  compactStudioDraftValue,
  studioDraftByteSize,
} from "./studioDraft";

const MAX_CLOUD_STUDIO_DRAFT_BYTES = 90 * 1024;

export function sanitizeAssetForDraft(asset) {
  if (!asset) return null;
  const clean = {};
  for (const key of [
    "id", "asset_ref", "origin", "type", "url", "thumb", "preview_url", "original_url", "original_thumb",
    "source_page_url", "source_captured_at", "width", "height", "thumb_width", "thumb_height",
    "duration", "retention_expires_at", "expired", "available", "filename",
    "unlock_cost", "unlocked", "favorite", "retained",
  ]) {
    if (asset[key] !== undefined && asset[key] !== null) clean[key] = asset[key];
  }
  return clean;
}

export function sanitizeWorkspaceForDraft(current) {
  if (!current) return null;
  return {
    prompt: current.prompt || "",
    negative: current.negative || "",
    imageEditProductMode: !!current.imageEditProductMode,
    editSubjectMode: current.editSubjectMode || "general",
    ratio: current.ratio || "1:1",
    imageQuality: current.imageQuality || "1k",
    n: current.n || 1,
    seed: current.seed || "",
    vDuration: current.vDuration || 5,
    vResolution: current.vResolution || "720p",
    productVideoTemplate: normalizeProductVideoStrategyKey(current.productVideoTemplate) || "prompt_driven",
    editMaskMode: current.editMaskMode || "protect_subject",
    productPixelLockMode: current.productPixelLockMode || "auto",
    videoAnalysisPreset: current.videoAnalysisPreset || "standard",
    url: current.url || "",
    appliedUrl: current.appliedUrl || "",
    assets: (current.assets || []).map(sanitizeAssetForDraft).filter(Boolean).slice(0, 12),
    selected: sanitizeAssetForDraft(current.selected),
    lastFrameAsset: sanitizeAssetForDraft(current.lastFrameAsset),
    productAsset: sanitizeAssetForDraft(current.productAsset),
    productDetailAssets: (current.productDetailAssets || [])
      .map(sanitizeAssetForDraft)
      .filter(Boolean)
      .slice(0, MAX_PRODUCT_DETAIL_IMAGES),
    productProfile: compactStudioDraftValue(current.productProfile || null),
    productProfileSource: current.productProfileSource || "",
    portraitProfile: compactStudioDraftValue(current.portraitProfile || null),
    portraitProfileSource: current.portraitProfileSource || "",
    variationSource: sanitizeAssetForDraft(current.variationSource),
    structured: compactStudioDraftValue(current.structured || {}),
    structuredBaseline: compactStudioDraftValue(current.structuredBaseline || {}),
    structuredDirty: !!current.structuredDirty,
    structuredSource: current.structuredSource || "",
    reverseVideoAnalysis: compactStudioDraftValue(current.reverseVideoAnalysis || null),
    reverseSources: compactStudioDraftValue(
      Array.isArray(current.reverseSources) ? current.reverseSources.slice(0, 12) : [],
    ),
    reverseConfig: compactStudioDraftValue(current.reverseConfig || null),
    pendingReverseResult: compactPendingReverseResultForStudioDraft(current.pendingReverseResult),
    reverseResultTab: current.reverseResultTab || "draft",
    reverseResultSchemaVersion: current.reverseResultSchemaVersion || "",
    reverseAppliedVersion: current.reverseAppliedVersion || null,
    reverseAppliedRevisionId: current.reverseAppliedRevisionId || null,
    reverseUndoSnapshot: compactStudioDraftValue(current.reverseUndoSnapshot || null),
    // Revisions are immutable server state and are reloaded by operation id.
    reverseResultRevisions: [],
    reverseFeedback: compactStudioDraftValue(current.reverseFeedback || null),
    batchReverseAssets: (current.batchReverseAssets || [])
      .map(sanitizeAssetForDraft)
      .filter(Boolean)
      .slice(0, MAX_REVERSE_BATCH_ITEMS),
    creationRecipeId: current.creationRecipeId || null,
    creationRecipeVersion: current.creationRecipeVersion || null,
    creationRecipeShareSlug: current.creationRecipeShareSlug || "",
    creationRecipeSource: current.creationRecipeSource || "",
    reverseOperation: current.reverseOperation && (
      ["queued", "running", "needs_confirmation"].includes(current.reverseOperation.status)
      || current.pendingReverseResult
    )
      ? compactReverseOperationForStudioDraft(current.reverseOperation)
      : null,
    profileOperation: current.profileOperation && ["queued", "running", "needs_confirmation"].includes(current.profileOperation.status)
      ? compactReverseOperationForStudioDraft(current.profileOperation)
      : null,
    promptSourceSignature: current.promptSourceSignature || "",
    negativeTouched: !!current.negativeTouched,
    promptDirty: !!current.promptDirty,
    parsing: false,
    uploading: false,
    uploadingRole: null,
    reversing: false,
    productProfiling: false,
    subjectProtection: null,
    subjectProtectionLoading: false,
    subjectProtectionSource: "",
  };
}

export function buildStudioSessionDraftFromState(reason, state, savedAt, selections) {
  const savedWorkspaces = Object.fromEntries(
    Object.entries(state?.workspaces || {}).map(([mode, current]) => [mode, sanitizeWorkspaceForDraft(current)]),
  );
  return {
    version: 1,
    reason,
    savedAt,
    creationMode: state?.creationMode || "image",
    showNegative: Boolean(state?.showNegative),
    refOpen: Boolean(state?.refOpen),
    structOpen: state?.structOpen !== false,
    modelSelections: selections || {},
    workspaces: savedWorkspaces,
    activeTaskId: state?.activeTaskId || null,
    ...(String(state?.restoreNotice || "").trim()
      ? { restoreNotice: String(state.restoreNotice).trim() }
      : {}),
  };
}

function compactPendingReverseResultForCloud(value) {
  if (!value || typeof value !== "object") return null;
  const resultKey = value.payload && typeof value.payload === "object"
    ? "payload"
    : value.result && typeof value.result === "object"
      ? "result"
      : null;
  if (!resultKey) return compactStudioDraftValue(value);
  const result = value[resultKey];
  return {
    ...Object.fromEntries(
      Object.entries(value).filter(([key]) => key !== "payload" && key !== "result"),
    ),
    [resultKey]: compactStudioDraftValue({
      final_text: result.final_text || "",
      structured: result.structured || {},
      shots: Array.isArray(result.shots) ? result.shots : [],
      result_schema_version: result.result_schema_version || "",
      applied_result_version: result.applied_result_version ?? null,
    }),
  };
}

export function compactStudioSessionDraftForCloud(draft) {
  if (!draft || typeof draft !== "object" || studioDraftByteSize(draft) <= MAX_CLOUD_STUDIO_DRAFT_BYTES) {
    return draft;
  }
  const activeMode = draft.creationMode || "image";
  const workspaces = Object.fromEntries(
    Object.entries(draft.workspaces || {}).map(([mode, workspace]) => [mode, {
      ...workspace,
      pendingReverseResult: mode === activeMode ? workspace?.pendingReverseResult || null : null,
      reverseVideoAnalysis: mode === activeMode ? workspace?.reverseVideoAnalysis || null : null,
      reverseUndoSnapshot: null,
    }]),
  );
  let compacted = { ...draft, workspaces };
  if (studioDraftByteSize(compacted) <= MAX_CLOUD_STUDIO_DRAFT_BYTES) return compacted;

  const activeWorkspace = workspaces[activeMode];
  if (activeWorkspace?.pendingReverseResult) {
    compacted = {
      ...compacted,
      workspaces: {
        ...workspaces,
        [activeMode]: {
          ...activeWorkspace,
          pendingReverseResult: compactPendingReverseResultForCloud(
            activeWorkspace.pendingReverseResult,
          ),
          reverseVideoAnalysis: null,
        },
      },
    };
  }
  if (studioDraftByteSize(compacted) <= MAX_CLOUD_STUDIO_DRAFT_BYTES) return compacted;

  return {
    ...compacted,
    workspaces: {
      ...compacted.workspaces,
      [activeMode]: {
        ...compacted.workspaces[activeMode],
        pendingReverseResult: null,
        reverseVideoAnalysis: null,
      },
    },
  };
}
