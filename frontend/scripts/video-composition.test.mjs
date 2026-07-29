import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { readStudioSourceFromUrl } from "./studio-source.mjs";

import {
  bindGenerationTaskToVideoCompositionShot,
  buildVideoCompositionInput,
  normalizeVideoComposition,
  subtitlesFromStoryboard,
  videoCompositionDuration,
  videoCompositionCapabilityStatus,
  videoCompositionProblems,
  videoCompositionShotTracksGenerationTask,
  videoCompositionWorkflowRecord,
  videoCompositionWorkflowOutput,
  videoCompositionWorkflowProgress,
} from "../app/studio/videoComposition.ts";

const storyboard = [
  { shot_id: "shot-a", start_seconds: 10, end_seconds: 13, ocr: "新品上市" },
  { shot_id: "shot-b", start_seconds: 13, end_seconds: 17, audio_cue: "立即购买" },
];

const pageSource = readStudioSourceFromUrl(import.meta.url);
const shotGenerationSource = readFileSync(new URL("../hooks/useShotGeneration.js", import.meta.url), "utf8");

function sourceSection(source, label, startToken, endToken, fromIndex = 0) {
  const start = source.indexOf(startToken, fromIndex);
  assert.notEqual(start, -1, `${label} must contain ${startToken}`);
  const end = source.indexOf(endToken, start + startToken.length);
  assert.notEqual(end, -1, `${label} must contain ${endToken} after ${startToken}`);
  return { source: source.slice(start, end), start, end };
}

function pageSection(startToken, endToken, fromIndex = 0) {
  return sourceSection(pageSource, "page.jsx", startToken, endToken, fromIndex);
}

function shotGenerationSection(startToken, endToken, fromIndex = 0) {
  return sourceSection(shotGenerationSource, "useShotGeneration.js", startToken, endToken, fromIndex);
}

test("composition drafts keep stable shot ids and explicit generation lineage", () => {
  const draft = normalizeVideoComposition({
    shots: [{
      shot_id: "old-id",
      asset_ref: "g.10",
      asset: {
        asset_ref: "g.10",
        task_id: 91,
        preview_url: "/preview.jpg",
        type: "video",
      },
      transition: { type: "crossfade", duration_seconds: 0.5 },
    }],
  }, storyboard, 12);
  assert.deepEqual(draft.shots.map((shot) => shot.shot_id), ["shot-a", "shot-b"]);
  assert.equal(draft.shots[0].asset_ref, "g.10");
  assert.equal(draft.shots[0].generation_task_id, 91);
  assert.equal(draft.reverse_operation_id, 12);
  assert.deepEqual(videoCompositionProblems(draft), ["还有 1 个镜头未绑定视频素材"]);

  draft.shots[1].asset_ref = "u.dXBsb2FkX3ZpZGVvL2IubXA0";
  const input = buildVideoCompositionInput(draft);
  assert.equal("asset" in input.shots[0], false);
  assert.equal(input.shots[0].generation_task_id, 91);
  assert.equal(input.shots[0].transition.type, "crossfade");
});

test("successful shot generation binds its generated asset and task lineage", () => {
  const draft = normalizeVideoComposition({}, storyboard, 12);
  const binding = bindGenerationTaskToVideoCompositionShot(draft, {
    shotId: "shot-b",
    task: {
      id: 321,
      status: "succeeded",
      category: "video",
      assets: [{
        id: 44,
        task_id: 321,
        type: "video",
        preview_url: "/api/assets/44/preview",
      }],
    },
  });

  assert.equal(binding.status, "bound");
  assert.equal(binding.changed, true);
  assert.equal(binding.shotId, "shot-b");
  assert.equal(binding.assetRef, "g.44");
  assert.equal(binding.generationTaskId, 321);
  assert.equal(draft.shots[1].asset_ref, "", "binding must not mutate the active draft");
  assert.equal(binding.composition.shots[0], draft.shots[0]);
  assert.deepEqual(binding.composition.shots[1], {
    ...draft.shots[1],
    asset_ref: "g.44",
    generation_task_id: 321,
    asset: {
      asset_ref: "g.44",
      generation_task_id: 321,
      id: 44,
      origin: null,
      type: "video",
      filename: null,
      preview_url: "/api/assets/44/preview",
      thumb: null,
      url: null,
    },
  });
});

