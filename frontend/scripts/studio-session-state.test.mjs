import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sessionState = await import("../lib/studioSession.js").catch(() => ({}));
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const taskTrackingSource = readFileSync(join(root, "hooks/useTaskTracking.js"), "utf8");
const mediaUploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const referenceParsingSource = readFileSync(join(root, "hooks/useReferenceParsing.js"), "utf8");
const generationSubmitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const promptsSource = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");
const historySource = readFileSync(join(root, "app/history/page.jsx"), "utf8");
const profileSource = readFileSync(join(root, "app/profile/page.jsx"), "utf8");
const packageJson = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
    has(key) {
      return values.has(key);
    },
  };
}

assert.equal(
  typeof sessionState.saveStudioUserDraft,
  "function",
  "studio drafts need an authenticated user-scoped storage writer",
);
assert.match(pageSource, /saveStudioUserDraft\(/, "Studio should persist session drafts through the user-scoped writer");
assert.match(
  pageSource,
  /localDraft\s*=\s*readStudioUserDraft\(window\.localStorage,\s*STUDIO_SESSION_DRAFT_KEY,\s*u\?\.id\)/,
  "Studio should read the authenticated user's local draft without consuming it before cloud loading finishes",
);
assert.doesNotMatch(
  pageSource,
  /localDraft\s*=\s*takeStudioUserDraft\(/,
  "Studio initialization must not consume the local draft before it has been applied",
);
assert.doesNotMatch(
  pageSource,
  /localStorage\.setItem\(STUDIO_SESSION_DRAFT_KEY/,
  "Studio must not write the legacy global session-draft key",
);
assert.doesNotMatch(
  pageSource,
  /localStorage\.getItem\(STUDIO_SESSION_DRAFT_KEY/,
  "Studio must not restore the legacy global session-draft key",
);
assert.equal(
  typeof sessionState.takeStudioUserDraft,
  "function",
  "studio drafts need an authenticated user-scoped one-shot reader",
);
assert.equal(typeof sessionState.readStudioUserDraft, "function");
assert.equal(typeof sessionState.removeStudioUserDraft, "function");

const sessionKey = "studio_session_draft_v1";
const legacyDraft = {
  workspaces: {
    image: {
      prompt: "user A private prompt",
      assets: [{ url: "/api/uploads/user-a.png" }],
      productProfile: { final_text: "user A private subject profile" },
    },
  },
};
const storage = memoryStorage({ [sessionKey]: JSON.stringify(legacyDraft) });

assert.equal(
  sessionState.takeStudioUserDraft(storage, sessionKey, null),
  null,
  "unauthenticated Studio must not restore a draft",
);
assert.equal(storage.has(sessionKey), false, "legacy global Studio drafts must be discarded, not claimed by the next login");
assert.equal(
  sessionState.saveStudioUserDraft(storage, sessionKey, null, legacyDraft),
  false,
  "unauthenticated Studio must not persist sensitive workspace state",
);

assert.equal(sessionState.saveStudioUserDraft(storage, sessionKey, 101, legacyDraft), true);
assert.equal(
  sessionState.takeStudioUserDraft(storage, sessionKey, 202),
  null,
  "user B must not restore user A's prompt, assets, or subject profile",
);
assert.deepEqual(
  sessionState.takeStudioUserDraft(storage, sessionKey, 101)?.workspaces,
  legacyDraft.workspaces,
  "the owning user should recover the scoped draft once",
);
assert.equal(
  sessionState.takeStudioUserDraft(storage, sessionKey, 101),
  null,
  "a recovered local draft should be consumed exactly once",
);

assert.equal(sessionState.saveStudioUserDraft(storage, "transfer", 101, legacyDraft), true);
assert.deepEqual(
  sessionState.readStudioUserDraft(storage, "transfer", 101)?.workspaces,
  legacyDraft.workspaces,
  "validated transfer drafts should remain available until the page applies them",
);
assert.equal(storage.has(sessionState.studioUserDraftKey("transfer", 101)), true);
assert.equal(sessionState.removeStudioUserDraft(storage, "transfer", 101), true);
assert.equal(storage.has(sessionState.studioUserDraftKey("transfer", 101)), false);

assert.equal(
  typeof sessionState.mergeStudioWorkspaceLayers,
  "function",
  "Studio initialization needs one deterministic field-level merge helper",
);

assert.equal(
  typeof sessionState.createLatestOnlyDraftWriter,
  "function",
  "Studio cloud saves need a latest-only serialized writer",
);
assert.equal(
  typeof sessionState.createStudioDraftClock,
  "function",
  "Studio drafts need an instance-scoped logical clock",
);
assert.equal(
  typeof sessionState.createStudioOwnerSessionCoordinator,
  "function",
  "Studio owner changes need one executable lifecycle coordinator",
);
assert.equal(
  typeof sessionState.createLatestOnlyStudioOwnerRequest,
  "function",
  "owner-sensitive refreshes need a latest-only executable request guard",
);
assert.equal(
  typeof sessionState.createStudioOwnerRequestContext,
  "function",
  "owner-sensitive async work needs a reusable executable capture/reset/commit guard",
);
assert.equal(
  typeof sessionState.createStudioTaskActionContext,
  "function",
  "task refresh and cancellation need an executable captured-task guard",
);
for (const [name, source] of [
  ["task tracking", taskTrackingSource],
  ["media upload", mediaUploadSource],
  ["reference parsing", referenceParsingSource],
  ["generation submit", generationSubmitSource],
]) {
  assert.match(
    source,
    /getOwnerSession/,
    `${name} must capture the current owner session before asynchronous work`,
  );
  assert.match(
    source,
    /createStudioOwnerRequestContext/,
    `${name} must use the shared executable owner request context`,
  );
  assert.match(
    source,
    /ownerRequestContextRef\.current\.capture\(\)/,
    `${name} must capture the shared owner request context before async work`,
  );
  assert.match(
    source,
    /ownerRequest\.isCurrent\(\)/,
    `${name} must check the captured production guard after awaiting`,
  );
}
assert.match(
  pageSource,
  /createStudioOwnerRequestContext/,
  "Studio asset actions must use the shared executable owner request context",
);
assert.match(
  pageSource,
  /async function unlock[\s\S]*?\.capture\(\)[\s\S]*?await requestQuoteConfirmation[\s\S]*?ownerRequest\.isCurrent\(\)[\s\S]*?api\.unlock[\s\S]*?ownerRequest\.commit/,
  "asset unlock must capture the owner before quote confirmation and commit through the shared guard",
);
assert.match(
  pageSource,
  /setTask\(\s*\(prev\)\s*=>\s*prev\?\.id\s*===\s*capturedTaskId\s*\?\s*nextTask\s*:\s*prev\s*,?\s*\)/,
  "unlock task refresh must not replace a newer task for the same owner",
);
assert.match(
  pageSource,
  /async function download[\s\S]*?\.capture\(\)[\s\S]*?await downloadBlob[\s\S]*?ownerRequest\.commit/,
  "asset download must not write messages or busy state after its owner becomes stale",
);
assert.match(taskTrackingSource, /resetOwnerTracking/, "task tracking must expose a complete owner reset");
assert.match(
  taskTrackingSource,
  /createStudioTaskActionContext/,
  "task refresh and cancellation must use the executable captured-task guard",
);
assert.match(mediaUploadSource, /resetOwnerMediaUpload/, "media upload must invalidate pending owner requests");
assert.match(referenceParsingSource, /resetOwnerReferenceParsing/, "reference parsing must invalidate pending owner requests");
assert.match(generationSubmitSource, /resetOwnerGenerationSubmit/, "generation submit must invalidate pending owner requests");
assert.match(
  pageSource,
  /createLatestOnlyStudioOwnerRequest\(\s*\(\)\s*=>\s*studioOwnerSessionRef\.current\s*,?\s*\)/,
  "Studio should guard current-user refreshes with the active owner session",
);
for (const resetName of [
  "resetOwnerTracking",
  "resetOwnerMediaUpload",
  "resetOwnerReferenceParsing",
  "resetOwnerGenerationSubmit",
]) {
  assert.match(
    pageSource,
    new RegExp(`${resetName}\\(\\)`),
    `Studio owner reset must call ${resetName} synchronously`,
  );
}
assert.equal(
  (pageSource.match(/getOwnerSession:\s*\(\)\s*=>\s*studioOwnerSessionRef\.current/g) || []).length,
  4,
  "all four owner-sensitive hooks must receive the current Studio owner session",
);
assert.equal(
  typeof sessionState.mergeStudioLiveDraftState,
  "function",
  "Studio restore needs an executable whole-state transition",
);
assert.equal(
  typeof sessionState.applyStudioVariationTransferState,
  "function",
  "variation transfer needs an executable atomic state transition",
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flushAsync = () => new Promise((resolve) => setImmediate(resolve));

{
  const writes = [];
  const writer = sessionState.createLatestOnlyDraftWriter((payload) => {
    const pending = deferred();
    writes.push({ payload, ...pending });
    return pending.promise;
  });

  const a = writer("A");
  await Promise.resolve();
  const b = writer("B");
  const c = writer("C");
  assert.deepEqual(writes.map((entry) => entry.payload), ["A"]);

  writes[0].resolve("saved A");
  assert.equal(await a, "saved A");
  await flushAsync();
  assert.deepEqual(
    writes.map((entry) => entry.payload),
    ["A", "C"],
    "B should be replaced before it starts while both B and C callers follow the C write",
  );
  writes[1].resolve("saved C");
  assert.equal(await b, "saved C");
  assert.equal(await c, "saved C");
}

{
  const writes = [];
  const writer = sessionState.createLatestOnlyDraftWriter((payload) => {
    const pending = deferred();
    writes.push({ payload, ...pending });
    return pending.promise;
  });
  const failure = new Error("A failed");
  const a = writer("A");
  const observedA = a.catch((error) => error);
  await Promise.resolve();
  const c = writer("C");
  writes[0].reject(failure);
  assert.equal(await observedA, failure, "only the active A caller should observe A's failure");
  await flushAsync();
  assert.deepEqual(writes.map((entry) => entry.payload), ["A", "C"]);
  writes[1].resolve("saved C after failure");
  assert.equal(await c, "saved C after failure", "A failure must not stop the pending latest write");
}

{
  const writes = [];
  const writer = sessionState.createLatestOnlyDraftWriter((payload, owner) => {
    const pending = deferred();
    writes.push({ payload, owner, ...pending });
    return pending.promise;
  });
  writer.setOwner("user-a");
  const activeA = writer("A active", "user-a");
  await Promise.resolve();
  const pendingA = writer("A pending", "user-a");
  writer.cancelOwner("user-a");
  writer.setOwner("user-b");

  assert.deepEqual(
    await pendingA,
    { canceled: true, reason: "owner_canceled" },
    "an old owner's pending promise should resolve with an explicit cancellation result",
  );
  writes[0].resolve("saved A active");
  assert.equal(await activeA, "saved A active");
  await flushAsync();
  assert.deepEqual(
    writes.map(({ payload, owner }) => ({ payload, owner })),
    [{ payload: "A active", owner: "user-a" }],
    "releasing A's active write must not start A's canceled pending write under user B",
  );

  const writeB = writer("B active", "user-b");
  await flushAsync();
  assert.deepEqual(writes[1] && { payload: writes[1].payload, owner: writes[1].owner }, {
    payload: "B active",
    owner: "user-b",
  });
  writes[1].resolve("saved B");
  assert.equal(await writeB, "saved B");
}

function clockTest(name, run) {
  try {
    run();
  } catch (error) {
    error.message = `${name}: ${error.message}`;
    throw error;
  }
}

clockTest("draft clocks isolate restore provenance across component owners", () => {
  const now = 1_700_000_000_000;
  const ownerAClock = sessionState.createStudioDraftClock();
  ownerAClock.bind("owner-a", 1).observeRestore({ cloudDraft: { savedAt: now + 2 } }, now);

  const ownerBClock = sessionState.createStudioDraftClock();
  const ownerB = ownerBClock.bind("owner-b", 1);
  assert.deepEqual(
    [ownerB.next(now), ownerB.next(now), ownerB.next(now), ownerB.next(now)],
    [now, now + 1, now + 2, now + 3],
  );
});

clockTest("owner rebinding resets issued clock and restore provenance", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  clock.bind("owner-a", 1).observeRestore({ cloudDraft: { savedAt: now + 2 } }, now);
  assert.equal(clock.bind("owner-b", 1).next(now), now);
});

clockTest("same-owner generation rollover preserves value but clears restore provenance", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  clock.bind("owner-a", 1).observeRestore({ cloudDraft: { savedAt: now + 2 } }, now);
  assert.equal(clock.bind("owner-a", 2).next(now), now + 3);
});

clockTest("older cloud draft does not relabel a local logical clock as restored", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const bound = clock.bind("owner-a", 1);
  assert.deepEqual(
    [bound.next(now), bound.next(now), bound.next(now)],
    [now, now + 1, now + 2],
  );
  bound.observeRestore({ cloudDraft: { savedAt: now - 10 } }, now);
  assert.equal(bound.next(now), now + 3);
});

clockTest("future restore thaws once and remains strictly increasing", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const bound = clock.bind("owner-a", 1);
  bound.observeRestore({ cloudDraft: { savedAt: now + 2 } }, now);
  assert.deepEqual([bound.next(now), bound.next(now)], [now, now + 1]);
});

clockTest("older generation calls are ignored", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const oldGeneration = clock.bind("owner-a", 1);
  const currentGeneration = clock.bind("owner-a", 2);
  assert.equal(
    oldGeneration.observeRestore({ cloudDraft: { savedAt: now + 20 } }, now),
    null,
  );
  assert.equal(oldGeneration.next(now), null);
  assert.equal(currentGeneration.next(now), now);
  assert.equal(clock.bind("owner-a", 1).next(now), null);
  assert.equal(currentGeneration.next(now), now + 1, "a late bind must not roll back generation 2");
});

