import { api } from "./api";
import type {
  ReverseOperation,
  ReverseOperationCreate,
  ReverseOperationFeedback,
  ReverseResult,
  ReverseResultRevision,
  ReverseOperationStatus,
} from "./api";

export const ACTIVE_REVERSE_OPERATION_STATUSES = new Set<ReverseOperationStatus>([
  "queued",
  "running",
  "needs_confirmation",
]);
export const TERMINAL_REVERSE_OPERATION_STATUSES = new Set<ReverseOperationStatus>([
  "succeeded",
  "failed",
  "canceled",
]);

function canonicalRequestValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalRequestValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value as Record<string, unknown>)
      .filter((key) => (value as Record<string, unknown>)[key] !== undefined)
      .sort()
      .map((key) => [key, canonicalRequestValue((value as Record<string, unknown>)[key])]),
  );
}

export function reverseOperationRequestSignature(
  body: Omit<ReverseOperationCreate, "client_request_id"> | Record<string, unknown>,
) {
  return JSON.stringify(canonicalRequestValue(body));
}

export function normalizeReverseOperation(payload: unknown): ReverseOperation {
  const wrapped = payload && typeof payload === "object"
    ? (payload as { operation?: unknown }).operation
    : null;
  const raw = (wrapped && typeof wrapped === "object" ? wrapped : payload) as Record<string, unknown> | null;
  if (!raw || (typeof raw.id !== "string" && typeof raw.id !== "number")) {
    throw new Error("服务端未返回有效的反推任务");
  }
  const status = String(raw.status || "queued") as ReverseOperationStatus;
  const result = raw.result && typeof raw.result === "object"
    ? raw.result as ReverseResult
    : null;
  const sourceRange = raw.source_range && typeof raw.source_range === "object" && !Array.isArray(raw.source_range)
    ? raw.source_range as Record<string, unknown>
    : null;
  const sourceRanges = (Array.isArray(raw.source_ranges) ? raw.source_ranges : sourceRange ? [sourceRange] : [])
    .flatMap((item) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const row = item as Record<string, unknown>;
      const start = Number(row.start_seconds);
      const end = Number(row.end_seconds);
      return Number.isFinite(start) && Number.isFinite(end) && end > start
        ? [{ start_seconds: start, end_seconds: end }]
        : [];
    });
  const appliedResultVersion = Number(raw.applied_result_version);
  const retryOfOperationId = Number(raw.retry_of_operation_id);
  return {
    ...raw,
    id: raw.id,
    status,
    progress: Math.max(0, Math.min(100, Number(raw.progress ?? raw.percent ?? 0) || 0)),
    result,
    workspace_snapshot_v2: raw.workspace_snapshot_v2 && typeof raw.workspace_snapshot_v2 === "object"
      ? raw.workspace_snapshot_v2 as Record<string, unknown>
      : null,
    workspace_snapshot_v3: raw.workspace_snapshot_v3 && typeof raw.workspace_snapshot_v3 === "object"
      ? raw.workspace_snapshot_v3 as Record<string, unknown>
      : null,
    analysis_focus: String(raw.analysis_focus || "comprehensive"),
    analysis_precision: String(raw.analysis_precision || raw.video_analysis_preset || "standard"),
    output_purpose: String(raw.output_purpose || "generation"),
    include_audio: Boolean(raw.include_audio),
    source_range: sourceRanges.length === 1 ? sourceRanges[0] : null,
    source_ranges: sourceRanges,
    result_schema_version: String(raw.result_schema_version || "reverse.v2"),
    ...(Object.prototype.hasOwnProperty.call(raw, "applied_result_version")
      ? {
          applied_result_version: Number.isInteger(appliedResultVersion) && appliedResultVersion > 0
            ? appliedResultVersion
            : null,
        }
      : {}),
    retry_of_operation_id: Number.isInteger(retryOfOperationId) && retryOfOperationId > 0
      ? retryOfOperationId
      : null,
    video_analysis: (
      raw.video_analysis && typeof raw.video_analysis === "object"
        ? raw.video_analysis
        : result?.video_analysis
    ) as Record<string, unknown> | null | undefined,
    cost_frozen: Number(raw.cost_frozen ?? raw.frozen_credits ?? 0) || 0,
    cost_settled: Number(raw.cost_settled ?? raw.charged_credits ?? 0) || 0,
    cancel_requested: Boolean(raw.cancel_requested),
    expired: Boolean(raw.expired || raw.error_code === "RESULT_EXPIRED"),
  } as ReverseOperation;
}

