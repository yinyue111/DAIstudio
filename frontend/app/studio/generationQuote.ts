import type {
  GeneratePayload,
  GenerationQuote,
  GenerationQuotePayload,
  StudioQuote,
  StudioQuoteKind,
  StudioQuoteRequest,
  StudioQuoteView,
} from "../../lib/api";

type QuoteApi = Pick<
  {
    quote(payload: GenerationQuotePayload): Promise<GenerationQuote>;
    studioQuote(payload: StudioQuoteRequest): Promise<StudioQuote>;
  },
  "quote" | "studioQuote"
>;

export const QUOTE_REFRESH_ERROR_CODES = new Set([
  "QUOTE_EXPIRED",
  "QUOTE_MISMATCH",
  "QUOTE_REPRICED",
  "QUOTE_NOT_FOUND",
]);

export function studioQuoteErrorCode(error: unknown) {
  if (!error || typeof error !== "object") return "";
  const detail = (error as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return "";
  return String((detail as { code?: unknown }).code || "").trim();
}

export function shouldRefreshGenerationQuote(error: unknown) {
  return QUOTE_REFRESH_ERROR_CODES.has(studioQuoteErrorCode(error));
}

export function quotePayload(
  payload: GenerationQuotePayload | GeneratePayload,
): GenerationQuotePayload {
  if (!("quote_id" in payload)) return { ...payload };
  const { quote_id: _quoteId, ...request } = payload;
  return request as GenerationQuotePayload;
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalValue(item)]),
  );
}

export function studioQuoteRequestFingerprint(value: unknown) {
  return JSON.stringify(canonicalValue(value));
}

export const PENDING_STUDIO_ACTION_STORAGE_KEY = "studio_pending_action_request_v1";
const PENDING_STUDIO_ACTION_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_PENDING_STUDIO_ACTIONS = 32;

type StudioActionStorage = Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
type StudioActionPendingRef = { current: unknown };
type StudioActionPendingRecord = {
  scope: string;
  signature: string;
  id: string;
  createdAt: number;
};
type StudioActionPendingOptions = {
  storage?: StudioActionStorage;
  now?: () => number;
  randomId?: () => string;
};

function studioActionStorage(): StudioActionStorage {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage || null;
  } catch (_error) {
    return null;
  }
}

function randomStudioActionId() {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function studioActionSignature(request: Record<string, unknown>) {
  const {
    quote_id: _quoteId,
    client_request_id: _clientRequestId,
    idempotency_key: _idempotencyKey,
    ...stableRequest
  } = request;
  const serialized = studioQuoteRequestFingerprint(stableRequest);
  let left = 2166136261;
  let right = 2246822507;
  for (let index = 0; index < serialized.length; index += 1) {
    const code = serialized.charCodeAt(index);
    left = Math.imul(left ^ code, 16777619);
    right = Math.imul(right ^ code, 3266489909);
  }
  return `${serialized.length}:${(left >>> 0).toString(16)}:${(right >>> 0).toString(16)}`;
}

function studioActionPendingRecords(value: unknown, now: number) {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is StudioActionPendingRecord => {
    if (!item || typeof item !== "object") return false;
    const record = item as Partial<StudioActionPendingRecord>;
    const age = now - Number(record.createdAt || 0);
    return (
      typeof record.scope === "string"
      && typeof record.signature === "string"
      && typeof record.id === "string"
      && Boolean(record.id)
      && Number.isFinite(age)
      && age >= 0
      && age <= PENDING_STUDIO_ACTION_TTL_MS
    );
  });
}

function mergedStudioActionPendingRecords(
  inMemory: StudioActionPendingRecord[],
  stored: StudioActionPendingRecord[],
) {
  const merged: StudioActionPendingRecord[] = [];
  for (const record of [...stored, ...inMemory]) {
    const existingIndex = merged.findIndex((candidate) => (
      candidate.scope === record.scope && candidate.signature === record.signature
    ));
    if (existingIndex < 0) merged.push(record);
    else if (record.createdAt >= merged[existingIndex].createdAt) merged[existingIndex] = record;
  }
  return merged
    .sort((left, right) => left.createdAt - right.createdAt)
    .slice(-MAX_PENDING_STUDIO_ACTIONS);
}

function readStoredStudioActionPendingRecords(storage: StudioActionStorage, now: number) {
  if (!storage) return [];
  try {
    return studioActionPendingRecords(
      JSON.parse(storage.getItem(PENDING_STUDIO_ACTION_STORAGE_KEY) || "null"),
      now,
    );
  } catch (_error) {
    return [];
  }
}