test("shot generation binding does not fall back to a different shot", () => {
  const draft = normalizeVideoComposition({}, storyboard, 12);
  const binding = bindGenerationTaskToVideoCompositionShot(draft, {
    shotId: "missing-shot",
    task: {
      id: 322,
      status: "succeeded",
      assets: [{ id: 45, type: "video" }],
    },
  });

  assert.equal(binding.status, "shot_not_found");
  assert.equal(binding.changed, false);
  assert.equal(binding.composition, draft);
  assert.deepEqual(binding.composition.shots.map((shot) => shot.asset_ref), ["", ""]);
});

test("a completed task without a published asset stores lineage without inventing an asset ref", () => {
  const draft = normalizeVideoComposition({
    shots: [{
      shot_id: "shot-a",
      asset_ref: "g.10",
      generation_task_id: 100,
      asset: { asset_ref: "g.10", task_id: 100, type: "video" },
    }, {
      shot_id: "shot-b",
      asset_ref: "g.11",
      generation_task_id: 101,
      asset: { asset_ref: "g.11", task_id: 101, type: "video" },
    }],
  }, storyboard, 12);
  const binding = bindGenerationTaskToVideoCompositionShot(draft, {
    shotId: "shot-b",
    task: { id: 324, status: "succeeded", category: "video", assets: [] },
  });

  assert.equal(binding.status, "pending_task_bound");
  assert.equal(binding.assetRef, null);
  assert.equal(binding.composition.shots[1].generation_task_id, 324);
  assert.equal(binding.composition.shots[1].asset_ref, "");
  assert.equal(binding.composition.shots[1].asset, null);
  assert.equal(binding.composition.shots[0].asset_ref, "g.10");
});

test("pending shot tasks persist for refresh recovery and replay idempotently", () => {
  const draft = normalizeVideoComposition({}, storyboard, 12);
  const pending = bindGenerationTaskToVideoCompositionShot(draft, {
    shotId: "shot-a",
    task: { id: 323, status: "running", category: "video", assets: [] },
  });
  assert.equal(pending.status, "pending_task_bound");
  assert.equal(pending.composition.shots[0].generation_task_id, 323);
  assert.equal(pending.composition.shots[0].asset_ref, "");

  const restored = JSON.parse(JSON.stringify(pending.composition));
  const pendingReplay = bindGenerationTaskToVideoCompositionShot(restored, {
    shotId: "shot-a",
    task: { id: 323, status: "running", category: "video", assets: [] },
  });
  assert.equal(pendingReplay.status, "unchanged");
  assert.equal(pendingReplay.changed, false);
  assert.equal(pendingReplay.composition, restored);

  const succeededAfterRefresh = bindGenerationTaskToVideoCompositionShot(restored, {
    task: {
      id: 323,
      status: "succeeded",
      category: "video",
      assets: [{ asset_ref: "g.46", id: 46, task_id: 323, type: "video" }],
    },
  });
  assert.equal(succeededAfterRefresh.status, "bound");
  assert.equal(succeededAfterRefresh.shotId, "shot-a");
  assert.equal(succeededAfterRefresh.composition.shots[0].asset_ref, "g.46");
  assert.equal(succeededAfterRefresh.composition.shots[0].generation_task_id, 323);

  const successReplay = bindGenerationTaskToVideoCompositionShot(
    succeededAfterRefresh.composition,
    {
      task: {
        id: 323,
        status: "succeeded",
        assets: [{ asset_ref: "g.46", id: 46, task_id: 323, type: "video" }],
      },
    },
  );
  assert.equal(successReplay.status, "unchanged");
  assert.equal(successReplay.changed, false);
  assert.equal(successReplay.composition, succeededAfterRefresh.composition);
});

