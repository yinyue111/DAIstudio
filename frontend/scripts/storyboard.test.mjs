import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  buildStoryboardRevisionRequest,
  buildStoryboardShotGenerationPrepareRequest,
  buildStoryboardEditRequest,
  clearStoryboardShotCompilation,
  composeStoryboardShotPrompt,
  createStoryboardMutationCoordinator,
  mergeStoryboardShots,
  moveStoryboardBoundary,
  moveStoryboardShot,
  splitStoryboardShot,
  StoryboardEditError,
  toggleStoryboardShotLock,
  optimisticStoryboardReducer,
  resolveAppliedRevision,
  storyboardShotsFromRevision,
  storyboardResultShots,
  storyboardResultWithShots,
  verifiedAppliedStoryboardRevision,
} from "../app/studio/storyboard.ts";
import { normalizeReverseOperation } from "../lib/reverseOperations.ts";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function shots() {
  return [
    {
      source_segment_index: 1,
      start_seconds: 10,
      end_seconds: 13,
      visual: "产品入场",
      action: "缓慢旋转",
      camera: "推近",
      transition: "闪白",
      audio_cue: "鼓点",
      evidence_frame_indices: [1, 2, 3],
      confidence: 0.8,
      compiled_prompt: "旧编译稿",
      compilation: { target_model_id: "video-model" },
    },
    {
      source_segment_index: 1,
      start_seconds: 13,
      end_seconds: 16,
      visual: "产品特写",
      action: "液滴滑落",
      camera: "环绕",
      transition: "淡出",
      audio_cue: "水声",
      evidence_frame_indices: [4, 5],
      confidence: 0.6,
    },
    {
      source_segment_index: 2,
      start_seconds: 30,
      end_seconds: 33,
      visual: "第二片段",
      evidence_frame_indices: [6],
      confidence: 0.9,
    },
  ];
}

test("split preserves segment boundaries and partitions frame evidence by absolute time", () => {
  const result = splitStoryboardShot(shots(), 0, 11.5, [
    { index: 1, absolute_timestamp_seconds: 10.2, source_segment_index: 1 },
    { index: 2, absolute_timestamp_seconds: 11.5, source_segment_index: 1 },
    { index: 3, absolute_timestamp_seconds: 12.8, source_segment_index: 1 },
  ]);
  assert.equal(result.length, 4);
  assert.deepEqual(result[0].evidence_frame_indices, [1, 2]);
  assert.deepEqual(result[1].evidence_frame_indices, [3]);
  assert.equal(result[0].source_segment_index, 1);
  assert.equal(result[1].source_segment_index, 1);
  assert.equal(result[0].compiled_prompt, undefined);
  assert.equal(result[1].compilation, undefined);
});

test("split rejects boundaries and locked shots", () => {
  assert.throws(() => splitStoryboardShot(shots(), 0, 10), StoryboardEditError);
  const locked = shots();
  locked[0].locked = true;
  assert.throws(() => splitStoryboardShot(locked, 0, 11.5), /已锁定/);
});

test("merge combines adjacent evidence but never crosses source segments", () => {
  const result = mergeStoryboardShots(shots(), 0);
  assert.equal(result.length, 2);
  assert.equal(result[0].start_seconds, 10);
  assert.equal(result[0].end_seconds, 16);
  assert.equal(result[0].visual, "产品入场；产品特写");
  assert.equal(result[0].transition, "淡出");
  assert.deepEqual(result[0].evidence_frame_indices, [1, 2, 3, 4, 5]);
  assert.equal(result[0].compiled_prompt, undefined);
  assert.throws(() => mergeStoryboardShots(shots(), 1), /不同来源片段/);
});

test("move and lock preserve evidence and enforce segment boundaries", () => {
  const original = shots();
  const moved = moveStoryboardShot(original, 0, 1);
  assert.equal(moved[0].visual, "产品特写");
  assert.deepEqual(moved[1].evidence_frame_indices, [1, 2, 3]);
  assert.throws(() => moveStoryboardShot(original, 1, 1), /跨来源片段/);
  const locked = toggleStoryboardShotLock(original, 0);
  assert.equal(locked[0].locked, true);
  assert.throws(() => moveStoryboardShot(locked, 0, 1), /已锁定/);
});

