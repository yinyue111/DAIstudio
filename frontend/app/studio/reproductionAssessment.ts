export type ReproductionAssessmentStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "canceled";

export type ReproductionAnalyzerStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "unsupported"
  | "degraded"
  | "failed";

export type ReproductionBBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ReproductionFinding = {
  id: number;
  dimension: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  confidence: number | null;
  title: string;
  detail: string | null;
  bbox: ReproductionBBox | null;
  start_seconds: number | null;
  end_seconds: number | null;
  shot_index: number | null;
  shot_id: string | null;
  evidence: Record<string, unknown> | null;
};

export type ReproductionDimension = {
  key: string;
  label: string;
  status: ReproductionAnalyzerStatus;
  score: number | null;
  analyzer: string | null;
  analyzer_version: string | null;
  degraded_reason: string | null;
};

export type ReproductionAssessment = {
  id: number;
  media_type: "image" | "video";
  status: ReproductionAssessmentStatus;
  progress: number;
  source_asset_ref: string;
  generated_asset_ref: string;
  reverse_operation_id: number | null;
  reverse_revision_id: number | null;
  generation_task_id: number | null;
  phase: string | null;
  dimensions: ReproductionDimension[];
  findings: ReproductionFinding[];
  warnings: string[];
  error: string | null;
  correction_revision_id: number | null;
  created_at: string | null;
  updated_at: string | null;
};

const ASSESSMENT_STATUSES = new Set<ReproductionAssessmentStatus>([
  "queued",
  "running",
  "succeeded",
  "partial",
  "failed",
  "canceled",
]);
const ANALYZER_STATUSES = new Set<ReproductionAnalyzerStatus>([
  "queued",
  "running",
  "succeeded",
  "unsupported",
  "degraded",
  "failed",
]);
const SEVERITIES = new Set<ReproductionFinding["severity"]>([
  "info",
  "low",
  "medium",
  "high",
  "critical",
]);

