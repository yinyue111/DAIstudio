import type {
  UnifiedTask,
  UnifiedTaskFailureSuggestion,
  UnifiedTaskPage,
  UnifiedTaskRetryModel,
  UnifiedTaskStatusGroup,
  WorkflowCompensationStatus,
  WorkflowNodeStatus,
  WorkflowNodeType,
  WorkflowTaskNodeSummary,
} from "./api";

export const UNIFIED_TASK_KINDS = ["all", "generation", "reverse", "parse", "workflow"] as const;
export const UNIFIED_TASK_STATUSES = [
  "all",
  "active",
  "needs_attention",
  "succeeded",
  "failed",
  "canceled",
] as const;
export const UNIFIED_TASK_CATEGORIES = ["all", "image", "video"] as const;

export const EMPTY_TASK_COUNTS: UnifiedTaskPage["counts"] = {
  active: 0,
  succeeded: 0,
  failed: 0,
  canceled: 0,
  needs_attention: 0,
  all: 0,
};

const TASK_KINDS = new Set(UNIFIED_TASK_KINDS.filter((kind) => kind !== "all"));
const STATUS_GROUPS = new Set(UNIFIED_TASK_STATUSES.filter((status) => status !== "all"));
const WORKFLOW_NODE_TYPES = new Set<WorkflowNodeType>([
  "parse",
  "reverse",
  "manual_review",
  "compile",
  "generate",
  "compose",
  "export",
]);
const WORKFLOW_NODE_STATUSES = new Set<WorkflowNodeStatus>([
  "queued",
  "running",
  "waiting_review",
  "waiting_external",
  "succeeded",
  "failed",
  "canceled",
]);
const WORKFLOW_COMPENSATION_STATUSES = new Set<WorkflowCompensationStatus>([
  "none",
  "pending",
  "running",
  "succeeded",
  "failed",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function cleanNullableString(value: unknown): string | null {
  return cleanString(value) || null;
}

function cleanNonNegativeNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

function cleanPositiveInteger(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function cleanStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(cleanString).filter(Boolean))];
}

function normalizeFailureSuggestion(value: unknown): UnifiedTaskFailureSuggestion | null {
  if (!isRecord(value)) return null;
  const errorType = cleanString(value.error_type);
  const title = cleanString(value.title);
  const message = cleanString(value.message);
  const recommendedAction = cleanString(value.recommended_action);
  if (!errorType || !title || !message || !recommendedAction) return null;
  return {
    error_type: errorType,
    title,
    message,
    recommended_action: recommendedAction,
  };
}

function normalizeRetryModel(value: unknown): UnifiedTaskRetryModel | null {
  if (!isRecord(value)) return null;
  const modelConfigId = typeof value.model_config_id === "number"
    && Number.isSafeInteger(value.model_config_id)
    && value.model_config_id > 0
    ? value.model_config_id
    : null;
  const displayName = cleanString(value.display_name);
  const modelId = cleanString(value.model_id);
  const provider = cleanString(value.provider);
  const estimatedCredits = typeof value.estimated_credits === "number"
    ? value.estimated_credits
    : Number.NaN;
  const routeStatus = cleanString(value.route_status);
  const reason = cleanString(value.reason);
  if (
    modelConfigId === null
    || !displayName
    || !modelId
    || !provider
    || !Number.isFinite(estimatedCredits)
    || estimatedCredits < 0
    || !routeStatus
    || !reason
  ) return null;
  return {
    model_config_id: modelConfigId,
    display_name: displayName,
    model_id: modelId,
    provider,
    estimated_credits: estimatedCredits,
    route_status: routeStatus,
    reason,
  };
}

function normalizeRetryModels(value: unknown, currentModelConfigId: number | null): UnifiedTaskRetryModel[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<number>();
  const models: UnifiedTaskRetryModel[] = [];
  for (const candidate of value) {
    const model = normalizeRetryModel(candidate);
    if (
      !model
      || model.model_config_id === currentModelConfigId
      || seen.has(model.model_config_id)
    ) continue;
    seen.add(model.model_config_id);
    models.push(model);
  }
  return models;
}

function statusGroupFor(status: string): UnifiedTaskStatusGroup {
  if (["queued", "running", "compensating", "waiting_external"].includes(status)) return "active";
  if (["succeeded", "done"].includes(status)) return "succeeded";
  if (status === "failed") return "failed";
  if (status === "canceled") return "canceled";
  return "needs_attention";
}

