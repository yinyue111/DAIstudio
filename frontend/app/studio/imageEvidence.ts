export type ImageEvidenceBBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ImageEvidencePoint = {
  x: number;
  y: number;
};

export type ImageEvidenceReviewStatus = "pending" | "confirmed" | "rejected";
export type ImageEvidenceAnalysisStatus = "ready" | "pending" | "unsupported" | "degraded";

export type ImageEvidenceItem = {
  evidence_id: string;
  evidence_type: "visual_field" | "ocr" | "logo" | "packaging" | "subject_protection";
  bbox: ImageEvidenceBBox | null;
  polygon?: ImageEvidencePoint[];
  field_key: string;
  evidence_text: string;
  confidence: number;
  source_index: number;
  fact_status: "visible" | "inferred" | "unknown";
  protected: boolean;
  editable: boolean;
  review_status: ImageEvidenceReviewStatus;
  analyzer: string | null;
  analyzer_source: string | null;
  analyzer_status: string | null;
  analyzer_version: string | null;
  evidence_source: string | null;
  analysis_status: ImageEvidenceAnalysisStatus;
  degraded_reason: string | null;
  conflict_group: string | null;
  conflict_status: "none" | "conflict";
  conflicts_with: string[];
};

const EVIDENCE_TYPES = new Set([
  "visual_field",
  "ocr",
  "logo",
  "packaging",
  "subject_protection",
]);
const FACT_STATUSES = new Set(["visible", "inferred", "unknown"]);
const REVIEW_STATUSES = new Set(["pending", "confirmed", "rejected"]);
const ANALYSIS_STATUSES = new Set(["ready", "pending", "unsupported", "degraded"]);
const MAX_POLYGON_POINTS = 128;

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function roundCoordinate(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

export function normalizeImageEvidenceBBox(value: unknown): ImageEvidenceBBox | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  const x = finiteNumber(source.x);
  const y = finiteNumber(source.y);
  const width = finiteNumber(source.width);
  const height = finiteNumber(source.height);
  if (x == null || y == null || width == null || height == null) return null;
  if (x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > 1.000001 || y + height > 1.000001) {
    return null;
  }
  return {
    x: roundCoordinate(x),
    y: roundCoordinate(y),
    width: roundCoordinate(width),
    height: roundCoordinate(height),
  };
}

function polygonArea(points: ImageEvidencePoint[]): number {
  return Math.abs(points.reduce((sum, point, index) => {
    const next = points[(index + 1) % points.length];
    return sum + point.x * next.y - next.x * point.y;
  }, 0) / 2);
}

export function normalizeImageEvidencePolygon(value: unknown): ImageEvidencePoint[] | null {
  if (!Array.isArray(value) || value.length < 3 || value.length > MAX_POLYGON_POINTS) return null;
  const points: ImageEvidencePoint[] = [];
  for (const point of value) {
    if (!point || typeof point !== "object" || Array.isArray(point)) return null;
    const source = point as Record<string, unknown>;
    const x = finiteNumber(source.x);
    const y = finiteNumber(source.y);
    if (x == null || y == null || x < 0 || x > 1 || y < 0 || y > 1) return null;
    points.push({ x: roundCoordinate(x), y: roundCoordinate(y) });
  }
  return polygonArea(points) > 0.00000001 ? points : null;
}

function safeEvidenceId(value: unknown, fallback: string): string {
  const requested = String(value || "").trim();
  return /^[A-Za-z0-9._:-]{1,128}$/.test(requested) ? requested : fallback;
}