test("dragged shot boundaries update both shots and repartition evidence", () => {
  const moved = moveStoryboardBoundary(shots(), 0, 14, [
    { index: 1, absolute_timestamp_seconds: 10.5 },
    { index: 2, absolute_timestamp_seconds: 12.5 },
    { index: 3, absolute_timestamp_seconds: 14.5 },
    { index: 4, absolute_timestamp_seconds: 15.5 },
  ]);
  assert.equal(moved[0].end_seconds, 14);
  assert.equal(moved[1].start_seconds, 14);
  assert.deepEqual(moved[0].evidence_frame_indices, [1, 2]);
  assert.deepEqual(moved[1].evidence_frame_indices, [3, 4, 5]);
  assert.equal(moved[0].compiled_prompt, undefined);
  assert.throws(() => moveStoryboardBoundary(shots(), 1, 17), /不同来源片段/);
});

test("shot edits invalidate compile metadata and prompt includes executable fields", () => {
  const cleared = clearStoryboardShotCompilation(shots()[0]);
  assert.equal(cleared.compiled_prompt, undefined);
  assert.equal(cleared.compilation, undefined);
  const prompt = composeStoryboardShotPrompt(shots()[0], { index: 2 });
  assert.match(prompt, /镜头 3/);
  assert.match(prompt, /来源片段 1/);
  assert.match(prompt, /画面：产品入场/);
  assert.match(prompt, /镜头：推近/);
});

