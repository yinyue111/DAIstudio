import assert from "node:assert/strict";
import fs from "node:fs";

const pageSource = fs.readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");
const workspaceSource = fs.readFileSync(new URL("../app/studio/StudioPromptWorkspace.jsx", import.meta.url), "utf8");
const constantsSource = fs.readFileSync(new URL("../app/studio/constants.ts", import.meta.url), "utf8");
const apiSource = fs.readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
const adminHelpers = fs.readFileSync(new URL("../app/admin/components/admin-helpers.js", import.meta.url), "utf8");
const optimizeDirectStart = pageSource.indexOf("async function optimizeDirectPrompt()");
const compileShotStart = pageSource.indexOf("async function compileStoryboardShot(", optimizeDirectStart);
const applyShotStart = pageSource.indexOf("async function applyStoryboardShot(", compileShotStart);
const optimizeDirectSource = pageSource.slice(optimizeDirectStart, compileShotStart);
const compileShotSource = pageSource.slice(compileShotStart, applyShotStart);

assert.match(apiSource, /\/api\/studio\/prompt-optimizations/);
assert.match(
  pageSource,
  /function promptOptimizationRequestOptions\([\s\S]{0,2400}reverse_operation_id:[\s\S]{0,300}reverse_revision_id:[\s\S]{0,800}lineage \|\| \{[\s\S]{0,400}prompt:/,
  "lineage-backed optimization should send only server identities while ordinary prompts send text",
);
assert.match(
  pageSource,
  /function promptOptimizationRequestOptions\([\s\S]{0,2600}mode: direction,[\s\S]{0,400}target_model_config_id:/,
  "optimization should explicitly identify its mode and versioned target catalog model",
);
assert.match(
  optimizeDirectSource,
  /kind: "prompt_optimization"[\s\S]{0,500}clientRequestId: actionRequestId[\s\S]{0,500}api\.createStudioPromptOptimization\(confirmedRequest\)/,
  "paid Studio rewrites should quote first and execute the confirmed proposal request",
);
assert.match(optimizeDirectSource, /idempotency_key: actionRequestId/);
assert.doesNotMatch(
  optimizeDirectSource,
  /client_request_id/,
  "the extra=forbid prompt proposal body must keep its request id in the quote envelope only",
);
assert.doesNotMatch(optimizeDirectSource, /crypto\.randomUUID/);
assert.match(
  compileShotSource,
  /prompt: source[\s\S]{0,500}mode: "target_model_adaptation"[\s\S]{0,300}target_model_config_id:[\s\S]{0,700}api\.createStudioPromptOptimization/,
  "storyboard shots should use the free Studio model compiler with an ordinary prompt",
);
assert.doesNotMatch(compileShotSource, /requestQuoteConfirmation|api\.optimizePrompt/);
assert.doesNotMatch(apiSource, /\/api\/prompt\/optimize|optimizePrompt:/);
assert.match(apiSource, /acceptStudioPromptOptimization[\s\S]{0,300}\/accept/);
assert.match(apiSource, /rejectStudioPromptOptimization[\s\S]{0,300}\/reject/);
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
assert.match(workspaceSource, /生成优化建议/);
assert.match(workspaceSource, /编译到当前模型/);
assert.match(workspaceSource, /PROMPT_OPTIMIZATION_DIRECTIONS/);
assert.match(workspaceSource, /预计 \{Math\.max\(0, Number\(estimatedCredits/);
assert.match(workspaceSource, /disabled=\{!canOptimizePrompt \|\| optimizingPrompt\}/);
assert.match(workspaceSource, /use="prompt"/);
assert.doesNotMatch(workspaceSource, /提示词库|EDIT_PROMPT_CHIPS|promptChipsForEditMode|onAppendPrompt|onTogglePromptLibrary/);
assert.doesNotMatch(constantsSource, /EXAMPLES|赛博朋克城市夜景|宇航服柴犬|极简北欧风咖啡馆|国潮水墨山水/);
assert.doesNotMatch(pageSource, /promptLibraryOpen|setPromptLibraryOpen|<PromptLibraryBrowser/);
assert.match(adminHelpers, /对话 \/ 提示词/);
assert.match(pageSource, /promptOptimizationRecordsRef/);
assert.match(
  pageSource,
  /function promptOptimizationRequestOptions\(\{[\s\S]{0,160}direction = promptOptimizationSetting\.direction/,
  "prompt optimization should default to the direction selected for the active workspace",
);
assert.match(pageSource, /direction: result\.mode \|\| promptOptimizationSetting\.direction/);
assert.match(pageSource, /compiler_metadata: result\.compiler_profile/);
assert.match(
  pageSource,
  /applyPromptOptimizationDecision\([\s\S]{0,180}latestWorkspace,[\s\S]{0,180}decision\?\.result,[\s\S]{0,180}decision\?\.accepted_segment_ids/,
  "accepted decisions should apply the authoritative server payload to the latest workspace",
);
assert.match(pageSource, /undoPromptOptimization\(latestWorkspace, undo\)/);
assert.match(
  pageSource,
  /function changeGenerationModelSelection[\s\S]{0,1800}reverse_revision_id[\s\S]{0,500}mode: "target_model_adaptation"/,
  "switching a generation model should compile the existing revision without launching reverse analysis",
);
assert.match(workspaceSource, /约束覆盖证据/);
assert.match(workspaceSource, /选择要应用的字段/);
assert.match(workspaceSource, /isSupportedPromptOptimizationSegment\(item\.field_path\)/);
assert.match(workspaceSource, /supportedChangeSummary\.map\(\(item\) =>/);
assert.doesNotMatch(workspaceSource, /保留要求和禁止变化/);
assert.match(pageSource, /raw_text:[\s\S]{0,300}optimized_text:[\s\S]{0,300}optimizer_model_id:/);
assert.match(pageSource, /promptOptimizationScopeKey/);
assert.doesNotMatch(
  pageSource,
  /function updatePromptFromUser\([^)]*\) \{[\s\S]{0,160}delete promptOptimizationRecordsRef[\s\S]{0,80}setPrompt/,
  "manual edits after optimization should retain raw and optimized prompt provenance",
);

console.log("prompt optimizer tests passed");
