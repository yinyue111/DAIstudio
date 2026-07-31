import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readStudioSource } from "./studio-source.mjs";

import { generationSubmitDisabled, shouldBlockNewGeneration } from "../app/studio/taskConcurrency.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readStudioSource(root);
const ownerSessionSource = readFileSync(join(root, "hooks/useStudioOwnerSession.js"), "utf8");
const taskTrackingSource = readFileSync(join(root, "hooks/useTaskTracking.js"), "utf8");
const studioRuntimeSource = `${pageSource}\n${ownerSessionSource}\n${taskTrackingSource}`;
const studioHelpersSource = readFileSync(join(root, "app/studio/helpers.ts"), "utf8");
const studioResultsSource = readFileSync(join(root, "app/studio/StudioResults.jsx"), "utf8");
const viewModelSource = readFileSync(join(root, "app/studio/viewModel.ts"), "utf8");
const versionUpgradeSource = readFileSync(join(root, "app/admin/components/version-upgrade.jsx"), "utf8");

assert.equal(
  shouldBlockNewGeneration({ category: "image", status: "running" }, "image"),
  false,
  "running image tasks should not block another image generation",
);
assert.equal(
  generationSubmitDisabled({
    submitting: false,
    currentTask: { category: "image", status: "queued" },
    nextCategory: "image",
    currentModelEnabled: true,
  }),
  false,
  "queued image tasks should not disable the image submit button",
);
assert.equal(
  shouldBlockNewGeneration({ category: "video", status: "running" }, "video"),
  true,
  "video tasks should keep the single-active-task guard",
);
assert.equal(
  shouldBlockNewGeneration({ category: "image", status: "succeeded" }, "image"),
  false,
  "terminal tasks should not block new generation",
);
assert.equal(
  shouldBlockNewGeneration({ category: "video", status: "canceled" }, "video"),
  false,
  "canceled tasks should not block new generation",
);
assert.equal(
  shouldBlockNewGeneration({ category: "video", status: "needs_review" }, "video"),
  true,
  "a video awaiting provider reconciliation must block duplicate video submission",
);
assert.equal(
  shouldBlockNewGeneration({ category: "video", status: "needs_review", phase: "reconciling" }, "video"),
  true,
  "a reconciling video must remain guarded even though needs_review stops task polling",
);
assert.equal(
  shouldBlockNewGeneration({ category: "image", status: "needs_review" }, "image"),
  false,
  "terminal image review state should keep the existing image concurrency behavior",
);
assert.equal(
  generationSubmitDisabled({
    submitting: true,
    currentTask: null,
    nextCategory: "image",
    currentModelEnabled: true,
  }),
  true,
  "in-flight form submission should still be guarded against double click",
);
assert.equal(
  generationSubmitDisabled({
    submitting: false,
    busy: true,
    currentTask: null,
    nextCategory: "image",
    currentModelEnabled: true,
  }),
  true,
  "reference parsing, reverse prompt, upload, or subject profiling should block accidental generation",
);
assert.match(
  ownerSessionSource,
  /loadWorks\(\{\s*restoreActive:\s*true\s*\}\)/,
  "studio should restore active tasks from backend on initial load",
);
assert.match(
  studioRuntimeSource,
  /item\.category\s*===\s*"video"/,
  "restored task guard should focus on long-running video tasks",
);
assert.match(
  studioRuntimeSource,
  /startTracking\(active\.id\)/,
  "restored active tasks should resume websocket/poll tracking",
);
assert.match(
  studioHelpersSource,
  /canceled:\s*"已取消"/,
  "studio status labels should render canceled tasks as canceled, not raw enum text",
);
assert.doesNotMatch(
  studioResultsSource,
  /hasPendingFinal|finalStatus/,
  "studio results should no longer expose preview-to-final render cancellation state",
);
assert.match(
  taskTrackingSource,
  /api\.cancelTask\((?:task|currentTask)\.id\)/,
  "studio cancellation should submit the currently tracked direct generation task",
);
assert.doesNotMatch(
  taskTrackingSource,
  /final_status|final_task_id/,
  "studio cancellation should not depend on hidden final child task state",
);
assert.match(
  viewModelSource,
  /cfg\?\.features\?\.reverse_prompt_enabled\s*!==\s*false/,
  "reverse prompt UI should consume normalized feature flags, not raw defaults",
);
assert.doesNotMatch(
  pageSource,
  /reverse_prompt_enabled/,
  "main page should not own reverse feature flag normalization",
);
assert.match(
  versionUpgradeSource,
  /adminRunUpdate\(\{[\s\S]*apply:\s*true,[\s\S]*confirm:\s*"UPDATE",[\s\S]*force_apply:\s*reapplyCurrent,[\s\S]*\}\)/,
  "online update apply requests should include the confirmation code and explicit reapply flag",
);

console.log("studio concurrency test passed");
