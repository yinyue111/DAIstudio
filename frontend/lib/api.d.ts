export const API_BASE: string;

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryAfter?: number;
}

export type AssetType = "image" | "video";
export type ReverseTarget = AssetType | "product_profile";
export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "needs_review" | "canceled";

export interface Asset {
  id?: number;
  type: AssetType;
  url?: string;
  thumb?: string | null;
  display_url?: string | null;
  display_thumb?: string | null;
  original_url?: string | null;
  original_thumb?: string | null;
  source_page_url?: string | null;
  source_captured_at?: string | null;
  preview_url?: string | null;
  hd_url?: string | null;
  width?: number | null;
  height?: number | null;
  thumb_width?: number | null;
  thumb_height?: number | null;
  unlocked?: boolean;
  favorite?: boolean;
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
  adminSaveModel(body: Record<string, unknown>): Promise<unknown>;
  adminProbeModels(body: Record<string, unknown>): Promise<unknown>;
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
