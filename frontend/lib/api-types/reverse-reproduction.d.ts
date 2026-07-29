import type { AssetType } from "./core";
import type { GenerationQuotePayload } from "./generation-prompt";

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