clockTest("cross-owner lower generation cannot replace the current owner", () => {
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const ownerA = clock.bind("owner-a", 5);
  assert.equal(ownerA.next(now), now);

  const staleOwnerB = clock.bind("owner-b", 1);
  assert.equal(staleOwnerB.observeRestore({ cloudDraft: { savedAt: now + 20 } }, now), null);
  assert.equal(staleOwnerB.next(now), null);
  assert.equal(ownerA.next(now), now + 1);
});

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(callback) {
      const id = nextId;
      nextId += 1;
      pending.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    flush() {
      const callbacks = [...pending.values()];
      pending.clear();
      callbacks.forEach((callback) => callback());
    },
  };
}

{
  const now = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const coordinator = sessionState.createStudioOwnerSessionCoordinator({ clock });
  const cloudDraft = deferred();
  const consumedStorage = [];
  let uiOwner = "owner-a-loading";
  const ownerA = coordinator.bindOwner("owner-a", 1);
  const restoreOwnerA = cloudDraft.promise.then((draft) => ownerA.commit(() => {
    uiOwner = "owner-a-restored";
    consumedStorage.push("owner-a");
    ownerA.observeRestore({ cloudDraft: draft }, now);
  }));

  const ownerB = coordinator.bindOwner("owner-b", 2);
  uiOwner = "owner-b-clean";
  cloudDraft.resolve({ savedAt: now + 10 });
  await restoreOwnerA;

  assert.equal(uiOwner, "owner-b-clean", "A delayed cloud response must not mutate B's UI");
  assert.deepEqual(consumedStorage, [], "A delayed cloud response must not consume A storage under B");
  assert.equal(ownerA.observeRestore({ cloudDraft: { savedAt: now + 20 } }, now), null);
  assert.equal(ownerB.next(now), now, "A delayed cloud response must not advance B's clock");
}

