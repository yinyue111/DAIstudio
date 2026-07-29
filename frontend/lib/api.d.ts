import type * as AdminTypes from "./api-types/admin";
import type * as AssetProjectRecipeTypes from "./api-types/assets-projects-recipes";
import type * as BillingCatalogTypes from "./api-types/billing-catalog";
import type * as ReverseReproductionTypes from "./api-types/reverse-reproduction";
import type * as WorkflowTaskTypes from "./api-types/workflow-tasks";
import type { AdminModelCostsResponse, AdminReverseUsageResponse, AdminUsageDashboardResponse, AdminUsersQuery } from "./api-types/admin";
import type {
  Asset,
  AssetFolder,
  CreationRecipe,
  CreationRecipeCloneInput,
  CreationRecipeCreate,
  CreationRecipeMetadataPatch,
  CreationRecipeModerationStatus,
  CreationRecipeQuery,
  CreationRecipeShare,
  CreationRecipeShared,
  CreationRecipeUsageEvent,
  CreationRecipeUsageSummary,
  CreationRecipeVersion,
  ProfileAssetQuery,
  UserAssetMetadata,
} from "./api-types/assets-projects-recipes";
import type {
  AdminModelRoute,
  AdminModelRouteHealthEvent,
  AdminModelRouteVersion,
  BillingEntryPage,
  CreditTransactionPage,
  CreditTransactionType,
  ModelCatalogItem,
  ToolCatalogItem,
} from "./api-types/billing-catalog";
import type { AppNavigationCatalog, AssetType } from "./api-types/core";
import type {
  EditMaskMode,
  GeneratePayload,
  GenerationQuote,
  GenerationQuotePayload,
  PromptHistoryQuery,
  StudioPromptOptimizationDecision,
  StudioPromptOptimizationDecisionResponse,
  StudioPromptOptimizationDetail,
  StudioPromptOptimizationPage,
  StudioPromptOptimizationProposal,
  StudioPromptOptimizationQuery,
  StudioPromptOptimizationRequest,
  StudioQuote,
  StudioQuoteRequest,
  SubjectProtectionPreview,
  Task,
} from "./api-types/generation-prompt";
import type {
  ReproductionAssessmentCreateInput,
  ReproductionAssessmentPage,
  ReproductionAssessmentResponse,
  ReproductionAssessmentStatus,
  ReproductionCorrectionInput,
  ReproductionCorrectionResponse,
  ReproductionRemediationCreateInput,
  ReproductionRemediationPage,
  ReproductionRemediationResponse,
  ReproductionRemediationStatus,
  ReverseAnalyzerStatus,
  ReverseBatch,
  ReverseBatchCreate,
  ReverseOperation,
  ReverseOperationCreate,
  ReverseOperationFeedback,
  ReverseOperationFeedbackInput,
  ReverseOperationQuery,
  ReverseResultApplyResult,
  ReverseResultRevision,
  ReverseShotGenerationPrepareInput,
  ReverseShotGenerationPrepareResult,
  ReverseShotReanalyzeInput,
  ReverseShotTimelineEditInput,
  ReverseTarget,
} from "./api-types/reverse-reproduction";
import type {
  UnifiedTaskPage,
  UnifiedTaskQuery,
  WorkflowRetryInput,
  WorkflowReviewInput,
  WorkflowRunCreateInput,
  WorkflowRunPage,
} from "./api-types/workflow-tasks";

export * from "./api-types/admin";
export * from "./api-types/assets-projects-recipes";
export * from "./api-types/billing-catalog";
export * from "./api-types/core";
export * from "./api-types/generation-prompt";
export * from "./api-types/reverse-reproduction";
export * from "./api-types/workflow-tasks";

export const API_BASE: string;

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryAfter?: number;
}

// Explicit facade declarations preserve the legacy declaration entrypoint and its
// source-level contract checks while the complete shapes live in domain modules.
export type UnifiedTaskKind = "generation" | "reverse" | "parse" | "workflow";

export interface WorkflowNodeAttempt {}
export interface WorkflowNodeAttempt extends WorkflowTaskTypes.WorkflowNodeAttempt {}

export interface WorkflowNodeRun {}
export interface WorkflowNodeRun extends WorkflowTaskTypes.WorkflowNodeRun {}

export interface WorkflowRun {}
export interface WorkflowRun extends WorkflowTaskTypes.WorkflowRun {}

export interface UnifiedTaskFailureSuggestion {}
export interface UnifiedTaskFailureSuggestion extends WorkflowTaskTypes.UnifiedTaskFailureSuggestion {}

