const STUDIO_METADATA_FIELDS = ["creationMode", "showNegative", "refOpen", "structOpen"];
export const STUDIO_DRAFT_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;

function normalizedUserId(userId) {
  const value = String(userId ?? "").trim();
  return value || "";
}

export function studioUserDraftKey(baseKey, userId) {
  const ownerUserId = normalizedUserId(userId);
  if (!ownerUserId) return "";
  return `${baseKey}:user:${encodeURIComponent(ownerUserId)}`;
}

function removeItem(storage, key) {
  if (!storage || !key) return;
  try {
    storage.removeItem(key);
  } catch (_error) {
    // Storage may be disabled; draft recovery should never block the Studio.
  }
}

export function saveStudioUserDraft(storage, baseKey, userId, draft) {
  const ownerUserId = normalizedUserId(userId);
  const key = studioUserDraftKey(baseKey, ownerUserId);
  if (!storage || !key || !draft || typeof draft !== "object") return false;
  try {
    storage.setItem(key, JSON.stringify({ ...draft, ownerUserId }));
    return true;
  } catch (_error) {
    return false;
  }
}

export function takeStudioUserDraft(storage, baseKey, userId) {
  const draft = readStudioUserDraft(storage, baseKey, userId);
  if (draft) removeStudioUserDraft(storage, baseKey, userId);
  return draft;
}

export function readStudioUserDraft(storage, baseKey, userId) {
  // A legacy global draft has no trustworthy owner. Never migrate it to the
  // next authenticated account.
  removeItem(storage, baseKey);

  const ownerUserId = normalizedUserId(userId);
  const key = studioUserDraftKey(baseKey, ownerUserId);
  if (!storage || !key) return null;

  let raw = null;
  try {
    raw = storage.getItem(key);
  } catch (_error) {
    return null;
  }
  if (!raw) return null;

  try {
    const draft = JSON.parse(raw);
    if (!draft || typeof draft !== "object") return null;
    if (normalizedUserId(draft.ownerUserId) !== ownerUserId) return null;
    return draft;
  } catch (_error) {
    return null;
  }
}

export function removeStudioUserDraft(storage, baseKey, userId) {
  const key = studioUserDraftKey(baseKey, userId);
  if (!key) return false;
  removeItem(storage, key);
  return true;
}

function applyWorkspaceDraftLayer(workspaces, draft) {
  if (!draft?.workspaces || typeof draft.workspaces !== "object") return;
  for (const [mode, current] of Object.entries(draft.workspaces)) {
    if (!Object.prototype.hasOwnProperty.call(workspaces, mode)) continue;
    if (!current || typeof current !== "object" || Array.isArray(current)) continue;
    workspaces[mode] = { ...workspaces[mode], ...current };
  }
}

function isDeepEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((value, index) => isDeepEqual(value, right[index]));
  }
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every(
    (key) => Object.prototype.hasOwnProperty.call(right, key) && isDeepEqual(left[key], right[key]),
  );
}

function normalizedStudioNow(now) {
  return Number.isSafeInteger(now) && now > 0 ? now : Date.now();
}

export function isValidStudioDraftSavedAt(value, now = Date.now()) {
  const current = normalizedStudioNow(now);
  const maxSavedAt = current <= Number.MAX_SAFE_INTEGER - STUDIO_DRAFT_MAX_FUTURE_SKEW_MS
    ? current + STUDIO_DRAFT_MAX_FUTURE_SKEW_MS
    : Number.MAX_SAFE_INTEGER;
  return (
    Number.isSafeInteger(value)
    && value > 0
    && value <= maxSavedAt
  );
}

function draftSavedAt(draft, now) {
  const value = draft?.savedAt;
  if (!isValidStudioDraftSavedAt(value, now)) return { valid: false, value: 0 };
  return { valid: true, value };
}

