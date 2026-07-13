import assert from "node:assert/strict";
import fs from "node:fs";

const pageSource = fs.readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");
const workspaceSource = fs.readFileSync(new URL("../app/studio/StudioPromptWorkspace.jsx", import.meta.url), "utf8");
const apiSource = fs.readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
const adminHelpers = fs.readFileSync(new URL("../app/admin/components/admin-helpers.js", import.meta.url), "utf8");

assert.match(apiSource, /\/api\/prompt\/optimize/);
assert.match(pageSource, /api\.optimizePrompt\(source, \{[\s\S]{0,1200}duration:[\s\S]{0,1200}subject_mode:[\s\S]{0,1200}reference_type:[\s\S]{0,1200}subject_profile:/);
assert.match(pageSource, /api\.optimizePrompt\(source, \{[\s\S]{0,1600}aspect_ratio:[\s\S]{0,1600}resolution:[\s\S]{0,1600}product_lock_mode:[\s\S]{0,1600}product_video_template:/);
assert.match(apiSource, /optimizePrompt:\s*\(prompt, options[\s\S]{0,500}body:\s*\{ prompt, \.\.\.context \}/);
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
assert.match(pageSource, /promptOptimizationRecordsRef/);
assert.match(pageSource, /raw_text:[\s\S]{0,300}optimized_text:[\s\S]{0,300}optimizer_model_id:/);
assert.match(pageSource, /promptOptimizationScopeKey/);
assert.doesNotMatch(
  pageSource,
  /function updatePromptFromUser\([^)]*\) \{[\s\S]{0,160}delete promptOptimizationRecordsRef[\s\S]{0,80}setPrompt/,
  "manual edits after optimization should retain raw and optimized prompt provenance",
);

console.log("prompt optimizer tests passed");
