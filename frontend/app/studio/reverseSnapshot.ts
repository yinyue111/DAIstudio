import type {
  Asset,
  ReverseOperationFeedback,
  ReverseReferenceRole,
  ReverseResult,
  ReverseResultRevision,
  ReverseSourceReference,
  ReverseTarget,
} from "../../lib/api";
import type { ReverseUndoSnapshot } from "../../lib/types";
import {
  DEFAULT_REVERSE_CONFIG,
  normalizeReverseConfig,
  type ReverseCategory,
  type ReverseConfig,
} from "./reverseConfig";
import { normalizeRecoveredVisualPrompt } from "./helpers";
import { normalizeProductVideoStrategyKey } from "./productVideoStrategy";

export const REVERSE_SNAPSHOT_VERSION = 3;
export const REVERSE_SNAPSHOT_COMPATIBLE_VERSIONS = new Set([2, 3]);
const CREATION_MODES = new Set(["image", "video", "image_edit", "video_edit"]);
const SUBJECT_MODES = new Set(["general", "product", "portrait"]);
const RESULT_TABS = new Set(["draft", "structure", "storyboard", "evidence", "versions"]);
const UNDO_FIELDS = new Set([
  "prompt", "negative", "structured", "structuredBaseline", "ratio", "vDuration",
  "vResolution", "promptSourceSignature", "structuredSource", "promptDirty",
  "negativeTouched", "structuredDirty", "reverseVideoAnalysis",
]);
const SOURCE_ROLES = new Set<ReverseReferenceRole>([
  "primary", "subject", "product", "style", "composition", "lighting",
  "text_layout", "negative", "motion", "first_frame", "last_frame",
]);

const ASSET_FIELDS = [
  "id", "type", "url", "thumb", "preview_url", "original_url", "original_thumb",
  "source_page_url", "source_captured_at", "width", "height", "thumb_width", "thumb_height",
  "duration", "retention_expires_at", "expired", "available",
] as const;

function cleanAsset(asset: Asset | null | undefined): Asset | null {
  if (!asset || !["image", "video"].includes(String(asset.type || ""))) return null;
  const raw = asset as Asset & {
    expired?: boolean;
    available?: boolean;
    retention_expires_at?: string | null;
  };
  const expiresAt = raw.retention_expires_at ? new Date(raw.retention_expires_at).getTime() : null;
  if (
    raw.expired === true
    || raw.available === false
    || (Number.isFinite(expiresAt) && Number(expiresAt) <= Date.now())
  ) return null;
  const clean: Record<string, unknown> = {};
  for (const key of ASSET_FIELDS) {
    const value = raw[key];
    if (value !== undefined && value !== null && value !== "") clean[key] = value;
  }
  if (!clean.url && !clean.thumb && !clean.preview_url) return null;
  return clean as unknown as Asset;
}

function cleanRecord(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
}

function cleanOptionalRecord(value: unknown) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : null;
}

function cleanJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cleanJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !["__proto__", "prototype", "constructor"].includes(key))
        .map(([key, item]) => [key, cleanJsonValue(item)]),
    );
  }
  return value;
}

function cleanPositiveInteger(value: unknown) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function cleanRecipeShareSlug(value: unknown) {
  const slug = String(value || "").trim();
  return /^[A-Za-z0-9_-]{20,64}$/.test(slug) ? slug : null;
}

function cleanRecipeSource(value: unknown) {
  const source = String(value || "");
  return ["owner", "public", "share"].includes(source) ? source : null;
}

function sourceTypeForUrl(value: unknown) {
  const path = String(value || "").split("?", 1)[0].toLowerCase();
  return path.endsWith(".mp4") || path.endsWith(".webm") || path.endsWith(".mov") || path.endsWith(".m3u8")
    ? "video"
    : "image";
}

