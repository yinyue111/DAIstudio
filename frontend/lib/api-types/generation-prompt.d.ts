import type { AssetType, TaskStatus } from "./core";
import type { Asset } from "./assets-projects-recipes";

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

export interface PromptHistoryQuery {
  favorite?: boolean | null;
  category?: string;
  source?: string;
  q?: string;
  limit?: number;
  offset?: number;
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
