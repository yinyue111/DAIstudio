import assert from "node:assert/strict";
import fs from "node:fs";

const pageSource = fs.readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");
const workspaceSource = fs.readFileSync(new URL("../app/studio/StudioPromptWorkspace.jsx", import.meta.url), "utf8");
const apiSource = fs.readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
const adminHelpers = fs.readFileSync(new URL("../app/admin/components/admin-helpers.js", import.meta.url), "utf8");

assert.match(apiSource, /\/api\/prompt\/optimize/);
assert.match(pageSource, /api\.optimizePrompt\(source, category, productGenerationMode\)/);
assert.match(pageSource, /Boolean\(prompt\.trim\(\) && promptDirty\)/);
assert.match(
  pageSource,
  /function updatePromptFromUser\([\s\S]*invalidatePromptOptimization\(\)[\s\S]*setPrompt\(valueOrUpdater\)/,
  "editing the prompt should invalidate any in-flight optimization response",
);
assert.match(
  pageSource,
  /onPromptChange=\{updatePromptFromUser\}/,
  "all prompt textarea edits should use the optimization-safe change handler",
);
assert.match(
  pageSource,
  /const request = \{ id: requestId, contextKey: currentPromptOptimizationContextKey \}/,
  "optimization requests should capture the current product or portrait context",
);
assert.match(
  pageSource,
  /isPromptOptimizationResultCurrent\([\s\S]{0,240}optimizePromptContextRef\.current\[mode\]/,
  "optimization responses should be rejected when the product or portrait context changed",
);
assert.match(
  pageSource,
  /onSubjectModeChange=\{changeEditSubjectMode\}/,
  "changing the subject mode should invalidate the in-flight optimization immediately",
);
assert.match(workspaceSource, /✦ 优化提示词/);
assert.match(workspaceSource, /disabled=\{!canOptimizePrompt \|\| optimizingPrompt\}/);
assert.match(adminHelpers, /提示词优化/);

console.log("prompt optimizer tests passed");