function cleanSource(value: unknown): ReverseSourceReference | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const assetUrl = String(raw.asset_url || raw.url || raw.preview_url || "").trim();
  if (!assetUrl) return null;
  const requestedType = String(raw.source_type || raw.type || "");
  const requestedRole = String(raw.role || "primary") as ReverseReferenceRole;
  const label = String(raw.label || "").trim().slice(0, 64);
  const assetId = cleanPositiveInteger(raw.asset_id ?? raw.id);
  return {
    asset_url: assetUrl,
    source_type: requestedType === "video" ? "video" : requestedType === "image" ? "image" : sourceTypeForUrl(assetUrl),
    role: SOURCE_ROLES.has(requestedRole) ? requestedRole : "primary",
    ...(assetId ? { asset_id: assetId } : {}),
    ...(label ? { label } : {}),
  };
}

function derivedSource(asset: Asset | null | undefined, role: ReverseReferenceRole) {
  const cleaned = cleanAsset(asset);
  return cleaned ? cleanSource({ ...cleaned, role }) : null;
}

function cleanSources(
  sources: unknown,
  {
    selected,
    productAsset,
    subjectMode = "general",
  }: { selected?: Asset | null; productAsset?: Asset | null; subjectMode?: string } = {},
) {
  const explicit = Array.isArray(sources) ? sources.map(cleanSource).filter(Boolean) as ReverseSourceReference[] : [];
  const rows = explicit.length ? explicit : [
    derivedSource(selected, "primary"),
    derivedSource(productAsset, subjectMode === "product" ? "product" : "subject"),
  ].filter(Boolean) as ReverseSourceReference[];
  if (!rows.length) return [];
  const primaryIndex = rows.findIndex((source) => source.role === "primary");
  if (primaryIndex < 0) rows[0] = { ...rows[0], role: "primary" };
  return rows.filter((source, index) => source.role !== "primary" || index === Math.max(0, primaryIndex)).slice(0, 12);
}

function cleanReverseResult(value: unknown): ReverseResult | null {
  return cleanOptionalRecord(value) as ReverseResult | null;
}

function cleanUndoSnapshot(value: unknown): ReverseUndoSnapshot | null {
  const raw = cleanOptionalRecord(value);
  if (!raw) return null;
  const mode = String(raw.mode || "replace");
  const changedFields = Array.isArray(raw.changedFields)
    ? raw.changedFields.map(String).filter((field) => UNDO_FIELDS.has(field))
    : [];
  const beforeRaw = cleanOptionalRecord(raw.before);
  if (
    Number(raw.version) === 1
    && ["replace", "append", "structure_only", "parameters_only"].includes(mode)
    && changedFields.length > 0
    && beforeRaw
  ) {
    const before: ReverseUndoSnapshot["before"] = {};
    for (const field of changedFields) {
      const entry = cleanOptionalRecord(beforeRaw[field]);
      if (!entry || typeof entry.present !== "boolean") continue;
      before[field as keyof typeof before] = {
        present: entry.present,
        ...(entry.present && Object.prototype.hasOwnProperty.call(entry, "value")
          ? { value: cleanJsonValue(entry.value) }
          : {}),
      };
    }
    const usableFields = changedFields.filter((field) => Object.prototype.hasOwnProperty.call(before, field));
    if (usableFields.length > 0) {
      return {
        version: 1,
        mode: mode as ReverseUndoSnapshot["mode"],
        changedFields: usableFields as ReverseUndoSnapshot["changedFields"],
        before,
      };
    }
  }

  // Convert one-window legacy full-workspace undo snapshots into the field token.
  const legacyValues: Record<string, unknown> = {
    prompt: String(raw.prompt || ""),
    negative: String(raw.negative || ""),
    structured: cleanRecord(raw.structured),
    structuredBaseline: cleanRecord(raw.structuredBaseline ?? raw.structured_baseline),
    ratio: String(raw.ratio || "1:1"),
    vDuration: Number(raw.vDuration ?? raw.v_duration ?? 5) || 5,
    vResolution: String(raw.vResolution ?? raw.v_resolution ?? "720p"),
    promptSourceSignature: String(raw.promptSourceSignature ?? raw.prompt_source_signature ?? ""),
    structuredSource: String(raw.structuredSource ?? raw.structured_source ?? ""),
    promptDirty: Boolean(raw.promptDirty ?? raw.prompt_dirty),
    negativeTouched: Boolean(raw.negativeTouched ?? raw.negative_touched),
    structuredDirty: Boolean(raw.structuredDirty ?? raw.structured_dirty),
  };
  const legacyBefore = Object.fromEntries(
    Object.entries(legacyValues).map(([field, item]) => [field, { present: true, value: item }]),
  ) as ReverseUndoSnapshot["before"];
  return {
    version: 1,
    mode: "replace",
    changedFields: Object.keys(legacyValues) as ReverseUndoSnapshot["changedFields"],
    before: legacyBefore,
  };
}