function persistStudioActionPendingRecords(
  storage: StudioActionStorage,
  records: StudioActionPendingRecord[],
) {
  if (!storage) return;
  try {
    if (records.length) {
      storage.setItem(PENDING_STUDIO_ACTION_STORAGE_KEY, JSON.stringify(records));
    } else {
      storage.removeItem(PENDING_STUDIO_ACTION_STORAGE_KEY);
    }
  } catch (_error) {
    // The in-memory ref still preserves idempotency for the current page lifetime.
  }
}

function resolveStudioActionPendingRecords(
  pendingRef: StudioActionPendingRef,
  storage: StudioActionStorage,
  now: number,
) {
  return mergedStudioActionPendingRecords(
    studioActionPendingRecords(pendingRef.current, now),
    readStoredStudioActionPendingRecords(storage, now),
  );
}

export function pendingStudioActionRequestId(
  pendingRef: StudioActionPendingRef,
  scope: string,
  request: Record<string, unknown>,
  {
    storage = studioActionStorage(),
    now = Date.now,
    randomId = randomStudioActionId,
  }: StudioActionPendingOptions = {},
) {
  const normalizedScope = String(scope || "").trim();
  if (!normalizedScope) throw new Error("付费操作缺少幂等作用域。");
  const currentTime = now();
  const signature = studioActionSignature(request);
  const records = resolveStudioActionPendingRecords(pendingRef, storage, currentTime);
  const existing = records.find((record) => (
    record.scope === normalizedScope && record.signature === signature
  ));
  if (existing) {
    pendingRef.current = records;
    persistStudioActionPendingRecords(storage, records);
    return existing.id;
  }
  const id = `studio-action-${randomId()}`;
  const nextRecords = mergedStudioActionPendingRecords(
    [...records, { scope: normalizedScope, signature, id, createdAt: currentTime }],
    [],
  );
  pendingRef.current = nextRecords;
  persistStudioActionPendingRecords(storage, nextRecords);
  return id;
}

export function clearPendingStudioActionRequest(
  pendingRef: StudioActionPendingRef,
  id: string | null | undefined,
  {
    storage = studioActionStorage(),
    now = Date.now,
  }: StudioActionPendingOptions = {},
) {
  const normalizedId = String(id || "").trim();
  if (!normalizedId) return;
  const records = resolveStudioActionPendingRecords(pendingRef, storage, now())
    .filter((record) => record.id !== normalizedId);
  pendingRef.current = records;
  persistStudioActionPendingRecords(storage, records);
}

export function studioQuoteEnvelope(
  kind: StudioQuoteKind,
  request: Record<string, unknown>,
  clientRequestId = "",
): StudioQuoteRequest {
  const { quote_id: _quoteId, ...normalizedRequest } = request;
  const requestClientId = String(normalizedRequest.client_request_id || "").trim();
  const resolvedRequestId = String(
    clientRequestId || requestClientId,
  ).trim();
  if (!resolvedRequestId) throw new Error("计费校验缺少 client_request_id。");
  if (requestClientId && requestClientId !== resolvedRequestId) {
    throw new Error("计费校验的 client_request_id 不一致。");
  }
  return {
    kind,
    client_request_id: resolvedRequestId,
    request: canonicalValue(normalizedRequest) as Record<string, unknown>,
  };
}

function finiteNumber(...values: unknown[]) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function quoteId(quote: Record<string, unknown>) {
  const value = Number(quote?.quote_id || quote?.id);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error("服务端计费校验失败，请稍后重试。");
  }
  return value;
}

function breakdownRows(value: unknown) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const items = (value as Record<string, unknown>).items;
    if (Array.isArray(items)) return breakdownRows(items);
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return null;
      const row = item as Record<string, unknown>;
      const credits = finiteNumber(row.credits, row.amount, row.cost_credits, row.estimated_credits);
      if (credits === null) return null;
      return {
        key: String(row.key || row.code || index),
        label: String(row.label || row.name || row.title || row.key || `费用项 ${index + 1}`),
        credits,
        detail: row.detail == null ? null : String(row.detail),
      };
    }).filter(Boolean) as StudioQuoteView["breakdown"];
  }
  if (!value || typeof value !== "object") return [];
  return Object.entries(value as Record<string, unknown>).map(([key, item]) => {
    const row = item && typeof item === "object" && !Array.isArray(item)
      ? item as Record<string, unknown>
      : null;
    const credits = finiteNumber(
      row?.credits,
      row?.amount,
      row?.cost_credits,
      row?.estimated_credits,
      item,
    );
    if (credits === null) return null;
    return {
      key,
      label: String(row?.label || row?.name || row?.title || key),
      credits,
      detail: row?.detail == null ? null : String(row.detail),
    };
  }).filter(Boolean) as StudioQuoteView["breakdown"];
}

