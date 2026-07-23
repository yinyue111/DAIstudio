import { normalizePendingReverseResult } from "./reverseResultApplication";
import { normalizeImageEvidence, type ImageEvidenceItem } from "./imageEvidence";

export type ReverseRevisionDiffGroup = "prompt" | "negative" | "structured" | "parameters";
export type ReverseRevisionDiffStatus = "added" | "removed" | "changed";

export interface ReverseRevisionContent {
  prompt: string;
  negative: string;
  structured: Record<string, unknown>;
  parameters: {
    ratio: string | null;
    vDuration: number | null;
    vResolution: string | null;
  };
}

export interface ReverseRevisionFieldDiff {
  key: string;
  label: string;
  group: ReverseRevisionDiffGroup;
  status: ReverseRevisionDiffStatus;
  before: unknown;
  after: unknown;
}

export interface ReverseRevisionComparison {
  before: ReverseRevisionContent;
  after: ReverseRevisionContent;
  diffs: ReverseRevisionFieldDiff[];
  summary: {
    total: number;
    added: number;
    removed: number;
    changed: number;
    groups: Record<ReverseRevisionDiffGroup, number>;
  };
}

const FORBIDDEN_RECORD_KEYS = new Set(["__proto__", "prototype", "constructor"]);
const EXPLICIT_EVIDENCE_ID = /^[A-Za-z0-9._:-]{1,128}$/;

type RevisionImageEvidenceContent = {
  source_index: number;
  field_key: string;
  evidence_type: string;
  evidence_text: string;
  region: Record<string, unknown> | null;
  review_status: string;
  protected: boolean;
  editable: boolean;
  conflict: {
    status: string;
    conflicts_with: string[];
  };
};

type RevisionImageEvidence = {
  identity: string;
  matchKey: string;
  sortKey: [number, string, string, string];
  label: string;
  content: RevisionImageEvidenceContent;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function payloadFrom(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  return isRecord(value.payload) ? value.payload : value;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .filter((key) => !FORBIDDEN_RECORD_KEYS.has(key))
      .sort((left, right) => left.localeCompare(right, "zh-CN"))
      .map((key) => [key, stableValue(value[key])]),
  );
}

function valuesEqual(left: unknown, right: unknown): boolean {
  return JSON.stringify(stableValue(left)) === JSON.stringify(stableValue(right));
}

function rawImageEvidence(value: unknown): unknown[] {
  const payload = payloadFrom(value);
  return Array.isArray(payload.image_evidence) ? payload.image_evidence : [];
}

function evidenceAnalyzerIdentity(raw: Record<string, unknown>, item: ImageEvidenceItem): string {
  return String(
    raw.analyzer_source
    || raw.analyzer
    || raw.evidence_source
    || item.analyzer_source
    || item.evidence_source
    || "",
  ).trim();
}

function legacyEvidenceBase(raw: Record<string, unknown>, item: ImageEvidenceItem): string {
  return JSON.stringify([
    item.source_index,
    item.field_key,
    item.evidence_type,
    evidenceAnalyzerIdentity(raw, item),
    String(raw.label || "").trim(),
  ]);
}

function evidenceRegion(item: ImageEvidenceItem): Record<string, unknown> | null {
  if (item.bbox) return { bbox: stableValue(item.bbox) };
  if (item.polygon) return { polygon: stableValue(item.polygon) };
  return null;
}

function revisionImageEvidence(value: unknown): RevisionImageEvidence[] {
  const occurrences = new Map<string, number>();
  const rows: RevisionImageEvidence[] = [];
  for (const rawValue of rawImageEvidence(value)) {
    if (!isRecord(rawValue)) continue;
    const item = normalizeImageEvidence([rawValue])[0];
    if (!item) continue;
    const requestedId = String(rawValue.evidence_id || "").trim();
    const explicitId = EXPLICIT_EVIDENCE_ID.test(requestedId) ? requestedId : "";
    const base = explicitId ? `id:${explicitId}` : `legacy:${legacyEvidenceBase(rawValue, item)}`;
    const occurrence = (occurrences.get(base) || 0) + 1;
    occurrences.set(base, occurrence);
    const identity = explicitId
      ? `${explicitId}${occurrence > 1 ? `#${occurrence}` : ""}`
      : `legacy:${item.source_index}:${item.field_key}:${item.evidence_type}:${occurrence}`;
    rows.push({
      identity,
      matchKey: `${base}#${occurrence}`,
      sortKey: [item.source_index, item.field_key, item.evidence_type, identity],
      label: `参考图 ${item.source_index} · ${item.field_key}`,
      content: {
        source_index: item.source_index,
        field_key: item.field_key,
        evidence_type: item.evidence_type,
        evidence_text: item.evidence_text,
        region: evidenceRegion(item),
        review_status: item.review_status,
        protected: item.protected,
        editable: item.editable,
        conflict: {
          status: item.conflict_status,
          conflicts_with: [...item.conflicts_with].sort((left, right) => left.localeCompare(right)),
        },
      },
    });
  }
  return rows.sort(compareEvidenceRows);
}

function compareEvidenceRows(left: RevisionImageEvidence, right: RevisionImageEvidence): number {
  if (left.sortKey[0] !== right.sortKey[0]) return left.sortKey[0] - right.sortKey[0];
  for (let index = 1; index < left.sortKey.length; index += 1) {
    const comparison = String(left.sortKey[index]).localeCompare(String(right.sortKey[index]), "zh-CN");
    if (comparison) return comparison;
  }
  return 0;
}

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return isRecord(value) && Object.keys(value).length === 0;
}