function orderedUserDraftLayers(localDraft, cloudDraft) {
  const now = Date.now();
  return [
    { source: "cloud", draft: cloudDraft },
    { source: "local", draft: localDraft },
  ]
    .filter(({ draft }) => draft && typeof draft === "object")
    .map((entry) => ({ ...entry, clock: draftSavedAt(entry.draft, now) }))
    .sort((left, right) => {
      if (left.clock.valid !== right.clock.valid) return left.clock.valid ? 1 : -1;
      const timeDifference = left.clock.value - right.clock.value;
      if (timeDifference) return timeDifference;
      return left.source === "local" ? 1 : -1;
    })
    .map(({ draft }) => draft);
}

export function createStudioDraftClock() {
  let ownerUserId = null;
  let generation = -1;
  let contextToken = 0;
  let lastSavedAt = 0;
  let restoredFutureSavedAt = 0;

  function normalizeGeneration(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value : 0;
  }

  function isCurrentContext(owner, boundGeneration, token) {
    return (
      owner === ownerUserId
      && boundGeneration === generation
      && token === contextToken
    );
  }

  function observeRestoreForContext(owner, boundGeneration, token, {
    localDraft = null,
    cloudDraft = null,
    promptDraft = null,
    promptTransferApplied = false,
    variationDraft = null,
    variationTransferApplied = false,
  } = {}, now = Date.now()) {
    if (!isCurrentContext(owner, boundGeneration, token)) return null;

    const current = normalizedStudioNow(now);
    const priorSavedAt = isValidStudioDraftSavedAt(lastSavedAt, current) ? lastSavedAt : 0;
    if (priorSavedAt !== lastSavedAt) {
      lastSavedAt = priorSavedAt;
      restoredFutureSavedAt = 0;
    }
    const observedSavedAt = [
      localDraft,
      cloudDraft,
      promptTransferApplied ? promptDraft : null,
      variationTransferApplied ? variationDraft : null,
    ].reduce((latest, draft) => {
      const observed = draftSavedAt(draft, current);
      return observed.valid ? Math.max(latest, observed.value) : latest;
    }, priorSavedAt);

    if (observedSavedAt > priorSavedAt) {
      lastSavedAt = observedSavedAt;
      if (observedSavedAt > current) restoredFutureSavedAt = observedSavedAt;
    }
    return lastSavedAt;
  }

  function nextForContext(owner, boundGeneration, token, now = Date.now()) {
    if (!isCurrentContext(owner, boundGeneration, token)) return null;

    const current = normalizedStudioNow(now);
    const previous = isValidStudioDraftSavedAt(lastSavedAt, current) ? lastSavedAt : 0;
    if (previous !== lastSavedAt) restoredFutureSavedAt = 0;

    let savedAt;
    if (restoredFutureSavedAt === previous && previous > current) {
      savedAt = current;
    } else if (previous < current) {
      savedAt = current;
    } else {
      savedAt = previous < Number.MAX_SAFE_INTEGER ? previous + 1 : current;
      if (!isValidStudioDraftSavedAt(savedAt, current)) savedAt = current;
    }
    lastSavedAt = savedAt;
    restoredFutureSavedAt = 0;
    return savedAt;
  }

  function scopedClock(owner, boundGeneration, token) {
    return {
      observeRestore(restoreSources, now = Date.now()) {
        return observeRestoreForContext(owner, boundGeneration, token, restoreSources, now);
      },
      next(now = Date.now()) {
        return nextForContext(owner, boundGeneration, token, now);
      },
    };
  }

  const clock = {
    bind(userId, nextGeneration) {
      const nextOwnerUserId = normalizedUserId(userId);
      const normalizedGeneration = normalizeGeneration(nextGeneration);
      if (normalizedGeneration < generation) {
        return scopedClock(nextOwnerUserId, normalizedGeneration, -1);
      }

      if (ownerUserId === null || nextOwnerUserId !== ownerUserId) {
        ownerUserId = nextOwnerUserId;
        generation = normalizedGeneration;
        lastSavedAt = 0;
        restoredFutureSavedAt = 0;
        contextToken += 1;
      } else if (normalizedGeneration > generation) {
        generation = normalizedGeneration;
        restoredFutureSavedAt = 0;
        contextToken += 1;
      }
      return scopedClock(ownerUserId, generation, contextToken);
    },
    observeRestore(restoreSources, now = Date.now()) {
      return observeRestoreForContext(ownerUserId, generation, contextToken, restoreSources, now);
    },
    next(now = Date.now()) {
      return nextForContext(ownerUserId, generation, contextToken, now);
    },
  };

  return clock;
}

