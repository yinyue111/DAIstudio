import {
  imageEvidenceSources,
  normalizeImageEvidence,
  normalizeImageEvidenceBBox,
  type ImageEvidenceBBox,
  type ImageEvidenceItem,
  type ImageEvidencePoint,
  type ImageEvidenceReviewStatus,
} from "./imageEvidence";

export type ImageEvidenceRegionMode = "none" | "protected" | "editable";

export type ImageEvidenceMaskRegion = {
  evidence_id: string;
  source_index: number;
  field_key: string;
  evidence_type: ImageEvidenceItem["evidence_type"];
  bbox?: ImageEvidenceBBox;
  polygon?: ImageEvidencePoint[];
};

export type ImageEvidenceMaskPlan = {
  schema_version: "image-mask-plan.v1";
  coordinate_space: "normalized";
  sources: Array<{
    source_index: number;
    protected_regions: ImageEvidenceMaskRegion[];
    editable_regions: ImageEvidenceMaskRegion[];
  }>;
};

export type ImageEvidenceMaskPreflight = {
  ok: boolean;
  message: string;
  edit_source_index: number | null;
  foreign_source_indexes: number[];
};

export type ImageEvidenceEditablePatch = Partial<Pick<ImageEvidenceItem,
  "evidence_type" | "field_key" | "evidence_text" | "bbox" | "polygon" | "protected" | "editable"
>>;

export type ManualImageEvidenceInput = {
  sourceIndex: number;
  evidenceType?: ImageEvidenceItem["evidence_type"];
  fieldKey?: string;
  evidenceText?: string;
};

function normalizedRows(value: unknown): ImageEvidenceItem[] {
  const rows = normalizeImageEvidence(value);
  if (!Array.isArray(value) || rows.length !== value.length) {
    throw new Error("图片证据包含无效条目，请刷新反推结果后重试。");
  }
  return rows;
}

export function replaceReverseResultImageEvidence(
  result: unknown,
  value: unknown,
): Record<string, unknown> {
  const current = result && typeof result === "object" && !Array.isArray(result)
    ? result as Record<string, unknown>
    : {};
  return {
    ...current,
    image_evidence: normalizedRows(value),
  };
}

function normalizedEditedItem(value: ImageEvidenceItem): ImageEvidenceItem {
  const rows = normalizeImageEvidence([value]);
  if (rows.length !== 1) throw new Error("证据修改后不符合区域数据约束。");
  return rows[0];
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function regionMode(item: ImageEvidenceItem): ImageEvidenceRegionMode {
  if (item.protected) return "protected";
  if (item.editable) return "editable";
  return "none";
}

function hasRegion(item: ImageEvidenceItem): boolean {
  return Boolean(item.bbox || (item.polygon && item.polygon.length >= 3));
}

function regionBounds(item: ImageEvidenceItem): ImageEvidenceBBox | null {
  if (item.bbox) return item.bbox;
  if (!item.polygon?.length) return null;
  const xs = item.polygon.map((point) => point.x);
  const ys = item.polygon.map((point) => point.y);
  return normalizeImageEvidenceBBox({
    x: Math.min(...xs),
    y: Math.min(...ys),
    width: Math.max(...xs) - Math.min(...xs),
    height: Math.max(...ys) - Math.min(...ys),
  });
}

function unionBounds(items: ImageEvidenceItem[]): ImageEvidenceBBox | null {
  const boxes = items.map(regionBounds).filter((box): box is ImageEvidenceBBox => Boolean(box));
  if (!boxes.length) return null;
  const x = Math.min(...boxes.map((box) => box.x));
  const y = Math.min(...boxes.map((box) => box.y));
  const right = Math.max(...boxes.map((box) => box.x + box.width));
  const bottom = Math.max(...boxes.map((box) => box.y + box.height));
  return normalizeImageEvidenceBBox({ x, y, width: right - x, height: bottom - y });
}

function uniqueText(items: ImageEvidenceItem[]): string {
  return [...new Set(items.map((item) => item.evidence_text.trim()).filter(Boolean))].join("；");
}

function nextEvidenceId(base: string, used: Set<string>): string {
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate)) {
    candidate = `${base}-${suffix}`;
    suffix += 1;
  }
  used.add(candidate);
  return candidate;
}