function warningText(value: unknown) {
  if (typeof value === "string" || typeof value === "number") {
    return String(value).trim();
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const item = value as Record<string, unknown>;
  for (const key of ["message", "msg", "detail", "reason", "label", "title", "code"]) {
    const text = item[key];
    if (typeof text === "string" || typeof text === "number") {
      const normalized = String(text).trim();
      if (normalized) return normalized;
    }
  }
  return "服务端返回了一项计费提醒。";
}

function quoteWarnings(quote: Record<string, unknown>) {
  const metadata = quote.metadata && typeof quote.metadata === "object" && !Array.isArray(quote.metadata)
    ? quote.metadata as Record<string, unknown>
    : {};
  const values = Array.isArray(quote.warnings)
    ? quote.warnings
    : Array.isArray(metadata.warnings) ? metadata.warnings : [];
  return values.map(warningText).filter(Boolean);
}

export function normalizeStudioQuote(
  rawQuote: GenerationQuote | StudioQuote,
  envelope: StudioQuoteRequest,
  accountBalance: number | null = null,
): StudioQuoteView {
  const quote = rawQuote as unknown as Record<string, unknown>;
  const consumedReplay = String(quote.status || "").toLowerCase() === "consumed";
  const balance = quote.balance && typeof quote.balance === "object" && !Array.isArray(quote.balance)
    ? quote.balance as Record<string, unknown>
    : {};
  const totalCredits = finiteNumber(
    quote.total_credits,
    quote.estimated_credits,
    quote.cost_credits,
    quote.amount,
  );
  if (totalCredits === null || totalCredits < 0) {
    throw new Error("服务端计费结果缺少有效总积分。");
  }
  const quotedBalanceBefore = finiteNumber(
    balance.before,
    balance.available,
    quote.balance_before,
    quote.available_balance,
    quote.balance_credits,
  );
  const explicitBalanceAfter = finiteNumber(
    balance.after,
    quote.balance_after,
    quote.balance_after_estimate,
  );
  const accountSnapshot = finiteNumber(accountBalance);
  const balanceBefore = consumedReplay
    ? accountSnapshot
    : quotedBalanceBefore
      ?? (explicitBalanceAfter === null ? accountSnapshot : explicitBalanceAfter + totalCredits);
  const balanceAfter = consumedReplay
    ? balanceBefore
    : explicitBalanceAfter ?? (
      balanceBefore === null ? null : balanceBefore - totalCredits
    );
  const affordable = consumedReplay
    ? true
    : typeof quote.affordable === "boolean"
      ? quote.affordable
      : typeof balance.sufficient === "boolean"
        ? balance.sufficient
        : balanceAfter !== null ? balanceAfter >= 0
          : balanceBefore === null ? null : balanceBefore >= totalCredits;
  const expiresAt = String(quote.expires_at || "").trim();
  if (!expiresAt || !Number.isFinite(Date.parse(expiresAt))) {
    throw new Error("服务端计费结果缺少有效过期时间。");
  }
  return {
    id: quoteId(quote),
    kind: envelope.kind,
    clientRequestId: envelope.client_request_id,
    totalCredits,
    breakdown: breakdownRows(quote.breakdown ?? quote.price_breakdown),
    balanceBefore,
    balanceAfter,
    balanceSource: consumedReplay
      ? (accountSnapshot === null ? "unavailable" : "account_snapshot")
      : finiteNumber(
        balance.before,
        balance.available,
        quote.balance_before,
        quote.available_balance,
        quote.balance_credits,
        quote.balance_after,
        quote.balance_after_estimate,
      ) === null ? (accountSnapshot === null ? "unavailable" : "account_snapshot") : "quote",
    affordable,
    expiresAt,
    warnings: consumedReplay ? [] : quoteWarnings(quote),
    modelName: String(quote.model_name || quote.model_id || "").trim() || null,
    raw: rawQuote,
    envelope,
  };
}

export async function requestAuthoritativeStudioQuote(
  api: QuoteApi,
  envelope: StudioQuoteRequest,
  accountBalance: number | null = null,
) {
  // Generation keeps the deployed flat contract. Reverse and workflow use the
  // unified envelope and fail closed until the server exposes that contract.
  const raw = envelope.kind === "generation"
    ? await api.quote(envelope.request as unknown as GenerationQuotePayload)
    : await api.studioQuote(envelope);
  return normalizeStudioQuote(raw, envelope, accountBalance);
}

export function confirmedRequestPayload(
  request: Record<string, unknown>,
  quote: StudioQuoteView,
) {
  return { ...request, quote_id: quote.id };
}
