/**
 * Core domain types for the Studio frontend.
 *
 * Re-exports from api.d.ts and adds additional interfaces used across hooks,
 * components, and view-model logic.
 */

import type { ProductVideoStrategy } from "../app/studio/productVideoStrategy";

export type { ProductVideoStrategy } from "../app/studio/productVideoStrategy";

// Re-export API-level types so the rest of the codebase can import from one place.
export type {
  Asset,
  AssetType,
  EditMaskMode,
  GeneratePayload,
  ProductPixelLockMode,
  CreationRecipe,
  CreationRecipeVersion,
  ReverseAnalysisFocus,
  ReverseAnalysisPrecision,
  ReverseOperationFeedback,
  ReverseTarget,
  ReverseOperation,
  ReverseOperationStatus,
  ReverseOutputPurpose,
  ReverseReferenceRole,
  ReverseResult,
  ReverseResultRevision,
  ReverseSourceRange,
  ReverseSourceReference,
  SubjectProtectionPreview,
  Task,
  TaskStatus,
} from "./api";

// ─── Studio workspace ────────────────────────────────────────────────────────

export interface RatioOption {
  key: string;
  label: string;
  hint: string;
  w: number;
  h: number;
}

export interface ImageQualityPreset {
  key: string;
  label: string;
  hint: string;
  maxSide: number;
}

export interface VideoQualityPreset {
  key: string;
  label: string;
  hint: string;
}

export interface VideoDurationPreset {
  seconds: number;
  label: string;
  hint: string;
}

export interface CreationMode {
  key: string;
  label: string;
  icon: string;
}

export type CreationModeKey = "image" | "image_edit" | "video" | "video_edit";
export type Category = "image" | "video";
export type SubjectMode = "general" | "product" | "portrait";

export interface SubjectProfile {
  structured?: Record<string, unknown>;
  final_text?: string;
}

export interface ReverseVideoAnalysis {
  source?: {
    width?: number | null;
    height?: number | null;
    ratio?: string | null;
    duration_seconds?: number | null;
    fps?: number | null;
    has_audio?: boolean;
    audio_analyzed?: boolean;
  };
  sampled_frames?: Array<{ index: number; timestamp_seconds: number }>;
  shots?: Array<Record<string, unknown>>;
  analysis_mode?: "multi_frame" | "cover" | "image_motion" | string;
  fallback_reason?: string | null;
  evidence_coverage?: { start_seconds?: number; end_seconds?: number; ratio?: number } | null;
  analysis_gaps?: Array<string | Record<string, unknown>>;
  audio_evidence?: Record<string, unknown> | Array<Record<string, unknown>> | null;
}

export type ReverseUndoSnapshot = import("../app/studio/reverseResultApplication").ReverseResultUndoToken;

export interface WorkspaceState {
  prompt: string;
  negative: string;
  imageEditProductMode: boolean;
  editSubjectMode: string;
  ratio: string;
  imageQuality: string;
  n: number;
  seed: string;
  vDuration: number;
  vResolution: string;
  productVideoTemplate: ProductVideoStrategy;
  editMaskMode: "protect_subject" | "center_box" | "off";
  productPixelLockMode: ProductPixelLockMode;
  videoAnalysisPreset: string;
  url: string;
  appliedUrl: string;
  parsing: boolean;
  uploading: boolean;
  uploadingRole: import("../app/studio/generationUploadPolicy").StudioUploadRole | null;
  reversing: boolean;
  reverseOperation: import("./api").ReverseOperation | null;
  profileOperation: import("./api").ReverseOperation | null;
  assets: Asset[];
  selected: Asset | null;
  lastFrameAsset: Asset | null;
  productAsset: Asset | null;
  productDetailAssets: Asset[];
  productProfile: SubjectProfile | null;
  productProfileSource: string;
  portraitProfile: SubjectProfile | null;
  portraitProfileSource: string;
  productProfiling: boolean;
  subjectProtection: SubjectProtectionPreview | null;
  subjectProtectionLoading: boolean;
  subjectProtectionSource: string;
  variationSource: Asset | null;
  structured: Record<string, string>;
  structuredBaseline: Record<string, string>;
  structuredDirty: boolean;
  structuredSource: string;
  reverseVideoAnalysis: ReverseVideoAnalysis | null;
  reverseSources: import("./api").ReverseSourceReference[];
  reverseConfig: import("../app/studio/reverseConfig").ReverseConfig;
  pendingReverseResult: Record<string, unknown> | null;
  reverseResultTab: "report" | "draft" | "structure" | "storyboard" | "evidence" | "versions";
  reverseResultSchemaVersion: string;
  reverseAppliedVersion: number | null;
  reverseAppliedRevisionId: number | null;
  reverseUndoSnapshot: ReverseUndoSnapshot | null;
  reverseResultRevisions: import("./api").ReverseResultRevision[];
  reverseFeedback: import("./api").ReverseOperationFeedback | null;
  creationRecipeId: number | null;
  creationRecipeVersion: number | null;
  creationRecipeShareSlug: string;
  creationRecipeSource: "" | "owner" | "public" | "share";
  promptSourceSignature: string;
  negativeTouched: boolean;
  promptDirty: boolean;
}