export function createStudioOwnerSessionCoordinator({
  clock = createStudioDraftClock(),
  setTimeout: setTimer = globalThis.setTimeout,
  clearTimeout: clearTimer = globalThis.clearTimeout,
} = {}) {
  let ownerUserId = null;
  let generation = -1;
  let contextToken = 0;
  let scheduledTimer = null;

  function cancelScheduled() {
    if (scheduledTimer === null) return false;
    clearTimer(scheduledTimer);
    scheduledTimer = null;
    return true;
  }

  function createSession(owner, boundGeneration, token, draftClock) {
    function isCurrent() {
      return (
        owner === ownerUserId
        && boundGeneration === generation
        && token === contextToken
      );
    }

    return {
      ownerUserId: owner,
      generation: boundGeneration,
      isCurrent,
      commit(apply) {
        if (!isCurrent()) return null;
        return apply();
      },
      observeRestore(restoreSources, now = Date.now()) {
        if (!isCurrent()) return null;
        return draftClock.observeRestore(restoreSources, now);
      },
      next(now = Date.now()) {
        if (!isCurrent()) return null;
        return draftClock.next(now);
      },
    };
  }

  return {
    bindOwner(userId, nextGeneration) {
      const nextOwnerUserId = normalizedUserId(userId);
      const normalizedGeneration = Number.isSafeInteger(nextGeneration) && nextGeneration >= 0
        ? nextGeneration
        : 0;
      const draftClock = clock.bind(nextOwnerUserId, normalizedGeneration);
      if (normalizedGeneration < generation) {
        return createSession(nextOwnerUserId, normalizedGeneration, -1, draftClock);
      }

      if (
        ownerUserId === null
        || nextOwnerUserId !== ownerUserId
        || normalizedGeneration > generation
      ) {
        cancelScheduled();
        ownerUserId = nextOwnerUserId;
        generation = normalizedGeneration;
        contextToken += 1;
      }
      return createSession(ownerUserId, generation, contextToken, draftClock);
    },
    schedule(session, callback, delay) {
      cancelScheduled();
      if (!session?.isCurrent()) return null;
      scheduledTimer = setTimer(() => {
        scheduledTimer = null;
        if (session.isCurrent()) callback();
      }, delay);
      return scheduledTimer;
    },
    cancelScheduled,
  };
}

export function createStudioTaskActionContext(taskId, getCurrentTaskId) {
  const capturedTaskId = String(taskId ?? "").trim();
  const isCurrent = () => (
    !!capturedTaskId
    && String(getCurrentTaskId?.() ?? "").trim() === capturedTaskId
  );

  return {
    taskId: capturedTaskId,
    isCurrent,
    commit(apply) {
      if (!isCurrent()) return null;
      return apply();
    },
  };
}

export function createStudioOwnerRequestContext(getOwnerSession) {
  let generation = 0;

  return {
    invalidate() {
      generation += 1;
    },
    capture() {
      const capturedGeneration = generation;
      const ownerSession = getOwnerSession?.();
      const isCurrent = () => (
        capturedGeneration === generation
        && ownerSession?.isCurrent() === true
      );
      return {
        isCurrent,
        commit(apply) {
          if (!isCurrent()) return null;
          return ownerSession.commit(apply);
        },
      };
    },
  };
}

