/**
 * Core domain types for the Studio frontend.
 *
 * Re-exports from api.d.ts and adds additional interfaces used across hooks,
 * components, and view-model logic.
 */

// Re-export API-level types so the rest of the codebase can import from one place.
export type { Asset, AssetType, GeneratePayload, ReverseTarget, Task, TaskStatus } from "./api";

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
  editMaskMode: "protect_subject" | "center_box" | "off";
  videoAnalysisPreset: string;
  url: string;
  parsing: boolean;
  uploading: boolean;
  reversing: boolean;
  assets: Asset[];
  selected: Asset | null;
  productAsset: Asset | null;
  productProfile: SubjectProfile | null;
  productProfileSource: string;
  productProfiling: boolean;
  variationSource: Asset | null;
  structured: Record<string, string>;
  structuredSource: string;
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
  defaults: Record<string, unknown>;
  features: {
    reverse_prompt_enabled: boolean;
    sms_auth_enabled: boolean;
    payment_enabled: boolean;
  };
  models: Record<string, ModelInfo>;
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

import type { Asset } from "./api";