{
  const timers = fakeTimers();
  const coordinator = sessionState.createStudioOwnerSessionCoordinator({
    clock: sessionState.createStudioDraftClock(),
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  });
  const writes = [];
  const ownerA = coordinator.bindOwner("owner-a", 1);
  coordinator.schedule(ownerA, () => writes.push({ owner: "owner-a", prompt: "A workspace" }), 1_600);

  const ownerB = coordinator.bindOwner("owner-b", 2);
  timers.flush();
  assert.deepEqual(writes, [], "switching to B must cancel A's pending automatic cloud sync");

  coordinator.schedule(ownerB, () => writes.push({ owner: "owner-b", prompt: "B workspace" }), 1_600);
  timers.flush();
  assert.deepEqual(writes, [{ owner: "owner-b", prompt: "B workspace" }]);
}

{
  const coordinator = sessionState.createStudioOwnerSessionCoordinator();
  const ownerA = coordinator.bindOwner("owner-a", 1);
  const taskResponse = deferred();
  const state = {
    task: { id: "task-a", status: "running" },
    runningSnapshot: { category: "image" },
    trackingLost: true,
    backgroundTasks: [{ id: "background-a" }],
    activeId: "task-a",
  };
  const pendingPoll = taskResponse.promise.then((nextTask) => ownerA.commit(() => {
    state.task = nextTask;
    state.trackingLost = false;
  }));

  coordinator.bindOwner("owner-b", 2);
  Object.assign(state, {
    task: null,
    runningSnapshot: null,
    trackingLost: false,
    backgroundTasks: [],
    activeId: null,
  });
  taskResponse.resolve({ id: "task-a", status: "done" });
  await pendingPoll;
  assert.deepEqual(state, {
    task: null,
    runningSnapshot: null,
    trackingLost: false,
    backgroundTasks: [],
    activeId: null,
  }, "A's pending active poll must not repopulate task tracking after switching to B");

  const restoredBTask = { id: "task-b", status: "running" };
  if (!state.task) state.task = restoredBTask;
  assert.equal(state.task, restoredBTask, "B restore must not be blocked by stale A task state");
}