export function normalizeReverseOperationList(payload: unknown): ReverseOperation[] {
  const rows = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as { items?: unknown[] }).items)
      ? (payload as { items: unknown[] }).items
      : [];
  return rows.map(normalizeReverseOperation);
}

export function normalizeReverseResultRevision(payload: unknown): ReverseResultRevision {
  const raw = payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : null;
  const id = Number(raw?.id);
  const operationId = Number(raw?.operation_id);
  const version = Number(raw?.version);
  const source = String(raw?.source || "");
  const revisionPayload = raw?.payload && typeof raw.payload === "object" && !Array.isArray(raw.payload)
    ? raw.payload as ReverseResult
    : null;
  if (
    !Number.isInteger(id)
    || id <= 0
    || !Number.isInteger(operationId)
    || operationId <= 0
    || !Number.isInteger(version)
    || version <= 0
    || !["provider_raw", "normalized", "user_edit", "applied", "model_compiled", "generation"].includes(source)
    || !revisionPayload
  ) throw new Error("服务端未返回有效的反推结果版本");
  return {
    id,
    operation_id: operationId,
    version,
    source: source as ReverseResultRevision["source"],
    payload: revisionPayload,
    parent_revision_id: Number.isInteger(Number(raw?.parent_revision_id)) && Number(raw?.parent_revision_id) > 0
      ? Number(raw?.parent_revision_id)
      : null,
    source_content_hash: raw?.source_content_hash ? String(raw.source_content_hash) : null,
    source_fingerprints: Array.isArray(raw?.source_fingerprints)
      ? raw.source_fingerprints.filter((item) => item && typeof item === "object" && !Array.isArray(item)) as Array<Record<string, unknown>>
      : null,
    payload_hash: raw?.payload_hash ? String(raw.payload_hash) : null,
    lineage_status: raw?.lineage_status === "verified" ? "verified" : "legacy_unverified",
    evidence_review_action: ["not_applicable", "inherited", "updated", "cleared"].includes(String(raw?.evidence_review_action || ""))
      ? raw?.evidence_review_action as ReverseResultRevision["evidence_review_action"]
      : null,
    created_at: raw?.created_at ? String(raw.created_at) : null,
  };
}

export function normalizeReverseResultRevisionList(payload: unknown): ReverseResultRevision[] {
  const rows = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as { items?: unknown[] }).items)
      ? (payload as { items: unknown[] }).items
      : [];
  return rows.map(normalizeReverseResultRevision);
}

export function normalizeReverseOperationFeedback(payload: unknown): ReverseOperationFeedback {
  const raw = payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : null;
  const operationId = Number(raw?.operation_id);
  const rating = String(raw?.rating || "");
  if (!Number.isInteger(operationId) || operationId <= 0 || !["useful", "not_useful"].includes(rating)) {
    throw new Error("服务端未返回有效的反推反馈");
  }
  return {
    operation_id: operationId,
    rating: rating as ReverseOperationFeedback["rating"],
    issue_types: Array.isArray(raw?.issue_types)
      ? raw.issue_types.map(String) as ReverseOperationFeedback["issue_types"]
      : [],
    note: raw?.note == null ? null : String(raw.note),
    created_at: raw?.created_at ? String(raw.created_at) : null,
    updated_at: raw?.updated_at ? String(raw.updated_at) : null,
  };
}

export function reverseOperationResult(operation: ReverseOperation | null | undefined) {
  if (!operation) return null;
  const result = operation.result && typeof operation.result === "object"
    ? operation.result
    : null;
  if (!result) return null;
  return {
    ...result,
    video_analysis: operation.video_analysis || result.video_analysis || null,
    charged_credits: operation.cost_settled ?? result.charged_credits ?? 0,
    result_schema_version: operation.result_schema_version,
    applied_result_version: operation.applied_result_version,
  };
}

