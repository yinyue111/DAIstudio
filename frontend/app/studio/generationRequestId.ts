export const PENDING_GENERATE_STORAGE_KEY = "studio_pending_generate_request_v1";
export const PENDING_REVERSE_STORAGE_KEY = "studio_pending_reverse_request_v1";
const PENDING_GENERATE_TTL_MS = 2 * 60 * 60 * 1000;

type RequestStorage = Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
type PendingRequestRef = { current: any };
type PendingRequestOptions = {
  storage?: RequestStorage;
  now?: () => number;
  randomId?: () => string;
};
type InternalPendingRequestOptions = PendingRequestOptions & {
  storageKey: string;
  prefix: string;
};
type InternalClearRequestOptions = {
  storage?: RequestStorage;
  storageKey: string;
};

function clientStorage() {
  if (typeof window === "undefined") return null;
  return window.sessionStorage || null;
}

function randomRequestSuffix() {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function generatePendingClientRequestId(
  pendingRef: PendingRequestRef,
  scope: string,
  signature: string,
  {
  storageKey,
  prefix,
  storage = clientStorage(),
  now = Date.now,
  randomId = randomRequestSuffix,
}: InternalPendingRequestOptions) {
  const existing = pendingRef.current;
  if (existing?.scope === scope && existing?.signature === signature && existing?.id) return existing.id;
  try {
    const stored = JSON.parse(storage?.getItem(storageKey) || "null");
    const ageMs = now() - Number(stored?.createdAt || 0);
    if (
      stored?.scope === scope
      && stored?.signature === signature
      && stored?.id
      && ageMs >= 0
      && ageMs <= PENDING_GENERATE_TTL_MS
    ) {
      pendingRef.current = stored;
      return stored.id;
    }
  } catch (e) {}
  const id = `${prefix}-${randomId()}`;
  const record = { scope, signature, id, createdAt: now() };
  pendingRef.current = record;
  try {
    storage?.setItem(storageKey, JSON.stringify(record));
  } catch (e) {}
  return id;
}

function clearPendingClientRequest(
  pendingRef: PendingRequestRef,
  id: string | null = null,
  {
  storageKey,
  storage = clientStorage(),
}: InternalClearRequestOptions) {
  if (!id || pendingRef.current?.id === id) {
    pendingRef.current = null;
  }
  try {
    const stored = JSON.parse(storage?.getItem(storageKey) || "null");
    if (!id || stored?.id === id) storage?.removeItem(storageKey);
  } catch (e) {
    try { storage?.removeItem(storageKey); } catch (_e) {}
  }
}

export function generateClientRequestId(
  pendingRef: PendingRequestRef,
  stage: string,
  signature: string,
  options: PendingRequestOptions = {},
) {
  return generatePendingClientRequestId(pendingRef, stage, signature, {
    ...options,
    storageKey: PENDING_GENERATE_STORAGE_KEY,
    prefix: `studio-${stage}`,
  });
}

export function clearPendingGenerateRequest(
  pendingRef: PendingRequestRef,
  id: string | null = null,
  options: PendingRequestOptions = {},
) {
  return clearPendingClientRequest(pendingRef, id, {
    ...options,
    storageKey: PENDING_GENERATE_STORAGE_KEY,
  });
}

export function generateReverseClientRequestId(
  pendingRef: PendingRequestRef,
  scope: string,
  signature: string,
  options: PendingRequestOptions = {},
) {
  return generatePendingClientRequestId(pendingRef, scope, signature, {
    ...options,
    storageKey: PENDING_REVERSE_STORAGE_KEY,
    prefix: `studio-reverse-${scope}`,
  });
}

export function clearPendingReverseRequest(
  pendingRef: PendingRequestRef,
  id: string | null = null,
  options: PendingRequestOptions = {},
) {
  return clearPendingClientRequest(pendingRef, id, {
    ...options,
    storageKey: PENDING_REVERSE_STORAGE_KEY,
  });
}