test("storyboard UI exposes structural and model-compile actions", () => {
  const editor = readFileSync(new URL("../app/studio/StudioReverseStoryboardShot.jsx", import.meta.url), "utf8");
  for (const marker of ["拆分时间", "与下一镜头合并", "锁定镜头", "编译镜头", "应用镜头"]) {
    assert.match(editor, new RegExp(marker));
  }
  const page = readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");
  assert.match(page, /mode: "target_model_adaptation"/);
  assert.match(page, /api\.createStudioPromptOptimization/);
  assert.match(page, /application_mode: "storyboard_shot"/);
  const api = readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
  for (const endpoint of ["shots/edit", "shots/reanalyze", "shots/prepare-generation"]) {
    assert.match(api, new RegExp(endpoint.replace("/", "\\/")));
  }
  for (const marker of ["重新分析当前镜头", "生成当前镜头", "保存相邻镜头边界"]) {
    assert.match(editor, new RegExp(marker));
  }
  const storyboard = readFileSync(new URL("../app/studio/StudioReverseStoryboard.jsx", import.meta.url), "utf8");
  assert.match(storyboard, /requestQuoteConfirmation\(\{[\s\S]*kind: "reverse"/);
  assert.match(storyboard, /requestQuoteConfirmation\(\{[\s\S]*kind: "generation"[\s\S]*api\.generate\(request\)/);
  assert.match(storyboard, /生成任务 #/);
});

test("storyboard revision payloads and optimistic rollback are deterministic", () => {
  const request = buildStoryboardRevisionRequest({
    final_text: "keep",
    shots: [{ visual: "stale root" }],
    video_analysis: { source: { duration_seconds: 33 }, shots: [{ visual: "old nested" }] },
  }, shots(), "update", 0);
  assert.equal(request.source, "user_edit");
  assert.equal(request.payload.final_text, "keep");
  assert.equal(request.payload.edit_metadata.action, "storyboard_update");
  assert.equal("shots" in request.payload, false);
  assert.deepEqual(request.payload.video_analysis.shots, shots());
  assert.deepEqual(request.payload.video_analysis.source, { duration_seconds: 33 });
  const initial = { shots: shots(), committed: shots(), pending_token: null, error: null };
  const applied = optimisticStoryboardReducer(initial, { type: "apply", token: 7, shots: shots().slice(0, 2) });
  assert.equal(applied.shots.length, 2);
  const ignored = optimisticStoryboardReducer(applied, { type: "rollback", token: 6, error: "old" });
  assert.equal(ignored.shots.length, 2);
  const rolledBack = optimisticStoryboardReducer(applied, { type: "rollback", token: 7, error: "failed" });
  assert.equal(rolledBack.shots.length, 3);
  assert.equal(rolledBack.error, "failed");
});

test("stable shot contracts map to authoritative edit endpoints", () => {
  const stable = shots().map((shot, index) => ({ ...shot, shot_id: `shot-${index + 1}` }));
  assert.deepEqual(buildStoryboardEditRequest(stable, "split", 0, "request-split-001", { split_seconds: 11.5 }), {
    client_request_id: "request-split-001",
    action: "split",
    shot_id: "shot-1",
    split_seconds: 11.5,
  });
  assert.deepEqual(buildStoryboardEditRequest(stable, "merge", 0, "request-merge-001"), {
    client_request_id: "request-merge-001",
    action: "merge",
    shot_ids: ["shot-1", "shot-2"],
  });
  assert.deepEqual(buildStoryboardEditRequest(stable, "lock", 0, "request-lock-001"), {
    client_request_id: "request-lock-001",
    action: "lock",
    shot_id: "shot-1",
    locked: true,
  });
  assert.deepEqual(buildStoryboardEditRequest(stable, "boundary", 0, "request-boundary-001", { boundary_seconds: 13.5 }), {
    client_request_id: "request-boundary-001",
    action: "boundary",
    shot_ids: ["shot-1", "shot-2"],
    boundary_seconds: 13.5,
  });
  assert.deepEqual(buildStoryboardEditRequest(stable, "reorder", 0, "request-order-001", { direction: 1 }).ordered_shot_ids, ["shot-2", "shot-1", "shot-3"]);
  assert.equal(buildStoryboardEditRequest(stable, "update", 0, "request-update-001"), null);
  assert.equal(buildStoryboardEditRequest(shots(), "lock", 0, "legacy-request-001"), null);
  assert.deepEqual(storyboardShotsFromRevision({ payload: { video_analysis: { shots: stable } } }), stable);
});

test("nested storyboard shots are authoritative with a legacy root fallback", () => {
  const nested = [{ shot_id: "nested" }];
  const legacy = [{ shot_id: "legacy" }];
  assert.deepEqual(storyboardResultShots({ shots: legacy, video_analysis: { shots: nested } }), nested);
  assert.deepEqual(storyboardResultShots({ shots: legacy }), legacy);
  const updated = storyboardResultWithShots({ shots: legacy, final_text: "keep", video_analysis: { source: { duration_seconds: 4 } } }, nested);
  assert.equal("shots" in updated, false);
  assert.deepEqual(updated.video_analysis.shots, nested);
  assert.deepEqual(updated.video_analysis.source, { duration_seconds: 4 });
});

test("shot generation preparation requires a persisted verified applied revision", () => {
  const revisions = [
    { id: 8, version: 3, source: "applied", lineage_status: "legacy_unverified" },
    { id: 9, version: 4, source: "user_edit", lineage_status: "verified" },
    { id: 10, version: 5, source: "applied", lineage_status: "verified" },
  ];
  const applied = verifiedAppliedStoryboardRevision(revisions, { applied_result_version: 5 });
  assert.equal(applied.id, 10);
  assert.equal(resolveAppliedRevision(revisions, { applied_result_version: 999 }), null);
  assert.equal(resolveAppliedRevision(revisions, { applied_revision_id: 999 }), null);
  assert.equal(resolveAppliedRevision(revisions, {
    applied_result_version: 5,
    applied_revision_id: 999,
  }), null);
  assert.equal(resolveAppliedRevision(revisions, { applied_result_version: 0 }), null);
  assert.equal(resolveAppliedRevision(revisions, { applied_result_version: null }), null);
  assert.equal(resolveAppliedRevision(revisions, { applied_revision_id: 10 }).id, 10);
  assert.equal(resolveAppliedRevision(revisions, {
    applied_result_version: 5,
    applied_result_revision_id: 10,
  }).id, 10);
  assert.equal(resolveAppliedRevision(revisions, {}).id, 10, "only marker-free legacy operations may use the latest eligible revision");
  const normalizedLegacyOperation = normalizeReverseOperation({ id: 17, status: "succeeded" });
  assert.equal(
    Object.prototype.hasOwnProperty.call(normalizedLegacyOperation, "applied_result_version"),
    false,
    "normalization must preserve an absent legacy applied marker",
  );
  assert.equal(resolveAppliedRevision(revisions, normalizedLegacyOperation).id, 10);
  const normalizedExplicitNull = normalizeReverseOperation({
    id: 18,
    status: "succeeded",
    applied_result_version: null,
  });
  assert.equal(Object.prototype.hasOwnProperty.call(normalizedExplicitNull, "applied_result_version"), true);
  assert.equal(resolveAppliedRevision(revisions, normalizedExplicitNull), null);
  assert.equal(verifiedAppliedStoryboardRevision(revisions.slice(0, 2), {}), null);
  assert.equal(buildStoryboardShotGenerationPrepareRequest(
    { shot_id: "shot-1" },
    revisions[0],
    "shot-generation-001",
  ), null);
  assert.deepEqual(buildStoryboardShotGenerationPrepareRequest(
    { shot_id: "shot-1" },
    applied,
    "shot-generation-001",
  ), {
    client_request_id: "shot-generation-001",
    shot_id: "shot-1",
    revision_id: 10,
    params: {},
  });
  const storyboardSource = readFileSync(new URL("../app/studio/StudioReverseStoryboard.jsx", import.meta.url), "utf8");
  assert.match(storyboardSource, /请先应用\/确认/);
  assert.match(storyboardSource, /if \(!operationId \|\| !prepareRequest\)/);
});

async function runTwoMutationScenario(firstOutcome, secondOutcome) {
  const requests = { A: deferred(), B: deferred() };
  const requestOrder = [];
  const failures = [];
  const coordinator = createStoryboardMutationCoordinator({
    initialShots: [{ shot_id: "base" }],
    onError: (error) => { if (error) failures.push(error.message); },
  });
  for (const label of ["A", "B"]) {
    coordinator.enqueue({
      action: label,
      apply: (current) => [...current, { shot_id: label }],
      request: () => {
        requestOrder.push(label);
        return requests[label].promise;
      },
    });
  }
  assert.deepEqual(requestOrder, ["A"], "B must not start before A settles");
  assert.deepEqual(
    coordinator.snapshot().optimistic.map((shot) => shot.shot_id),
    ["base", "A", "B"],
    "both edits should be visible optimistically",
  );
  if (firstOutcome === "success") requests.A.resolve({ ok: true });
  else requests.A.reject(new Error("A failed"));
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(requestOrder, ["A", "B"], "B should start only after A is confirmed or removed");
  if (secondOutcome === "success") requests.B.resolve({ ok: true });
  else requests.B.reject(new Error("B failed"));
  await coordinator.whenIdle();
  return {
    shots: coordinator.snapshot().confirmed.map((shot) => shot.shot_id),
    failures,
  };
}

test("serialized persistence replays A/B across all success and failure combinations", async () => {
  assert.deepEqual(await runTwoMutationScenario("success", "success"), {
    shots: ["base", "A", "B"],
    failures: [],
  });
  assert.deepEqual(await runTwoMutationScenario("success", "failure"), {
    shots: ["base", "A"],
    failures: ["B failed"],
  });
  assert.deepEqual(await runTwoMutationScenario("failure", "success"), {
    shots: ["base", "B"],
    failures: ["A failed"],
  });
  assert.deepEqual(await runTwoMutationScenario("failure", "failure"), {
    shots: ["base"],
    failures: ["A failed", "B failed"],
  });
});

test("three rapid edits replay on the latest confirmed server state", async () => {
  const requests = { A: deferred(), B: deferred(), C: deferred() };
  const bases = [];
  const coordinator = createStoryboardMutationCoordinator({ initialShots: [{ shot_id: "base" }] });
  for (const label of ["A", "B", "C"]) {
    coordinator.enqueue({
      action: label,
      apply: (current) => [...current, { shot_id: label }],
      request: (confirmed) => {
        bases.push(confirmed.map((shot) => shot.shot_id));
        return requests[label].promise;
      },
    });
  }
  requests.A.resolve({ ok: true });
  await Promise.resolve();
  await Promise.resolve();
  requests.B.reject(new Error("B failed"));
  await Promise.resolve();
  await Promise.resolve();
  requests.C.resolve({ ok: true });
  await coordinator.whenIdle();
  assert.deepEqual(bases, [["base"], ["base", "A"], ["base", "A"]]);
  assert.deepEqual(
    coordinator.snapshot().confirmed.map((shot) => shot.shot_id),
    ["base", "A", "C"],
  );
});

test("disposing the mutation coordinator ignores in-flight work and never starts queued writes", async () => {
  const first = deferred();
  const requestOrder = [];
  const visible = [];
  const coordinator = createStoryboardMutationCoordinator({
    initialShots: [{ shot_id: "base" }],
    onChange: (next) => visible.push(next.map((shot) => shot.shot_id)),
  });
  coordinator.enqueue({
    action: "A",
    apply: (current) => [...current, { shot_id: "A" }],
    request: () => {
      requestOrder.push("A");
      return first.promise;
    },
  });
  coordinator.enqueue({
    action: "B",
    apply: (current) => [...current, { shot_id: "B" }],
    request: async () => { requestOrder.push("B"); },
  });
  const visibleBeforeDispose = visible.length;
  coordinator.dispose();
  first.resolve({ ok: true });
  await Promise.resolve();
  await Promise.resolve();
  await coordinator.whenIdle();
  assert.deepEqual(requestOrder, ["A"]);
  assert.equal(visible.length, visibleBeforeDispose, "settlement after unmount must not publish state");
  assert.equal(coordinator.snapshot().active, false);

  const storyboardSource = readFileSync(new URL("../app/studio/StudioReverseStoryboard.jsx", import.meta.url), "utf8");
  assert.match(storyboardSource, /createStoryboardMutationCoordinator/);
  assert.match(storyboardSource, /persistenceCoordinatorRef\.current\?\.dispose\(\)/);
  assert.doesNotMatch(storyboardSource, /PersistenceGate|pending_token/);
});
