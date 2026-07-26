import {
  isReverseAnalysisPrecision,
  type ReverseAnalysisPrecision,
} from "../app/studio/reverseConfig";
import { normalizeReverseOperation } from "./reverseOperations";

export const MAX_REVERSE_BATCH_ITEMS = 20;

export type ReverseBatchItemOverride = {
  target?: "image" | "video";
  analysis_precision?: ReverseAnalysisPrecision;
  source_ranges?: Array<{ start_seconds: number; end_seconds: number }>;
  custom_keyframes?: number[];
  include_audio?: boolean;
};

export type ReverseBatchOverrideCapability = {
  supported: boolean;
  fields: Array<keyof ReverseBatchItemOverride>;
  audio_policies: Array<"inherit" | "exclude" | "analyze">;
  reason: string;
};

const ACTIVE_OPERATION_STATUSES = new Set(["queued", "running"]);
const TERMINAL_BATCH_STATUSES = new Set(["succeeded", "partial", "failed", "canceled"]);

function objectValue(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
}

export function reverseBatchOverrideCapability(value: unknown): ReverseBatchOverrideCapability {
  const raw = objectValue(value);
  const capabilities = objectValue(raw.capabilities || raw.batch_capabilities || raw.reverse_batch);
  const itemOverrides = capabilities.item_overrides;
  if (itemOverrides === true) {
    const rawFields = Array.isArray(capabilities.supported_override_keys)
      ? capabilities.supported_override_keys
      : Array.isArray(capabilities.supported_keys) ? capabilities.supported_keys : null;
    const supportedFields = rawFields
      ? rawFields.map((field) => String(field) === "audio_policy" ? "include_audio" : String(field))
        .filter((field) => ["target", "analysis_precision", "source_ranges", "custom_keyframes", "include_audio"].includes(field)) as Array<keyof ReverseBatchItemOverride>
      : ["target", "analysis_precision", "source_ranges", "custom_keyframes", "include_audio"] as Array<keyof ReverseBatchItemOverride>;
    return {
      supported: true,
      fields: supportedFields,
      audio_policies: Array.isArray(capabilities.audio_policies)
        ? capabilities.audio_policies.filter((item: unknown) => ["inherit", "exclude", "analyze"].includes(String(item)))
        : ["inherit", "exclude", "analyze"],
      reason: "",
    };
  }
  if (itemOverrides && typeof itemOverrides === "object" && !Array.isArray(itemOverrides)) {
    const config = itemOverrides as Record<string, unknown>;
    const allowed = new Set(["target", "analysis_precision", "source_ranges", "custom_keyframes", "include_audio"]);
    const configuredFields = Array.isArray(config.supported_keys)
      ? config.supported_keys
      : Array.isArray(config.fields) ? config.fields : null;
    const fields = configuredFields
      ? configuredFields.map((field) => String(field) === "audio_policy" ? "include_audio" : String(field)).filter((field) => allowed.has(field)) as Array<keyof ReverseBatchItemOverride>
      : [...allowed] as Array<keyof ReverseBatchItemOverride>;
    return {
      supported: config.supported !== false,
      fields,
      audio_policies: Array.isArray(config.audio_policies)
        ? config.audio_policies.map(String).filter((item) => ["inherit", "exclude", "analyze"].includes(item)) as Array<"inherit" | "exclude" | "analyze">
        : ["inherit", "exclude", "analyze"],
      reason: String(config.reason || ""),
    };
  }
  return { supported: false, fields: [], audio_policies: [], reason: "当前服务端未声明逐项覆盖能力。" };
}

function finiteRanges(value: unknown): Array<{ start_seconds: number; end_seconds: number }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const raw = objectValue(item);
    const start = Number(raw.start_seconds);
    const end = Number(raw.end_seconds);
    return Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start
      ? [{ start_seconds: start, end_seconds: end }]
      : [];
  }).sort((left, right) => left.start_seconds - right.start_seconds);
}

export function normalizeReverseBatchItemOverride(value: unknown): ReverseBatchItemOverride {
  const raw = objectValue(value);
  const target = String(raw.target);
  const precision = String(raw.analysis_precision);
  const keyframes = Array.isArray(raw.custom_keyframes)
    ? [...new Set(raw.custom_keyframes.map(Number).filter((item: number) => Number.isFinite(item) && item >= 0))].sort((a, b) => a - b)
    : [];
  return {
    ...(target === "image" || target === "video" ? { target } : {}),
    ...(isReverseAnalysisPrecision(precision) ? { analysis_precision: precision } : {}),
    ...(raw.source_ranges !== undefined ? { source_ranges: finiteRanges(raw.source_ranges) } : {}),
    ...(raw.custom_keyframes !== undefined ? { custom_keyframes: keyframes } : {}),
    ...(typeof raw.include_audio === "boolean" ? { include_audio: raw.include_audio } : {}),
  };
}