test("terminal auto-binding only applies while the same task still owns the shot", () => {
  const draft = normalizeVideoComposition({}, storyboard, 12);
  const pending = bindGenerationTaskToVideoCompositionShot(draft, {
    shotId: "shot-a",
    task: { id: 323, status: "running", category: "video", assets: [] },
  }).composition;
  assert.equal(videoCompositionShotTracksGenerationTask(pending, "shot-a", 323), true);

  const manuallyReplaced = {
    ...pending,
    shots: pending.shots.map((shot) => shot.shot_id === "shot-a" ? {
      ...shot,
      generation_task_id: 999,
      asset_ref: "g.99",
      asset: { asset_ref: "g.99", generation_task_id: 999, type: "video" },
    } : shot),
  };
  assert.equal(videoCompositionShotTracksGenerationTask(manuallyReplaced, "shot-a", 323), false);
  assert.equal(videoCompositionShotTracksGenerationTask(manuallyReplaced, "shot-a", 999), true);

  const cleared = {
    ...pending,
    shots: pending.shots.map((shot) => shot.shot_id === "shot-a" ? {
      ...shot,
      generation_task_id: null,
      asset_ref: "",
      asset: null,
    } : shot),
  };
  assert.equal(videoCompositionShotTracksGenerationTask(cleared, "shot-a", 323), false);
});

test("duration and storyboard subtitle timing account for transitions", () => {
  const draft = normalizeVideoComposition({}, storyboard, 12);
  draft.shots[0].transition = { type: "fade", duration_seconds: 0.5 };
  assert.equal(videoCompositionDuration(draft), 6.5);
  const subtitles = subtitlesFromStoryboard(storyboard, draft);
  assert.deepEqual(subtitles.map((item) => item.text), ["新品上市", "立即购买"]);
  assert.equal(subtitles[1].start_seconds, 2.5);
});

test("workflow output and progress recover from node state", () => {
  const run = {
    status: "running",
    nodes: [
      { key: "compose", status: "succeeded", output: { schema_version: "video-composition-result.v1" } },
      { key: "export", status: "running", output: null },
    ],
  };
  assert.equal(videoCompositionWorkflowProgress(run), 75);
  assert.equal(videoCompositionWorkflowOutput(run)?.schema_version, "video-composition-result.v1");
  assert.equal(videoCompositionWorkflowProgress({ status: "succeeded", nodes: [] }), 100);
});

test("workflow capability degradation survives node and persisted-state recovery", () => {
  const unsupported = {
    status: "failed",
    nodes: [{
      key: "compose",
      status: "failed",
      output: {
        capability: "video_compose",
        capability_status: "unsupported",
      },
    }],
  };
  assert.deepEqual(videoCompositionCapabilityStatus(unsupported), {
    status: "unsupported",
    capability: "video_compose",
  });
  const persisted = videoCompositionWorkflowRecord(unsupported);
  assert.deepEqual(videoCompositionCapabilityStatus(persisted), {
    status: "unsupported",
    capability: "video_compose",
  });
  assert.equal(videoCompositionCapabilityStatus({ status: "failed", output: {} }), null);
});