export function addManualImageEvidenceItem(
  value: unknown,
  {
    sourceIndex,
    evidenceType = "visual_field",
    fieldKey = "人工区域",
    evidenceText = "人工标注区域",
  }: ManualImageEvidenceInput,
): ImageEvidenceItem[] {
  const rows = normalizedRows(value);
  if (!Number.isInteger(sourceIndex) || sourceIndex < 1 || sourceIndex > 12) {
    throw new Error("人工证据引用的源图无效。");
  }
  const used = new Set(rows.map((item) => item.evidence_id));
  const evidenceId = nextEvidenceId(`manual:${sourceIndex}:1`, used);
  const subjectProtection = evidenceType === "subject_protection";
  const created = normalizedEditedItem({
    evidence_id: evidenceId,
    evidence_type: evidenceType,
    bbox: { x: 0.2, y: 0.2, width: 0.6, height: 0.6 },
    field_key: fieldKey.trim(),
    evidence_text: evidenceText.trim(),
    confidence: 1,
    source_index: sourceIndex,
    fact_status: "visible",
    protected: true,
    editable: false,
    review_status: "pending",
    analyzer: "人工标注",
    analyzer_source: "manual",
    analyzer_status: "analyzed",
    analyzer_version: "manual.v1",
    evidence_source: "manual",
    analysis_status: "ready",
    degraded_reason: null,
    conflict_group: null,
    conflict_status: "none",
    conflicts_with: [],
  });
  if (subjectProtection && !created.protected) {
    throw new Error("主体保护证据必须设为保护区。");
  }
  return [...rows, created];
}

export function updateImageEvidenceItem(
  value: unknown,
  evidenceId: string,
  patch: ImageEvidenceEditablePatch,
): ImageEvidenceItem[] {
  const rows = normalizedRows(value);
  const index = rows.findIndex((item) => item.evidence_id === evidenceId);
  if (index < 0) throw new Error("要修改的证据已不存在。");
  const current = rows[index];
  const requested = { ...current, ...patch };
  if (patch.evidence_type === "subject_protection") {
    requested.protected = true;
    requested.editable = false;
  }
  if (patch.protected === true) requested.editable = false;
  if (patch.editable === true) requested.protected = false;
  const changed = Object.keys(patch).some((key) => (
    !sameValue(current[key as keyof ImageEvidenceItem], requested[key as keyof ImageEvidenceItem])
  ));
  const next = normalizedEditedItem({
    ...requested,
    review_status: changed ? "pending" : current.review_status,
  });
  return rows.map((item, itemIndex) => itemIndex === index ? next : item);
}

export function reviewImageEvidenceItem(
  value: unknown,
  evidenceId: string,
  reviewStatus: ImageEvidenceReviewStatus,
): ImageEvidenceItem[] {
  if (!["pending", "confirmed", "rejected"].includes(reviewStatus)) {
    throw new Error("证据审阅状态无效。");
  }
  const rows = normalizedRows(value);
  const index = rows.findIndex((item) => item.evidence_id === evidenceId);
  if (index < 0) throw new Error("要审阅的证据已不存在。");
  return rows.map((item, itemIndex) => itemIndex === index
    ? normalizedEditedItem({ ...item, review_status: reviewStatus })
    : item);
}

export function setImageEvidenceRegionMode(
  value: unknown,
  evidenceId: string,
  mode: ImageEvidenceRegionMode,
): ImageEvidenceItem[] {
  if (!["none", "protected", "editable"].includes(mode)) throw new Error("区域模式无效。");
  const rows = normalizedRows(value);
  const item = rows.find((candidate) => candidate.evidence_id === evidenceId);
  if (!item) throw new Error("要修改的证据已不存在。");
  if (mode !== "none" && (item.fact_status !== "visible" || !hasRegion(item))) {
    throw new Error("只有可见且已定位的证据才能设为保护区或编辑区。");
  }
  return updateImageEvidenceItem(rows, evidenceId, {
    protected: mode === "protected",
    editable: mode === "editable",
  });
}