export function reverseBatchOverrideCount(value: unknown): number {
  return Object.keys(normalizeReverseBatchItemOverride(value)).length;
}

export function parseReverseBatchRanges(value: unknown) {
  const text = String(value || "").trim();
  if (!text) return [];
  return finiteRanges(text.split(/[,，]/).map((token) => {
    const [start, end] = token.trim().split(/[-~至]/).map(Number);
    return { start_seconds: start, end_seconds: end };
  }));
}

export function parseReverseBatchKeyframes(value: unknown) {
  return [...new Set(String(value || "").split(/[,，]/)
    .map((token) => Number(token.trim()))
    .filter((item) => Number.isFinite(item) && item >= 0))]
    .sort((left, right) => left - right);
}

export function buildReverseBatchItemPayload<T extends Record<string, unknown>>(
  source: T,
  override: unknown,
  capability: ReverseBatchOverrideCapability,
): T & ReverseBatchItemOverride & { audio_policy?: "inherit" | "exclude" | "analyze" } {
  const {
    overrides: _ignoredOverrides,
    target: _ignoredTarget,
    analysis_precision: _ignoredPrecision,
    source_ranges: _ignoredRanges,
    custom_keyframes: _ignoredKeyframes,
    include_audio: _ignoredAudio,
    audio_policy: _ignoredAudioPolicy,
    ...baseSource
  } = source as T & Record<string, unknown>;
  const normalized = normalizeReverseBatchItemOverride(override);
  if (!capability.supported) return { ...baseSource } as T;
  const allowed = Object.fromEntries(
    Object.entries(normalized).filter(([key]) => capability.fields.includes(key as keyof ReverseBatchItemOverride)),
  ) as ReverseBatchItemOverride;
  const { include_audio: includeAudio, ...direct } = allowed;
  return Object.keys(allowed).length
    ? {
        ...baseSource,
        ...direct,
        ...(typeof includeAudio === "boolean" ? { audio_policy: includeAudio ? "analyze" : "exclude" } : {}),
      } as T & ReverseBatchItemOverride & { audio_policy?: "analyze" | "exclude" }
    : { ...baseSource } as T;
}

function normalizeBatchItem(value: unknown, fallbackIndex: number) {
  const raw = objectValue(value);
  const operationRaw = objectValue(raw.operation || raw.reverse_operation || raw);
  let operation = null;
  try {
    if (operationRaw.id) operation = normalizeReverseOperation(operationRaw);
  } catch (_error) {
    operation = null;
  }
  const index = Number(raw.index ?? raw.position ?? fallbackIndex);
  const operationId = Number(raw.operation_id ?? operation?.id ?? operationRaw.id);
  const source = objectValue(raw.source || raw.source_snapshot || raw.asset);
  const assetUrl = String(
    raw.asset_url
    || source.asset_url
    || source.url
    || operation?.request_context?.asset_url
    || operationRaw.asset_url
    || "",
  );
  return {
    id: Number(raw.id) || null,
    index: Number.isInteger(index) && index >= 0 ? index : fallbackIndex,
    operation_id: Number.isInteger(operationId) && operationId > 0 ? operationId : null,
    source: {
      ...source,
      asset_url: assetUrl,
      source_type: String(
        source.source_type
        || raw.source_type
        || operation?.source_type
        || operationRaw.source_type
        || "image",
      ) === "video" ? "video" : "image",
      fallback_image: source.fallback_image || raw.fallback_image || null,
    },
    operation,
    overrides: normalizeReverseBatchItemOverride(raw.overrides || raw.override_config || raw.item_config),
  };
}

function countsForItems(items: ReturnType<typeof normalizeBatchItem>[]) {
  const counts = {
    total: items.length,
    queued: 0,
    running: 0,
    needs_confirmation: 0,
    succeeded: 0,
    failed: 0,
    canceled: 0,
  };
  for (const item of items) {
    const status = String(item.operation?.status || "queued") as keyof typeof counts;
    if (Object.prototype.hasOwnProperty.call(counts, status)) counts[status] += 1;
  }
  return counts;
}

function derivedBatchStatus(counts: ReturnType<typeof countsForItems>) {
  if (counts.running > 0) return "running";
  // 待确认必须暴露为独立状态：折叠成"等待中"会让用户错过 15 分钟确认 TTL，
  // 超时后任务直接取消退款（后端 reap_operations 的 CONFIRMATION_EXPIRED）。
  if (counts.needs_confirmation > 0) return "needs_confirmation";
  if (counts.queued > 0) return "queued";
  if (counts.total > 0 && counts.succeeded === counts.total) return "succeeded";
  if (counts.succeeded > 0) return "partial";
  if (counts.total > 0 && counts.canceled === counts.total) return "canceled";
  return counts.total > 0 ? "failed" : "queued";
}

