export const PENDING_GENERATE_STORAGE_KEY = "studio_pending_generate_request_v1";
export const PENDING_REVERSE_STORAGE_KEY = "studio_pending_reverse_request_v1";
const PENDING_GENERATE_TTL_MS = 2 * 60 * 60 * 1000;
const MAX_PENDING_REQUESTS = 16;

type RequestStorage = Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
type PendingRequestRef = { current: unknown };
type PendingRequestRecord = {
  scope: string;
  signature: string;
  id: string;
  createdAt: number;
};
type PendingRequestState = {
  records: PendingRequestRecord[];
  storageDisabled: boolean;
};
const sharedPendingStates = new WeakMap<object, Map<string, PendingRequestState>>();
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
  now?: () => number;
};

function clientStorage() {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage || null;
  } catch (e) {
    return null;
  }
}

function randomRequestSuffix() {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function pendingRecords(value: unknown, now: number): PendingRequestRecord[] {
  const candidates = Array.isArray(value) ? value : value ? [value] : [];
  return candidates.filter((record): record is PendingRequestRecord => {
    if (!record || typeof record !== "object") return false;
    const candidate = record as Partial<PendingRequestRecord>;
    const ageMs = now - Number(candidate.createdAt || 0);
    return (
      typeof candidate.scope === "string"
      && typeof candidate.signature === "string"
      && typeof candidate.id === "string"
      && Boolean(candidate.id)
      && Number.isFinite(ageMs)
      && ageMs >= 0
      && ageMs <= PENDING_GENERATE_TTL_MS
    );
  });
}

function mergePendingRecords(
  memoryRecords: PendingRequestRecord[],
  storedRecords: PendingRequestRecord[],
) {
  const merged: PendingRequestRecord[] = [];
  for (const record of [...storedRecords, ...memoryRecords]) {
    const index = merged.findIndex(
      (candidate) => candidate.scope === record.scope && candidate.signature === record.signature,
    );
    if (index < 0) {
      merged.push(record);
    } else if (record.createdAt >= merged[index].createdAt) {
      merged[index] = record;
    }
  }
  return merged
    .sort((left, right) => left.createdAt - right.createdAt)
    .slice(-MAX_PENDING_REQUESTS);
}

function pendingRequestState(value: unknown, now: number): PendingRequestState {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const state = value as Partial<PendingRequestState>;
    if (Array.isArray(state.records)) {
      return {
        records: mergePendingRecords(pendingRecords(state.records, now), []),
        storageDisabled: Boolean(state.storageDisabled),
      };
    }
  }
  return {
    records: mergePendingRecords(pendingRecords(value, now), []),
    storageDisabled: false,
  };
}

function sharedPendingState(
  storage: RequestStorage,
  storageKey: string,
  now: number,
): PendingRequestState | null {
  if (!storage) return null;
  const state = sharedPendingStates.get(storage)?.get(storageKey);
  return state ? pendingRequestState(state, now) : null;
}

function updateSharedPendingState(
  storage: RequestStorage,
  storageKey: string,
  records: PendingRequestRecord[],
  storageDisabled: boolean,
) {
  if (!storage) return;
  let statesByKey = sharedPendingStates.get(storage);
  if (!statesByKey) {
    statesByKey = new Map();
    sharedPendingStates.set(storage, statesByKey);
  }
  statesByKey.set(storageKey, { records, storageDisabled });
}

function storedPendingRecords(storage: RequestStorage, storageKey: string, now: number) {
  if (!storage) return { records: [], failed: false };
  let storedValue: string | null;
  try {
    storedValue = storage.getItem(storageKey);
  } catch (e) {
    return { records: [], failed: true };
  }
  try {
    return {
      records: pendingRecords(JSON.parse(storedValue || "null"), now),
      failed: false,
    };
  } catch (e) {
    return { records: [], failed: false };
  }
}

function persistPendingRecords(
  storage: RequestStorage,
  storageKey: string,
  records: PendingRequestRecord[],
) {
  if (!storage) return true;
  try {
    if (records.length) {
      storage.setItem(storageKey, JSON.stringify(records));
    } else {
      storage.removeItem(storageKey);
    }
    return true;
  } catch (e) {
    return false;
  }
}

function updatePendingRef(
  pendingRef: PendingRequestRef,
  records: PendingRequestRecord[],
  storageDisabled: boolean,
) {
  pendingRef.current = { records, storageDisabled };
}

function updatePendingState(
  pendingRef: PendingRequestRef,
  storage: RequestStorage,
  storageKey: string,
  records: PendingRequestRecord[],
  storageDisabled: boolean,
) {
  updatePendingRef(pendingRef, records, storageDisabled);
  updateSharedPendingState(storage, storageKey, records, storageDisabled);
}

function resolvePendingState(
  pendingRef: PendingRequestRef,
  storage: RequestStorage,
  storageKey: string,
  now: number,
): PendingRequestState {
  const localState = pendingRequestState(pendingRef.current, now);
  const sharedState = sharedPendingState(storage, storageKey, now);
  if (sharedState?.storageDisabled) return sharedState;
  if (localState.storageDisabled) {
    updateSharedPendingState(storage, storageKey, localState.records, true);
    return localState;
  }
  if (!storage) return localState;

  const stored = storedPendingRecords(storage, storageKey, now);
  if (stored.failed) {
    const fallback = sharedState || localState;
    const failedState = { records: fallback.records, storageDisabled: true };
    updateSharedPendingState(storage, storageKey, failedState.records, true);
    return failedState;
  }
  const records = mergePendingRecords(stored.records, []);
  updateSharedPendingState(storage, storageKey, records, false);
  return { records, storageDisabled: false };
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
  const currentTime = now();
  const state = resolvePendingState(pendingRef, storage, storageKey, currentTime);
  let records = state.records;
  let storageDisabled = state.storageDisabled;
  const existing = records.find(
    (record) => record.scope === scope && record.signature === signature,
  );
  if (existing) {
    if (!storageDisabled && !persistPendingRecords(storage, storageKey, records)) {
      storageDisabled = true;
    }
    updatePendingState(pendingRef, storage, storageKey, records, storageDisabled);
    return existing.id;
  }

  const id = `${prefix}-${randomId()}`;
  const nextRecords = mergePendingRecords(
    [...records, { scope, signature, id, createdAt: currentTime }],
    [],
  );
  if (!storageDisabled && !persistPendingRecords(storage, storageKey, nextRecords)) {
    storageDisabled = true;
  }
  updatePendingState(pendingRef, storage, storageKey, nextRecords, storageDisabled);
  return id;
}

function clearPendingClientRequest(
  pendingRef: PendingRequestRef,
  id: string | null = null,
  {
  storageKey,
  storage = clientStorage(),
  now = Date.now,
}: InternalClearRequestOptions) {
  if (!id) return;
  const currentTime = now();
  const state = resolvePendingState(pendingRef, storage, storageKey, currentTime);
  let storageDisabled = state.storageDisabled;
  let records = state.records;
  records = records.filter((record) => record.id !== id);
  if (!storageDisabled && !persistPendingRecords(storage, storageKey, records)) {
    storageDisabled = true;
  }
  updatePendingState(pendingRef, storage, storageKey, records, storageDisabled);
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

export function shouldKeepPendingReverseRequest(error: unknown) {
  const candidate = error as { status?: unknown; message?: unknown } | null;
  return (
    Number(candidate?.status) === 409
    && String(candidate?.message || "").includes("仍在处理中")
  );
}