export function removeImageEvidenceItem(value: unknown, evidenceId: string): ImageEvidenceItem[] {
  const rows = normalizedRows(value);
  if (!rows.some((item) => item.evidence_id === evidenceId)) {
    throw new Error("要删除的证据已不存在。");
  }
  return rows.filter((item) => item.evidence_id !== evidenceId);
}

export function mergeImageEvidenceItems(value: unknown, evidenceIds: string[]): ImageEvidenceItem[] {
  const rows = normalizedRows(value);
  const ids = [...new Set(evidenceIds.map(String))];
  if (ids.length < 2) throw new Error("请至少选择两条证据合并。");
  const selected = rows.filter((item) => ids.includes(item.evidence_id));
  if (selected.length !== ids.length) throw new Error("部分待合并证据已不存在。");
  const first = selected[0];
  const compatible = selected.every((item) => (
    item.source_index === first.source_index
    && item.field_key === first.field_key
    && item.evidence_type === first.evidence_type
    && item.fact_status === first.fact_status
    && regionMode(item) === regionMode(first)
  ));
  if (!compatible) {
    throw new Error("仅能合并同来源、同字段、同类型且区域模式一致的证据。");
  }
  const bbox = unionBounds(selected);
  const merged = normalizedEditedItem({
    ...first,
    bbox,
    polygon: undefined,
    evidence_text: uniqueText(selected),
    confidence: Math.min(...selected.map((item) => item.confidence)),
    review_status: "pending",
  });
  const firstIndex = rows.findIndex((item) => item.evidence_id === first.evidence_id);
  return rows.flatMap((item, index) => {
    if (index === firstIndex) return [merged];
    return ids.includes(item.evidence_id) ? [] : [item];
  });
}

export function splitImageEvidenceItem(
  value: unknown,
  evidenceId: string,
  axis: "horizontal" | "vertical",
): ImageEvidenceItem[] {
  const rows = normalizedRows(value);
  const index = rows.findIndex((item) => item.evidence_id === evidenceId);
  if (index < 0) throw new Error("要拆分的证据已不存在。");
  const item = rows[index];
  const bbox = regionBounds(item);
  if (!bbox) throw new Error("当前证据没有可拆分区域。");
  if (!["horizontal", "vertical"].includes(axis)) throw new Error("拆分方向无效。");
  const used = new Set(rows.map((row) => row.evidence_id));
  const firstId = nextEvidenceId(`${item.evidence_id}-a`, used);
  const secondId = nextEvidenceId(`${item.evidence_id}-b`, used);
  const boxes = axis === "vertical"
    ? [
        { ...bbox, width: bbox.width / 2 },
        { ...bbox, x: bbox.x + bbox.width / 2, width: bbox.width / 2 },
      ]
    : [
        { ...bbox, height: bbox.height / 2 },
        { ...bbox, y: bbox.y + bbox.height / 2, height: bbox.height / 2 },
      ];
  const splitItems = [firstId, secondId].map((id, boxIndex) => normalizedEditedItem({
    ...item,
    evidence_id: id,
    bbox: normalizeImageEvidenceBBox(boxes[boxIndex]),
    polygon: undefined,
    review_status: "pending",
  }));
  return rows.flatMap((row, rowIndex) => rowIndex === index ? splitItems : [row]);
}

