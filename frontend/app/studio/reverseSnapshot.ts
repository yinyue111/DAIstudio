import type { Asset, ReverseTarget } from "../../lib/api";

export const REVERSE_SNAPSHOT_VERSION = 2;
const CREATION_MODES = new Set(["image", "video", "image_edit", "video_edit"]);
const SUBJECT_MODES = new Set(["general", "product", "portrait"]);

const ASSET_FIELDS = [
  "id", "type", "url", "thumb", "preview_url", "original_url", "original_thumb",
  "source_page_url", "source_captured_at", "width", "height", "thumb_width", "thumb_height",
  "retention_expires_at", "expired", "available",
] as const;

function cleanAsset(asset: Asset | null | undefined) {
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
  return clean;
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

export function buildReverseSnapshotV2({
  creationMode,
  subjectMode,
  target,
  selected,
  productAsset,
  assets = [],
  structured,
  finalText,
  videoAnalysisPreset,
  videoAnalysis,
  subjectProfile,
}: {
  creationMode: string;
  subjectMode: string;
  target: ReverseTarget;
  selected?: Asset | null;
  productAsset?: Asset | null;
  assets?: Asset[];
  structured?: Record<string, unknown>;
  finalText?: string;
  videoAnalysisPreset?: string;
  videoAnalysis?: Record<string, unknown> | null;
  subjectProfile?: Record<string, unknown> | null;
}) {
  return {
    version: REVERSE_SNAPSHOT_VERSION,
    creation_mode: creationMode,
    subject_mode: subjectMode,
    target,
    selected: cleanAsset(selected),
    product_asset: cleanAsset(productAsset),
    assets: assets.map(cleanAsset).filter(Boolean).slice(0, 12),
    structured: cleanRecord(structured),
    final_text: String(finalText || ""),
    video_analysis_preset: String(videoAnalysisPreset || "standard"),
    video_analysis: cleanOptionalRecord(videoAnalysis),
    subject_profile: cleanOptionalRecord(subjectProfile),
  };
}

export function buildReverseOperationRequestSnapshotV2({
  creationMode,
  subjectMode,
  target,
  selected,
  productAsset,
  assets = [],
  videoAnalysisPreset,
  subjectProfile,
}: {
  creationMode: string;
  subjectMode: string;
  target: ReverseTarget;
  selected?: Asset | null;
  productAsset?: Asset | null;
  assets?: Asset[];
  videoAnalysisPreset?: string;
  subjectProfile?: Record<string, unknown> | null;
}) {
  return {
    version: REVERSE_SNAPSHOT_VERSION,
    creation_mode: creationMode,
    subject_mode: subjectMode,
    target,
    selected: cleanAsset(selected),
    product_asset: cleanAsset(productAsset),
    assets: assets.map(cleanAsset).filter(Boolean).slice(0, 12),
    video_analysis_preset: String(videoAnalysisPreset || "standard"),
    subject_profile: cleanOptionalRecord(subjectProfile),
  };
}

export function reverseSnapshotFromHistory(item: unknown) {
  if (!item || typeof item !== "object") return null;
  const row = item as Record<string, unknown>;
  const params = cleanRecord(row.params);
  const snapshot = params.reverse_snapshot_v2 || row.reverse_snapshot_v2;
  const snapshotVersion = snapshot && typeof snapshot === "object"
    ? Number(
        (snapshot as { version?: number }).version
        || (snapshot as { workspace_snapshot_v2?: { version?: number } }).workspace_snapshot_v2?.version,
      )
    : 0;
  if (!snapshot || typeof snapshot !== "object" || snapshotVersion !== 2) {
    return null;
  }
  return snapshot as Record<string, unknown>;
}

export function workspacePatchFromReverseSnapshot(snapshot: unknown) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const envelope = snapshot as Record<string, any>;
  const nested = envelope.workspace_snapshot_v2 && typeof envelope.workspace_snapshot_v2 === "object"
    ? envelope.workspace_snapshot_v2 as Record<string, any>
    : {};
  const raw: Record<string, any> = {
    ...nested,
    ...envelope,
    structured: envelope.structured ?? nested.structured,
    final_text: envelope.final_text ?? nested.final_text,
    video_analysis: envelope.video_analysis ?? nested.video_analysis,
  };
  if (Number(raw.version || nested.version) !== REVERSE_SNAPSHOT_VERSION) return null;
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
  return {
    creationMode,
    subjectMode,
    expiredAssetsSkipped: Boolean(
      (raw.selected && !selected)
      || (raw.product_asset && !productAsset)
      || (Array.isArray(raw.assets) && raw.assets.some((asset) => asset && !cleanAsset(asset)))
    ),
    workspace: {
      prompt: String(raw.final_text || ""),
      promptDirty: false,
      selected,
      productAsset,
      assets: Array.isArray(raw.assets) ? raw.assets.map(cleanAsset).filter(Boolean).slice(0, 12) : [],
      structured,
      structuredBaseline: structured,
      structuredDirty: false,
      reverseVideoAnalysis: cleanOptionalRecord(raw.video_analysis),
      videoAnalysisPreset: String(raw.video_analysis_preset || "standard"),
      productProfile: subjectMode === "portrait" ? null : subjectProfile,
      productProfileSource: subjectMode === "portrait" ? "" : profileSource,
      portraitProfile: subjectMode === "portrait" ? subjectProfile : null,
      portraitProfileSource: subjectMode === "portrait" ? profileSource : "",
    },
  };
}
