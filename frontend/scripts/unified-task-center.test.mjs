import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const api = readFileSync(join(root, "lib/api.js"), "utf8");
const types = readFileSync(join(root, "lib/api.d.ts"), "utf8");
const hook = readFileSync(join(root, "hooks/useUnifiedTaskCenter.js"), "utf8");
const component = readFileSync(join(root, "components/GlobalTaskCenter.jsx"), "utf8");
const detailDrawer = readFileSync(join(root, "components/TaskDetailDrawer.jsx"), "utf8");
const historyPage = readFileSync(join(root, "app/history/page.jsx"), "utf8");
const unifiedTasksSource = readFileSync(join(root, "lib/unifiedTasks.ts"), "utf8");

assert.match(api, /taskCenter:\s*\(/, "API client should expose the unified task endpoint");
assert.match(api, /\/api\/task-center/, "API client should call the unified task endpoint");
assert.match(types, /interface UnifiedTask/, "API declarations should describe unified task items");
assert.match(types, /interface UnifiedTaskFailureSuggestion/, "task declarations should describe structured failure suggestions");
assert.match(types, /interface UnifiedTaskRetryModel/, "task declarations should describe compatible retry models");
assert.match(types, /failure_suggestion\?: UnifiedTaskFailureSuggestion \| null/, "unified tasks should carry structured failure suggestions");
assert.match(types, /compatible_retry_models: UnifiedTaskRetryModel\[\]/, "unified tasks should carry compatible retry candidates");
assert.match(hook, /api\.eventWsTicket\(\)/, "task center should use the authenticated user event stream");
assert.match(hook, /ACTIVE_POLL_MS/, "task center should retain polling when push is unavailable");
assert.match(hook, /loadMore/, "task center should support cursor pagination");
assert.match(component, /project_ids/, "task rows should link their associated projects");
assert.match(component, /cancelReverseOperation/, "reverse cancellation should use its existing API");
assert.match(component, /retryReverseOperation/, "reverse retries should use their existing API");
assert.match(component, /cancelTask/, "generation cancellation should use its existing API");
assert.doesNotMatch(component, /api\.retryTask\(/, "task center must not mutate a failed generation task in place");
assert.match(api, /requoteRetryTask/, "API client should expose generation requote retries");
assert.match(api, /\/retry\/requote/, "requote retries should call the dedicated endpoint");
assert.match(types, /requoteRetryTask/, "API declarations should include generation requote retries");
assert.match(types, /body: \{ client_request_id: string; model_config_id\?: number \| null \}/, "generation requote declarations should accept a selected model");
assert.match(types, /interface ReverseOperationRetryInput\s*\{[\s\S]*?quote_id: number;[\s\S]*?model_config_id\?: number \| null;/, "reverse retry declarations should require a quote and accept a selected model");
assert.match(component, /当前模型重试/, "task center should expose current-price retries without quote UI wording");
assert.match(component, /generation-retry/, "generation retries should use a fresh idempotency request id");
assert.match(component, /retry_compatible_model: "切换模型重试"/, "task actions should label compatible-model retry explicitly");
assert.match(component, /<select[\s\S]*?task\.compatible_retry_models\.map/, "compatible retry should use a candidate model select");
assert.match(component, /onClick=\{\(\) => runAction\(task, "retry_compatible_model"\)\}/, "compatible retry should submit directly from its action button");
assert.doesNotMatch(component, /CompatibleRetryConfirmation|确认创建新的重试任务/, "compatible retry must not open a price confirmation dialog");
assert.match(component, /candidate\.estimated_credits/, "compatible model selection should retain estimated credits");
assert.match(component, /api\.requoteRetryTask\(task\.id, \{\s*client_request_id: request\.client_request_id,\s*model_config_id: candidate\.model_config_id,/m, "generation compatible retry should submit the selected model and stable request id directly");
assert.match(component, /quoteReverseRetry\(\s*task,\s*request\.client_request_id,\s*candidate\.model_config_id,/m, "reverse compatible retry should obtain a source-bound billing token before direct execution");
assert.match(component, /api\.retryReverseOperation\(task\.id, \{\s*client_request_id: quoted\.request\.client_request_id,\s*model_config_id: quoted\.request\.model_config_id,\s*quote_id: quoted\.quote_id,/m, "reverse compatible retry should submit the selected model, stable request id, and billing token");
assert.match(component, /kind: "reverse",\s*client_request_id: clientRequestId,\s*request,/m, "reverse retries should request a server-authoritative quote");
assert.match(component, /busyRef\.current/, "task actions should synchronously prevent duplicate submission");
assert.match(component, /sameModelRetryIdsRef\.current\.get/, "same-model network retries should reuse their idempotency id");
assert.match(component, /sameModelRetryIdsRef\.current\.delete/, "same-model retry ids should clear only after a successful refresh");
assert.match(component, /已切换至「\$\{candidate\.display_name\}」并创建新的重试任务/, "switched-model retry should have a distinct success message");
assert.match(component, /已使用当前模型「\$\{candidate\.display_name\}」创建新的重试任务/, "same-model retry should have a distinct success message");
assert.match(component, /重试自任务/, "retry chains should be visible in task metadata");
assert.match(api, /createWorkflowRun:\s*\(/, "API client should create workflow runs");
assert.match(api, /workflowRun:\s*\(/, "API client should get a workflow run");
assert.match(api, /workflowRuns:\s*\(/, "API client should list workflow runs");
assert.match(api, /resumeWorkflowRun:\s*\(/, "API client should resume workflow runs");
assert.match(api, /cancelWorkflowRun:\s*\(/, "API client should cancel workflow runs");
assert.match(api, /reviewWorkflowNode:\s*\(/, "API client should review workflow nodes");
assert.match(api, /retryWorkflowNode:\s*\(/, "API client should retry workflow nodes");
assert.match(api, /\/api\/workflows\/runs/, "workflow API methods should use the owner-gated routes");
assert.match(types, /interface WorkflowRun\s*\{/, "API declarations should describe workflow runs");
assert.match(types, /interface WorkflowNodeRun\s*\{/, "API declarations should describe workflow nodes");
assert.match(types, /interface WorkflowNodeAttempt\s*\{/, "API declarations should describe workflow attempts");
assert.match(types, /UnifiedTaskKind = "generation" \| "reverse" \| "parse" \| "workflow"/, "workflow should be a unified task kind");
assert.match(component, /\["workflow", "工作流"\]/, "task filters should include workflows");
assert.match(component, /api\.cancelWorkflowRun/, "active workflows should be cancellable");
assert.match(component, /api\.resumeWorkflowRun/, "active workflows should be resumable");
assert.match(component, /api\.reviewWorkflowNode/, "waiting reviews should continue through the review API");
assert.match(component, /api\.retryWorkflowNode/, "failed workflow nodes should use the node retry API");
assert.match(component, /\/api\/me\/assets\/download\?asset_ref=/, "workflow results should download by owner-gated asset ref");
assert.match(component, /taskAssetId\(assetRef\)/, "task downloads should identify legacy generation asset refs");
assert.match(component, /normalizedRef = generatedId \? `g\.\$\{generatedId\}` : assetRef/, "legacy generation asset refs should use the current download contract");
assert.match(historyPage, /useSearchParams/, "history should consume task detail deep links from the URL");
assert.match(historyPage, /searchParams\.get\("task"\)/, "history should open the task identified by the query");
assert.match(historyPage, /next\.delete\("task"\)/, "closing task detail should clear only the task query");
assert.match(historyPage, /<TaskDetailDrawer/, "history should render the shared task detail drawer");
assert.match(detailDrawer, /api\.task\(parsed\.id\)/, "generation details should use the existing owner-gated API");
assert.match(detailDrawer, /api\.reverseOperation\(parsed\.id\)/, "reverse details should use the existing owner-gated API");
assert.match(detailDrawer, /api\.parseStatus\(parsed\.id\)/, "parse details should use the existing owner-gated API");
assert.match(detailDrawer, /api\.workflowRun\(parsed\.id\)/, "workflow details should use the existing owner-gated API");
assert.match(detailDrawer, /输入快照/, "task detail should expose its input snapshot");
assert.match(detailDrawer, /结果下载/, "task detail should expose owner-gated result downloads");
assert.match(detailDrawer, /`g\.\$\{generatedId\}`/, "legacy generated asset refs should normalize to the current owner-gated ref");
assert.match(detailDrawer, /`\/api\/assets\/\$\{assetId\}\/download`/, "generation details should download numeric asset ids through the owner-gated endpoint");
assert.match(detailDrawer, /任务链路/, "task detail should expose retry and parent lineage");
assert.match(detailDrawer, /\/projects\?project=/, "task details should deep-link associated projects");
assert.match(detailDrawer, /失败建议/, "task detail should display structured failure suggestions");
assert.match(detailDrawer, /建议操作/, "task detail should display the recommended failure action");
assert.match(detailDrawer, /已验证的兼容模型/, "task detail should display compatible retry candidates");
assert.match(detailDrawer, /提交时会自动校验模型与账户状态/, "task detail should explain silent validation without asking for price confirmation");
assert.match(detailDrawer, /const errors = \[\.\.\.new Set\(/, "task detail should de-duplicate compatible error fields");
assert.match(detailDrawer, /\["active", "needs_attention"\]\.includes\(group\)/, "completed or failed tasks should not show misleading progress");

const compiledTasks = ts.transpileModule(unifiedTasksSource, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const taskHelpers = await import(`data:text/javascript;base64,${Buffer.from(compiledTasks).toString("base64")}`);
const normalized = taskHelpers.normalizeUnifiedTaskPage({
  items: [
    null,
    { kind: "workflow", id: 0 },
    {
      kind: "workflow",
      id: 42,
      status: "waiting_review",
      status_group: "needs_attention",
      title: "分镜合成",
      progress: 55,
      result_refs: [null, "", "generated:77", "generated:77"],
      available_actions: ["review", "cancel", null],
      workflow_current_node_key: "review",
      workflow_nodes: [
        {
          key: "review",
          type: "manual_review",
          status: "waiting_review",
          attempt_count: 1,
          max_attempts: 3,
          compensation_status: "none",
        },
        { key: "invalid", type: "unknown", status: "running" },
      ],
      created_at: "2026-07-18T10:00:00Z",
    },
  ],
  counts: { active: "2", needs_attention: 1, all: 3, unexpected: 999 },
  total: 3,
});
assert.equal(normalized.items.length, 1, "malformed task rows should be discarded");
assert.deepEqual(normalized.items[0].result_refs, ["generated:77"], "asset refs should be normalized without reading an output field");
assert.deepEqual(normalized.items[0].available_actions, ["review", "cancel"]);
assert.equal(normalized.items[0].workflow_nodes.length, 1, "malformed workflow node summaries should be discarded");
assert.equal(taskHelpers.workflowNodeKeyForAction(normalized.items[0], "review"), "review");
assert.equal(taskHelpers.taskAssetRef(normalized.items[0]), "generated:77");
assert.equal(taskHelpers.taskAssetId("generated:77"), 77);
assert.equal(taskHelpers.taskAssetId("g.77"), 77);
assert.equal(taskHelpers.taskAssetId("generated:0"), null);
assert.deepEqual(taskHelpers.parseUnifiedTaskKey("workflow:42"), { key: "workflow:42", kind: "workflow", id: 42 });
assert.deepEqual(taskHelpers.parseUnifiedTaskKey("generation:9007199254740991"), {
  key: "generation:9007199254740991",
  kind: "generation",
  id: 9007199254740991,
});
assert.equal(taskHelpers.parseUnifiedTaskKey("generation:0"), null);
assert.equal(taskHelpers.parseUnifiedTaskKey("unknown:42"), null);
assert.equal(taskHelpers.parseUnifiedTaskKey("generation:9007199254740992"), null);
assert.equal(normalized.counts.active, 2);
assert.equal(normalized.counts.unexpected, undefined, "unknown count keys should not leak into UI state");

const retryCandidate = {
  model_config_id: 11,
  display_name: "兼容图像 Pro",
  model_id: "compatible-image-pro",
  provider: "provider-a",
  estimated_credits: 28,
  route_status: "available",
  reason: "支持当前图片编辑参数",
};
const secondRetryCandidate = {
  model_config_id: 12,
  display_name: "兼容图像 Fast",
  model_id: "compatible-image-fast",
  provider: "provider-b",
  estimated_credits: 18,
  route_status: "available",
  reason: "支持当前参考图",
};
const compatiblePage = taskHelpers.normalizeUnifiedTaskPage({
  items: [
    {
      kind: "generation",
      id: 71,
      status: "failed",
      status_group: "failed",
      model_config_id: 10,
      title: "失败图片任务",
      available_actions: ["view", "retry", "retry_requote", "retry_compatible_model"],
      failure_suggestion: {
        error_type: "provider_error",
        title: "上游模型执行失败",
        message: "可以切换已验证兼容的模型。",
        recommended_action: "retry_compatible_model",
      },
      compatible_retry_models: [
        { ...retryCandidate, model_config_id: 10 },
        retryCandidate,
        { ...retryCandidate, display_name: "重复候选" },
        secondRetryCandidate,
        { ...retryCandidate, model_config_id: "13" },
        { ...retryCandidate, model_config_id: 14, estimated_credits: -1 },
        { ...retryCandidate, model_config_id: 15, estimated_credits: Number.NaN },
        { ...retryCandidate, model_config_id: 16, provider: "" },
      ],
      created_at: "2026-07-20T10:00:00Z",
    },
    {
      kind: "reverse",
      id: 72,
      status: "failed",
      status_group: "failed",
      model_config_id: 20,
      title: "失败反推任务",
      available_actions: ["view", "retry", "retry_compatible_model"],
      failure_suggestion: {
        error_type: "provider_timeout",
        title: "上游模型响应超时",
        message: "可以切换模型。",
        recommended_action: "retry_compatible_model",
      },
      compatible_retry_models: [{ ...retryCandidate, model_config_id: 0 }],
      created_at: "2026-07-20T10:01:00Z",
    },
  ],
  counts: { failed: 2, all: 2 },
  total: 2,
});
assert.deepEqual(
  compatiblePage.items[0].compatible_retry_models,
  [retryCandidate, secondRetryCandidate],
  "compatible models should exclude the current model and malformed or duplicate candidates",
);
assert.deepEqual(
  compatiblePage.items[0].available_actions,
  ["view", "retry_requote", "retry_compatible_model"],
  "legacy in-place generation retry should be removed while compatible retry remains available",
);
assert.equal(compatiblePage.items[0].failure_suggestion.title, "上游模型执行失败");
assert.deepEqual(
  compatiblePage.items[1].available_actions,
  ["view", "retry"],
  "compatible retry should be hidden when no valid candidate exists",
);
assert.equal(
  compatiblePage.items[1].failure_suggestion.recommended_action,
  "retry",
  "a failed reverse task should not recommend a hidden compatible-model action",
);

const firstRetryRequest = taskHelpers.prepareCompatibleRetryRequest(compatiblePage.items[0], 11);
const repeatedRetryRequest = taskHelpers.prepareCompatibleRetryRequest(compatiblePage.items[0], 11, firstRetryRequest);
const changedRetryRequest = taskHelpers.prepareCompatibleRetryRequest(compatiblePage.items[0], 12, firstRetryRequest);
assert.ok(firstRetryRequest.client_request_id.startsWith("generation-compatible-retry-"));
assert.strictEqual(repeatedRetryRequest, firstRetryRequest, "the same confirmation or network retry should reuse its idempotency id");
assert.notEqual(changedRetryRequest.client_request_id, firstRetryRequest.client_request_id, "changing the selected model should allocate a fresh idempotency id");
assert.equal(taskHelpers.prepareCompatibleRetryRequest(compatiblePage.items[0], 999), null, "unknown model ids should not create retry requests");

console.log("unified task center tests passed");