function cleanRevisions(value: unknown): ReverseResultRevision[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const raw = item as Record<string, unknown>;
    const id = cleanPositiveInteger(raw.id);
    const operationId = cleanPositiveInteger(raw.operation_id);
    const version = cleanPositiveInteger(raw.version);
    const source = String(raw.source || "");
    const payload = cleanReverseResult(raw.payload);
    if (!id || !operationId || !version || !payload || !["provider_raw", "normalized", "user_edit", "applied", "model_compiled", "generation"].includes(source)) return [];
    return [{
      id,
      operation_id: operationId,
      version,
      source: source as ReverseResultRevision["source"],
      payload,
      parent_revision_id: cleanPositiveInteger(raw.parent_revision_id),
      source_content_hash: raw.source_content_hash ? String(raw.source_content_hash) : null,
      source_fingerprints: Array.isArray(raw.source_fingerprints)
        ? raw.source_fingerprints.filter((entry) => entry && typeof entry === "object" && !Array.isArray(entry)) as Array<Record<string, unknown>>
        : null,
      payload_hash: raw.payload_hash ? String(raw.payload_hash) : null,
      lineage_status: raw.lineage_status === "verified" ? "verified" as const : "legacy_unverified" as const,
      evidence_review_action: ["not_applicable", "inherited", "updated", "cleared"].includes(String(raw.evidence_review_action || ""))
        ? raw.evidence_review_action as ReverseResultRevision["evidence_review_action"]
        : null,
      created_at: raw.created_at ? String(raw.created_at) : null,
    }];
  }).slice(-12);
}

function cleanFeedback(value: unknown): ReverseOperationFeedback | null {
  const raw = cleanOptionalRecord(value);
  if (!raw || !["useful", "not_useful"].includes(String(raw.rating || ""))) return null;
  return {
    operation_id: cleanPositiveInteger(raw.operation_id) || 0,
    rating: String(raw.rating) as ReverseOperationFeedback["rating"],
    issue_types: Array.isArray(raw.issue_types)
      ? raw.issue_types.map(String) as ReverseOperationFeedback["issue_types"]
      : [],
    note: raw.note == null ? null : String(raw.note),
    created_at: raw.created_at ? String(raw.created_at) : null,
    updated_at: raw.updated_at ? String(raw.updated_at) : null,
  };
}

function normalizeSubjectProfile(value: unknown, fallbackFinalText: unknown) {
  const profile = cleanOptionalRecord(value);
  if (!profile) return null;
  const wrappedStructured = cleanOptionalRecord(profile.structured);
  if (wrappedStructured) {
    return {
      ...profile,
      structured: wrappedStructured,
      final_text: String(profile.final_text || fallbackFinalText || ""),
    };
  }
  const { final_text: profileFinalText, ...structured } = profile;
  return {
    structured,
    final_text: String(profileFinalText || fallbackFinalText || ""),
  };
}

function categoryForTarget(target: ReverseTarget): ReverseCategory {
  return target === "video" ? "video" : "image";
}

function configSnapshot(config: ReverseConfig) {
  return {
    analysis_focus: config.analysis_focus,
    analysis_precision: config.analysis_precision,
    output_purpose: config.output_purpose,
    custom_instruction: config.custom_instruction,
    source_range: config.source_range,
    source_ranges: config.source_ranges,
    custom_keyframes: config.custom_keyframes,
    include_audio: config.include_audio,
  };
}