test("composition retry sends the workflow retry schema instead of a new idempotency key", () => {
  const source = readFileSync(new URL("../app/studio/StudioVideoComposition.jsx", import.meta.url), "utf8");
  const start = source.indexOf("async function retryRun()");
  const end = source.indexOf("async function downloadResult()", start);
  const retry = source.slice(start, end);
  assert.match(retry, /retryWorkflowNode\(runId, failedNode\.key, \{[\s\S]*?reason: "studio_video_composition_retry"/);
  assert.doesNotMatch(retry, /client_request_id/);
});

test("live workflow state wins over stale persisted state and can be invalidated", () => {
  const stored = {
    run_id: 40,
    client_request_id: "storyboard-compose-stored",
    status: "queued",
    output: null,
  };
  const live = videoCompositionWorkflowRecord({
    id: 40,
    status: "succeeded",
    output: { download_url: "/api/assets/9/download" },
    updated_at: "2026-07-18T10:00:00Z",
  }, stored);
  assert.equal(live.status, "succeeded");
  assert.equal(live.client_request_id, "storyboard-compose-stored");
  assert.equal(live.output.download_url, "/api/assets/9/download");
  assert.equal(videoCompositionWorkflowRecord(null, stored), stored);
  const invalidated = videoCompositionWorkflowRecord({}, stored);
  assert.equal(invalidated.run_id, null);
  assert.equal(invalidated.status, null);
  assert.equal(invalidated.output, null);
});

test("shot generation hook persists a submitted storyboard task before starting terminal tracking", () => {
  const persistence = shotGenerationSection(
    "async function persistShotGenerationBinding",
    "function enqueueShotGenerationBinding",
  ).source;
  const submission = shotGenerationSection(
    "async function handleGenerationSubmitted",
    "useEffect(() => {",
  ).source;

  assert.match(submission, /async function handleGenerationSubmitted\(\{ task: submittedTask, prepared = null, shot = null \}\)/);
  assert.match(submission, /await enqueueShotGenerationBinding\(\{[\s\S]*?task: submittedTask,[\s\S]*?editAction: "video_composition_generation_submitted"/);
  assert.match(submission, /trackShotGenerationTask\(\{[\s\S]*?task: submittedTask,[\s\S]*?shotId/);
  assert.ok(
    submission.indexOf("await enqueueShotGenerationBinding")
      < submission.indexOf("trackShotGenerationTask"),
    "the queued task id must be persisted before terminal tracking starts",
  );

  assert.match(persistence, /const transition = bindGenerationTaskToPendingReverseResult\(currentPending/);
  assert.match(persistence, /pendingReverseResult: replaceReverseResultPayload\(/);
  const revisionCallStart = persistence.indexOf("await api.createReverseOperationRevision");
  const revisionCallEnd = persistence.indexOf("\n    );", revisionCallStart);
  assert.notEqual(revisionCallStart, -1);
  assert.notEqual(revisionCallEnd, -1);
  const revisionCall = persistence.slice(revisionCallStart, revisionCallEnd);
  assert.match(revisionCall, /source: "user_edit"/);
  assert.match(
    revisionCall,
    /generation_task_id: transition\.binding\.generationTaskId/,
    "generation_task_id must be included in the persisted user-edit revision payload",
  );
});

test("shot generation hook binds terminal tasks only while they still own the shot", () => {
  const persistence = shotGenerationSection(
    "async function persistShotGenerationBinding",
    "function enqueueShotGenerationBinding",
  ).source;
  const tracking = shotGenerationSection(
    "function trackShotGenerationTask",
    "async function handleGenerationSubmitted",
  ).source;

  assert.match(tracking, /trackBackgroundTask\(generationTask, \{/);
  assert.match(tracking, /onTerminal: async \(terminalTask\) =>/);
  assert.match(tracking, /await enqueueShotGenerationBinding\(\{[\s\S]*?task: terminalTask,[\s\S]*?editAction: "video_composition_generation_terminal"/);

  const terminalGuardStart = persistence.indexOf('if (editAction === "video_composition_generation_terminal")');
  const bindingStart = persistence.indexOf("const transition = bindGenerationTaskToPendingReverseResult");
  assert.notEqual(terminalGuardStart, -1);
  assert.match(
    persistence.slice(terminalGuardStart, bindingStart),
    /if \(!videoCompositionShotTracksGenerationTask\([\s\S]*?generationTask\?\.id \|\| generationTask\?\.task_id,[\s\S]*?\)\) return null;/,
  );
  assert.ok(
    terminalGuardStart < bindingStart,
    "a stale terminal task must be rejected before it can replace a newer shot binding",
  );
});

test("page restores pending shot tracking from the persisted composition and wires storyboard submissions", () => {
  const submissionSection = shotGenerationSection(
    "async function handleGenerationSubmitted",
    "useEffect(() => {",
  );
  const restore = shotGenerationSection(
    "useEffect(() => {",
    "return { handleGenerationSubmitted }",
    submissionSection.end,
  ).source;
  const panel = pageSection(
    "<StudioReverseResultPanel",
    "<StudioRecentReversePanel",
  ).source;

  assert.match(restore, /const composition = normalizeVideoComposition\([\s\S]*?result\.video_composition/);
  assert.match(restore, /for \(const compositionShot of composition\.shots\)/);
  assert.match(restore, /const taskId = positiveRevisionId\(compositionShot\.generation_task_id\)/);
  assert.match(restore, /if \(!taskId \|\| compositionShot\.asset_ref\) continue/);
  assert.match(restore, /trackShotGenerationTask\(\{[\s\S]*?id: taskId,[\s\S]*?shotId: compositionShot\.shot_id/);
  assert.match(panel, /onGenerationSubmitted=\{handleGenerationSubmitted\}/);
});
