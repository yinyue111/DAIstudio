import { api } from "./api";
import type {
  ReverseOperation,
  ReverseOperationCreate,
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
    ? raw.result as Record<string, unknown>
    : null;
  return {
    ...raw,
    id: raw.id,
    status,
    progress: Math.max(0, Math.min(100, Number(raw.progress ?? raw.percent ?? 0) || 0)),
    result,
    video_analysis: (
      raw.video_analysis && typeof raw.video_analysis === "object"
        ? raw.video_analysis
        : result?.video_analysis
    ) as Record<string, unknown> | null | undefined,
    cost_frozen: Number(raw.cost_frozen ?? raw.frozen_credits ?? 0) || 0,
    cost_settled: Number(raw.cost_settled ?? raw.charged_credits ?? 0) || 0,
    cancel_requested: Boolean(raw.cancel_requested),
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