function revisionMediaType(payload: Record<string, unknown>): "image" | "video" {
  const parameters = isRecord(payload.parameters)
    ? payload.parameters
    : isRecord(payload.generation_parameters) ? payload.generation_parameters : {};
  if (
    payload.target === "video"
    || payload.source_type === "video"
    || isRecord(payload.video_analysis)
    || parameters.vDuration != null
    || parameters.v_duration != null
    || parameters.duration != null
  ) return "video";
  return "image";
}

export function normalizeReverseRevisionContent(value: unknown): ReverseRevisionContent {
  const payload = payloadFrom(value);
  const normalized = normalizePendingReverseResult(payload, {
    mediaType: revisionMediaType(payload),
  });
  const structured = Object.fromEntries(
    Object.entries(normalized.structured).filter(
      ([key]) => !["final_text", "shots", "负向", "negative"].includes(key),
    ),
  );
  return {
    prompt: normalized.prompt,
    negative: normalized.negative,
    structured: stableValue(structured) as Record<string, unknown>,
    parameters: { ...normalized.parameters },
  };
}

function fieldStatus(before: unknown, after: unknown): ReverseRevisionDiffStatus {
  if (isEmptyValue(before) && !isEmptyValue(after)) return "added";
  if (!isEmptyValue(before) && isEmptyValue(after)) return "removed";
  return "changed";
}

function addDiff(
  diffs: ReverseRevisionFieldDiff[],
  key: string,
  label: string,
  group: ReverseRevisionDiffGroup,
  before: unknown,
  after: unknown,
) {
  if (valuesEqual(before, after) || (isEmptyValue(before) && isEmptyValue(after))) return;
  diffs.push({ key, label, group, status: fieldStatus(before, after), before, after });
}

export function compareReverseResultRevisions(
  beforeValue: unknown,
  afterValue: unknown,
): ReverseRevisionComparison {
  const before = normalizeReverseRevisionContent(beforeValue);
  const after = normalizeReverseRevisionContent(afterValue);
  const diffs: ReverseRevisionFieldDiff[] = [];
  addDiff(diffs, "prompt", "生成稿", "prompt", before.prompt, after.prompt);
  addDiff(diffs, "negative", "负向提示词", "negative", before.negative, after.negative);

  const structuredKeys = Array.from(new Set([
    ...Object.keys(before.structured),
    ...Object.keys(after.structured),
  ])).sort((left, right) => left.localeCompare(right, "zh-CN"));
  for (const key of structuredKeys) {
    addDiff(
      diffs,
      `structured.${key}`,
      key,
      "structured",
      before.structured[key],
      after.structured[key],
    );
  }

  const beforeEvidence = revisionImageEvidence(beforeValue);
  const afterEvidence = revisionImageEvidence(afterValue);
  const beforeEvidenceByKey = new Map(beforeEvidence.map((row) => [row.matchKey, row]));
  const afterEvidenceByKey = new Map(afterEvidence.map((row) => [row.matchKey, row]));
  const evidenceRows = Array.from(new Set([
    ...beforeEvidenceByKey.keys(),
    ...afterEvidenceByKey.keys(),
  ])).map((key) => beforeEvidenceByKey.get(key) || afterEvidenceByKey.get(key))
    .filter((row): row is RevisionImageEvidence => Boolean(row))
    .sort(compareEvidenceRows);
  for (const row of evidenceRows) {
    const beforeRow = beforeEvidenceByKey.get(row.matchKey);
    const afterRow = afterEvidenceByKey.get(row.matchKey);
    const keyPrefix = `image_evidence.${row.identity}`;
    if (!beforeRow || !afterRow) {
      addDiff(
        diffs,
        keyPrefix,
        `${row.label} · 证据`,
        "structured",
        beforeRow?.content ?? null,
        afterRow?.content ?? null,
      );
      continue;
    }
    const evidenceFields: Array<{
      key: keyof Pick<RevisionImageEvidenceContent,
        "evidence_text" | "region" | "review_status" | "protected" | "editable" | "conflict">;
      label: string;
    }> = [
      { key: "evidence_text", label: "文字" },
      { key: "region", label: "区域" },
      { key: "review_status", label: "审阅状态" },
      { key: "protected", label: "保护" },
      { key: "editable", label: "可编辑" },
      { key: "conflict", label: "冲突" },
    ];
    for (const field of evidenceFields) {
      addDiff(
        diffs,
        `${keyPrefix}.${field.key}`,
        `${row.label} · ${field.label}`,
        "structured",
        beforeRow.content[field.key],
        afterRow.content[field.key],
      );
    }
  }

  addDiff(diffs, "parameters.ratio", "画幅", "parameters", before.parameters.ratio, after.parameters.ratio);
  addDiff(
    diffs,
    "parameters.vDuration",
    "时长",
    "parameters",
    before.parameters.vDuration,
    after.parameters.vDuration,
  );
  addDiff(
    diffs,
    "parameters.vResolution",
    "清晰度",
    "parameters",
    before.parameters.vResolution,
    after.parameters.vResolution,
  );

  const groups: Record<ReverseRevisionDiffGroup, number> = {
    prompt: 0,
    negative: 0,
    structured: 0,
    parameters: 0,
  };
  let added = 0;
  let removed = 0;
  let changed = 0;
  for (const diff of diffs) {
    groups[diff.group] += 1;
    if (diff.status === "added") added += 1;
    else if (diff.status === "removed") removed += 1;
    else changed += 1;
  }
  return {
    before,
    after,
    diffs,
    summary: { total: diffs.length, added, removed, changed, groups },
  };
}

export function formatReverseRevisionValue(value: unknown): string {
  if (isEmptyValue(value)) return "未设置";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(stableValue(value), null, 2);
}