{
  const coordinator = sessionState.createStudioOwnerSessionCoordinator();
  const ownerA = coordinator.bindOwner("owner-a", 1);
  const uploadResponse = deferred();
  const profileResponse = deferred();
  const workspace = { asset: null, profile: null };
  const upload = uploadResponse.promise.then((asset) => ownerA.commit(() => {
    workspace.asset = asset;
  }));
  const profile = profileResponse.promise.then((result) => ownerA.commit(() => {
    workspace.profile = result;
  }));

  coordinator.bindOwner("owner-b", 2);
  uploadResponse.resolve({ id: "asset-a" });
  profileResponse.resolve({ final_text: "profile-a" });
  await Promise.all([upload, profile]);
  assert.deepEqual(
    workspace,
    { asset: null, profile: null },
    "A upload and profile responses must not write B workspace",
  );
}

{
  const coordinator = sessionState.createStudioOwnerSessionCoordinator();
  const ownerA = coordinator.bindOwner("owner-a", 1);
  const generateResponse = deferred();
  let task = null;
  const generate = generateResponse.promise.then((nextTask) => ownerA.commit(() => {
    task = nextTask;
  }));
  coordinator.bindOwner("owner-b", 2);
  generateResponse.resolve({ id: "generated-by-a" });
  await generate;
  assert.equal(task, null, "A generate response must not install a task under B");
}

{
  const coordinator = sessionState.createStudioOwnerSessionCoordinator();
  const sessionRef = { current: coordinator.bindOwner("owner-a", 1) };
  const latestRefresh = sessionState.createLatestOnlyStudioOwnerRequest(
    () => sessionRef.current,
  );
  const ownerAResponse = deferred();
  let currentOwner = "owner-a";
  const refreshA = latestRefresh.run(
    () => ownerAResponse.promise,
    (nextOwner) => { currentOwner = nextOwner; },
  );

  sessionRef.current = coordinator.bindOwner("owner-b", 2);
  latestRefresh.invalidate();
  await latestRefresh.run(
    () => Promise.resolve("owner-b"),
    (nextOwner) => { currentOwner = nextOwner; },
  );
  ownerAResponse.resolve("owner-a");
  await refreshA;
  assert.equal(currentOwner, "owner-b", "a late A refresh must not roll the active owner back from B");
}

{
  const coordinator = sessionState.createStudioOwnerSessionCoordinator();
  const sessionRef = { current: coordinator.bindOwner("owner-a", 1) };
  const requestContext = sessionState.createStudioOwnerRequestContext(
    () => sessionRef.current,
  );
  const state = {
    lightbox: { id: "asset-b" },
    task: { id: "task-b" },
    busy: new Set(["asset-a"]),
  };
  const resetResponse = deferred();
  let resetWriteApplied = false;
  const resetRequest = requestContext.capture();
  const pendingReset = resetResponse.promise.then(() => resetRequest.commit(() => {
    resetWriteApplied = true;
  }));
  requestContext.invalidate();
  resetResponse.resolve();
  await pendingReset;
  assert.equal(
    resetWriteApplied,
    false,
    "explicit owner reset must invalidate a captured request even before owner rebinding",
  );

  const unlockResponse = deferred();
  const ownerARequest = requestContext.capture();
  const pendingUnlock = unlockResponse.promise.then((updated) => ownerARequest.commit(() => {
    state.lightbox = updated;
    state.task = { id: "task-a" };
    state.busy.delete("asset-a");
  }));

  sessionRef.current = coordinator.bindOwner("owner-b", 2);
  state.busy.clear();
  unlockResponse.resolve({ id: "asset-a", unlocked: true });
  await pendingUnlock;
  assert.deepEqual(
    state,
    {
      lightbox: { id: "asset-b" },
      task: { id: "task-b" },
      busy: new Set(),
    },
    "a deferred A unlock must not write B lightbox, task, or busy state after reset",
  );

  const ownerBRequest = requestContext.capture();
  assert.equal(ownerBRequest.commit(() => "owner-b-applied"), "owner-b-applied");
}

