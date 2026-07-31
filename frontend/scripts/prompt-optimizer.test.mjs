import assert from "node:assert/strict";
import fs from "node:fs";
import { readStudioSourceFromUrl } from "./studio-source.mjs";

const pageSource = readStudioSourceFromUrl(import.meta.url);
const optimizerSource = fs.readFileSync(new URL("../hooks/usePromptOptimization.js", import.meta.url), "utf8");
const storyboardActionsSource = fs.readFileSync(new URL("../hooks/useStoryboardActions.js", import.meta.url), "utf8");
const workspaceSource = fs.readFileSync(new URL("../app/studio/StudioPromptWorkspace.jsx", import.meta.url), "utf8");
const creationConsoleSource = fs.readFileSync(
  new URL("../app/studio/StudioCreationConsole.jsx", import.meta.url),
  "utf8",
);
const constantsSource = fs.readFileSync(new URL("../app/studio/constants.ts", import.meta.url), "utf8");
const apiSource = fs.readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
const adminHelpers = fs.readFileSync(new URL("../app/admin/components/admin-helpers.js", import.meta.url), "utf8");
const optimizeDirectStart = optimizerSource.indexOf("async function optimize()");
const optimizeDirectEnd = optimizerSource.indexOf("async function accept(", optimizeDirectStart);
const compileShotStart = storyboardActionsSource.indexOf("async function compileStoryboardShot(");
const applyShotStart = storyboardActionsSource.indexOf("async function applyStoryboardShot(", compileShotStart);
const optimizeDirectSource = optimizerSource.slice(optimizeDirectStart, optimizeDirectEnd);
const compileShotSource = storyboardActionsSource.slice(compileShotStart, applyShotStart);

assert.match(apiSource, /\/api\/studio\/prompt-optimizations/);
assert.match(
  optimizerSource,
  /function requestOptions\([\s\S]{0,500}promptText = String\(prompt \|\| ""\)\.trim\(\)[\s\S]{0,500}prompt: String\(promptText \|\| ""\)\.trim\(\)/,
  "direct optimization should always send the current editor text snapshot",
);
assert.doesNotMatch(
  optimizerSource.slice(optimizerSource.indexOf("function requestOptions("), optimizeDirectStart),
  /reverse_operation_id|reverse_revision_id|lineage/,
  "direct optimization must not replace edited text with an older reverse revision",
);
assert.match(
  optimizerSource,
  /function requestOptions\([\s\S]{0,2200}mode: direction,[\s\S]{0,400}target_model_config_id:/,
  "optimization should explicitly identify its mode and versioned target catalog model",
);
assert.match(
  optimizeDirectSource,
  /kind: "prompt_optimization"[\s\S]{0,500}clientRequestId: actionRequestId[\s\S]{0,700}api\.createStudioPromptOptimization\(confirmedRequest/,
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
assert.match(optimizerSource, /const ready = Boolean\(String\(prompt \|\| ""\)\.trim\(\)\)/);
assert.doesNotMatch(
  optimizeDirectSource,
  /!promptDirty/,
  "editor text should remain optimizable even when it came from reverse analysis or a saved workspace",
);
assert.match(
  optimizeDirectSource,
  /requestOptions\(\{ promptText: source \}\)/,
  "the optimization request should use the text captured at click time",
);
assert.match(
  optimizeDirectSource,
  /if \(!optimized\)[\s\S]{0,160}refreshMe\(\)[\s\S]{0,160}setProposals\(/,
  "a successfully settled proposal should refresh the visible account balance",
);
assert.match(
  optimizeDirectSource,
  /failedAndRefunded[\s\S]{0,240}clearPendingStudioActionRequest[\s\S]{0,180}attempt === 0\) continue/,
  "a failed and refunded replay should receive a fresh request id and retry once",
);
assert.match(
  optimizerSource,
  /function updatePromptFromUser\([\s\S]{0,160}invalidate\(\)[\s\S]{0,120}setPrompt\(valueOrUpdater\)/,
  "editing the prompt should invalidate any in-flight optimization response",
);
assert.match(
  optimizerSource,
  /function invalidate\([\s\S]{0,180}abortMode\(mode\)[\s\S]{0,180}requestRef\.current\[mode\]/,
  "invalidating edited prompt input should abort its active optimization request before ignoring stale state",
);
assert.match(
  optimizeDirectSource,
  /createStudioPromptOptimization\(confirmedRequest, \{ signal: controller\.signal \}\)/,
  "paid prompt optimization should pass its cancellation signal through quote execution",
);
assert.match(
  optimizerSource.slice(optimizerSource.indexOf("async function accept("), optimizerSource.indexOf("async function reject()")),
  /acceptStudioPromptOptimization[\s\S]{0,900}refreshMe\(\)[\s\S]{0,300}applyPromptOptimizationDecision/,
  "accepting a paid optimization should refresh the visible balance after settlement",
);
assert.match(
  creationConsoleSource,
  /onPromptChange=\{updatePromptFromUser\}/,
  "all prompt textarea edits should use the optimization-safe change handler",
);
assert.match(
  optimizerSource,
  /const request = \{ id: requestId, contextKey: currentContextKey \}/,
  "optimization requests should capture the current product or portrait context",
);
assert.match(
  optimizerSource,
  /isPromptOptimizationResultCurrent\([\s\S]{0,240}contextRef\.current\[mode\]/,
  "optimization responses should be rejected when the product or portrait context changed",
);
assert.match(
  creationConsoleSource,
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
assert.match(optimizerSource, /recordsRef/);
assert.match(
  optimizerSource,
  /function requestOptions\(\{[\s\S]{0,160}direction = setting\.direction/,
  "prompt optimization should default to the direction selected for the active workspace",
);
assert.match(optimizerSource, /direction: result\.mode \|\| setting\.direction/);
assert.match(optimizerSource, /compiler_metadata: result\.compiler_profile/);
assert.match(
  optimizerSource,
  /applyPromptOptimizationDecision\([\s\S]{0,180}latestWorkspace,[\s\S]{0,180}decision\?\.result,[\s\S]{0,180}decision\?\.accepted_segment_ids/,
  "accepted decisions should apply the authoritative server payload to the latest workspace",
);
assert.match(optimizerSource, /undoPromptOptimization\(latestWorkspace, undo\)/);
assert.match(
  optimizerSource,
  /function changeGenerationModelSelection[\s\S]{0,1800}reverse_revision_id[\s\S]{0,500}mode: "target_model_adaptation"/,
  "switching a generation model should compile the existing revision without launching reverse analysis",
);
assert.match(workspaceSource, /约束覆盖证据/);
assert.match(workspaceSource, /选择要应用的字段/);
assert.match(workspaceSource, /isSupportedPromptOptimizationSegment\(item\.field_path\)/);
assert.match(workspaceSource, /supportedChangeSummary\.map\(\(item\) =>/);
assert.doesNotMatch(workspaceSource, /保留要求和禁止变化/);
assert.match(optimizerSource, /raw_text:[\s\S]{0,300}optimized_text:[\s\S]{0,300}optimizer_model_id:/);
assert.match(optimizerSource, /scopeKey/);
assert.doesNotMatch(
  optimizerSource,
  /function updatePromptFromUser\([^)]*\) \{[\s\S]{0,160}delete recordsRef[\s\S]{0,80}setPrompt/,
  "manual edits after optimization should retain raw and optimized prompt provenance",
);

console.log("prompt optimizer tests passed");
