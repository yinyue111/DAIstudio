export type UnifiedTaskKind = "generation" | "reverse" | "parse" | "workflow";

export type UnifiedTaskStatusGroup = "active" | "succeeded" | "failed" | "canceled" | "needs_attention";

export type WorkflowNodeType =
  | "parse"
  | "reverse"
  | "manual_review"
  | "compile"
  | "generate"
  | "compose"
  | "export";

export type WorkflowRunStatus =
  | "queued"
  | "running"
  | "waiting_review"
  | "succeeded"
  | "failed"
  | "canceled"
  | "compensating";

export type WorkflowNodeStatus =
  | "queued"
  | "running"
  | "waiting_review"
  | "waiting_external"
  | "succeeded"
  | "failed"
  | "canceled";

export type WorkflowNodeAttemptKind = "execution" | "compensation";

export type WorkflowNodeAttemptStatus =
  | "running"
  | "waiting_review"
  | "waiting_external"
  | "succeeded"
  | "failed"
  | "canceled";

export type WorkflowCompensationStatus = "none" | "pending" | "running" | "succeeded" | "failed";

export interface WorkflowNodeAttempt {
  id: number;
  kind: WorkflowNodeAttemptKind;
  attempt_number: number;
  status: WorkflowNodeAttemptStatus;
  input: Record<string, unknown> | null;
  output?: Record<string, unknown> | null;
  error_code: string | null;
  error: string | null;
  external_kind: string | null;
  external_id: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface WorkflowNodeRun {
  id: number;
  key: string;
  type: WorkflowNodeType;
  depends_on: string[];
  topological_index: number;
  status: WorkflowNodeStatus;
  input: Record<string, unknown> | null;
  output?: Record<string, unknown> | null;
  error_code: string | null;
  error: string | null;
  external_kind: string | null;
  external_id: string | null;
  attempt_count: number;
  max_attempts: number;
  compensation_status: WorkflowCompensationStatus;
  compensation_error: string | null;
  started_at: string | null;
  finished_at: string | null;
  attempts?: WorkflowNodeAttempt[];
}

export interface WorkflowRun {
  id: number;
  tool_run_id: number;
  tool_definition_id: number | null;
  tool_version_id: number | null;
  quote_id: number | null;
  client_request_id: string | null;
  pricing_snapshot: Record<string, unknown>;
  cost_frozen: number;
  cost_settled: number;
  cost_refunded: number;
  cost_outstanding: number;
  status: WorkflowRunStatus;
  current_node_key: string | null;
  input: Record<string, unknown> | null;
  output?: Record<string, unknown> | null;
  error_code: string | null;
  error: string | null;
  cancel_requested: boolean;
  workflow_schema_version: string;
  nodes: WorkflowNodeRun[];
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  idempotent_replay?: boolean;
}

export interface WorkflowRunPage {
  items: WorkflowRun[];
  total: number;
}

export interface WorkflowRunCreateInput {
  tool_slug: string;
  quote_id: number;
  client_request_id: string;
  input: Record<string, unknown>;
}

export interface WorkflowReviewInput {
  decision: "approve" | "reject";
  output?: Record<string, unknown>;
  note?: string | null;
}

export interface WorkflowRetryInput {
  reason?: string | null;
}

export interface WorkflowTaskNodeSummary {
  key: string;
  type: WorkflowNodeType;
  status: WorkflowNodeStatus;
  attempt_count: number;
  max_attempts: number;
  compensation_status: WorkflowCompensationStatus;
}

export interface UnifiedTaskFailureSuggestion {
  error_type: string;
  title: string;
  message: string;
  recommended_action: string;
}

export interface UnifiedTaskRetryModel {
  model_config_id: number;
  display_name: string;
  model_id: string;
  provider: string;
  estimated_credits: number;
  route_status: string;
  reason: string;
}

export interface UnifiedTask {
  key: string;
  kind: UnifiedTaskKind;
  id: number;
  status: string;
  status_group: UnifiedTaskStatusGroup;
  category?: string | null;
  stage?: string | null;
  retry_of_task_id?: number | null;
  phase?: string | null;
  progress: number;
  title: string;
  summary?: string | null;
  model_config_id?: number | null;
  model_name?: string | null;
  model_id?: string | null;
  model_provider?: string | null;
  cost_frozen: number;
  cost_settled: number;
  project_ids: number[];
  result_refs: string[];
  available_actions: string[];
  workflow_current_node_key?: string | null;
  workflow_failed_node_key?: string | null;
  workflow_tool_slug?: string | null;
  workflow_entry_path?: string | null;
  workflow_nodes?: WorkflowTaskNodeSummary[];
  error_type?: string | null;
  error_message?: string | null;
  failure_suggestion?: UnifiedTaskFailureSuggestion | null;
  compatible_retry_models: UnifiedTaskRetryModel[];
  created_at: string;
  updated_at?: string | null;
  finished_at?: string | null;
}

export interface UnifiedTaskPage {
  items: UnifiedTask[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
  counts: Record<UnifiedTaskStatusGroup | "all", number>;
}

export interface UnifiedTaskQuery {
  kind?: UnifiedTaskKind | "all";
  status?: UnifiedTaskStatusGroup | "all";
  category?: "image" | "video" | "all";
  limit?: number;
  cursor?: string;
}