{
  let currentTask = { id: "task-1", status: "running" };
  const taskAction = sessionState.createStudioTaskActionContext(
    currentTask.id,
    () => currentTask?.id,
  );
  const taskOneResponse = deferred();
  const pendingRefresh = taskOneResponse.promise.then((nextTask) => taskAction.commit(() => {
    currentTask = nextTask;
  }));

  currentTask = { id: "task-2", status: "running" };
  taskOneResponse.resolve({ id: "task-1", status: "done" });
  await pendingRefresh;
  assert.deepEqual(
    currentTask,
    { id: "task-2", status: "running" },
    "a late T1 response must not replace a newer T2 for the same owner",
  );
}

{
  const sameMillisecondNow = 1_700_000_000_000;
  const clock = sessionState.createStudioDraftClock();
  const bound = clock.bind("owner-a", 1);
  const savedAtSequence = Array.from({ length: 4 }, () => bound.next(sameMillisecondNow));
  assert.deepEqual(
    savedAtSequence,
    [
      sameMillisecondNow,
      sameMillisecondNow + 1,
      sameMillisecondNow + 2,
      sameMillisecondNow + 3,
    ],
    "repeated saves in one millisecond must keep a strictly increasing logical clock",
  );
}
const maxFutureSkewMs = 24 * 60 * 60 * 1000;
const fixedNow = 1_700_000_000_000;
const legacyMaxStudioSavedAt = 253_402_300_799_999;
assert.equal(sessionState.STUDIO_DRAFT_MAX_FUTURE_SKEW_MS, maxFutureSkewMs);
for (const invalidSavedAt of [
  -1,
  0,
  1.5,
  2 ** 53,
  1e308,
  legacyMaxStudioSavedAt,
  Number.MAX_SAFE_INTEGER,
]) {
  assert.equal(
    sessionState.isValidStudioDraftSavedAt(invalidSavedAt, fixedNow),
    false,
    `${invalidSavedAt} must not be accepted as a Studio clock`,
  );
  assert.equal(
    sessionState.createStudioDraftClock()
      .bind("owner-a", 1)
      .observeRestore({ cloudDraft: { savedAt: invalidSavedAt } }, fixedNow),
    0,
    "invalid savedAt values must not advance the observed clock",
  );
  assert.equal(
    sessionState.createStudioDraftClock()
      .bind("owner-a", 1)
      .observeRestore({ cloudDraft: { savedAt: invalidSavedAt } }, 500),
    0,
    "invalid prior clocks must be ignored",
  );
  assert.equal(
    sessionState.createStudioDraftClock().bind("owner-a", 1).next(500),
    500,
    "invalid prior clocks must not freeze the next save",
  );
}
assert.equal(
  sessionState.isValidStudioDraftSavedAt(fixedNow + maxFutureSkewMs + 1, fixedNow),
  false,
  "a clock beyond the configured future-skew boundary must be rejected",
);
const legacyMaxClock = sessionState.createStudioDraftClock();
const legacyMaxBoundClock = legacyMaxClock.bind("owner-a", 1);
legacyMaxBoundClock.observeRestore({ cloudDraft: { savedAt: legacyMaxStudioSavedAt } }, fixedNow);
const firstSaveAfterLegacyMax = legacyMaxBoundClock.next(fixedNow);
assert.equal(firstSaveAfterLegacyMax, fixedNow, "a legacy MAX clock must thaw on the next save");
assert.equal(
  legacyMaxBoundClock.next(fixedNow),
  fixedNow + 1,
  "two consecutive saves after a legacy MAX clock must both produce increasing clocks",
);
assert.equal(
  sessionState.isValidStudioDraftSavedAt(fixedNow + maxFutureSkewMs, fixedNow),
  true,
  "the configured future-skew boundary should remain valid",
);
const futureBoundarySavedAt = fixedNow + maxFutureSkewMs;
const futureBoundaryClock = sessionState.createStudioDraftClock();
const futureBoundaryBoundClock = futureBoundaryClock.bind("owner-a", 1);
const observedFutureBoundary = futureBoundaryBoundClock.observeRestore({
  cloudDraft: { savedAt: futureBoundarySavedAt },
}, fixedNow);
assert.equal(
  observedFutureBoundary,
  futureBoundarySavedAt,
  "restore should observe an exact valid future-skew boundary",
);
const firstSaveAfterFutureBoundary = futureBoundaryBoundClock.next(fixedNow);
const secondSaveAfterFutureBoundary = futureBoundaryBoundClock.next(fixedNow);
assert.deepEqual(
  [firstSaveAfterFutureBoundary, secondSaveAfterFutureBoundary],
  [fixedNow, fixedNow + 1],
  "a restored future boundary must thaw while consecutive same-millisecond saves remain increasing",
);
for (const savedAt of [firstSaveAfterFutureBoundary, secondSaveAfterFutureBoundary]) {
  assert.equal(Number.isSafeInteger(savedAt), true);
  assert.equal(
    sessionState.isValidStudioDraftSavedAt(savedAt, fixedNow),
    true,
    "every save after a restored future boundary must remain a valid Studio clock",
  );
}
const adoptedRestoreClock = sessionState.createStudioDraftClock();
const adoptedRestore = adoptedRestoreClock.bind("owner-a", 1);
assert.equal(
  adoptedRestore.observeRestore({
    localDraft: { savedAt: 200 },
    cloudDraft: { savedAt: 100 },
  }, 50),
  200,
  "initialization should advance the clock to the newest local/cloud draft",
);
assert.equal(
  adoptedRestore.observeRestore({
    localDraft: { savedAt: 200 },
    cloudDraft: { savedAt: 300 },
    promptDraft: { savedAt: 9_000 },
    promptTransferApplied: true,
    variationDraft: { savedAt: 10_000 },
    variationTransferApplied: false,
  }, 50),
  9_000,
  "only adopted transfers should advance the restore clock, including future timestamps",
);
const transferClock = sessionState.createStudioDraftClock();
const transferBoundClock = transferClock.bind("owner-a", 1);
transferBoundClock.observeRestore({
  promptDraft: { savedAt: 9_000 },
  promptTransferApplied: true,
}, 100);
assert.equal(
  transferBoundClock.next(100),
  100,
  "an adopted future-dated transfer must not freeze the next save ahead of the current clock",
);