export function normalizeImageEvidence(value: unknown): ImageEvidenceItem[] {
  if (!Array.isArray(value)) return [];
  const usedIds = new Set<string>();
  return value.flatMap((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const source = item as Record<string, unknown>;
    const evidenceType = String(source.evidence_type || "");
    const factStatus = String(source.fact_status || "");
    const fieldKey = String(source.field_key || "").trim();
    const evidenceText = String(source.evidence_text || "").trim();
    const confidence = finiteNumber(source.confidence);
    const sourceIndex = finiteNumber(source.source_index);
    if (
      !EVIDENCE_TYPES.has(evidenceType)
      || !FACT_STATUSES.has(factStatus)
      || !fieldKey
      || fieldKey.length > 64
      || !evidenceText
      || evidenceText.length > 2000
      || confidence == null
      || confidence < 0
      || confidence > 1
      || sourceIndex == null
      || !Number.isInteger(sourceIndex)
      || sourceIndex < 1
      || typeof source.protected !== "boolean"
      || typeof source.editable !== "boolean"
    ) return [];
    if (source.protected && source.editable) return [];
    const bbox = normalizeImageEvidenceBBox(source.bbox);
    if (source.bbox != null && !bbox) return [];
    const polygon = normalizeImageEvidencePolygon(source.polygon);
    if (source.polygon != null && !polygon) return [];
    if (factStatus === "visible" && !bbox && !polygon) return [];
    const reviewCandidate = String(source.review_status || "pending");
    const reviewStatus = REVIEW_STATUSES.has(reviewCandidate)
      ? reviewCandidate as ImageEvidenceReviewStatus
      : "pending";
    const analyzerSource = String(
      source.analyzer_source || source.analyzer || source.analyzer_name || "",
    ).trim() || null;
    const analyzerStatus = String(
      source.analyzer_status || source.analysis_status || source.status || "",
    ).trim().toLowerCase() || null;
    const analysisCandidate = analyzerStatus === "analyzed"
      ? "ready"
      : analyzerStatus === "failed" ? "degraded" : analyzerStatus || "ready";
    const analysisStatus = ANALYSIS_STATUSES.has(analysisCandidate)
      ? analysisCandidate as ImageEvidenceAnalysisStatus
      : "degraded";
    const fallbackId = `evidence-${sourceIndex}-${index + 1}`;
    const baseId = safeEvidenceId(source.evidence_id, fallbackId);
    let evidenceId = baseId;
    let suffix = 2;
    while (usedIds.has(evidenceId)) {
      evidenceId = `${baseId}-${suffix}`;
      suffix += 1;
    }
    usedIds.add(evidenceId);
    const conflictsWith = Array.isArray(source.conflicts_with)
      ? [...new Set(source.conflicts_with.map((item) => String(item || "").trim()).filter(Boolean))]
      : Array.isArray(source.conflict_ids)
        ? [...new Set(source.conflict_ids.map((item) => String(item || "").trim()).filter(Boolean))]
        : [];
    const conflictCandidate = String(source.conflict_status || "").trim().toLowerCase();
    const conflictStatus = conflictCandidate === "conflict" || conflictsWith.length > 0
      ? "conflict" as const
      : "none" as const;
    const explicitConflictGroup = String(source.conflict_group || "").trim();
    return [{
      evidence_id: evidenceId,
      evidence_type: evidenceType as ImageEvidenceItem["evidence_type"],
      bbox,
      ...(polygon ? { polygon } : {}),
      field_key: fieldKey,
      evidence_text: evidenceText,
      confidence: roundCoordinate(confidence),
      source_index: sourceIndex,
      fact_status: factStatus as ImageEvidenceItem["fact_status"],
      protected: source.protected,
      editable: source.editable,
      review_status: reviewStatus,
      analyzer: analyzerSource,
      analyzer_source: analyzerSource,
      analyzer_status: analyzerStatus,
      analyzer_version: String(source.analyzer_version || source.analysis_version || "").trim() || null,
      evidence_source: String(source.evidence_source || source.source || "").trim() || null,
      analysis_status: analysisStatus,
      degraded_reason: String(source.degraded_reason || "").trim() || null,
      conflict_group: explicitConflictGroup || (conflictStatus === "conflict"
        ? `backend:${[evidenceId, ...conflictsWith].sort().join("|")}`
        : null),
      conflict_status: conflictStatus,
      conflicts_with: conflictsWith,
    }];
  });
}

export type ImageEvidenceConflictGroup = {
  id: string;
  source_index: number;
  field_key: string;
  evidence_ids: string[];
  analyzers: string[];
};