export function isActiveReverseOperation(operation: ReverseOperation | null | undefined) {
  return Boolean(operation && ACTIVE_REVERSE_OPERATION_STATUSES.has(operation.status));
}

export function isTerminalReverseOperation(operation: ReverseOperation | null | undefined) {
  return Boolean(operation && TERMINAL_REVERSE_OPERATION_STATUSES.has(operation.status));
}

export function reverseOperationTrackingLost(
  operation: ReverseOperation,
): ReverseOperation {
  const normalized = normalizeReverseOperation(operation);
  return {
    ...normalized,
    phase: "进度连接中断，刷新页面可继续恢复",
    error_code: "tracking_lost",
    error: "反推任务仍在后台处理，当前无法获取进度。",
  };
}

export function reverseOperationResumeCandidates(
  workspaces: Record<string, Record<string, any>> | null | undefined,
) {
  return Object.entries(workspaces || {}).flatMap(([mode, current]) => [
    isActiveReverseOperation(current?.reverseOperation)
      ? {
          mode,
          trackingKey: mode,
          operation: current.reverseOperation as ReverseOperation,
          sourceAsset: current.selected || null,
        }
      : null,
    isActiveReverseOperation(current?.profileOperation)
      ? {
          mode,
          trackingKey: `${mode}::profile`,
          operation: current.profileOperation as ReverseOperation,
          sourceAsset: current.productAsset || null,
        }
      : null,
  ].filter(Boolean));
}

export function reverseOperationFailure(operation: ReverseOperation) {
  const error = String(operation.error || "").trim();
  if (operation.status === "canceled") return new Error(error || "反推任务已取消");
  return new Error(error || "反推任务失败，请稍后重试");
}

type ReverseOperationWaitOptions = {
  intervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal | null;
  onUpdate?: ((operation: ReverseOperation) => void) | null;
  maxConsecutiveFailures?: number;
};

function retryableTrackingError(error: unknown) {
  const status = Number((error as { status?: unknown } | null)?.status || 0);
  return !status || status >= 500;
}

export async function waitForReverseOperation(
  initialPayload: unknown,
  {
    intervalMs = 1200,
    timeoutMs = 30 * 60 * 1000,
    signal = null,
    onUpdate = null,
    maxConsecutiveFailures = 5,
  }: ReverseOperationWaitOptions = {},
) {
  let operation = normalizeReverseOperation(initialPayload);
  const startedAt = Date.now();
  let consecutiveFailures = 0;
  onUpdate?.(operation);
  while (!isTerminalReverseOperation(operation) && operation.status !== "needs_confirmation") {
    if (signal?.aborted) throw Object.assign(new Error("反推任务跟踪已取消"), { name: "AbortError" });
    if (Date.now() - startedAt > timeoutMs) {
      throw new Error("反推仍在后台处理中，稍后会自动恢复进度");
    }
    await new Promise<void>((resolve, reject) => {
      const onAbort = () => {
        clearTimeout(timer);
        reject(Object.assign(new Error("反推任务跟踪已取消"), { name: "AbortError" }));
      };
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, intervalMs);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
    try {
      operation = normalizeReverseOperation(await api.reverseOperation(operation.id));
      consecutiveFailures = 0;
      onUpdate?.(operation);
    } catch (error) {
      consecutiveFailures += 1;
      if (!retryableTrackingError(error) || consecutiveFailures >= maxConsecutiveFailures) {
        if (!retryableTrackingError(error)) throw error;
        throw Object.assign(
          new Error("反推任务仍在后台处理中，稍后会自动恢复进度"),
          { cause: error, operation_id: operation.id },
        );
      }
    }
  }
  return operation;
}

export async function createAndWaitForReverseOperation(
  body: ReverseOperationCreate,
  options: ReverseOperationWaitOptions & {
    onCreated?: ((operation: ReverseOperation) => void | Promise<void>) | null;
  } = {},
) {
  const created = normalizeReverseOperation(await api.createReverseOperation(body));
  await options.onCreated?.(created);
  const operation = await waitForReverseOperation(created, options);
  if (operation.status !== "succeeded") throw reverseOperationFailure(operation);
  const result = reverseOperationResult(operation);
  if (!result) throw new Error("反推任务完成但未返回结果");
  return { operation, result };
}