const merged = sessionState.mergeStudioWorkspaceLayers(
  {
    image: { ratio: "1:1", imageQuality: "1k", n: 1, prompt: "" },
    image_edit: { ratio: "1:1", imageQuality: "1k", n: 1, prompt: "" },
    video: { ratio: "16:9", prompt: "base video" },
  },
  {
    adminImagePatch: { ratio: "4:5", imageQuality: "2k", n: 4 },
    localDraft: {
      workspaces: {
        image: { ratio: "3:2", prompt: "local prompt" },
        video: { prompt: "local video" },
      },
    },
    cloudDraft: {
      workspaces: {
        image: { prompt: "cloud prompt", n: 2 },
      },
    },
  },
);
assert.deepEqual(
  merged.image,
  { ratio: "3:2", imageQuality: "2k", n: 2, prompt: "local prompt" },
  "equal-time local fields must override cloud fields while missing fields still fall back",
);
assert.deepEqual(
  merged.image_edit,
  { ratio: "4:5", imageQuality: "2k", n: 4, prompt: "" },
  "admin image defaults should apply when neither user draft supplies a field",
);
assert.equal(merged.video.prompt, "local video", "local non-image workspace fields should survive without a cloud override");

for (const [label, localSavedAt, cloudSavedAt, expectedPrompt] of [
  ["newer local", 300, 200, "local"],
  ["newer cloud", 200, 300, "cloud"],
  ["equal timestamp", 300, 300, "local"],
]) {
  const ordered = sessionState.mergeStudioWorkspaceLayers(
    { image: { prompt: "base", ratio: "1:1" } },
    {
      localDraft: { savedAt: localSavedAt, workspaces: { image: { prompt: "local" } } },
      cloudDraft: { savedAt: cloudSavedAt, workspaces: { image: { prompt: "cloud" } } },
    },
  );
  assert.equal(ordered.image.prompt, expectedPrompt, `${label} draft ordering should win deterministically`);
}

assert.equal(
  sessionState.mergeStudioWorkspaceLayers(
    { image: { prompt: "base" } },
    {
      localDraft: {
        savedAt: 2 ** 53,
        workspaces: { image: { prompt: "invalid huge local" } },
      },
      cloudDraft: {
        savedAt: 500,
        workspaces: { image: { prompt: "valid cloud" } },
      },
    },
  ).image.prompt,
  "valid cloud",
  "an invalid huge local timestamp must not outrank a valid cloud draft",
);

assert.equal(
  sessionState.mergeStudioWorkspaceLayers(
    { image: { prompt: "base" } },
    {
      localDraft: {
        savedAt: legacyMaxStudioSavedAt,
        workspaces: { image: { prompt: "legacy MAX local" } },
      },
      cloudDraft: {
        savedAt: Date.now() + maxFutureSkewMs - 1_000,
        workspaces: { image: { prompt: "valid boundary cloud" } },
      },
    },
  ).image.prompt,
  "valid boundary cloud",
  "an invalid local clock must not win by being coerced into the valid ordering domain",
);

{
  const baseline = {
    image: {
      prompt: "",
      ratio: "1:1",
      structured: { lighting: "base" },
      assets: [{ id: 1 }],
    },
  };
  const current = {
    image: {
      prompt: "user is typing",
      ratio: "1:1",
      structured: { lighting: "base" },
      assets: [{ id: 2 }],
    },
  };
  const restored = {
    image: {
      prompt: "late draft prompt",
      ratio: "16:9",
      structured: { camera: "restored as one field" },
      assets: [{ id: 3 }],
    },
  };
  assert.deepEqual(
    sessionState.mergeStudioLiveWorkspaceState(baseline, current, restored).image,
    {
      prompt: "user is typing",
      ratio: "16:9",
      structured: { camera: "restored as one field" },
      assets: [{ id: 2 }],
    },
    "late initialization should preserve edited fields and restore only untouched fields, treating objects/arrays atomically",
  );
}

{
  const baseline = {
    workspaces: {
      image: { prompt: "", ratio: "1:1" },
      image_edit: { prompt: "", ratio: "1:1" },
    },
    creationMode: "image",
    showNegative: false,
    refOpen: false,
    structOpen: true,
  };
  const current = {
    ...baseline,
    workspaces: {
      ...baseline.workspaces,
      image: { ...baseline.workspaces.image, prompt: "user typed while cloud was loading" },
    },
  };
  const restored = sessionState.mergeStudioWorkspaceLayers(baseline.workspaces, {
    cloudDraft: {
      savedAt: 500,
      workspaces: { image: { prompt: "late cloud prompt", ratio: "16:9" } },
    },
  });
  const next = sessionState.mergeStudioLiveDraftState(baseline, current, {
    restoredWorkspaces: restored,
    restoredMetadata: {},
  });
  assert.deepEqual(
    next.workspaces.image,
    { prompt: "user typed while cloud was loading", ratio: "16:9" },
    "a delayed cloud response must preserve the user's live edit while restoring untouched fields",
  );
}