export function normalizeReverseBatch(value: unknown) {
  const raw = objectValue(value);
  const rows = Array.isArray(raw.items)
    ? raw.items
    : Array.isArray(raw.operations)
      ? raw.operations
      : [];
  const items = rows
    .map(normalizeBatchItem)
    .sort((left, right) => left.index - right.index);
  const derivedCounts = countsForItems(items);
  const countPayload = objectValue(raw.counts || raw.status_counts);
  const counts = {
    ...derivedCounts,
    ...Object.fromEntries(
      Object.entries(countPayload)
        .filter(([key]) => Object.prototype.hasOwnProperty.call(derivedCounts, key))
        .map(([key, item]) => [key, Math.max(0, Number(item) || 0)]),
    ),
    ...(Number.isInteger(Number(raw.total_count)) && Number(raw.total_count) >= 0
      ? { total: Number(raw.total_count) }
      : {}),
  };
  const id = Number(raw.id);
  if (!Number.isInteger(id) || id <= 0) throw new Error("服务端未返回有效的批量反推任务");
  const status = String(raw.status || derivedBatchStatus(counts));
  // 批次响应没有批级费用字段（serialize_batch 只回子项 operation 的
  // cost_frozen/cost_settled），批级总费用由子项求和得出；若服务端将来
  // 下发批级字段则以服务端为准。
  const rawFrozen = raw.cost_frozen ?? raw.frozen_credits;
  const rawSettled = raw.cost_settled ?? raw.charged_credits;
  const summedFrozen = items.reduce(
    (total, item) => total + Math.max(0, Number(item.operation?.cost_frozen) || 0),
    0,
  );
  const summedSettled = items.reduce(
    (total, item) => total + Math.max(0, Number(item.operation?.cost_settled) || 0),
    0,
  );
  return {
    id,
    client_request_id: String(raw.client_request_id || ""),
    name: String(raw.name || raw.title || `批量反推 #${id}`),
    target: String(raw.target || "image") === "video" ? "video" : "image",
    status,
    active: !TERMINAL_BATCH_STATUSES.has(status) || items.some(
      (item) => ACTIVE_OPERATION_STATUSES.has(String(item.operation?.status || "")),
    ),
    shared_config: objectValue(raw.shared_config || raw.shared_config_snapshot || raw.config_snapshot || raw.request_context),
    capabilities: objectValue(raw.capabilities || raw.batch_capabilities),
    item_override_capability: reverseBatchOverrideCapability(raw),
    counts,
    items,
    cost_frozen: rawFrozen != null ? Math.max(0, Number(rawFrozen) || 0) : summedFrozen,
    cost_settled: rawSettled != null ? Math.max(0, Number(rawSettled) || 0) : summedSettled,
    created_at: raw.created_at ? String(raw.created_at) : null,
    updated_at: raw.updated_at ? String(raw.updated_at) : null,
    finished_at: raw.finished_at ? String(raw.finished_at) : null,
  };
}

export function normalizeReverseBatchList(value: unknown) {
  const rows = Array.isArray(value)
    ? value
    : Array.isArray(objectValue(value).items)
      ? objectValue(value).items
      : [];
  return rows.flatMap((item) => {
    try {
      return [normalizeReverseBatch(item)];
    } catch (_error) {
      return [];
    }
  });
}

export function reverseBatchSucceededOperations(batch: ReturnType<typeof normalizeReverseBatch> | null) {
  return (batch?.items || [])
    .map((item) => item.operation)
    .filter((operation) => operation?.status === "succeeded" && operation?.result);
}

// 待确认子项汇总：数量 + 最早的确认截止时间（用于 UI 倒计时）。
// 超时未确认后端会自动取消并退款（CONFIRMATION_EXPIRED），所以必须醒目提示。
export function reverseBatchConfirmationState(batch: ReturnType<typeof normalizeReverseBatch> | null) {
  const operations = (batch?.items || [])
    .map((item) => item.operation)
    .filter((operation) => operation?.status === "needs_confirmation");
  const expiries = operations
    .map((operation) => Date.parse(String(operation?.confirmation_expires_at || "")))
    .filter((value) => Number.isFinite(value));
  return {
    count: operations.length,
    operations,
    expires_at: expiries.length ? new Date(Math.min(...expiries)).toISOString() : null,
  };
}