const DIMENSION_LABELS: Record<string, string> = {
  structure_layout: "构图结构",
  color_light: "色彩光线",
  detail_material: "细节材质",
  subject_semantics: "主体一致性",
  ocr_text: "文字与 Logo",
  frame_structure: "镜头画面",
  frame_color_light: "视频色光",
  frame_detail_material: "视频细节",
  duration_pacing: "时长节奏",
  motion: "画面变化",
  camera_motion: "运镜一致性",
  action_semantics: "动作一致性",
  transition_semantics: "转场一致性",
  audio: "音频一致性",
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function finite(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function positiveInteger(value: unknown): number | null {
  const parsed = finite(value);
  return parsed !== null && Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function clamped(value: unknown, min: number, max: number): number | null {
  const parsed = finite(value);
  if (parsed === null) return null;
  return Math.max(min, Math.min(max, parsed));
}

function percentageScore(value: unknown): number | null {
  const parsed = finite(value);
  if (parsed === null || parsed < 0) return null;
  return Math.max(0, Math.min(100, parsed <= 1 ? parsed * 100 : parsed));
}

function normalizeBBox(value: unknown): ReproductionBBox | null {
  const raw = record(value);
  const x = finite(raw.x);
  const y = finite(raw.y);
  const width = finite(raw.width);
  const height = finite(raw.height);
  if (x === null || y === null || width === null || height === null) return null;
  if (x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > 1.000001 || y + height > 1.000001) {
    return null;
  }
  return { x, y, width, height };
}

function analyzerStatus(value: unknown): ReproductionAnalyzerStatus {
  const candidate = String(value || "").toLowerCase();
  if (ANALYZER_STATUSES.has(candidate as ReproductionAnalyzerStatus)) {
    return candidate as ReproductionAnalyzerStatus;
  }
  if (candidate === "ready" || candidate === "analyzed") return "succeeded";
  return "unsupported";
}

export function normalizeReproductionDimensions(value: unknown): ReproductionDimension[] {
  const rows = Array.isArray(value)
    ? value
    : Object.entries(record(value)).map(([key, dimension]) => ({ key, ...record(dimension) }));
  const seen = new Set<string>();
  return rows.flatMap((item) => {
    const raw = record(item);
    const key = String(raw.key || raw.dimension || "").trim();
    if (!key || seen.has(key)) return [];
    seen.add(key);
    const status = analyzerStatus(raw.status || raw.analyzer_status);
    const score = status === "succeeded" ? percentageScore(raw.score) : null;
    return [{
      key,
      label: String(raw.label || raw.name || DIMENSION_LABELS[key] || key).trim(),
      status,
      score,
      analyzer: String(raw.analyzer || raw.analyzer_name || "").trim() || null,
      analyzer_version: String(raw.analyzer_version || raw.version || "").trim() || null,
      degraded_reason: String(raw.degraded_reason || raw.reason || "").trim() || null,
    }];
  });
}

export function normalizeReproductionFindings(value: unknown): ReproductionFinding[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.flatMap((item) => {
    const raw = record(item);
    const id = positiveInteger(raw.id || raw.finding_id);
    const dimension = String(raw.dimension || raw.dimension_key || "").trim();
    const title = String(raw.title || raw.summary || raw.message || "").trim();
    if (!id || seen.has(String(id)) || !dimension || !title) return [];
    seen.add(String(id));
    const severityCandidate = String(raw.severity || "medium").toLowerCase() as ReproductionFinding["severity"];
    const timeRange = record(raw.time_range);
    const start = finite(raw.start_seconds ?? timeRange.start_seconds);
    const end = finite(raw.end_seconds ?? timeRange.end_seconds);
    const validRange = start !== null && end !== null && start >= 0 && end > start;
    return [{
      id,
      dimension,
      severity: SEVERITIES.has(severityCandidate) ? severityCandidate : "medium",
      confidence: clamped(raw.confidence, 0, 1),
      title,
      detail: String(raw.detail || raw.description || "").trim() || null,
      bbox: normalizeBBox(raw.bbox),
      start_seconds: validRange ? start : null,
      end_seconds: validRange ? end : null,
      shot_index: positiveInteger(raw.shot_index),
      shot_id: String(raw.shot_id || "").trim() || null,
      evidence: Object.keys(record(raw.evidence)).length ? record(raw.evidence) : null,
    }];
  });
}

export function normalizeReproductionAssessment(value: unknown): ReproductionAssessment {
  const wrapped = record(value);
  const raw = Object.keys(record(wrapped.assessment)).length ? record(wrapped.assessment) : wrapped;
  const id = positiveInteger(raw.id);
  const lineage = record(raw.lineage);
  const generationTaskId = positiveInteger(
    raw.generation_task_id || raw.gen_task_id || lineage.generation_task_id,
  );
  const mediaType = String(raw.media_type || raw.type || "");
  const sourceAssetRef = String(raw.source_asset_ref || "").trim();
  const generatedAssetRef = String(raw.generated_asset_ref || "").trim();
  if (!id || !["image", "video"].includes(mediaType) || !sourceAssetRef || !generatedAssetRef) {
    throw new Error("服务端未返回有效的复刻度评估");
  }
  const statusCandidate = String(raw.status || "queued").toLowerCase() as ReproductionAssessmentStatus;
  const dimensions = normalizeReproductionDimensions(raw.dimensions || record(raw.metrics).dimensions || raw.metrics);
  const findings = normalizeReproductionFindings(raw.findings);
  return {
    id,
    media_type: mediaType as ReproductionAssessment["media_type"],
    status: ASSESSMENT_STATUSES.has(statusCandidate) ? statusCandidate : "failed",
    progress: clamped(raw.progress, 0, 100) ?? 0,
    source_asset_ref: sourceAssetRef,
    generated_asset_ref: generatedAssetRef,
    reverse_operation_id: positiveInteger(raw.reverse_operation_id || lineage.reverse_operation_id),
    reverse_revision_id: positiveInteger(
      raw.reverse_revision_id || raw.source_revision_id || lineage.reverse_revision_id,
    ),
    generation_task_id: generationTaskId,
    phase: String(raw.phase || "").trim() || null,
    dimensions,
    findings,
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String).filter(Boolean) : [],
    error: String(raw.error || raw.error_message || "").trim() || null,
    correction_revision_id: positiveInteger(raw.correction_revision_id),
    created_at: String(raw.created_at || "").trim() || null,
    updated_at: String(raw.updated_at || "").trim() || null,
  };
}

export function isReproductionAssessmentTerminal(value: ReproductionAssessment | null | undefined): boolean {
  return Boolean(value && ["succeeded", "partial", "failed", "canceled"].includes(value.status));
}

export function selectableReproductionFindingIds(value: ReproductionAssessment | null | undefined): number[] {
  if (!value || !["succeeded", "partial"].includes(value.status)) return [];
  return value.findings
    .filter((finding) => finding.severity !== "info")
    .map((finding) => finding.id);
}

export function reproductionFindingTimelineStyle(
  finding: ReproductionFinding,
  durationSeconds: number,
): { left: string; width: string } | null {
  if (
    !Number.isFinite(durationSeconds)
    || durationSeconds <= 0
    || finding.start_seconds === null
    || finding.end_seconds === null
  ) return null;
  const start = Math.max(0, Math.min(durationSeconds, finding.start_seconds));
  const end = Math.max(start, Math.min(durationSeconds, finding.end_seconds));
  if (end <= start) return null;
  return {
    left: `${(start / durationSeconds) * 100}%`,
    width: `${Math.max(0.75, ((end - start) / durationSeconds) * 100)}%`,
  };
}

export function reproductionCorrectionPayload({
  idempotencyKey,
  parentRevisionId,
  findingIds,
  promptPatch = "",
  negativePatch = "",
  structuredPatch = {},
}: {
  idempotencyKey: string;
  parentRevisionId: number;
  findingIds: number[];
  promptPatch?: string;
  negativePatch?: string;
  structuredPatch?: Record<string, unknown>;
}) {
  const selected = [...new Set(findingIds.map(positiveInteger).filter((item): item is number => item !== null))];
  if (!selected.length) throw new Error("请至少选择一个需要修正的问题");
  const idempotency = String(idempotencyKey || "").trim();
  const parentRevision = positiveInteger(parentRevisionId);
  if (!idempotency || !parentRevision) throw new Error("修正版本缺少有效血缘");
  return {
    idempotency_key: idempotency,
    parent_revision_id: parentRevision,
    selected_finding_ids: selected,
    prompt_patch: promptPatch.trim() || null,
    negative_prompt_patch: negativePatch.trim() || null,
    structured_patch: record(structuredPatch),
    mask_patch: null,
    apply: false,
  };
}

export function reproductionSourceAsset(
  taskValue: unknown,
  fallbackValue: unknown = null,
): Record<string, unknown> | null {
  const task = record(taskValue);
  const trace = record(record(task.params)._source_trace);
  const assetRef = String(trace.asset_ref || "").trim();
  if (!/^(g\.[1-9]\d*|u\.[A-Za-z0-9_-]+)$/.test(assetRef)) return null;

  const fallback = record(fallbackValue);
  if (String(fallback.asset_ref || "").trim() === assetRef) return fallback;

  const type = String(trace.source_type || trace.selected_type || "").trim();
  const url = String(trace.selected_url || trace.original_url || trace.source_asset_url || "").trim();
  if (!url || !["image", "video"].includes(type)) return null;
  return {
    asset_ref: assetRef,
    origin: assetRef.startsWith("g.") ? "generated" : "uploaded",
    type,
    url,
    thumb: String(trace.selected_thumb || trace.original_thumb || "").trim() || null,
    original_url: String(trace.original_url || "").trim() || null,
    original_thumb: String(trace.original_thumb || "").trim() || null,
  };
}

export function reproductionIdempotencyKey(prefix: string, ...parts: unknown[]): string {
  const normalizedPrefix = String(prefix || "reproduction").replace(/[^a-z0-9_-]/gi, "-").slice(0, 32);
  const input = parts.map((part) => String(part ?? "")).join("\u001f");
  let hash = 2166136261;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${normalizedPrefix || "reproduction"}-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}