{
  const baseline = {
    workspaces: {
      image: { prompt: "image" },
      image_edit: { prompt: "", ratio: "1:1", productAsset: null },
    },
    creationMode: "image",
    showNegative: true,
    refOpen: true,
    structOpen: true,
  };
  const transfer = {
    metadataPatch: {
      creationMode: "image_edit",
      showNegative: false,
      refOpen: false,
      structOpen: false,
    },
    workspacePatch: {
      prompt: "variation prompt",
      ratio: "4:5",
      productAsset: { id: 77, type: "image", url: "/api/assets/77" },
    },
  };
  const applied = sessionState.applyStudioVariationTransferState(baseline, baseline, transfer);
  assert.equal(applied.applied, true);
  assert.deepEqual(
    applied.state,
    {
      ...baseline,
      ...transfer.metadataPatch,
      workspaces: {
        ...baseline.workspaces,
        image_edit: { ...baseline.workspaces.image_edit, ...transfer.workspacePatch },
      },
    },
    "an accepted variation transfer must apply its workspace and top-level controls as one group",
  );

  const liveEdit = { ...baseline, refOpen: false };
  const rejected = sessionState.applyStudioVariationTransferState(baseline, liveEdit, transfer);
  assert.equal(rejected.applied, false);
  assert.deepEqual(
    rejected.state,
    liveEdit,
    "a conflicting live edit must reject the entire variation group without partial application",
  );
}

{
  const baseline = {
    creationMode: "image",
    showNegative: false,
    refOpen: false,
    structOpen: true,
  };
  const current = { ...baseline, showNegative: true };
  const restored = sessionState.mergeStudioDraftMetadataLayers(
    { savedAt: 300, creationMode: "video", refOpen: true },
    { savedAt: 200, creationMode: "image_edit", showNegative: false, structOpen: false },
  );
  assert.deepEqual(
    sessionState.mergeStudioLiveMetadataState(baseline, current, restored),
    {
      creationMode: "video",
      showNegative: true,
      refOpen: true,
      structOpen: false,
    },
    "top-level Studio controls should restore only explicitly supplied and still-untouched fields",
  );
}

{
  const baseline = {
    workspaces: { image: { prompt: "" }, video: { prompt: "" }, image_edit: { prompt: "" } },
    creationMode: "image",
    showNegative: false,
    refOpen: false,
    structOpen: true,
  };
  assert.equal(
    sessionState.canApplyStudioPromptTransfer(baseline, baseline, "video"),
    true,
    "an untouched target prompt and mode should accept the one-shot prompt transfer",
  );
  assert.equal(
    sessionState.canApplyStudioPromptTransfer(
      baseline,
      { ...baseline, workspaces: { ...baseline.workspaces, video: { prompt: "live edit" } } },
      "video",
    ),
    false,
    "a live edit in the target prompt must reject the transfer",
  );
  assert.equal(
    sessionState.canApplyStudioPromptTransfer(baseline, { ...baseline, creationMode: "video" }, "video"),
    false,
    "a live mode change must reject the transfer",
  );

  assert.equal(sessionState.canApplyStudioVariationTransfer(baseline, baseline), true);
  assert.equal(
    sessionState.canApplyStudioVariationTransfer(
      baseline,
      { ...baseline, workspaces: { ...baseline.workspaces, image_edit: { prompt: "live edit" } } },
    ),
    false,
    "variation transfer must reject atomically when any image_edit field changed",
  );
  assert.equal(
    sessionState.canApplyStudioVariationTransfer(baseline, { ...baseline, refOpen: true }),
    false,
    "variation transfer must reject atomically when any target top-level control changed",
  );
}