export interface UnifiedTaskRetryModel {}
export interface UnifiedTaskRetryModel extends WorkflowTaskTypes.UnifiedTaskRetryModel {}

export interface UnifiedTask {
  failure_suggestion?: UnifiedTaskFailureSuggestion | null;
  compatible_retry_models: UnifiedTaskRetryModel[];
}
export interface UnifiedTask extends WorkflowTaskTypes.UnifiedTask {}

export type BillingKind = "generation" | "reverse" | "workflow" | "prompt" | "unlock" | "recharge" | "grant" | "other";

export interface BillingEntry {
  quote_id?: number | null;
  reservation_refunded_credits: number;
  balance_conserved: boolean;
}
export interface BillingEntry extends BillingCatalogTypes.BillingEntry {}

export type CatalogMetadataOrigin = BillingCatalogTypes.CatalogMetadataOrigin;

export interface ModelCatalogMetadataSnapshot {
  schema_version: "model-catalog-metadata.v1";
  origin: CatalogMetadataOrigin;
  model_id: string;
  display_name: string;
  is_default: boolean;
  sort_order: number;
  enabled: boolean;
}
export interface ModelCatalogMetadataSnapshot extends BillingCatalogTypes.ModelCatalogMetadataSnapshot {}

export interface ModelCapabilityVersion {
  metadata_snapshot: ModelCatalogMetadataSnapshot;
}
export interface ModelCapabilityVersion extends BillingCatalogTypes.ModelCapabilityVersion {}

export type ToolCatalogCategory = BillingCatalogTypes.ToolCatalogCategory;

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
export interface ToolCatalogMetadataSnapshot extends BillingCatalogTypes.ToolCatalogMetadataSnapshot {}

export interface ToolCatalogVersion {
  metadata_snapshot: ToolCatalogMetadataSnapshot;
}
export interface ToolCatalogVersion extends BillingCatalogTypes.ToolCatalogVersion {}

export interface ReverseResultRevisionCreate {
  source: "user_edit" | "applied";
  payload: ReverseReproductionTypes.ReverseResult;
  parent_revision_id?: number | null;
  clear_image_evidence?: boolean;
}
export interface ReverseResultRevisionCreate extends ReverseReproductionTypes.ReverseResultRevisionCreate {}

export interface ReverseResultApplyInput {
  payload: ReverseReproductionTypes.ReverseResult;
  parent_revision_id: number;
  clear_image_evidence?: boolean;
}
export interface ReverseResultApplyInput extends ReverseReproductionTypes.ReverseResultApplyInput {}

export interface ReverseOperationRetryInput {
  client_request_id: string;
  quote_id: number;
  model_config_id?: number | null;
}
export interface ReverseOperationRetryInput extends ReverseReproductionTypes.ReverseOperationRetryInput {}

export interface AssetSimilarityResult {}
export interface AssetSimilarityResult extends AssetProjectRecipeTypes.AssetSimilarityResult {}

export interface MediaProject {
  auto_archive_after_days: number | null;
  cost_settled: number;
}
export interface MediaProject extends AssetProjectRecipeTypes.MediaProject {}

export type AdminReverseCostStatus = "complete" | "partial" | "unavailable";

export interface AdminReverseEconomics {
  gross_profit_credits: number | null;
  gross_margin_rate: number | null;
}
export interface AdminReverseEconomics extends AdminTypes.AdminReverseEconomics {}

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
  paymentOrders(limit?: number, options?: { cursor?: string | number | null }): Promise<unknown>;
  createPaymentOrder(body: Record<string, unknown>): Promise<unknown>;
  paymentOrder(orderNo: string): Promise<unknown>;
  requestPaymentInvoice(orderNo: string, body: Record<string, unknown>): Promise<unknown>;
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
  adminSetUserRole(userId: number | string, body: Record<string, unknown>): Promise<unknown>;
  adminWorkflowSummary(): Promise<unknown>;
  adminCompleteWorkflowNode(
    runId: number | string,
    nodeKey: string,
    body: Record<string, unknown>,
  ): Promise<unknown>;
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
  adminPaymentOrders(params?: Record<string, string | number>): Promise<unknown>;
  adminSyncPaymentOrder(orderNo: string): Promise<unknown>;
  adminRefundPaymentOrder(orderNo: string, body: Record<string, unknown>): Promise<unknown>;
  adminPaymentInvoices(query?: { status?: string; limit?: number; offset?: number }): Promise<unknown>;
  adminProcessPaymentInvoice(orderNo: string, body: Record<string, unknown>): Promise<unknown>;
};