type ReverseSnapshotArgs = {
  creationMode: string;
  subjectMode: string;
  target: ReverseTarget;
  selected?: Asset | null;
  productAsset?: Asset | null;
  assets?: Asset[];
  sources?: ReverseSourceReference[];
  structured?: Record<string, unknown>;
  finalText?: string;
  promptDirty?: boolean;
  videoAnalysisPreset?: string;
  videoAnalysis?: Record<string, unknown> | null;
  subjectProfile?: Record<string, unknown> | null;
  reverseConfig?: Partial<ReverseConfig> | null;
  pendingResult?: ReverseResult | null;
  resultTab?: string;
  resultSchemaVersion?: string;
  appliedVersion?: number | null;
  appliedRevisionId?: number | null;
  undoSnapshot?: ReverseUndoSnapshot | null;
  resultRevisions?: ReverseResultRevision[];
  feedback?: ReverseOperationFeedback | null;
  creationRecipeId?: number | null;
  creationRecipeVersion?: number | null;
  creationRecipeShareSlug?: string | null;
  creationRecipeSource?: string | null;
};

export function buildReverseSnapshotV3({
  creationMode,
  subjectMode,
  target,
  selected,
  productAsset,
  assets = [],
  sources = [],
  structured,
  finalText,
  promptDirty = false,
  videoAnalysisPreset,
  videoAnalysis,
  subjectProfile,
  reverseConfig,
  pendingResult,
  resultTab = "draft",
  resultSchemaVersion = "",
  appliedVersion = null,
  appliedRevisionId = null,
  undoSnapshot = null,
  resultRevisions = [],
  feedback = null,
  creationRecipeId = null,
  creationRecipeVersion = null,
  creationRecipeShareSlug = null,
  creationRecipeSource = null,
}: ReverseSnapshotArgs) {
  const category = categoryForTarget(target);
  const config = normalizeReverseConfig({
    ...DEFAULT_REVERSE_CONFIG,
    ...reverseConfig,
    analysis_precision: reverseConfig?.analysis_precision || videoAnalysisPreset || "standard",
  }, { category, selectedType: selected?.type || null });
  return {
    version: REVERSE_SNAPSHOT_VERSION,
    creation_mode: creationMode,
    subject_mode: subjectMode,
    target,
    selected: cleanAsset(selected),
    product_asset: cleanAsset(productAsset),
    assets: assets.map(cleanAsset).filter(Boolean).slice(0, 12),
    sources: cleanSources(sources, { selected, productAsset, subjectMode }),
    structured: cleanRecord(structured),
    final_text: String(finalText || ""),
    prompt_dirty: Boolean(promptDirty),
    video_analysis_preset: config.analysis_precision,
    ...configSnapshot(config),
    video_analysis: cleanOptionalRecord(videoAnalysis),
    reverse_result: cleanReverseResult(pendingResult),
    reverse_result_tab: RESULT_TABS.has(resultTab) ? resultTab : "draft",
    result_schema_version: String(resultSchemaVersion || ""),
    reverse_applied_version: cleanPositiveInteger(appliedVersion),
    reverse_applied_revision_id: cleanPositiveInteger(appliedRevisionId),
    undo_snapshot: cleanUndoSnapshot(undoSnapshot),
    result_revisions: cleanRevisions(resultRevisions),
    feedback: cleanFeedback(feedback),
    creation_recipe_id: cleanPositiveInteger(creationRecipeId),
    creation_recipe_version: cleanPositiveInteger(creationRecipeVersion),
    creation_recipe_share_slug: cleanRecipeShareSlug(creationRecipeShareSlug),
    creation_recipe_source: cleanRecipeSource(creationRecipeSource),
    subject_profile: cleanOptionalRecord(subjectProfile),
  };
}

export function buildReverseOperationRequestSnapshotV3({
  creationMode,
  subjectMode,
  target,
  selected,
  productAsset,
  assets = [],
  sources = [],
  videoAnalysisPreset,
  subjectProfile,
  reverseConfig,
}: ReverseSnapshotArgs) {
  const category = categoryForTarget(target);
  const config = normalizeReverseConfig({
    ...DEFAULT_REVERSE_CONFIG,
    ...reverseConfig,
    analysis_precision: reverseConfig?.analysis_precision || videoAnalysisPreset || "standard",
  }, { category, selectedType: selected?.type || null });
  return {
    version: REVERSE_SNAPSHOT_VERSION,
    creation_mode: creationMode,
    subject_mode: subjectMode,
    target,
    selected: cleanAsset(selected),
    product_asset: cleanAsset(productAsset),
    assets: assets.map(cleanAsset).filter(Boolean).slice(0, 12),
    sources: cleanSources(sources, { selected, productAsset, subjectMode }),
    video_analysis_preset: config.analysis_precision,
    ...configSnapshot(config),
    subject_profile: cleanOptionalRecord(subjectProfile),
  };
}

