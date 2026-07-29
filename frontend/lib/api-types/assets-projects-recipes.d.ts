import type { AssetType, TaskStatus } from "./core";
import type { UnifiedTaskKind } from "./workflow-tasks";

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
