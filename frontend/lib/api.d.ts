export const API_BASE: string;

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryAfter?: number;
}

export type AssetType = "image" | "video";
export type ReverseTarget = AssetType | "product_profile" | "portrait_profile";
export type ReverseAnalysisPrecision = "fast" | "standard" | "fine";
export type ReverseAnalysisFocus =
  | "comprehensive"
  | "replica"
  | "style"
  | "product_ad"
  | "portrait"
  | "composition_lighting"
  | "poster_layout"
  | "camera_motion"
  | "subject_action"
  | "storyboard"
  | "editing_rhythm"
  | "audio_script";
export type ReverseOutputPurpose = "generation" | "style_transfer" | "edit" | "storyboard" | "analysis_report";
export type ReverseReferenceRole =
  | "primary"
  | "subject"
  | "product"
  | "style"
  | "composition"
  | "lighting"
  | "text_layout"
  | "negative"
  | "motion"
  | "first_frame"
  | "last_frame";
export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "needs_review" | "canceled";
export type ReverseOperationStatus = "queued" | "running" | "needs_confirmation" | "succeeded" | "failed" | "canceled";
export type AnalyzerHealthStatus = "available" | "partial" | "degraded" | "unsupported";

export interface AnalyzerHealthFeature {
  status: AnalyzerHealthStatus;
  analyzer?: string | null;
  analyzer_version?: string | null;
  capability?: string | null;
  degraded_reason?: string | null;
  enabled?: boolean;
  configured?: boolean;
  verification_status?: string | null;
  [key: string]: unknown;
}

export interface AudioAnalyzerHealth {
  contract_version: string;
  status: AnalyzerHealthStatus;
  analysis_enabled: boolean;
  signal?: AnalyzerHealthFeature;
  asr_provider?: AnalyzerHealthFeature;
  asr: AnalyzerHealthFeature;
  speaker: AnalyzerHealthFeature;
  music: AnalyzerHealthFeature;
  beat: AnalyzerHealthFeature;
  sfx: AnalyzerHealthFeature;
}

export interface ReverseAnalyzerStatus {
  image: Record<string, unknown>;
  video: Record<string, unknown>;
  audio: AudioAnalyzerHealth;
}
export type UnifiedTaskKind = "generation" | "reverse" | "parse" | "workflow";
export type UnifiedTaskStatusGroup = "active" | "succeeded" | "failed" | "canceled" | "needs_attention";

export interface AppNavigationItem {
  key: string;
  label: string;
  href: string;
  width: "w-16" | "w-20" | "w-24";
  permission?: "admin";
  reserve_desktop?: boolean;
  enabled: boolean;
  visible: boolean;
  disabled_reason: string | null;
}

export interface AppNavigationCatalog {
  schema_version: number;
  default_key: string;
  items: AppNavigationItem[];
}

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

export type CreditTransactionType = "grant" | "freeze" | "settle" | "refund" | "unlock" | "consume";
export type BillingKind = "generation" | "reverse" | "workflow" | "prompt" | "unlock" | "recharge" | "grant" | "other";

export interface CreditTransaction {
  id: number;
  type: CreditTransactionType;
  change: number;
  balance_delta: number;
  frozen_delta: number;
  balance_after: number;
  frozen_after?: number | null;
  reserved_amount?: number | null;
  real_cost?: number | null;
  biz_type?: string | null;
  biz_ref?: number | null;
  note?: string | null;
  created_at: string;
}