assert.match(pageSource, /mergeStudioWorkspaceLayers\(/, "Studio page must use the deterministic merge helper");
assert.match(pageSource, /Promise\.allSettled\(/, "me and config response order must not determine initialization state");
const localDraftReadIndex = pageSource.indexOf(
  "localDraft = readStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, u?.id)",
);
const cloudDraftReadIndex = pageSource.indexOf('const row = await api.getDraft("studio")');
const localDraftRemoveIndex = pageSource.indexOf(
  "removeStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, u?.id)",
);
assert.ok(
  localDraftReadIndex >= 0
    && cloudDraftReadIndex > localDraftReadIndex
    && localDraftRemoveIndex > cloudDraftReadIndex,
  "the local Studio draft should only be removed after the cloud request returns and the draft is applied",
);
assert.match(pageSource, /createLatestOnlyDraftWriter\(/, "both Studio cloud-save paths should share a serialized writer");
assert.match(
  pageSource,
  /studioDraftClockRef\.current\s*=\s*createStudioDraftClock\(\)/,
  "each Home instance should lazily create its own Studio draft clock",
);
assert.match(
  pageSource,
  /studioOwnerSessionCoordinatorRef\.current\s*=\s*createStudioOwnerSessionCoordinator\(/,
  "Home should connect owner lifecycle work to the executable coordinator",
);
assert.doesNotMatch(
  pageSource,
  /lastStudioDraftSavedAtRef|nextStudioDraftSavedAt|observeStudioRestoreSavedAt/,
  "the Studio page must not retain the module-shared draft-clock helpers or a separate numeric ref",
);
assert.match(
  pageSource,
  /cloudDraftWriterRef\.current\.setOwner\(ownerUserId\)/,
  "the cloud writer should bind pending writes to the authenticated owner",
);
assert.match(
  pageSource,
  /cloudDraftWriterRef\.current\.cancelOwner\(ownerUserId\)/,
  "owner changes and unmount cleanup should cancel the old owner's pending writes",
);
assert.match(
  pageSource,
  /cloudDraftWriterRef\.current\([^,]+,\s*me\?\.id\)/,
  "every cloud draft enqueue should carry the owner identity",
);
assert.match(pageSource, /mergeStudioLiveDraftState\(/, "Studio initialization should preserve live field edits");
assert.match(pageSource, /canApplyStudioPromptTransfer\(/, "prompt transfer should be guarded by its baseline");
assert.match(pageSource, /applyStudioVariationTransferState\(/, "variation transfer should be guarded atomically");
assert.match(
  pageSource,
  /const seq = \+\+studioInitSeqRef\.current;[\s\S]*?cancelScheduled\(\);[\s\S]*?cloudDraftLoadedRef\.current = false;/,
  "owner changes should invalidate prior initialization and automatic sync work",
);
assert.match(
  pageSource,
  /const ownerSession = studioOwnerSessionCoordinatorRef\.current\.bindOwner\(me\?\.id,\s*seq\)/,
  "each initialization generation should receive a scoped owner session",
);
assert.match(
  pageSource,
  /ownerSession\.commit\(\(\) => \{[\s\S]*?ownerSession\.observeRestore\(\{[\s\S]*?promptTransferApplied,[\s\S]*?variationTransferApplied,/,
  "the scoped initialization clock should observe every adopted restore source",
);
assert.match(
  pageSource,
  /resetStudioOwnerWorkspace\([\s\S]*?initializeStudioOwnerSession\(me,\s*ownerSession/,
  "switching owners should clear the prior workspace before restoring the new owner",
);
assert.match(
  pageSource,
  /studioOwnerSessionCoordinatorRef\.current\.schedule\(ownerSession,[\s\S]*?syncStudioDraftToCloud/,
  "automatic cloud sync should be guarded by the current owner session",
);
assert.doesNotMatch(
  pageSource,
  /draftSyncTimerRef|window\.setTimeout\(\(\) => \{\s*syncStudioDraftToCloud/,
  "Home must not retain an unguarded cross-owner draft sync timer",
);
assert.match(
  pageSource,
  /const savedAt = studioDraftClockRef\.current\.next\(\)/,
  "Studio saves should issue timestamps from the instance draft clock",
);
assert.match(
  pageSource,
  /const checkpoint = buildStudioSessionDraftFromState\([\s\S]*?saveStudioUserDraft\([\s\S]*?STUDIO_SESSION_DRAFT_KEY[\s\S]*?if \(checkpointSaved\) \{[\s\S]*?removeStudioUserDraft\(window\.localStorage, STUDIO_DRAFT_PROMPT_KEY, u\?\.id\);/,
  "the one-shot prompt intent should only be consumed after its adopted state is checkpointed",
);
assert.match(
  pageSource,
  /const promptTransferPresent = !!promptDraft;[\s\S]*?if \(!promptTransferPresent && variationDraft\)/,
  "a prompt intent should take precedence over any retained variation intent",
);
assert.match(
  pageSource,
  /setUnauthorizedHandler\(\(\)\s*=>\s*saveStudioSessionDraft\("auth_expired",\s*\{\s*persistCloud:\s*false\s*\}\)\)/,
  "the 401 handler must save locally without calling the protected cloud draft endpoint again",
);
assert.match(
  pageSource,
  /if\s*\(persistCloud\s*&&\s*me\?\.id\)/,
  "cloud draft persistence must be explicitly gated off during unauthorized handling",
);

for (const [label, source, key] of [
  ["Studio prompt transfer", pageSource, "STUDIO_DRAFT_PROMPT_KEY"],
  ["Studio variation transfer", pageSource, "STUDIO_VARIATION_DRAFT_KEY"],
]) {
  assert.match(
    source,
    new RegExp(`readStudioUserDraft\\(window\\.localStorage,\\s*${key},\\s*u\\?\\.id\\)`),
    `${label} must only restore the authenticated user's draft`,
  );
}
for (const [label, source, key] of [
  ["prompt library", promptsSource, "STUDIO_DRAFT_PROMPT_KEY"],
  ["history variation", historySource, "STUDIO_VARIATION_DRAFT_KEY"],
  ["profile variation", profileSource, "STUDIO_VARIATION_DRAFT_KEY"],
]) {
  assert.match(source, /saveStudioUserDraft\(/, `${label} must write a user-scoped transfer draft`);
  assert.doesNotMatch(
    source,
    new RegExp(`localStorage\\.setItem\\(${key}`),
    `${label} must not write a global cross-account draft`,
  );
}

assert.match(
  packageJson.scripts["test:unit"],
  /studio-session-state\.test\.mjs/,
  "the standard frontend unit suite must execute Studio session isolation tests",
);
assert.match(
  packageJson.scripts["test:unit"],
  /studio-subject-profile-idempotency\.test\.mjs/,
  "the standard frontend unit suite must execute subject-profile idempotency tests",
);

console.log("studio session state tests passed");