// Compatibility exports keep existing callers source-compatible while all new writes use V3.
export const buildReverseSnapshotV2 = buildReverseSnapshotV3;
export const buildReverseOperationRequestSnapshotV2 = buildReverseOperationRequestSnapshotV3;

export function reverseSnapshotFromHistory(item: unknown) {
  if (!item || typeof item !== "object") return null;
  const row = item as Record<string, unknown>;
  const params = cleanRecord(row.params);
  const snapshot = params.reverse_snapshot_v3
    || row.reverse_snapshot_v3
    || params.reverse_snapshot_v2
    || row.reverse_snapshot_v2;
  const snapshotVersion = snapshot && typeof snapshot === "object"
    ? Number(
        (snapshot as { version?: number }).version
        || (snapshot as { workspace_snapshot_v3?: { version?: number } }).workspace_snapshot_v3?.version
        || (snapshot as { workspace_snapshot_v2?: { version?: number } }).workspace_snapshot_v2?.version,
      )
    : 0;
  if (!snapshot || typeof snapshot !== "object" || !REVERSE_SNAPSHOT_COMPATIBLE_VERSIONS.has(snapshotVersion)) {
    return null;
  }
  return snapshot as Record<string, unknown>;
}

export function workspacePatchFromReverseSnapshot(snapshot: unknown) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const envelope = snapshot as Record<string, any>;
  const nestedV3 = envelope.workspace_snapshot_v3 && typeof envelope.workspace_snapshot_v3 === "object"
    ? envelope.workspace_snapshot_v3 as Record<string, any>
    : null;
  const nestedV2 = envelope.workspace_snapshot_v2 && typeof envelope.workspace_snapshot_v2 === "object"
    ? envelope.workspace_snapshot_v2 as Record<string, any>
    : null;
  const nested = nestedV3 || nestedV2 || {};
  const raw: Record<string, any> = {
    ...nested,
    ...envelope,
    structured: envelope.structured ?? nested.structured,
    final_text: envelope.final_text ?? nested.final_text,
    video_analysis: envelope.video_analysis ?? nested.video_analysis,
    reverse_result: envelope.reverse_result ?? nested.reverse_result,
  };
  const version = Number(raw.version || nested.version);
  if (!REVERSE_SNAPSHOT_COMPATIBLE_VERSIONS.has(version)) return null;
  const requestedCreationMode = String(raw.creation_mode || "");
  const creationMode = CREATION_MODES.has(requestedCreationMode)
    ? requestedCreationMode
    : String(raw.target || "") === "video" ? "video" : "image";
  const selected = cleanAsset(raw.selected);
  const productAsset = cleanAsset(raw.product_asset);
  const structured = cleanRecord(raw.structured) as Record<string, string>;
  const requestedSubjectMode = String(raw.subject_mode || "");
  const inferredSubjectMode = String(raw.target || "") === "portrait_profile"
    ? "portrait"
    : String(raw.target || "") === "product_profile" ? "product" : "general";
  const subjectMode = SUBJECT_MODES.has(requestedSubjectMode)
    ? requestedSubjectMode
    : requestedSubjectMode ? "general" : inferredSubjectMode;
  const profilePayload = raw.subject_profile
    ?? (subjectMode === "portrait" ? raw.portrait_profile : raw.product_profile);
  const subjectProfile = normalizeSubjectProfile(profilePayload, raw.final_text);
  const profileSource = productAsset
    ? [productAsset.type, productAsset.url || "", productAsset.thumb || ""].join("|")
    : "";
  const category: ReverseCategory = String(raw.target || "") === "video" ? "video" : "image";
  const reverseTarget = String(raw.target || category);
  const storedPrompt = String(raw.final_text || "");
  const promptDirty = raw.prompt_dirty === true;
  const restoredPrompt = ["image", "product_profile", "portrait_profile"].includes(reverseTarget)
    ? normalizeRecoveredVisualPrompt(structured, storedPrompt, {
        target: reverseTarget,
        preserveFallback: promptDirty,
      })
    : storedPrompt;
  const reverseConfig = normalizeReverseConfig({
    ...(cleanOptionalRecord(raw.reverse_config) || {}),
    analysis_focus: raw.analysis_focus,
    analysis_precision: raw.analysis_precision || raw.video_analysis_preset,
    output_purpose: raw.output_purpose,
    custom_instruction: raw.custom_instruction,
    source_range: raw.source_range,
    source_ranges: raw.source_ranges,
    custom_keyframes: raw.custom_keyframes,
    include_audio: raw.include_audio,
  }, { category, selectedType: selected?.type || null });
  const generation = cleanRecord(raw.generation);
  const resultRevisions = cleanRevisions(raw.result_revisions);
  const reverseAppliedVersion = cleanPositiveInteger(
    raw.reverse_applied_version ?? raw.applied_result_version,
  );
  const reverseAppliedRevisionId = cleanPositiveInteger(raw.reverse_applied_revision_id)
    || resultRevisions.find((revision) => (
      revision.version === reverseAppliedVersion
      && ["normalized", "user_edit", "applied"].includes(revision.source)
    ))?.id
    || null;
  return {
    snapshotVersion: version,
    creationMode,
    subjectMode,
    expiredAssetsSkipped: Boolean(
      (raw.selected && !selected)
      || (raw.product_asset && !productAsset)
      || (Array.isArray(raw.assets) && raw.assets.some((asset) => asset && !cleanAsset(asset)))
    ),
    workspace: {
      prompt: restoredPrompt,
      negative: String(generation.negative ?? raw.negative ?? ""),
      promptDirty,
      selected,
      productAsset,
      assets: Array.isArray(raw.assets) ? raw.assets.map(cleanAsset).filter(Boolean).slice(0, 12) : [],
      structured,
      structuredBaseline: structured,
      structuredDirty: false,
      reverseVideoAnalysis: cleanOptionalRecord(raw.video_analysis),
      videoAnalysisPreset: reverseConfig.analysis_precision,
      reverseSources: cleanSources(raw.sources, { selected, productAsset, subjectMode }),
      reverseConfig,
      pendingReverseResult: cleanReverseResult(raw.reverse_result),
      reverseResultTab: RESULT_TABS.has(String(raw.reverse_result_tab || ""))
        ? String(raw.reverse_result_tab)
        : "draft",
      reverseResultSchemaVersion: String(raw.result_schema_version || ""),
      reverseAppliedVersion,
      reverseAppliedRevisionId,
      reverseUndoSnapshot: cleanUndoSnapshot(raw.undo_snapshot),
      reverseResultRevisions: resultRevisions,
      reverseFeedback: cleanFeedback(raw.feedback),
      creationRecipeId: cleanPositiveInteger(raw.creation_recipe_id),
      creationRecipeVersion: cleanPositiveInteger(raw.creation_recipe_version),
      creationRecipeShareSlug: cleanRecipeShareSlug(raw.creation_recipe_share_slug) || "",
      creationRecipeSource: cleanRecipeSource(raw.creation_recipe_source) || "",
      ratio: String(generation.ratio || raw.ratio || (category === "video" ? "16:9" : "1:1")),
      imageQuality: String(generation.image_quality || raw.image_quality || "1k"),
      n: cleanPositiveInteger(generation.count ?? generation.n ?? raw.n) || 1,
      seed: generation.seed == null ? String(raw.seed || "") : String(generation.seed),
      vDuration: cleanPositiveInteger(generation.duration ?? raw.v_duration) || 5,
      vResolution: String(generation.resolution || raw.v_resolution || "720p"),
      productVideoTemplate: normalizeProductVideoStrategyKey(
        generation.product_video_template
        ?? generation.productVideoTemplate
        ?? raw.product_video_template
        ?? raw.productVideoTemplate,
      ) || "prompt_driven",
      productProfile: subjectMode === "portrait" ? null : subjectProfile,
      productProfileSource: subjectMode === "portrait" ? "" : profileSource,
      portraitProfile: subjectMode === "portrait" ? subjectProfile : null,
      portraitProfileSource: subjectMode === "portrait" ? profileSource : "",
    },
  };
}