export function imageEvidenceConflictGroups(value: unknown): ImageEvidenceConflictGroup[] {
  const rows = normalizeImageEvidence(value).filter((item) => item.review_status !== "rejected");
  const groups = new Map<string, ImageEvidenceItem[]>();
  for (const item of rows) {
    const key = item.conflict_group
      ? `explicit:${item.conflict_group}`
      : `field:${item.source_index}:${item.field_key}`;
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  return [...groups.entries()].flatMap(([id, items]) => {
    if (items.length < 2) return [];
    const texts = new Set(items.map((item) => item.evidence_text.trim().toLocaleLowerCase()));
    const analyzers = new Set(items.map((item) => item.analyzer || item.evidence_type));
    if (!id.startsWith("explicit:") && (texts.size < 2 || analyzers.size < 2)) return [];
    return [{
      id,
      source_index: items[0].source_index,
      field_key: items[0].field_key,
      evidence_ids: items.map((item) => item.evidence_id),
      analyzers: [...analyzers],
    }];
  });
}

export type ImageEvidenceMaskReadiness = {
  status: "ready" | "pending" | "unsupported" | "degraded" | "source_expired" | "source_hash_changed";
  label: string;
  reason: string;
};

export function imageEvidenceMaskReadiness(result: unknown, operation: unknown): ImageEvidenceMaskReadiness {
  const payload = result && typeof result === "object" && !Array.isArray(result)
    ? result as Record<string, any>
    : {};
  const op = operation && typeof operation === "object" && !Array.isArray(operation)
    ? operation as Record<string, any>
    : {};
  const readiness = payload.image_mask_readiness && typeof payload.image_mask_readiness === "object"
    ? payload.image_mask_readiness as Record<string, unknown>
    : payload.evidence_mask_readiness && typeof payload.evidence_mask_readiness === "object"
      ? payload.evidence_mask_readiness as Record<string, unknown>
      : {};
  const code = String(readiness.status || readiness.code || payload.image_mask_status || "").toLowerCase();
  const reason = String(readiness.reason || readiness.message || "").trim();
  if (op.expired || code === "source_expired") {
    return { status: "source_expired", label: "源素材已过期", reason: reason || "原始素材已过期，不能生成服务端蒙版。" };
  }
  if (["source_hash_changed", "hash_changed", "source_changed"].includes(code)) {
    return { status: "source_hash_changed", label: "源素材哈希已变化", reason: reason || "素材内容与证据版本不一致，请重新分析。" };
  }
  if (code === "ready") return { status: "ready", label: "服务端蒙版就绪", reason: reason || "服务端已校验证据版本与源素材。" };
  if (code === "unsupported") return { status: "unsupported", label: "服务端暂不支持", reason: reason || "当前服务端未开放证据蒙版。" };
  if (["degraded", "failed"].includes(code)) return { status: "degraded", label: "服务端蒙版降级", reason: reason || "服务端无法可靠生成蒙版。" };
  return { status: "pending", label: "服务端蒙版待校验", reason: reason || "保存审阅版本后，由服务端校验源素材与证据；不会自动应用。" };
}

export function imageEvidenceSources(operation: unknown): Array<{ asset_url: string; label: string }> {
  if (!operation || typeof operation !== "object") return [];
  const record = operation as Record<string, any>;
  const context = record.request_context && typeof record.request_context === "object"
    ? record.request_context
    : {};
  const rawSources = Array.isArray(context.sources) ? context.sources : [];
  if (rawSources.length) {
    return rawSources.map((source: unknown, index: number) => {
      const row = source && typeof source === "object"
        ? source as Record<string, unknown>
        : {};
      return {
        asset_url: String(row.asset_url || "").trim(),
        label: String(row.label || row.role || `参考图 ${index + 1}`),
      };
    });
  }
  const assetUrl = String(record.asset_url || "").trim();
  return assetUrl ? [{ asset_url: assetUrl, label: "主素材" }] : [];
}