function normalizeWorkflowNode(value: unknown): WorkflowTaskNodeSummary | null {
  if (!isRecord(value)) return null;
  const key = cleanString(value.key);
  const type = cleanString(value.type) as WorkflowNodeType;
  const status = cleanString(value.status) as WorkflowNodeStatus;
  if (!key || !WORKFLOW_NODE_TYPES.has(type) || !WORKFLOW_NODE_STATUSES.has(status)) return null;
  const compensation = cleanString(value.compensation_status) as WorkflowCompensationStatus;
  return {
    key,
    type,
    status,
    attempt_count: Math.floor(cleanNonNegativeNumber(value.attempt_count)),
    max_attempts: Math.max(1, Math.floor(cleanNonNegativeNumber(value.max_attempts)) || 1),
    compensation_status: WORKFLOW_COMPENSATION_STATUSES.has(compensation) ? compensation : "none",
  };
}

export function normalizeUnifiedTask(value: unknown): UnifiedTask | null {
  if (!isRecord(value)) return null;
  const kind = cleanString(value.kind);
  const id = cleanPositiveInteger(value.id);
  if (!TASK_KINDS.has(kind as UnifiedTask["kind"]) || id === null) return null;
  const status = cleanString(value.status) || "unknown";
  const rawStatusGroup = cleanString(value.status_group);
  const statusGroup = STATUS_GROUPS.has(rawStatusGroup as UnifiedTaskStatusGroup)
    ? rawStatusGroup as UnifiedTaskStatusGroup
    : statusGroupFor(status);
  const workflowNodes = Array.isArray(value.workflow_nodes)
    ? value.workflow_nodes.map(normalizeWorkflowNode).filter((node): node is WorkflowTaskNodeSummary => Boolean(node))
    : [];
  const modelConfigId = cleanPositiveInteger(value.model_config_id);
  const compatibleRetryModels = normalizeRetryModels(value.compatible_retry_models, modelConfigId);
  const availableActions = cleanStringList(value.available_actions).filter((action) => {
    if (kind === "generation" && action === "retry") return false;
    return action !== "retry_compatible_model" || compatibleRetryModels.length > 0;
  });
  const normalizedFailureSuggestion = normalizeFailureSuggestion(value.failure_suggestion);
  const fallbackRetryAction = kind === "generation"
    ? availableActions.includes("retry_requote") ? "retry_requote" : "view"
    : availableActions.includes("retry") ? "retry" : "view";
  const failureSuggestion = normalizedFailureSuggestion?.recommended_action === "retry_compatible_model"
    && compatibleRetryModels.length === 0
    ? {
        ...normalizedFailureSuggestion,
        recommended_action: fallbackRetryAction,
      }
    : normalizedFailureSuggestion;
  return {
    key: cleanString(value.key) || `${kind}:${id}`,
    kind: kind as UnifiedTask["kind"],
    id,
    status,
    status_group: statusGroup,
    category: cleanNullableString(value.category),
    stage: cleanNullableString(value.stage),
    retry_of_task_id: cleanPositiveInteger(value.retry_of_task_id),
    phase: cleanNullableString(value.phase),
    progress: Math.min(100, cleanNonNegativeNumber(value.progress)),
    title: cleanString(value.title) || taskKindLabel(kind),
    summary: cleanNullableString(value.summary),
    model_config_id: modelConfigId,
    model_name: cleanNullableString(value.model_name),
    model_id: cleanNullableString(value.model_id),
    model_provider: cleanNullableString(value.model_provider),
    cost_frozen: cleanNonNegativeNumber(value.cost_frozen),
    cost_settled: cleanNonNegativeNumber(value.cost_settled),
    project_ids: Array.isArray(value.project_ids)
      ? [...new Set(value.project_ids.map(cleanPositiveInteger).filter((projectId): projectId is number => projectId !== null))]
      : [],
    result_refs: cleanStringList(value.result_refs),
    available_actions: availableActions,
    workflow_current_node_key: cleanNullableString(value.workflow_current_node_key),
    workflow_failed_node_key: cleanNullableString(value.workflow_failed_node_key),
    workflow_tool_slug: cleanNullableString(value.workflow_tool_slug),
    workflow_entry_path: cleanNullableString(value.workflow_entry_path),
    workflow_nodes: workflowNodes,
    error_type: cleanNullableString(value.error_type),
    error_message: cleanNullableString(value.error_message),
    failure_suggestion: failureSuggestion,
    compatible_retry_models: compatibleRetryModels,
    created_at: cleanString(value.created_at),
    updated_at: cleanNullableString(value.updated_at),
    finished_at: cleanNullableString(value.finished_at),
  };
}

export function normalizeUnifiedTaskPage(value: unknown): UnifiedTaskPage {
  const payload = value && typeof value === "object" ? value as Partial<UnifiedTaskPage> : {};
  const rawCounts = payload.counts && typeof payload.counts === "object" ? payload.counts : {};
  return {
    items: Array.isArray(payload.items)
      ? payload.items.map(normalizeUnifiedTask).filter((task): task is UnifiedTask => Boolean(task))
      : [],
    next_cursor: typeof payload.next_cursor === "string" ? payload.next_cursor : null,
    has_more: Boolean(payload.has_more),
    total: Math.max(0, Number(payload.total) || 0),
    counts: {
      active: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).active),
      succeeded: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).succeeded),
      failed: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).failed),
      canceled: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).canceled),
      needs_attention: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).needs_attention),
      all: cleanNonNegativeNumber((rawCounts as Record<string, unknown>).all),
    } as UnifiedTaskPage["counts"],
  };
}