export function createLatestOnlyStudioOwnerRequest(getOwnerSession) {
  const requestContext = createStudioOwnerRequestContext(getOwnerSession);

  return {
    invalidate() {
      requestContext.invalidate();
    },
    async run(request, apply) {
      requestContext.invalidate();
      const ownerRequest = requestContext.capture();
      const value = await request();
      return ownerRequest.commit(() => apply(value));
    },
  };
}

export function createLatestOnlyDraftWriter(write) {
  let active = null;
  let pending = null;
  let owner = "";
  let generation = 0;
  let closed = false;

  function cancellation(reason) {
    return { canceled: true, reason };
  }

  function cancelEntry(entry, reason) {
    if (!entry) return false;
    const result = cancellation(reason);
    entry.waiters.forEach(({ resolve }) => resolve(result));
    return true;
  }

  function cancelPending(reason = "pending_canceled") {
    if (!pending) return false;
    const entry = pending;
    pending = null;
    return cancelEntry(entry, reason);
  }

  function invalidateOwner(reason) {
    generation += 1;
    if (active && !active.started) active.cancelReason = reason;
    cancelPending(reason);
  }

  function isCurrentEntry(entry) {
    return !closed && entry.generation === generation && entry.owner === owner;
  }

  function start(entry) {
    active = entry;
    Promise.resolve()
      .then(() => {
        if (entry.cancelReason || !isCurrentEntry(entry)) {
          return cancellation(entry.cancelReason || "owner_changed");
        }
        entry.started = true;
        return write(entry.payload, entry.owner);
      })
      .then(
        (value) => entry.waiters.forEach(({ resolve }) => resolve(value)),
        (error) => entry.waiters.forEach(({ reject }) => reject(error)),
      )
      .finally(() => {
        if (active === entry) active = null;
        if (!pending) return;
        const next = pending;
        pending = null;
        if (!isCurrentEntry(next)) {
          cancelEntry(next, closed ? "writer_closed" : "owner_changed");
          return;
        }
        start(next);
      });
  }

  function enqueue(payload, entryOwner = owner) {
    return new Promise((resolve, reject) => {
      const normalizedOwner = normalizedUserId(entryOwner);
      if (closed) {
        resolve(cancellation("writer_closed"));
        return;
      }
      if (normalizedOwner !== owner) {
        resolve(cancellation("owner_mismatch"));
        return;
      }
      if (!active) {
        start({
          payload,
          owner: normalizedOwner,
          generation,
          started: false,
          waiters: [{ resolve, reject }],
        });
        return;
      }
      if (!pending) {
        pending = {
          payload,
          owner: normalizedOwner,
          generation,
          started: false,
          waiters: [],
        };
      }
      pending.payload = payload;
      pending.waiters.push({ resolve, reject });
    });
  }

  enqueue.setOwner = (nextOwner) => {
    if (closed) return false;
    const normalizedOwner = normalizedUserId(nextOwner);
    if (normalizedOwner === owner) return true;
    invalidateOwner("owner_changed");
    owner = normalizedOwner;
    return true;
  };
  enqueue.cancelOwner = (ownerToCancel) => {
    const normalizedOwner = normalizedUserId(ownerToCancel);
    if (!normalizedOwner || normalizedOwner !== owner) return false;
    invalidateOwner("owner_canceled");
    owner = "";
    return true;
  };
  enqueue.cancelPending = cancelPending;
  enqueue.close = () => {
    if (closed) return false;
    invalidateOwner("writer_closed");
    closed = true;
    owner = "";
    return true;
  };
  return enqueue;
}