export interface CreditTransactionPage {
  items: CreditTransaction[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
}

export interface BillingEntry {
  key: string;
  kind: BillingKind;
  biz_type: string;
  biz_ref?: number | null;
  title: string;
  subtitle?: string | null;
  status: string;
  status_group: string;
  model_config_id?: number | null;
  model_name?: string | null;
  model_id?: string | null;
  model_provider?: string | null;
  quote_id?: number | null;
  quote_kind?: string | null;
  quote_status?: string | null;
  quoted_credits?: number | null;
  price_version_id?: number | null;
  related_kind?: string | null;
  related_id?: number | null;
  review_status?: string | null;
  review_reason?: string | null;
  review_note?: string | null;
  reviewed_at?: string | null;
  frozen_credits: number;
  settled_credits: number;
  refunded_credits: number;
  settlement_returned_credits: number;
  reservation_refunded_credits: number;
  consumed_refunded_credits: number;
  outstanding_frozen_credits: number;
  net_consumed_credits: number;
  credited_credits: number;
  net_balance_change: number;
  balance_conserved: boolean;
  reservation_conserved: boolean;
  balance_after: number;
  frozen_after?: number | null;
  transaction_count: number;
  created_at: string;
  updated_at: string;
}

export interface BillingEntryPage {
  items: BillingEntry[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
}

export type CatalogMetadataOrigin = "recorded" | "legacy_backfill";

export interface ModelCatalogMetadataSnapshot {
  schema_version: "model-catalog-metadata.v1";
  origin: CatalogMetadataOrigin;
  model_id: string;
  display_name: string;
  is_default: boolean;
  sort_order: number;
  enabled: boolean;
}

export interface ModelCapabilityVersion {
  id: number;
  version: number;
  schema_version: string;
  capabilities: Record<string, unknown>;
  metadata_snapshot: ModelCatalogMetadataSnapshot;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelPriceVersion {
  id: number;
  version: number;
  schema_version: string;
  base_cost_credits: number;
  unlock_cost_credits: number;
  pricing: Record<string, unknown>;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelCatalogItem {
  id: number;
  use: "vision" | "image" | "video" | "prompt";
  name: string;
  model_id: string;
  provider: string;
  provider_label: string;
  is_default: boolean;
  sort_order: number;
  cost_credits: number;
  unlock_cost: number;
  preview_cost?: number | null;
  final_cost: number;
  enabled: boolean;
  capabilities: Record<string, unknown>;
  route_availability?: {
    configured: number;
    available: number;
    status: "available" | "unavailable";
    legacy?: boolean;
  } | null;
  capability_version?: ModelCapabilityVersion | null;
  price_version?: ModelPriceVersion | null;
  capability_versions?: ModelCapabilityVersion[];
  price_versions?: ModelPriceVersion[];
}

export interface AdminModelRoute {
  id: number;
  model_config_id: number;
  route_key: string;
  name: string;
  model_id?: string | null;
  provider?: string | null;
  base_url?: string | null;
  gateway_format?: string | null;
  api_key_configured: boolean;
  extra: Record<string, unknown>;
  priority: number;
  enabled: boolean;
  managed_by_model_config: boolean;
  config_revision: number;
  failure_threshold: number;
  window_seconds: number;
  cooldown_seconds: number;
  health_status: "closed" | "open" | "half_open";
  window_requests: number;
  window_failures: number;
  consecutive_failures: number;
  opened_at?: string | null;
  cooldown_until?: string | null;
  last_probe_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  latency_ema_ms?: number | null;
  last_error_code?: string | null;
  created_at: string;
  updated_at: string;
  active_version?: AdminModelRouteVersion | null;
}

export interface AdminModelRouteVersion {
  id: number;
  route_id: number;
  version: number;
  schema_version: string;
  config: Record<string, unknown>;
  api_key_configured: boolean;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminModelRouteHealthEvent {
  id: number;
  operation: string;
  outcome: "success" | "failure" | "ignored";
  counts_toward_circuit: boolean;
  latency_ms?: number | null;
  error_code?: string | null;
  created_at: string;
}

export type ToolCatalogCategory = "image" | "video" | "workflow" | "utility";

export interface ToolCatalogMetadataSnapshot {
  schema_version: "tool-catalog-metadata.v1";
  origin: CatalogMetadataOrigin;
  slug: string;
  name: string;
  description: string | null;
  category: ToolCatalogCategory;
  renderer: string;
  entry_path: string;
  icon: string | null;
  sort_order: number;
  enabled: boolean;
  featured: boolean;
}

export interface ToolCatalogVersion {
  id: number;
  version: number;
  schema_version: string;
  input_schema: Record<string, unknown>;
  workflow: Record<string, unknown>;
  pricing_policy: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  metadata_snapshot: ToolCatalogMetadataSnapshot;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCatalogItem {
  id: number;
  slug: string;
  name: string;
  description?: string | null;
  category: ToolCatalogCategory;
  renderer: string;
  entry_path: string;
  icon?: string | null;
  sort_order: number;
  enabled: boolean;
  featured: boolean;
  active_version?: ToolCatalogVersion | null;
  versions?: ToolCatalogVersion[];
  created_at: string;
  updated_at: string;
}

export type VideoCompositionTransitionType =
  | "cut"
  | "fade"
  | "crossfade"
  | "wipeleft"
  | "slideright";

export interface VideoCompositionCanvas {
  width: number;
  height: number;
  fps: number;
  background_color: string;
}

export interface VideoCompositionTransition {
  type: VideoCompositionTransitionType;
  duration_seconds: number;
}

export interface VideoCompositionShot {
  shot_id: string;
  asset_ref: string;
  source_start_seconds?: number;
  source_end_seconds?: number | null;
  duration_seconds?: number | null;
  transition?: VideoCompositionTransition;
}

export interface VideoCompositionSubtitle {
  start_seconds: number;
  end_seconds: number;
  text: string;
  font_size?: number;
  text_color?: string;
  background_color?: string;
  bottom_margin?: number;
}

export interface VideoCompositionAudioTrack {
  asset_ref: string;
  start_seconds?: number;
  source_start_seconds?: number;
  source_end_seconds?: number | null;
  volume?: number;
  loop?: boolean;
}

export interface VideoCompositionSpec {
  schema_version: "video-composition.v1";
  title?: string;
  reverse_operation_id?: number | null;
  canvas?: VideoCompositionCanvas;
  shots: VideoCompositionShot[];
  subtitles?: VideoCompositionSubtitle[];
  audio_tracks?: VideoCompositionAudioTrack[];
  original_audio_volume?: number;
}

export interface VideoCompositionResult {
  schema_version: "video-composition-result.v1";
  task_id: number;
  asset_id: number;
  asset_ref: string;
  download_url: string;
  manifest: Record<string, unknown>;
  idempotent_replay: boolean;
}

export interface VideoCompositionExportResult {
  schema_version: "video-composition-export.v1";
  asset_id: number;
  asset_ref: string;
  download_url: string;
  filename: string;
  manifest: Record<string, unknown>;
}

export interface ReverseSourceRange {
  start_seconds: number;
  end_seconds: number;
}

export interface ReverseSourceReference {
  asset_url: string;
  source_type: AssetType;
  role: ReverseReferenceRole;
  asset_id?: number | null;
  label?: string | null;
}

export interface ReverseResult {
  structured?: Record<string, unknown>;
  final_text?: string;
  negative_prompt?: string | null;
  observations?: Record<string, unknown> | Array<Record<string, unknown>> | null;
  video_analysis?: Record<string, unknown> | null;
  charged_credits?: number;
  reference_count?: number;
  [key: string]: unknown;
}

export interface ReverseOperationTimestamps {
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ReverseOperation {
  id: number | string;
  status: ReverseOperationStatus;
  target: ReverseTarget;
  source_type: AssetType | null;
  phase: string | null;
  progress: number;
  result: ReverseResult | null;
  video_analysis: Record<string, unknown> | null;
  request_context: Record<string, unknown> | null;
  workspace_snapshot_v2: Record<string, unknown> | null;
  workspace_snapshot_v3: Record<string, unknown> | null;
  analysis_focus: ReverseAnalysisFocus;
  analysis_precision: ReverseAnalysisPrecision;
  output_purpose: ReverseOutputPurpose;
  sources?: ReverseSourceReference[];
  custom_instruction?: string | null;
  custom_keyframes?: number[];
  include_audio: boolean;
  source_range: ReverseSourceRange | null;
  source_ranges: ReverseSourceRange[];
  result_schema_version: string;
  applied_result_version?: number | null;
  retry_of_operation_id: number | null;
  reference_count: number;
  charged_credits: number;
  cost_frozen: number;
  cost_settled: number;
  confirmation_expires_at: string | null;
  cancel_requested: boolean;
  error_code: string | null;
  error: string | null;
  expired: boolean;
  created_at: string | null;
  updated_at: string | null;
  queued_at?: string | null;
  started_at: string | null;
  finished_at: string | null;
  timestamps: ReverseOperationTimestamps;
  model_config_id?: number | null;
  model_name?: string | null;
  model_id?: string | null;
}

export interface ReverseOperationCreate {
  asset_url?: string | null;
  sources?: ReverseSourceReference[];
  target: ReverseTarget;
  client_request_id: string;
  fallback_image?: string | null;
  source_type?: AssetType | null;
  video_analysis_preset?: ReverseAnalysisPrecision | null;
  analysis_precision?: ReverseAnalysisPrecision | null;
  analysis_focus?: ReverseAnalysisFocus;
  output_purpose?: ReverseOutputPurpose;
  custom_instruction?: string | null;
  source_range?: ReverseSourceRange | null;
  source_ranges?: ReverseSourceRange[];
  custom_keyframes?: number[];
  include_audio?: boolean;
  workspace_snapshot_v2?: Record<string, unknown> | null;
  workspace_snapshot_v3?: Record<string, unknown> | null;
  model_config_id?: number | null;
}

export interface ReverseOperationQuery {
  status?: ReverseOperationStatus | "";
  target?: ReverseTarget | "";
  source_type?: AssetType | "";
  analysis_focus?: ReverseAnalysisFocus | "";
  include_audio?: boolean | null;
  limit?: number;
  offset?: number;
}

export interface ReverseBatchSource {
  asset_url: string;
  source_type?: AssetType;
  fallback_image?: string | null;
  workspace_snapshot_v3?: Record<string, unknown> | null;
  target?: ReverseTarget | null;
  analysis_precision?: ReverseAnalysisPrecision | null;
  source_ranges?: ReverseSourceRange[];
  custom_keyframes?: number[];
  audio_policy?: "inherit" | "exclude" | "analyze";
}

export interface ReverseBatchCreate {
  client_request_id: string;
  project_id?: number | null;
  name?: string | null;
  target?: ReverseTarget;
  analysis_focus?: ReverseAnalysisFocus;
  analysis_precision?: ReverseAnalysisPrecision;
  output_purpose?: ReverseOutputPurpose;
  custom_instruction?: string | null;
  include_audio?: boolean;
  model_config_id?: number | null;
  items: ReverseBatchSource[];
}

export interface ReverseBatchItem {
  id: number;
  index: number;
  operation_id: number;
  operation: ReverseOperation;
}

export interface ReverseBatchCapabilitiesV2 {
  schema_version: "reverse-batch-capabilities.v2";
  item_overrides: true;
  supported_override_keys: Array<
    "target" | "analysis_precision" | "source_ranges" | "custom_keyframes" | "audio_policy"
  >;
  audio_policies: Array<"inherit" | "exclude" | "analyze">;
}

export interface ReverseBatchStatusCounts {
  queued: number;
  running: number;
  needs_confirmation: number;
  succeeded: number;
  failed: number;
  canceled: number;
}

export interface ReverseBatch {
  id: number;
  client_request_id: string;
  name: string | null;
  target: ReverseTarget;
  shared_config_snapshot: Record<string, unknown>;
  capabilities: ReverseBatchCapabilitiesV2;
  status: "queued" | "running" | "needs_confirmation" | "succeeded" | "partial" | "failed" | "canceled";
  status_counts: ReverseBatchStatusCounts;
  total_count: number;
  cancel_requested: boolean;
  items: ReverseBatchItem[];
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export type ReverseResultRevisionSource =
  | "provider_raw"
  | "normalized"
  | "user_edit"
  | "applied"
  | "model_compiled"
  | "generation";

export interface ReverseResultRevision {
  id: number;
  operation_id: number;
  version: number;
  source: ReverseResultRevisionSource;
  payload: ReverseResult;
  parent_revision_id: number | null;
  source_content_hash: string | null;
  source_fingerprints: Array<Record<string, unknown>> | null;
  payload_hash: string | null;
  lineage_status: "verified" | "legacy_unverified";
  evidence_review_action: "not_applicable" | "inherited" | "updated" | "cleared" | null;
  created_at: string | null;
}

export interface ReverseShotTimelineEditInput {
  client_request_id: string;
  action: "split" | "merge" | "reorder" | "lock";
  shot_id?: string | null;
  shot_ids?: string[];
  split_seconds?: number | null;
  ordered_shot_ids?: string[];
  locked?: boolean | null;
}

export interface ReverseShotReanalyzeInput {
  client_request_id: string;
  shot_id: string;
  analysis_precision?: ReverseAnalysisPrecision | null;
  include_audio?: boolean | null;
}

export interface ReverseShotGenerationPrepareInput {
  shot_id: string;
  revision_id?: number | null;
  client_request_id: string;
  model_config_id?: number | null;
  params?: Record<string, unknown>;
}

export interface ReverseShotGenerationPrepareResult {
  reverse_operation_id: number;
  source_revision_id: number;
  shot_id: string;
  quote_endpoint: string;
  generation_endpoint: string;
  request: GenerationQuotePayload;
}

export interface ReverseResultRevisionCreate {
  source: "user_edit" | "applied";
  payload: ReverseResult;
  parent_revision_id?: number | null;
  clear_image_evidence?: boolean;
}

export interface ReverseResultApplyInput {
  payload: ReverseResult;
  parent_revision_id: number;
  clear_image_evidence?: boolean;
}

export interface ReverseResultApplyResult {
  user_edit: ReverseResultRevision;
  applied: ReverseResultRevision;
}

export type ReproductionAssessmentStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "canceled";

export interface ReproductionAssessmentCreateInput {
  source_asset_ref: string;
  generated_asset_ref: string;
  idempotency_key: string;
  reverse_operation_id?: number | null;
  reverse_revision_id?: number | null;
  generation_task_id?: number | null;
}

export interface ReproductionAssessmentResponse {
  id: number;
  status: ReproductionAssessmentStatus;
  phase?: string | null;
  progress: number;
  media_type: AssetType;
  source_asset_ref: string;
  generated_asset_ref: string;
  cost_credits?: number;
  cancel_requested?: boolean;
  lineage?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  analyzers?: Record<string, unknown> | Array<Record<string, unknown>>;
  findings?: Array<Record<string, unknown>>;
  warnings?: string[];
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ReproductionAssessmentPage {
  items: ReproductionAssessmentResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReproductionCorrectionInput {
  idempotency_key: string;
  parent_revision_id: number;
  selected_finding_ids: number[];
  structured_patch?: Record<string, unknown>;
  prompt_patch?: string | null;
  negative_prompt_patch?: string | null;
  mask_patch?: Record<string, unknown> | null;
  apply?: boolean;
}

export interface ReproductionCorrectionResponse {
  id: number;
  assessment_id: number;
  status: "created" | "applied";
  idempotency_key: string;
  parent_revision_id: number;
  selected_finding_ids: number[];
  structured_patch: Record<string, unknown>;
  prompt_patch: string | null;
  negative_prompt_patch: string | null;
  mask_patch: Record<string, unknown> | null;
  apply_requested: boolean;
  edited_revision: ReverseResultRevision;
  applied_revision: ReverseResultRevision | null;
  created_at: string;
}

export type ReproductionRemediationMode = "image_inpaint" | "video_shot_regenerate";

export type ReproductionRemediationStatus =
  | "planned"
  | "generating"
  | "composing"
  | "reassessing"
  | "succeeded"
  | "partial"
  | "failed"
  | "canceled";

export interface ReproductionRemediationCreateInput {
  idempotency_key: string;
  parent_revision_id: number;
  parent_remediation_id?: number | null;
  selected_finding_ids: number[];
  selected_shot_ids?: string[];
  mode: ReproductionRemediationMode;
  model_config_id: number;
  prompt_patch?: string | null;
  negative_prompt_patch?: string | null;
  params?: Record<string, unknown>;
  video_composition?: Record<string, unknown> | null;
  auto_reassess?: boolean;
}

export interface ReproductionRemediationPlanSnapshotItem {
  item_id: string;
  kind: ReproductionRemediationMode;
  shot_id: string | null;
  finding_ids: number[];
  request: GenerationQuotePayload;
}

export type ReproductionRemediationPlanItemStatus =
  | "planned"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "needs_review"
  | "canceled";

export interface ReproductionRemediationPlanItem {
  item_id: string;
  kind: ReproductionRemediationMode;
  shot_id: string | null;
  finding_ids: number[];
  status: ReproductionRemediationPlanItemStatus;
  generation_task_id?: number | null;
  asset_id?: number | null;
  asset_ref?: string | null;
  error_code?: string | null;
  error?: string | null;
}

export interface ReproductionRemediationResponse {
  id: number;
  assessment_id: number;
  correction_id: number;
  user_id: number;
  parent_remediation_id?: number | null;
  idempotency_key: string;
  request_fingerprint: string;
  parent_revision_id: number;
  applied_revision_id: number;
  mode: ReproductionRemediationMode;
  status: ReproductionRemediationStatus;
  phase?: string | null;
  selected_finding_ids: number[];
  selected_shot_ids: string[];
  source_asset_ref: string;
  target_asset_ref: string;
  plan_snapshot: ReproductionRemediationPlanSnapshotItem[];
  plan_items: ReproductionRemediationPlanItem[];
  plan_hash: string;
  video_composition?: Record<string, unknown> | null;
  generation_requests: GenerationQuotePayload[];
  generation_task_ids: number[];
  composition_tool_run_id?: number | null;
  composition_workflow_run_id?: number | null;
  final_asset_id?: number | null;
  final_asset_ref?: string | null;
  successor_assessment_id?: number | null;
  auto_reassess: boolean;
  error_code?: string | null;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
}

export interface ReproductionRemediationPage {
  items: ReproductionRemediationResponse[];
  total: number;
  limit: number;
  offset: number;
}

export type ReverseFeedbackRating = "useful" | "not_useful";
export type ReverseFeedbackIssue =
  | "subject_error"
  | "style_error"
  | "action_missing"
  | "shot_missing"
  | "camera_error"
  | "text_error"
  | "audio_error"
  | "hallucination"
  | "other";

export interface ReverseOperationFeedbackInput {
  rating: ReverseFeedbackRating;
  issue_types?: ReverseFeedbackIssue[];
  note?: string | null;
}

export interface MediaProjectAssetLink {
  id: number;
  asset_ref: string;
  role: string;
  sort_order: number;
  note: string | null;
  asset: Asset | null;
  metadata: UserAssetMetadata | null;
  duplicate_count: number;
  similar_count: number;
  created_at: string | null;
}

export interface UserAssetMetadata {
  asset_ref: string;
  media_type: AssetType;
  tags: string[];
  content_sha256: string | null;
  perceptual_hash: string | null;
  perceptual_hash_algorithm: string | null;
  analysis_status: "pending" | "ready" | "degraded";
  analysis_error: string | null;
  analyzed_at: string | null;
  updated_at: string | null;
}

export interface AssetSimilarityMatch {
  asset_ref: string;
  match_type: "exact" | "similar";
  hamming_distance: number | null;
  metadata: UserAssetMetadata;
  asset: Asset | null;
}

export interface AssetSimilarityResult {
  status: "ready" | "degraded";
  query_asset_ref: string;
  exact_available: boolean;
  perceptual_available: boolean;
  message: string | null;
  query: UserAssetMetadata;
  matches: AssetSimilarityMatch[];
  degraded_asset_refs: string[];
}

export interface MediaProjectRecipeLink {
  id: number;
  recipe_id: number;
  title: string | null;
  category: AssetType | null;
  visibility: CreationRecipeVisibility | null;
  favorite: boolean;
  current_version: number | null;
  cover_asset_url: string | null;
  schema_version: string | null;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

export interface MediaProjectTaskLink {
  id: number;
  task_kind: UnifiedTaskKind;
  task_id: number;
  status: string | null;
  category: string | null;
  stage: string | null;
  phase: string | null;
  progress: number;
  title: string | null;
  summary: string | null;
  result_refs: string[];
  cost_frozen: number;
  cost_settled: number;
  error: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface MediaProject {
  id: number;
  title: string;
  description: string | null;
  project_type: "image" | "video" | "mixed";
  status: "active" | "archived";
  cover_asset_ref: string | null;
  auto_archive_after_days: number | null;
  asset_count: number;
  recipe_count: number;
  task_count: number;
  cost_frozen: number;
  cost_settled: number;
  assets: MediaProjectAssetLink[];
  recipes: MediaProjectRecipeLink[];
  tasks: MediaProjectTaskLink[];
  draft_key: string;
  draft: Record<string, unknown>;
  draft_updated_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AssetFolder {
  id: number;
  name: string;
  parent_id: number | null;
  sort_order: number;
  item_count: number;
  items: Array<{ asset_ref: string; created_at: string | null }>;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReverseOperationFeedback extends ReverseOperationFeedbackInput {
  operation_id: number;
  issue_types: ReverseFeedbackIssue[];
  created_at: string | null;
  updated_at: string | null;
}

export interface ReverseOperationRetryInput {
  quote_id: number;
  client_request_id: string;
  model_config_id?: number | null;
}

export type CreationRecipeVisibility = "private" | "public";
export type CreationRecipeModerationStatus = "draft" | "pending" | "approved" | "rejected";

export interface CreationRecipeVersion {
  id: number;
  recipe_id: number;
  version: number;
  schema_version: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface CreationRecipe {
  id: number;
  source_operation_id: number | null;
  title: string;
  category: AssetType;
  visibility: CreationRecipeVisibility;
  moderation_status: CreationRecipeModerationStatus;
  favorite: boolean;
  current_version: number;
  approved_version: number | null;
  cover_asset_url: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  version: CreationRecipeVersion | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreationRecipeCreate {
  title: string;
  category: AssetType;
  visibility?: CreationRecipeVisibility;
  favorite?: boolean;
  source_operation_id?: number | null;
  cover_asset_url?: string | null;
  payload: Record<string, unknown>;
}

export interface CreationRecipeQuery {
  category?: AssetType | "";
  favorite?: boolean | null;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface CreationRecipeMetadataPatch {
  title?: string;
  visibility?: CreationRecipeVisibility;
  cover_asset_url?: string | null;
}

export interface CreationRecipeCloneInput {
  version?: number | null;
  title?: string;
  share_slug?: string;
}

export interface CreationRecipeShare {
  id: number;
  recipe_id: number;
  version: number;
  slug: string;
  status: "active" | "revoked";
  expires_at: string | null;
  revoked_at: string | null;
  share_url: string;
  created_at: string | null;
}

export interface CreationRecipeShared {
  share: CreationRecipeShare;
  recipe: CreationRecipe;
}

export interface CreationRecipeUsageEvent {
  id: number;
  recipe_id: number | null;
  recipe_version: number;
  user_id: number | null;
  event_type: "apply" | "clone" | "generation_prepare" | "generation_submit";
  source: "owner" | "public" | "share";
  share_id: number | null;
  derived_recipe_id: number | null;
  generation_task_id: number | null;
  client_event_id: string | null;
  context: Record<string, unknown> | null;
  created_at: string | null;
}

export interface CreationRecipeUsageSummary {
  recipe_id: number;
  total: number;
  unique_users: number;
  by_event: Record<string, number>;
  last_used_at: string | null;
}

export interface Asset {
  id?: number;
  asset_ref?: string;
  origin?: "generated" | "uploaded" | "fetched";
  type: AssetType;
  url?: string;
  thumb?: string | null;
  display_url?: string | null;
  display_thumb?: string | null;
  original_url?: string | null;
  original_thumb?: string | null;
  source_page_url?: string | null;
  source_captured_at?: string | null;
  retention_expires_at?: string | null;
  expired?: boolean;
  available?: boolean;
  preview_url?: string | null;
  hd_url?: string | null;
  width?: number | null;
  height?: number | null;
  thumb_width?: number | null;
  thumb_height?: number | null;
  unlocked?: boolean;
  favorite?: boolean;
  retained?: boolean;
  created_at?: string | null;
  expires_at?: string | null;
  filename?: string | null;
  bytes?: number | null;
  duration?: number | null;
  unlock_cost?: number | null;
  quality_status?: string | null;
  quality_message?: string | null;
  moderation_status?: string | null;
  days_left?: number | null;
  task_id?: number | null;
  tags?: string[];
  content_sha256?: string | null;
  perceptual_hash?: string | null;
  analysis_status?: "pending" | "ready" | "degraded";
  analysis_error?: string | null;
  folder_id?: number | null;
  folder_name?: string | null;
  _cat?: "image" | "video";
  _task_status?: TaskStatus;
  _task_stage?: "preview" | "final";
}

export interface Task {
  id: number;
  category: "image" | "video";
  stage: "preview" | "final";
  status: TaskStatus;
  model_use?: string | null;
  model_id?: string | null;
  model_provider?: string | null;
  model_config_id?: number | null;
  model_name?: string | null;
  quote_id?: number | null;
  reverse_operation_id?: number | null;
  source_revision_id?: number | null;
  compiled_revision_id?: number | null;
  generation_revision_id?: number | null;
  capability_version_id?: number | null;
  price_version_id?: number | null;
  quote_estimated_credits?: number | null;
  prompt_text?: string | null;
  prompt_text_source?: "generation" | "request" | null;
  request_prompt_text?: string | null;
  generation_prompt_text?: string | null;
  raw_prompt_text?: string | null;
  optimized_prompt_text?: string | null;
  assembled_prompt_text?: string | null;
  prompt_optimizer_model_id?: string | null;
  prompt_compiler_version?: string | null;
  prompt_optimization_direction?: string | null;
  prompt_optimization_kind?: string | null;
  prompt_warnings?: string[];
  post_overlays?: string[];
  voiceover?: string | null;
  sfx?: string[];
  sequence_required?: boolean;
  parent_task_id?: number | null;
  retry_of_task_id?: number | null;
  dispatch_attempt?: number | null;
  dispatch_status?: string | null;
  dispatch_task_id?: string | null;
  dispatch_publish_attempts?: number;
  dispatch_reconciliation_required?: boolean;
  percent?: number;
  progress?: number;
  phase?: string | null;
  error?: string | null;
  assets?: Asset[];
  params?: Record<string, unknown>;
  final_task_id?: number | null;
  final_status?: TaskStatus | null;
  final_asset_count?: number | null;
  final_cost_estimate?: number | null;
  partial?: boolean;
  saved_count?: number;
  requested_count?: number;
  partial_errors?: string[];
}

export interface GenerationShotContext {
  contract_version: 1;
  shot_id: string;
  source_segment_index: number;
  source_range: {
    start_seconds: number;
    end_seconds: number;
  };
}

export interface GenerationQuotePayload {
  source_asset_url?: string | null;
  source_type?: AssetType | null;
  source_asset_meta?: Record<string, unknown> | null;
  shot_context?: GenerationShotContext | null;
  category: "image" | "video";
  stage: "preview" | "final";
  instruction?: string | null;
  prompt?: Record<string, unknown>;
  params?: Record<string, unknown>;
  parent_task_id?: number | null;
  client_request_id?: string | null;
  model_config_id?: number | null;
  reverse_operation_id?: number | null;
  reverse_revision_id?: number | null;
  reproduction_remediation_id?: number | null;
  reproduction_plan_item_id?: string | null;
  project_id?: number | null;
  creation_recipe_id?: number | null;
  creation_recipe_version?: number | null;
  creation_recipe_share_slug?: string | null;
}

export interface GeneratePayload extends GenerationQuotePayload {
  quote_id: number;
}

export type StudioQuoteKind =
  | "generation"
  | "reverse"
  | "reverse_batch"
  | "workflow"
  | "prompt_optimization"
  | "asset_unlock";

export interface StudioQuoteRequest {
  kind: StudioQuoteKind;
  client_request_id: string;
  request: Record<string, unknown>;
}

export interface StudioQuote {
  id?: number;
  quote_id: number;
  kind?: StudioQuoteKind;
  status?: string;
  total_credits?: number;
  estimated_credits?: number;
  cost_credits?: number;
  breakdown?: Array<Record<string, unknown>> | Record<string, unknown>;
  price_breakdown?: Record<string, unknown>;
  balance?: Record<string, unknown>;
  balance_before?: number | null;
  balance_after?: number | null;
  balance_after_estimate?: number | null;
  affordable?: boolean | null;
  expires_at: string;
  warnings?: Array<string | Record<string, unknown>>;
  model_name?: string | null;
  model_id?: string | null;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StudioQuoteBreakdownItem {
  key: string;
  label: string;
  credits: number;
  detail: string | null;
}

export interface StudioQuoteView {
  id: number;
  kind: StudioQuoteKind;
  clientRequestId: string;
  totalCredits: number;
  breakdown: StudioQuoteBreakdownItem[];
  balanceBefore: number | null;
  balanceAfter: number | null;
  balanceSource: "quote" | "account_snapshot" | "unavailable";
  affordable: boolean | null;
  expiresAt: string;
  warnings: string[];
  modelName: string | null;
  raw: StudioQuote | GenerationQuote;
  envelope: StudioQuoteRequest;
}

export type GenerationQuoteStatus = "active" | "consumed" | "expired" | "canceled";

export interface GenerationQuote {
  id: number;
  quote_id: number;
  status: GenerationQuoteStatus;
  category: "image" | "video";
  stage: "preview" | "final";
  model_config_id: number;
  model_name?: string | null;
  model_id?: string | null;
  model_provider?: string | null;
  capability_version_id: number;
  capability_version?: number | null;
  price_version_id: number;
  price_version?: number | null;
  estimated_credits: number;
  price_breakdown: Record<string, unknown>;
  request_fingerprint: string;
  expires_at: string;
  consumed_at?: string | null;
  task_id?: number | null;
  created_at?: string | null;
}

export interface PromptOptimizeOptions {
  category?: "image" | "video";
  product_mode?: boolean;
  duration?: number;
  subject_mode?: "general" | "product" | "portrait";
  reference_type?: string;
  subject_profile?: Record<string, unknown> | string | null;
  target_model_id?: string;
  target_model_provider?: string;
  aspect_ratio?: string;
  resolution?: string;
  product_lock_mode?: "free" | "locked";
  product_video_template?: string;
  optimizer_model_config_id?: number | null;
  target_model_config_id?: number | null;
  direction?: "faithful" | "concise" | "expand" | "commercial" | "cinematic" | "model_adaptation" | "constraints" | "translate";
  target_language?: "zh-CN" | "en";
}

export interface PromptOptimizeResult {
  prompt: string;
  model_id: string;
  source_prompt: string;
  compiled_prompt?: string | null;
  direction: "faithful" | "concise" | "expand" | "commercial" | "cinematic" | "model_adaptation" | "constraints" | "translate";
  target_language?: "zh-CN" | "en" | null;
  optimization_kind: "rewrite" | "model_compile";
  change_summary: string[];
  preserved_requirements: string[];
  forbidden_changes: string[];
  warnings: string[];
  estimated_credits: number;
  charged_credits: number;
  optimizer_model_id?: string | null;
  compiler_metadata?: Record<string, unknown> | null;
  context_metadata?: Record<string, unknown> | null;
  optimizer_model_config_id?: number | null;
  optimizer_model_name?: string | null;
}

export interface StudioPromptOptimizationSegment {
  id: string;
  field_path: string;
  label: string;
  original: unknown;
  suggestion: unknown;
  changed: boolean;
}

export interface StudioPromptOptimizationProposal {
  proposal_id: number;
  proposal_version: number;
  mode: PromptOptimizeOptions["direction"] | "target_model_adaptation";
  optimization_kind: "rewrite" | "model_compile";
  original: Record<string, unknown>;
  suggestion: Record<string, unknown>;
  segments: StudioPromptOptimizationSegment[];
  constraint_coverage: Array<Record<string, unknown>>;
  warnings: Array<{ code: string; field: string; action: string; message: string }>;
  provenance: Record<string, unknown>;
  compiler_profile: Record<string, unknown>;
  charged_credits: number;
}

export type StudioPromptOptimizationStatus =
  | "proposed"
  | "accepted"
  | "partially_accepted"
  | "rejected"
  | "expired";

export interface StudioPromptOptimizationDetail extends StudioPromptOptimizationProposal {
  status: StudioPromptOptimizationStatus;
  category: "image" | "video";
  target_model_config_id: number;
  source_operation_id: number | null;
  source_revision_id: number | null;
  accepted_segment_ids: string[];
  rejected_segment_ids: string[];
  decision_result: StudioPromptOptimizationDecisionResponse | Record<string, unknown> | null;
  expires_at: string;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
  can_decide: boolean;
}

export interface StudioPromptOptimizationSummary {
  proposal_id: number;
  proposal_version: number;
  status: StudioPromptOptimizationStatus;
  category: "image" | "video";
  mode: StudioPromptOptimizationProposal["mode"];
  optimization_kind: "rewrite" | "model_compile";
  target_model_config_id: number;
  target_model_id: string | null;
  original_preview: string;
  suggestion_preview: string;
  changed_segment_count: number;
  charged_credits: number;
  expires_at: string;
  decided_at: string | null;
  created_at: string;
  can_decide: boolean;
}

export interface StudioPromptOptimizationPage {
  items: StudioPromptOptimizationSummary[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface StudioPromptOptimizationQuery {
  status?: StudioPromptOptimizationStatus | "";
  limit?: number;
  offset?: number;
}

export interface StudioPromptOptimizationRequest {
  prompt?: string | null;
  reverse_operation_id?: number | null;
  reverse_revision_id?: number | null;
  mode: StudioPromptOptimizationProposal["mode"];
  target_language?: "zh-CN" | "en" | null;
  target_model_config_id: number;
  optimizer_model_config_id?: number | null;
  idempotency_key: string;
  quote_id?: number | null;
  protected_constraints?: Array<{ type: string; value: string | number | string[] }>;
  category?: "image" | "video" | null;
  product_mode?: boolean | null;
  duration?: number | null;
  aspect_ratio?: string | null;
  resolution?: string | null;
}

export interface StudioPromptOptimizationDecision {
  proposal_version: number;
  idempotency_key: string;
  accepted_segment_ids?: string[];
  rejected_segment_ids?: string[];
}

export interface StudioPromptOptimizationDecisionResult {
  final_text?: string | null;
  negative?: string | null;
  negative_prompt?: string | null;
  structured?: Record<string, unknown> | null;
  parameters?: {
    ratio?: string | null;
    aspect_ratio?: string | null;
    duration?: number | string | null;
    vDuration?: number | string | null;
    resolution?: string | null;
    vResolution?: string | null;
  };
  compiler_metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface StudioPromptOptimizationDecisionResponse {
  proposal_id: number;
  proposal_version: number;
  status: "accepted" | "partially_accepted" | "rejected";
  accepted_segment_ids: string[];
  rejected_segment_ids: string[];
  result: StudioPromptOptimizationDecisionResult | null;
  revision: Record<string, unknown> | null;
}

export type ModelUse = "vision" | "image" | "video" | "prompt";

export interface ModelOption {
  id: number;
  use: ModelUse;
  name: string;
  model_id: string;
  provider: string;
  provider_label: string;
  cost_credits: number;
  unlock_cost: number;
  is_default: boolean;
  capabilities: Record<string, boolean | number | string | string[]>;
}

export interface PromptHistoryQuery {
  favorite?: boolean | null;
  category?: string;
  source?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface ProfileAssetQuery {
  type?: "all" | AssetType;
  favorite?: boolean;
  limit?: number;
  offset?: number;
  created_from?: string;
  created_to?: string;
  model_use?: string;
  size?: string;
  min_width?: string | number;
  min_height?: string | number;
  max_width?: string | number;
  max_height?: string | number;
}

export interface AdminUsersQuery {
  q?: string;
  status?: string;
  is_admin?: boolean | "";
  limit?: number;
  offset?: number;
}

export interface AdminUsageDashboardResponse {
  summary: {
    dau: number;
    task_count: number;
    success_count: number;
    failed_count: number;
    needs_review_count: number;
    success_rate: number;
  };
  by_category: Record<string, number>;
  failure_reasons: Record<string, number>;
  avg_duration_seconds: number;
  daily: Array<{ date: string; tasks: number; succeeded: number; failed: number }>;
}

export interface AdminModelCostRow {
  model_id: string;
  kind: string;
  call_count: number;
  ok_count: number;
  failed_count: number;
  failure_rate: number;
  total_tokens: number;
  estimated_cost_credits: number;
  avg_latency_ms: number;
}

export interface AdminModelCostsResponse {
  models: AdminModelCostRow[];
}

export type AdminReverseCostStatus = "complete" | "partial" | "unavailable";

export interface AdminReverseUsageSummary {
  operation_count: number;
  succeeded: number;
  failed: number;
  canceled: number;
  needs_confirmation: number;
  success_rate: number;
  failure_rate: number;
  cancel_rate: number;
  settled_credits: number;
}

export interface AdminReverseQualityMetrics {
  video_operation_count: number;
  cover_fallback_count: number;
  cover_fallback_rate: number;
  cover_confirmation_required_count: number;
  cover_confirmed_count: number;
  cover_confirmation_rate: number;
  repair_count: number;
  repair_succeeded_count: number;
  repair_failed_count: number;
  repair_rate: number;
  avg_evidence_coverage: number;
  feedback_count: number;
  useful_count: number;
  not_useful_count: number;
  useful_rate: number;
  adopted_operation_count: number;
  adoption_rate: number;
  edited_operation_count: number;
  edit_rate: number;
  avg_edit_ratio: number;
  generated_operation_count: number;
  generation_conversion_rate: number;
  recipe_operation_count: number;
  recipe_conversion_rate: number;
  issue_types: Record<string, number>;
}

export interface AdminReverseEconomics {
  basis: string;
  cost_status: AdminReverseCostStatus;
  revenue_credits: number;
  provider_cost_credits: number;
  attributed_provider_cost_credits: number;
  unattributed_provider_cost_credits: number;
  gross_profit_credits: number | null;
  gross_margin_rate: number | null;
  gateway_call_count: number;
  gateway_cost_record_count: number;
  gateway_cost_coverage_rate: number;
  operation_cost_coverage_rate: number;
}

export interface AdminReverseQualityDimensionRow {
  media_type?: AssetType | "unknown";
  model?: string;
  focus?: ReverseAnalysisFocus | "unknown";
  operation_count: number;
  succeeded: number;
  failed: number;
  canceled: number;
  settled_credits: number;
  adopted: number;
  edited: number;
  generated: number;
  recipe: number;
  feedback_count: number;
  useful: number;
  success_rate: number;
  avg_evidence_coverage: number;
  adoption_rate: number;
  edit_rate: number;
  avg_edit_ratio: number;
  generation_conversion_rate: number;
  recipe_conversion_rate: number;
  useful_rate: number;
  provider_cost_credits: number;
  cost_status: AdminReverseCostStatus;
  gross_profit_credits: number | null;
  gross_margin_rate: number | null;
  cost_coverage_rate: number;
}

export interface AdminReverseUsageResponse {
  summary: AdminReverseUsageSummary;
  by_status: Record<string, number>;
  latency: {
    queue_p50_seconds: number | null;
    queue_p95_seconds: number | null;
    total_p50_seconds: number | null;
    total_p95_seconds: number | null;
  };
  quality: AdminReverseQualityMetrics;
  economics: AdminReverseEconomics;
  filters: {
    selected: {
      media_type: AssetType | null;
      model: string | null;
      focus: ReverseAnalysisFocus | string | null;
    };
    options: {
      media_types: Array<AssetType | "unknown">;
      models: string[];
      focuses: string[];
    };
  };
  by_preset: Array<{ preset: string; operation_count: number; succeeded: number; settled_credits: number }>;
  by_target: Array<{ target: string; operation_count: number; succeeded: number; settled_credits: number }>;
  by_focus: AdminReverseQualityDimensionRow[];
  by_media_type: AdminReverseQualityDimensionRow[];
  by_model_quality: AdminReverseQualityDimensionRow[];
  model_costs: Array<{
    model_id: string;
    call_count: number;
    failed_count: number;
    total_tokens: number;
    cost_credits: number;
  }>;
}

export type EditMaskMode = "protect_subject" | "center_box" | "off";
export type ProductPixelLockMode = "auto" | "strict" | "off";

export interface SubjectProtectionPreview {
  mode: "alpha_subject" | "auto_subject" | "center_box" | "none";
  reason?: string | null;
  confidence: number;
  bbox: number[] | null;
  width: number;
  height: number;
  mask_data_uri: string | null;
  will_send_mask: boolean;
  pixel_lock_recommended: boolean;
  risk_level: "low" | "medium" | "high";
  title: string;
  message: string;
  recommendations: string[];
}

export function wsUrl(path: string): string;
export const APP_SESSION_EVENT: string;
export function getToken(): string | null;
export function setToken(token: string | null): void;
export function clearToken(): void;
export function loginPath(nextPath?: string | null): string;
export function downloadBlob(path: string, filename?: string, options?: { method?: string; body?: unknown }): Promise<string>;
export function authenticatedObjectUrl(pathOrUrl: string): Promise<string>;
export function assetDownloadObjectUrl(assetId: number | string): Promise<string>;

export const api: {
  register(phone: string, password: string, sms_code?: string, nickname?: string): Promise<unknown>;
  authFeatures(): Promise<unknown>;
  sendSmsCode(phone: string): Promise<unknown>;
  login(phone: string, password: string): Promise<unknown>;
  me(options?: Record<string, unknown>): Promise<unknown>;
  config(): Promise<unknown>;
  navigation(): Promise<AppNavigationCatalog>;
  modelCatalog(query?: { use?: string; q?: string }): Promise<{ items: ModelCatalogItem[]; total: number }>;
  modelCatalogDetail(id: number | string): Promise<ModelCatalogItem>;
  toolCatalog(query?: { category?: string; featured?: boolean | ""; q?: string }): Promise<{ items: ToolCatalogItem[]; total: number }>;
  toolCatalogDetail(slug: string): Promise<ToolCatalogItem>;
  createWorkflowRun(body: WorkflowRunCreateInput): Promise<WorkflowRun>;
  workflowRun(id: number | string): Promise<WorkflowRun>;
  workflowRuns(query?: { limit?: number }): Promise<WorkflowRunPage>;
  resumeWorkflowRun(id: number | string): Promise<WorkflowRun>;
  cancelWorkflowRun(id: number | string): Promise<WorkflowRun>;
  reviewWorkflowNode(
    id: number | string,
    nodeKey: string,
    body: WorkflowReviewInput,
  ): Promise<WorkflowRun>;
  retryWorkflowNode(
    id: number | string,
    nodeKey: string,
    body?: WorkflowRetryInput,
  ): Promise<WorkflowRun>;
  profile(): Promise<unknown>;
  profileAssets(query?: ProfileAssetQuery): Promise<Asset[]>;
  meAssets(query?: {
    origin?: "all" | "generated" | "uploaded" | "fetched";
    type?: "all" | AssetType;
    favorite?: boolean | "";
    retention?: "all" | "retained" | "expiring";
    q?: string;
    tag?: string;
    folder?: "all" | "unfiled" | string;
    limit?: number;
    offset?: number;
    cursor?: string;
  }): Promise<{ items: Asset[]; total: number; stats?: Record<string, number>; next_cursor?: string | null }>;
  updateMeAssetMetadata(body: {
    asset_refs: string[];
    favorite?: boolean;
    retained?: boolean;
  }): Promise<unknown>;
  updateMeAssetTags(assetRef: string, tags: string[]): Promise<UserAssetMetadata>;
  findMeSimilarAssets(
    assetRef: string,
    options?: { maxDistance?: number },
  ): Promise<AssetSimilarityResult>;
  batchDeleteMeAssets(asset_refs: string[]): Promise<unknown>;
  creditTransactions(query?: {
    type?: CreditTransactionType | "all";
    biz_type?: string;
    limit?: number;
    cursor?: string;
  }): Promise<CreditTransactionPage>;
  billingEntries(query?: {
    kind?: BillingKind | "all";
    limit?: number;
    cursor?: string;
  }): Promise<BillingEntryPage>;
  paymentPackages(): Promise<unknown>;
  paymentConfig(): Promise<unknown>;
  paymentOrders(limit?: number): Promise<unknown>;
  createPaymentOrder(body: Record<string, unknown>): Promise<unknown>;
  paymentOrder(orderNo: string): Promise<unknown>;
  mockPayOrder(orderNo: string): Promise<unknown>;
  changePassword(old_password: string, new_password: string): Promise<unknown>;
  logout(): Promise<unknown>;
  parse(url: string): Promise<unknown>;
  parseStatus(id: number | string): Promise<unknown>;
  uploadImage(file: File, options?: { signal?: AbortSignal }): Promise<Asset>;
  uploadVideo(file: File, options?: { signal?: AbortSignal }): Promise<Asset>;
  reverse(
    asset_url: string,
    target?: ReverseTarget,
    fallback_image?: string | null,
    source_type?: AssetType | null,
    video_analysis_preset?: string | null,
    client_request_id?: string | null,
  ): Promise<unknown>;
  createReverseOperation(body: ReverseOperationCreate): Promise<ReverseOperation>;
  reverseAnalyzerStatus(): Promise<ReverseAnalyzerStatus>;
  createReverseBatch(body: ReverseBatchCreate): Promise<ReverseBatch>;
  reverseBatch(id: number | string): Promise<ReverseBatch>;
  reverseBatches(query?: { limit?: number; offset?: number }): Promise<ReverseBatch[]>;
  cancelReverseBatch(id: number | string): Promise<ReverseBatch>;
  reverseOperation(id: number | string): Promise<ReverseOperation>;
  reverseOperations(query?: ReverseOperationQuery): Promise<ReverseOperation[]>;
  createReproductionAssessment(body: ReproductionAssessmentCreateInput): Promise<ReproductionAssessmentResponse>;
  reproductionAssessment(id: number | string): Promise<ReproductionAssessmentResponse>;
  reproductionAssessments(query?: {
    status?: ReproductionAssessmentStatus | "";
    media_type?: AssetType | "";
    limit?: number;
    offset?: number;
  }): Promise<ReproductionAssessmentPage>;
  cancelReproductionAssessment(id: number | string): Promise<ReproductionAssessmentResponse>;
  createReproductionCorrection(
    id: number | string,
    body: ReproductionCorrectionInput,
  ): Promise<ReproductionCorrectionResponse>;
  createReproductionRemediation(
    assessmentId: number | string,
    body: ReproductionRemediationCreateInput,
  ): Promise<ReproductionRemediationResponse>;
  reproductionRemediations(
    assessmentId: number | string,
    query?: { status?: ReproductionRemediationStatus | ""; limit?: number; offset?: number },
  ): Promise<ReproductionRemediationPage>;
  reproductionRemediation(
    assessmentId: number | string,
    remediationId: number | string,
  ): Promise<ReproductionRemediationResponse>;
  confirmReverseOperationCover(id: number | string, fallback_image?: string | null): Promise<ReverseOperation>;
  cancelReverseOperation(id: number | string): Promise<ReverseOperation>;
  reverseOperationWsTicket(id: number | string): Promise<{ ticket: string; expires_in: number }>;
  reverseOperationRevisions(id: number | string): Promise<ReverseResultRevision[]>;
  reverseOperationFeedback(id: number | string): Promise<ReverseOperationFeedback | null>;
  createReverseOperationRevision(id: number | string, body: ReverseResultRevisionCreate): Promise<ReverseResultRevision>;
  applyReverseOperationResult(id: number | string, body: ReverseResultApplyInput): Promise<ReverseResultApplyResult>;
  editReverseOperationShots(id: number | string, body: ReverseShotTimelineEditInput): Promise<ReverseResultRevision>;
  reanalyzeReverseOperationShot(id: number | string, body: ReverseShotReanalyzeInput): Promise<ReverseOperation>;
  prepareReverseOperationShotGeneration(id: number | string, body: ReverseShotGenerationPrepareInput): Promise<ReverseShotGenerationPrepareResult>;
  updateReverseOperationFeedback(id: number | string, body: ReverseOperationFeedbackInput): Promise<ReverseOperationFeedback>;
  retryReverseOperation(id: number | string, body: ReverseOperationRetryInput): Promise<ReverseOperation>;
  studioPromptOptimizations(query?: StudioPromptOptimizationQuery): Promise<StudioPromptOptimizationPage>;
  studioPromptOptimization(proposalId: number | string): Promise<StudioPromptOptimizationDetail>;
  createStudioPromptOptimization(body: StudioPromptOptimizationRequest): Promise<StudioPromptOptimizationProposal>;
  acceptStudioPromptOptimization(proposalId: number | string, body: StudioPromptOptimizationDecision): Promise<StudioPromptOptimizationDecisionResponse>;
  rejectStudioPromptOptimization(proposalId: number | string, body: StudioPromptOptimizationDecision): Promise<StudioPromptOptimizationDecisionResponse>;
  subjectProtectionPreview(asset_url: string, edit_mask_mode?: EditMaskMode): Promise<SubjectProtectionPreview>;
  quote(payload: GenerationQuotePayload): Promise<GenerationQuote>;
  studioQuote(payload: StudioQuoteRequest): Promise<StudioQuote>;
  generate(payload: GeneratePayload): Promise<Task>;
  task(id: number | string): Promise<Task>;
  taskWsTicket(id: number | string): Promise<{ ticket: string; expires_in: number }>;
  eventWsTicket(): Promise<{ ticket: string; expires_in: number }>;
  taskCenter(query?: UnifiedTaskQuery): Promise<UnifiedTaskPage>;
  tasks(limit?: number, offset?: number): Promise<Task[]>;
  cancelTask(taskId: number | string): Promise<Task>;
  unlock(assetId: number | string, body: { quote_id: number }): Promise<Asset>;
  playbackTicket(assetId: number | string): Promise<unknown>;
  playbackUrl(assetId: number | string): string;
  favoriteAsset(assetId: number | string): Promise<Asset>;
  reportAsset(assetId: number | string, body: Record<string, unknown>): Promise<unknown>;
  deleteAsset(assetId: number | string): Promise<unknown>;
  batchDeleteAssets(assetIds: Array<number | string>): Promise<unknown>;
  batchDownloadAssets(assetIds: Array<number | string>): Promise<string>;
  retryTask(taskId: number | string): Promise<Task>;
  requoteRetryTask(
    taskId: number | string,
    body: { client_request_id: string; model_config_id?: number | null },
  ): Promise<Task>;
  downloadUrl(assetId: number | string): string;
  promptHistory(query?: PromptHistoryQuery): Promise<unknown[]>;
  createPromptHistory(body: Record<string, unknown>): Promise<unknown>;
  updatePromptHistory(id: number | string, body: Record<string, unknown>): Promise<unknown>;
  favoritePromptHistory(id: number | string): Promise<unknown>;
  deletePromptHistory(id: number | string): Promise<unknown>;
  creationRecipes(query?: CreationRecipeQuery): Promise<CreationRecipe[]>;
  publicCreationRecipes(query?: CreationRecipeQuery): Promise<CreationRecipe[]>;
  createCreationRecipe(body: CreationRecipeCreate): Promise<CreationRecipe>;
  creationRecipe(id: number | string): Promise<CreationRecipe>;
  publicCreationRecipe(id: number | string): Promise<CreationRecipe>;
  sharedCreationRecipe(slug: string): Promise<CreationRecipeShared>;
  updateCreationRecipe(id: number | string, body: CreationRecipeMetadataPatch): Promise<CreationRecipe>;
  submitCreationRecipeReview(id: number | string): Promise<CreationRecipe>;
  creationRecipeShares(id: number | string): Promise<CreationRecipeShare[]>;
  createCreationRecipeShare(
    id: number | string,
    body?: { version?: number | null; expires_at?: string | null },
  ): Promise<CreationRecipeShare>;
  revokeCreationRecipeShare(id: number | string, shareId: number | string): Promise<CreationRecipeShare>;
  recordCreationRecipeUsage(
    id: number | string,
    body: {
      event_type: "apply" | "generation_prepare" | "generation_submit";
      version?: number | null;
      share_slug?: string | null;
      generation_task_id?: number | null;
      client_event_id?: string | null;
      context?: Record<string, unknown> | null;
    },
  ): Promise<CreationRecipeUsageEvent>;
  creationRecipeUsage(id: number | string): Promise<CreationRecipeUsageSummary>;
  adminRecipeReviews(query?: {
    status?: CreationRecipeModerationStatus | "all";
    limit?: number;
    offset?: number;
  }): Promise<CreationRecipe[]>;
  adminReviewRecipe(
    id: number | string,
    body: { action: "approve" | "reject"; note?: string | null },
  ): Promise<CreationRecipe>;
  creationRecipeVersions(id: number | string): Promise<CreationRecipeVersion[]>;
  creationRecipeVersion(id: number | string, version: number): Promise<CreationRecipeVersion>;
  createCreationRecipeVersion(id: number | string, body: { payload: Record<string, unknown> }): Promise<CreationRecipeVersion>;
  activateCreationRecipeVersion(id: number | string, version: number): Promise<CreationRecipe>;
  cloneCreationRecipe(id: number | string, body?: CreationRecipeCloneInput): Promise<CreationRecipe>;
  favoriteCreationRecipe(id: number | string): Promise<CreationRecipe>;
  deleteCreationRecipe(id: number | string): Promise<{ ok: boolean }>;
  projects(query?: { status?: "active" | "archived" | "all"; limit?: number; offset?: number }): Promise<MediaProject[]>;
  createProject(body: Record<string, unknown>): Promise<MediaProject>;
  project(id: number | string): Promise<MediaProject>;
  updateProject(id: number | string, body: Record<string, unknown>): Promise<MediaProject>;
  deleteProject(id: number | string): Promise<unknown>;
  exportProject(id: number | string, options?: { includeMedia?: boolean }): Promise<string>;
  addProjectAssets(id: number | string, body: { asset_refs: string[]; role?: string; note?: string | null }): Promise<MediaProject>;
  removeProjectAssets(id: number | string, asset_refs: string[]): Promise<MediaProject>;
  updateProjectAssetTags(id: number | string, assetRef: string, tags: string[]): Promise<UserAssetMetadata>;
  findProjectSimilarAssets(
    id: number | string,
    assetRef: string,
    options?: { maxDistance?: number },
  ): Promise<AssetSimilarityResult>;
  addProjectRecipes(id: number | string, recipe_ids: number[]): Promise<MediaProject>;
  removeProjectRecipe(id: number | string, recipeId: number | string): Promise<MediaProject>;
  addProjectTasks(id: number | string, task_kind: "generation" | "reverse" | "parse", task_ids: number[]): Promise<MediaProject>;
  removeProjectTask(id: number | string, taskKind: string, taskId: number | string): Promise<MediaProject>;
  assetFolders(): Promise<AssetFolder[]>;
  assetFolder(id: number | string): Promise<AssetFolder>;
  createAssetFolder(body: Record<string, unknown>): Promise<AssetFolder>;
  updateAssetFolder(id: number | string, body: Record<string, unknown>): Promise<AssetFolder>;
  deleteAssetFolder(id: number | string): Promise<unknown>;
  moveAssetsToFolder(id: number | string, asset_refs: string[]): Promise<AssetFolder>;
  removeAssetsFromFolder(id: number | string, asset_refs: string[]): Promise<AssetFolder>;
  adminWhitelist(): Promise<unknown>;
  adminAddWhitelist(body: Record<string, unknown>): Promise<unknown>;
  adminRemoveWhitelist(phone: string, body?: Record<string, unknown>): Promise<unknown>;
  adminUsers(query?: AdminUsersQuery): Promise<unknown>;
  adminGrant(body: Record<string, unknown>): Promise<unknown>;
  adminBulkGrant(body: Record<string, unknown>): Promise<unknown>;
  adminSetUserStatus(userId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminResetPassword(userId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminModels(): Promise<unknown>;
  adminCreateModel(body: Record<string, unknown>): Promise<unknown>;
  adminUpdateModel(id: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminDeleteModel(id: number | string): Promise<unknown>;
  adminSaveModel(body: Record<string, unknown>): Promise<unknown>;
  adminProbeModels(body: Record<string, unknown>): Promise<unknown>;
  adminImportModels(body: Record<string, unknown>): Promise<unknown>;
  adminModelVersions(id: number | string): Promise<ModelCatalogItem>;
  adminCreateModelVersion(id: number | string, body: Record<string, unknown>): Promise<ModelCatalogItem>;
  adminUpdateModelVersion(id: number | string, kind: "capability" | "price", version: number, body: Record<string, unknown>): Promise<ModelCatalogItem>;
  adminPublishModelVersion(id: number | string, kind: "capability" | "price", version: number): Promise<ModelCatalogItem>;
  adminDisableModelVersion(id: number | string, kind: "capability" | "price", version: number): Promise<ModelCatalogItem>;
  adminRetireModelVersion(id: number | string, kind: "capability" | "price", version: number): Promise<ModelCatalogItem>;
  adminRollbackModelVersion(id: number | string, kind: "capability" | "price", version: number): Promise<ModelCatalogItem>;
  adminActivateModelVersion(id: number | string, body: { kind: "capability" | "price"; version: number }): Promise<ModelCatalogItem>;
  adminModelRoutes(id: number | string): Promise<{ items: AdminModelRoute[]; total: number }>;
  adminCreateModelRoute(id: number | string, body: Record<string, unknown>): Promise<AdminModelRoute>;
  adminUpdateModelRoute(id: number | string, routeId: number | string, body: Record<string, unknown>): Promise<AdminModelRoute>;
  adminModelRouteVersions(id: number | string, routeId: number | string): Promise<{ route: AdminModelRoute; versions: AdminModelRouteVersion[]; total: number }>;
  adminRollbackModelRouteVersion(id: number | string, routeId: number | string, version: number): Promise<AdminModelRoute>;
  adminRetireModelRouteVersion(id: number | string, routeId: number | string, version: number): Promise<AdminModelRouteVersion>;
  adminProbeModelRoute(id: number | string, routeId: number | string): Promise<{ ok: boolean; route_id: number; models: unknown[] }>;
  adminResetModelRouteHealth(id: number | string, routeId: number | string): Promise<AdminModelRoute>;
  adminModelRouteHealthEvents(id: number | string, routeId: number | string, limit?: number): Promise<{ items: AdminModelRouteHealthEvent[]; total: number }>;
  adminTools(): Promise<{ items: ToolCatalogItem[]; total: number }>;
  adminCreateTool(body: Record<string, unknown>): Promise<ToolCatalogItem>;
  adminUpdateTool(id: number | string, body: Record<string, unknown>): Promise<ToolCatalogItem>;
  adminToolVersions(id: number | string): Promise<ToolCatalogItem>;
  adminCreateToolVersion(id: number | string, body: Record<string, unknown>): Promise<ToolCatalogItem>;
  adminUpdateToolVersion(id: number | string, version: number, body: Record<string, unknown>): Promise<ToolCatalogItem>;
  adminPublishToolVersion(id: number | string, version: number): Promise<ToolCatalogItem>;
  adminDisableToolVersion(id: number | string, version: number): Promise<ToolCatalogItem>;
  adminRetireToolVersion(id: number | string, version: number): Promise<ToolCatalogItem>;
  adminRollbackToolVersion(id: number | string, version: number): Promise<ToolCatalogItem>;
  adminActivateToolVersion(id: number | string, version: number): Promise<ToolCatalogItem>;
  adminUsageDashboard(qs?: string): Promise<AdminUsageDashboardResponse>;
  adminModelCosts(qs?: string): Promise<AdminModelCostsResponse>;
  adminReverseUsage(qs?: string): Promise<AdminReverseUsageResponse>;
  adminReport(qs?: string): Promise<unknown>;
  adminReportCsvUrl(qs?: string): string;
  adminAudit(qs?: string): Promise<unknown>;
  adminReviewTasks(limit?: number, offset?: number): Promise<unknown>;
  adminAssetReports(query?: { status?: string; limit?: number; offset?: number }): Promise<unknown>;
  adminHandleAssetReport(reportId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminRefundReviewTask(taskId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminSettleReviewTask(taskId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminSettings(): Promise<unknown>;
  adminSaveSettings(body: Record<string, unknown>): Promise<unknown>;
  adminGateway(): Promise<unknown>;
  adminUpdateStatus(checkRemote?: boolean): Promise<unknown>;
  adminRunUpdate(body: Record<string, unknown>): Promise<unknown>;
  adminPaymentConfig(): Promise<unknown>;
  adminPaymentPackages(): Promise<unknown>;
  adminSavePaymentPackage(body: Record<string, unknown>): Promise<unknown>;
  adminDisablePaymentPackage(id: number | string, body?: Record<string, unknown>): Promise<unknown>;
  adminPaymentProviders(): Promise<unknown>;
  adminSavePaymentProvider(provider: string, body: Record<string, unknown>): Promise<unknown>;
};