export function mergeUnifiedTasks(current: UnifiedTask[], incoming: UnifiedTask[], replace = false): UnifiedTask[] {
  if (replace) return incoming;
  const incomingKeys = new Set(incoming.map((task) => task.key));
  return [
    ...incoming,
    ...current.filter((task) => !incomingKeys.has(task.key)),
  ].filter((task, index, tasks) => tasks.findIndex((candidate) => candidate.key === task.key) === index);
}

export function isLiveUnifiedTask(task: UnifiedTask | null | undefined): boolean {
  return task?.status_group === "active" || task?.status_group === "needs_attention";
}

export function taskKindLabel(kind: string): string {
  return { generation: "生成", reverse: "反推", parse: "链接解析", workflow: "工作流" }[kind] || kind || "任务";
}

export function taskStatusLabel(status: string, statusGroup?: UnifiedTaskStatusGroup): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "处理中",
    succeeded: "已完成",
    done: "已完成",
    failed: "失败",
    canceled: "已取消",
    needs_review: "待确认",
    needs_confirmation: "待确认",
    compensating: "正在回滚",
    waiting_review: "待审核",
    waiting_external: "等待外部处理",
  };
  return labels[status] || {
    active: "处理中",
    succeeded: "已完成",
    failed: "失败",
    canceled: "已取消",
    needs_attention: "待确认",
  }[statusGroup || ""] || "状态未知";
}

export function taskStatusClass(statusGroup: UnifiedTaskStatusGroup): string {
  return {
    active: "bg-aqua/15 text-aqua",
    succeeded: "bg-good/15 text-good",
    failed: "bg-bad/15 text-bad",
    canceled: "bg-white/10 text-fog",
    needs_attention: "bg-warn/15 text-warn",
  }[statusGroup] || "bg-white/10 text-fog";
}

export function taskAssetId(ref: string): number | null {
  const match = /^(?:g\.|generated:)(\d+)$/.exec(String(ref || ""));
  const id = Number(match?.[1]);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

export function taskAssetRef(task: Pick<UnifiedTask, "result_refs">): string | null {
  return cleanStringList(task.result_refs)[0] || null;
}

export function workflowNodeKeyForAction(
  task: Pick<UnifiedTask, "workflow_current_node_key" | "workflow_failed_node_key" | "workflow_nodes">,
  action: "review" | "retry",
): string | null {
  const explicit = action === "review"
    ? cleanNullableString(task.workflow_current_node_key)
    : cleanNullableString(task.workflow_failed_node_key);
  if (explicit) return explicit;
  const expectedStatus = action === "review" ? "waiting_review" : "failed";
  const node = Array.isArray(task.workflow_nodes)
    ? task.workflow_nodes.find((candidate) => candidate?.status === expectedStatus)
    : null;
  return cleanNullableString(node?.key);
}

export function taskDetailHref(task: Pick<UnifiedTask, "key">): string {
  return `/history?task=${encodeURIComponent(task.key)}`;
}

export function parseUnifiedTaskKey(value: unknown): {
  key: string;
  kind: UnifiedTask["kind"];
  id: number;
} | null {
  const match = /^(generation|reverse|parse|workflow):([1-9]\d*)$/.exec(cleanString(value));
  if (!match) return null;
  const id = Number(match[2]);
  if (!Number.isSafeInteger(id)) return null;
  return {
    key: `${match[1]}:${id}`,
    kind: match[1] as UnifiedTask["kind"],
    id,
  };
}

export function createUnifiedTaskRequestId(prefix = "task-center"): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${uuid || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

export function prepareCompatibleRetryRequest(
  task: Pick<UnifiedTask, "key" | "kind" | "id" | "compatible_retry_models">,
  modelConfigId: unknown,
  current?: {
    task_key: string;
    model_config_id: number;
    client_request_id: string;
  } | null,
): {
  task_key: string;
  model_config_id: number;
  client_request_id: string;
} | null {
  const selectedId = cleanPositiveInteger(modelConfigId);
  if (selectedId === null) return null;
  const candidate = task.compatible_retry_models.find((item) => item.model_config_id === selectedId);
  if (!candidate) return null;
  if (
    current?.task_key === task.key
    && current.model_config_id === selectedId
    && cleanString(current.client_request_id)
  ) {
    return current;
  }
  return {
    task_key: task.key,
    model_config_id: selectedId,
    client_request_id: createUnifiedTaskRequestId(`${task.kind}-compatible-retry`),
  };
}