export function bboxFromNormalizedDrag(
  start: ImageEvidencePoint,
  end: ImageEvidencePoint,
  minimumSize = 0.005,
): ImageEvidenceBBox | null {
  const bounded = [start, end].map((point) => ({
    x: Math.min(1, Math.max(0, Number(point.x))),
    y: Math.min(1, Math.max(0, Number(point.y))),
  }));
  if (bounded.some((point) => !Number.isFinite(point.x) || !Number.isFinite(point.y))) return null;
  const x = Math.min(bounded[0].x, bounded[1].x);
  const y = Math.min(bounded[0].y, bounded[1].y);
  const width = Math.abs(bounded[1].x - bounded[0].x);
  const height = Math.abs(bounded[1].y - bounded[0].y);
  if (width < minimumSize || height < minimumSize) return null;
  return normalizeImageEvidenceBBox({ x, y, width, height });
}

export function buildImageEvidenceMaskPlan(value: unknown): ImageEvidenceMaskPlan {
  const rows = normalizeImageEvidence(value);
  const bySource = new Map<number, ImageEvidenceMaskPlan["sources"][number]>();
  for (const item of rows) {
    if (
      item.review_status !== "confirmed"
      || item.fact_status !== "visible"
      || !hasRegion(item)
      || item.protected === item.editable
    ) continue;
    const source = bySource.get(item.source_index) || {
      source_index: item.source_index,
      protected_regions: [],
      editable_regions: [],
    };
    const region: ImageEvidenceMaskRegion = {
      evidence_id: item.evidence_id,
      source_index: item.source_index,
      field_key: item.field_key,
      evidence_type: item.evidence_type,
      ...(item.bbox ? { bbox: { ...item.bbox } } : {}),
      ...(item.polygon ? { polygon: item.polygon.map((point) => ({ ...point })) } : {}),
    };
    if (item.protected) source.protected_regions.push(region);
    else source.editable_regions.push(region);
    bySource.set(item.source_index, source);
  }
  return {
    schema_version: "image-mask-plan.v1",
    coordinate_space: "normalized",
    sources: [...bySource.values()].sort((left, right) => left.source_index - right.source_index),
  };
}

function comparableAssetIdentity(value: unknown): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw, "https://studio.local");
    for (const marker of ["/api/uploads/", "/media/"]) {
      if (parsed.pathname.startsWith(marker)) {
        return `owned:${decodeURIComponent(parsed.pathname.slice(marker.length))}`;
      }
    }
    if (!/^[A-Za-z][A-Za-z0-9+.-]*:/.test(raw)) {
      return `relative:${parsed.pathname}${parsed.search}`;
    }
    return `url:${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${parsed.pathname}${parsed.search}`;
  } catch {
    return `raw:${raw}`;
  }
}

export function validateImageEvidenceMaskPreflight(
  value: unknown,
  operation: unknown,
  editSourceUrl: unknown,
): ImageEvidenceMaskPreflight {
  const plan = buildImageEvidenceMaskPlan(value);
  if (!plan.sources.length) {
    return { ok: true, message: "", edit_source_index: null, foreign_source_indexes: [] };
  }
  const editIdentity = comparableAssetIdentity(editSourceUrl);
  const sources = imageEvidenceSources(operation);
  const sourceIndex = editIdentity
    ? sources.findIndex((source) => comparableAssetIdentity(source.asset_url) === editIdentity) + 1
    : 0;
  if (sourceIndex < 1) {
    return {
      ok: false,
      message: "当前编辑源图与反推证据来源不一致，请重新应用反推结果或更换编辑源图。",
      edit_source_index: null,
      foreign_source_indexes: plan.sources.map((source) => source.source_index),
    };
  }
  const foreignSourceIndexes = plan.sources
    .map((source) => source.source_index)
    .filter((index) => index !== sourceIndex);
  if (!foreignSourceIndexes.length) {
    return {
      ok: true,
      message: "",
      edit_source_index: sourceIndex,
      foreign_source_indexes: [],
    };
  }
  const labels = foreignSourceIndexes.map((index) => `参考图 ${index}`).join("、");
  return {
    ok: false,
    message: `当前图片网关只支持对一张编辑源图生成一个蒙版，请取消${labels}的保护/编辑区域后再生成。`,
    edit_source_index: sourceIndex,
    foreign_source_indexes: foreignSourceIndexes,
  };
}