export type Workspaces = Record<CreationModeKey, WorkspaceState>;

// ─── Runtime config from backend /api/config ─────────────────────────────────

export interface ModelInfo {
  cost_credits: number;
  unlock_cost: number;
  enabled: boolean;
  preview_cost?: number | null;
  final_cost?: number;
  model_config_id?: number | null;
  model_id?: string | null;
  name?: string | null;
  provider?: string | null;
}

export interface ModelOption {
  id: number;
  use: "vision" | "image" | "video" | "prompt";
  name: string;
  model_id: string;
  provider: string;
  provider_label: string;
  cost_credits: number;
  unlock_cost: number;
  is_default: boolean;
  capabilities: Record<string, boolean | number | string | string[]>;
}

export interface PricingConfig {
  image: { base: number; per_image: number; edit_multiplier: number; size_multipliers?: Record<string, number> };
  video: { preview_base: number; final_per_second: number; resolution_multipliers?: Record<string, number> };
  reverse: { image_cost: number; video_preset_costs: Record<string, number> };
}

export interface GatewayStatus {
  mock_mode: boolean;
}

export interface ReverseVideoPreset {
  key: string;
  label: string;
  max_frames: number;
  max_cost: number;
}

export interface AppConfig {
  product_edition: "full" | "launch_lite";
  defaults: Record<string, unknown>;
  features: {
    reverse_prompt_enabled: boolean;
    sms_auth_enabled: boolean;
    payment_enabled: boolean;
    reverse_batch_enabled: boolean;
    video_composition_enabled: boolean;
    reproduction_assessment_enabled: boolean;
    recipes_enabled: boolean;
    projects_enabled: boolean;
    tool_workflows_enabled: boolean;
  };
  models: Record<string, ModelInfo>;
  model_options: Record<"vision" | "image" | "video" | "prompt", ModelOption[]>;
  pricing: PricingConfig;
  image_sizes: string[];
  image_size_max_dim: number;
  image_n_max: number;
  max_upload_image_bytes: number;
  max_upload_video_bytes: number;
  video_duration_max_seconds: number;
  reverse: {
    image_cost: number;
    video_default_preset: string;
    video_frame_count: number;
    video_max_cost: number;
    video_presets: ReverseVideoPreset[];
    batch_capabilities?: Record<string, unknown>;
    capabilities?: Record<string, unknown>;
  };
  mock_mode: boolean;
  gateways: Record<string, GatewayStatus>;
}

export type Config = AppConfig;

// ─── User ────────────────────────────────────────────────────────────────────

export interface User {
  id: number;
  phone: string;
  nickname?: string | null;
  department?: string | null;
  status: string;
  is_admin: boolean;
  balance_credits: number;
  frozen_credits: number;
}

// ─── View helpers ────────────────────────────────────────────────────────────

export interface AssetDims {
  width: number;
  height: number;
}

export interface EditReadyStep {
  label: string;
  ready: boolean;
  readyText: string;
  pendingText: string;
}

import type { Asset, ProductPixelLockMode, SubjectProtectionPreview } from "./api";