export function mergeStudioWorkspaceLayers(
  baseWorkspaces,
  {
    adminImagePatch = {},
    localDraft = null,
    cloudDraft = null,
    imageModes = ["image", "image_edit"],
  } = {},
) {
  const merged = Object.fromEntries(
    Object.entries(baseWorkspaces || {}).map(([mode, current]) => [mode, { ...(current || {}) }]),
  );
  for (const mode of imageModes) {
    if (!Object.prototype.hasOwnProperty.call(merged, mode)) continue;
    merged[mode] = { ...merged[mode], ...adminImagePatch };
  }
  for (const draft of orderedUserDraftLayers(localDraft, cloudDraft)) {
    applyWorkspaceDraftLayer(merged, draft);
  }
  return merged;
}

export function mergeStudioLiveWorkspaceState(baselineWorkspaces, currentWorkspaces, restoredWorkspaces) {
  const merged = Object.fromEntries(
    Object.entries(currentWorkspaces || {}).map(([mode, current]) => [mode, { ...(current || {}) }]),
  );
  for (const [mode, restored] of Object.entries(restoredWorkspaces || {})) {
    if (!restored || typeof restored !== "object" || Array.isArray(restored)) continue;
    const baseline = baselineWorkspaces?.[mode] || {};
    const current = currentWorkspaces?.[mode] || {};
    const next = { ...current };
    for (const [field, value] of Object.entries(restored)) {
      if (isDeepEqual(current[field], baseline[field])) next[field] = value;
    }
    merged[mode] = next;
  }
  return merged;
}

export function mergeStudioDraftMetadataLayers(localDraft, cloudDraft) {
  const restored = {};
  for (const draft of orderedUserDraftLayers(localDraft, cloudDraft)) {
    for (const field of STUDIO_METADATA_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(draft, field)) restored[field] = draft[field];
    }
  }
  return restored;
}

export function mergeStudioLiveMetadataState(baseline, current, restored) {
  const merged = { ...(current || {}) };
  for (const field of STUDIO_METADATA_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(restored || {}, field)) continue;
    if (isDeepEqual(current?.[field], baseline?.[field])) merged[field] = restored[field];
  }
  return merged;
}

export function mergeStudioLiveDraftState(
  baseline,
  current,
  { restoredWorkspaces = {}, restoredMetadata = {} } = {},
) {
  return {
    ...mergeStudioLiveMetadataState(baseline, current, restoredMetadata),
    workspaces: mergeStudioLiveWorkspaceState(
      baseline?.workspaces,
      current?.workspaces,
      restoredWorkspaces,
    ),
  };
}

export function canApplyStudioPromptTransfer(baseline, current, targetMode) {
  return (
    isDeepEqual(current?.creationMode, baseline?.creationMode)
    && isDeepEqual(
      current?.workspaces?.[targetMode]?.prompt,
      baseline?.workspaces?.[targetMode]?.prompt,
    )
  );
}

export function canApplyStudioVariationTransfer(baseline, current) {
  return (
    STUDIO_METADATA_FIELDS.every((field) => isDeepEqual(current?.[field], baseline?.[field]))
    && isDeepEqual(current?.workspaces?.image_edit, baseline?.workspaces?.image_edit)
  );
}

export function applyStudioVariationTransferState(
  baseline,
  current,
  { metadataPatch = {}, workspacePatch = {} } = {},
) {
  if (!canApplyStudioVariationTransfer(baseline, current)) {
    return { applied: false, state: current };
  }
  const nextMetadata = Object.fromEntries(
    STUDIO_METADATA_FIELDS
      .filter((field) => Object.prototype.hasOwnProperty.call(metadataPatch, field))
      .map((field) => [field, metadataPatch[field]]),
  );
  const nextWorkspacePatch = { ...(workspacePatch || {}) };
  return {
    applied: true,
    metadataPatch: nextMetadata,
    workspacePatch: nextWorkspacePatch,
    state: {
      ...(current || {}),
      ...nextMetadata,
      workspaces: {
        ...(current?.workspaces || {}),
        image_edit: {
          ...(current?.workspaces?.image_edit || {}),
          ...nextWorkspacePatch,
        },
      },
    },
  };
}
