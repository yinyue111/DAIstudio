export const API_BASE: string;

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryAfter?: number;
}

export type AssetType = "image" | "video";
export type ReverseTarget = AssetType | "product_profile" | "portrait_profile";
export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "needs_review" | "canceled";
export type ReverseOperationStatus = "queued" | "running" | "needs_confirmation" | "succeeded" | "failed" | "canceled";

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
  result: Record<string, unknown> | null;
  video_analysis: Record<string, unknown> | null;
  request_context: Record<string, unknown> | null;
  workspace_snapshot_v2: Record<string, unknown> | null;
  reference_count: number;
  charged_credits: number;
  cost_frozen: number;
  cost_settled: number;
  confirmation_expires_at: string | null;
  cancel_requested: boolean;
  error_code: string | null;
  error: string | null;
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
  asset_url: string;
  target: ReverseTarget;
  client_request_id: string;
  fallback_image?: string | null;
  source_type?: AssetType | null;
  video_analysis_preset?: string | null;
  workspace_snapshot_v2?: Record<string, unknown> | null;
  model_config_id?: number | null;
}

export interface Asset {
  id?: number;
  asset_ref?: string;
  origin?: "generated" | "uploaded";
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
  prompt_text?: string | null;
  prompt_text_source?: "generation" | "request" | null;
  request_prompt_text?: string | null;
  generation_prompt_text?: string | null;
  raw_prompt_text?: string | null;
  optimized_prompt_text?: string | null;
  assembled_prompt_text?: string | null;
  prompt_optimizer_model_id?: string | null;
  prompt_compiler_version?: string | null;
  prompt_warnings?: string[];
  post_overlays?: string[];
  voiceover?: string | null;
  sfx?: string[];
  sequence_required?: boolean;
  parent_task_id?: number | null;
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

export interface GeneratePayload {
  source_asset_url?: string | null;
  source_type?: AssetType | null;
  source_asset_meta?: Record<string, unknown> | null;
  category: "image" | "video";
  stage: "preview" | "final";
  instruction?: string | null;
  prompt?: Record<string, unknown>;
  params?: Record<string, unknown>;
  parent_task_id?: number | null;
  client_request_id?: string | null;
  model_config_id?: number | null;
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
}

export interface PromptOptimizeResult {
  prompt: string;
  model_id: string;
  optimizer_model_id?: string | null;
  compiler_metadata?: Record<string, unknown> | null;
  context_metadata?: Record<string, unknown> | null;
  optimizer_model_config_id?: number | null;
  optimizer_model_name?: string | null;
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
export function getToken(): string | null;
export function setToken(token: string | null): void;
export function clearToken(): void;
export function loginPath(nextPath?: string | null): string;
export function downloadBlob(path: string, filename: string, options?: { method?: string; body?: unknown }): Promise<string>;
export function authenticatedObjectUrl(pathOrUrl: string): Promise<string>;
export function assetDownloadObjectUrl(assetId: number | string): Promise<string>;

export const api: {
  register(phone: string, password: string, sms_code?: string, nickname?: string): Promise<unknown>;
  authFeatures(): Promise<unknown>;
  sendSmsCode(phone: string): Promise<unknown>;
  login(phone: string, password: string): Promise<unknown>;
  me(options?: Record<string, unknown>): Promise<unknown>;
  config(): Promise<unknown>;
  profile(): Promise<unknown>;
  profileAssets(query?: ProfileAssetQuery): Promise<Asset[]>;
  meAssets(query?: {
    origin?: "all" | "generated" | "uploaded";
    type?: "all" | AssetType;
    favorite?: boolean | "";
    retention?: "all" | "retained" | "expiring";
    limit?: number;
    offset?: number;
    cursor?: string;
  }): Promise<{ items: Asset[]; total: number; stats?: Record<string, number>; next_cursor?: string | null }>;
  updateMeAssetMetadata(body: {
    asset_refs: string[];
    favorite?: boolean;
    retained?: boolean;
  }): Promise<unknown>;
  batchDeleteMeAssets(asset_refs: string[]): Promise<unknown>;
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
  reverseOperation(id: number | string): Promise<ReverseOperation>;
  reverseOperations(query?: { status?: string; limit?: number; offset?: number }): Promise<ReverseOperation[]>;
  confirmReverseOperationCover(id: number | string, fallback_image?: string | null): Promise<ReverseOperation>;
  cancelReverseOperation(id: number | string): Promise<ReverseOperation>;
  reverseOperationWsTicket(id: number | string): Promise<{ ticket: string; expires_in: number }>;
  optimizePrompt(
    prompt: string,
    options?: "image" | "video" | PromptOptimizeOptions,
    product_mode?: boolean,
  ): Promise<PromptOptimizeResult>;
  subjectProtectionPreview(asset_url: string, edit_mask_mode?: EditMaskMode): Promise<SubjectProtectionPreview>;
  generate(payload: GeneratePayload): Promise<Task>;
  task(id: number | string): Promise<Task>;
  taskWsTicket(id: number | string): Promise<{ ticket: string; expires_in: number }>;
  eventWsTicket(): Promise<{ ticket: string; expires_in: number }>;
  tasks(limit?: number, offset?: number): Promise<Task[]>;
  cancelTask(taskId: number | string): Promise<Task>;
  unlock(assetId: number | string): Promise<Asset>;
  playbackTicket(assetId: number | string): Promise<unknown>;
  playbackUrl(assetId: number | string): string;
  favoriteAsset(assetId: number | string): Promise<Asset>;
  reportAsset(assetId: number | string, body: Record<string, unknown>): Promise<unknown>;
  deleteAsset(assetId: number | string): Promise<unknown>;
  batchDeleteAssets(assetIds: Array<number | string>): Promise<unknown>;
  batchDownloadAssets(assetIds: Array<number | string>): Promise<string>;
  retryTask(taskId: number | string): Promise<Task>;
  downloadUrl(assetId: number | string): string;
  promptHistory(query?: PromptHistoryQuery): Promise<unknown[]>;
  createPromptHistory(body: Record<string, unknown>): Promise<unknown>;
  updatePromptHistory(id: number | string, body: Record<string, unknown>): Promise<unknown>;
  favoritePromptHistory(id: number | string): Promise<unknown>;
  deletePromptHistory(id: number | string): Promise<unknown>;
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
  adminSaveModel(body: Record<string, unknown>): Promise<unknown>;
  adminProbeModels(body: Record<string, unknown>): Promise<unknown>;
  adminImportModels(body: Record<string, unknown>): Promise<unknown>;
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
