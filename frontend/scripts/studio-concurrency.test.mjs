import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/studio/taskConcurrency.js"), "utf8")
  .replace(/"use client";\s*/, "")
  .replace(/import\s+\{\s*isTerminalTaskStatus\s*\}\s+from\s+"\.\/helpers\.js";\s*/, "")
  .replaceAll("export function", "function");
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const versionUpgradeSource = readFileSync(join(root, "app/admin/components/version-upgrade.jsx"), "utf8");

const isTerminalTaskStatus = (status) => new Set(["succeeded", "failed", "needs_review"]).has(status);
const { shouldBlockNewGeneration, generationSubmitDisabled } = new Function(
  "isTerminalTaskStatus",
  `${source}
  return { shouldBlockNewGeneration, generationSubmitDisabled };`
)(isTerminalTaskStatus);

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
  generationSubmitDisabled({
    submitting: true,
    currentTask: null,
    nextCategory: "image",
    currentModelEnabled: true,
  }),
  true,
  "in-flight form submission should still be guarded against double click",
);
assert.match(
  pageSource,
  /loadWorks\(\{\s*restoreActive:\s*true\s*\}\)/,
  "studio should restore active tasks from backend on initial load",
);
assert.match(
  pageSource,
  /item\.category\s*===\s*"video"/,
  "restored task guard should focus on long-running video tasks",
);
assert.match(
  pageSource,
  /startTracking\(active\.id\)/,
  "restored active tasks should resume websocket/poll tracking",
);
assert.match(
  pageSource,
  /cfg\?\.features\?\.reverse_prompt_enabled\s*!==\s*false/,
  "reverse prompt UI should consume normalized feature flags, not raw defaults",
);
assert.match(
  versionUpgradeSource,
  /adminRunUpdate\(\{\s*apply:\s*true,\s*confirm:\s*"UPDATE"\s*\}\)/,
  "online update apply requests should include the explicit confirmation code",
);

console.log("studio concurrency test passed");
