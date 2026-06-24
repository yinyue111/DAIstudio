import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/studio/taskConcurrency.js"), "utf8")
  .replace(/"use client";\s*/, "")
  .replace(/import\s+\{\s*isTerminalTaskStatus\s*\}\s+from\s+"\.\/helpers\.js";\s*/, "")
  .replaceAll("export function", "function");

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

console.log("studio concurrency test passed");
