import type {
  ReproductionAssessment,
  ReproductionFinding,
} from "./reproductionAssessment";

export type ReproductionRemediationMode = "image_inpaint" | "video_shot_regenerate";

export type ReproductionRemediationStatus =
  | "planned"
  | "awaiting_generation"
  | "queued"
  | "running"
  | "generating"
  | "awaiting_composition"
  | "composing"
  | "reassessing"
  | "succeeded"
  | "partial"
  | "failed"
  | "canceled"
  | "unknown";

export type ReproductionRemediationPlanItem = {
  id: string;
  index: number;
  kind: ReproductionRemediationMode;
  shot_id: string | null;
  finding_ids: number[];
  status: string;
  generation_request: Record<string, unknown> | null;
  generation_task_id: number | null;
  asset_id: number | null;
  asset_ref: string | null;
  error_code: string | null;
  error: string | null;
};

export type ReproductionRemediation = {
  id: number;
  assessment_id: number;
  correction_id: number | null;
  parent_remediation_id: number | null;
  parent_revision_id: number;
  applied_revision_id: number | null;
  mode: ReproductionRemediationMode;
  status: ReproductionRemediationStatus;
  phase: string | null;
  selected_finding_ids: number[];
  selected_shot_ids: string[];
  plan_items: ReproductionRemediationPlanItem[];
  generation_requests: Record<string, unknown>[];
  generation_task_ids: number[];
  composition_tool_run_id: number | null;
  composition_workflow_run_id: number | null;
  final_asset_id: number | null;
  final_asset_ref: string | null;
  successor_assessment_id: number | null;
  auto_reassess: boolean;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
};

export type ReproductionRemediationPage = {
  items: ReproductionRemediation[];
  total: number;
  limit: number;
  offset: number;
};

const MODES = new Set<ReproductionRemediationMode>([
  "image_inpaint",
  "video_shot_regenerate",
]);

const STATUSES = new Set<ReproductionRemediationStatus>([
  "planned",
  "awaiting_generation",
  "queued",
  "running",
  "generating",
  "awaiting_composition",
  "composing",
  "reassessing",
  "succeeded",
  "partial",
  "failed",
  "canceled",
]);

const ACTIVE_STATUSES = new Set<ReproductionRemediationStatus>([
  "queued",
  "running",
  "generating",
  "awaiting_composition",
  "composing",
  "reassessing",
]);

const TERMINAL_STATUSES = new Set<ReproductionRemediationStatus>([
  "succeeded",
  "partial",
  "failed",
  "canceled",
]);

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function positiveInteger(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function uniquePositiveIntegers(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(positiveInteger).filter((item): item is number => item !== null))];
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => String(item || "").trim()).filter(Boolean))];
}

function nullableString(value: unknown): string | null {
  return String(value || "").trim() || null;
}

function normalizeStatus(value: unknown): ReproductionRemediationStatus {
  const candidate = String(value || "planned").trim().toLowerCase();
  const aliases: Record<string, ReproductionRemediationStatus> = {
    created: "planned",
    ready: "planned",
    awaiting_quote: "planned",
    pending_generation: "awaiting_generation",
    processing: "running",
    composing_video: "composing",
    auto_reassessing: "reassessing",
    completed: "succeeded",
    cancelled: "canceled",
  };
  const normalized = aliases[candidate] || candidate;
  return STATUSES.has(normalized as ReproductionRemediationStatus)
    ? normalized as ReproductionRemediationStatus
    : "unknown";
}

function requestRecord(value: unknown): Record<string, unknown> | null {
  const raw = record(value);
  const nested = record(raw.generation_request);
  const request = Object.keys(nested).length ? nested : Object.keys(record(raw.request)).length
    ? record(raw.request)
    : raw;
  return Object.keys(request).length ? request : null;
}

function normalizePlanItems(value: unknown): ReproductionRemediationPlanItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const raw = record(item);
    const generationRequest = requestRecord(raw.generation_request || raw.request);
    const id = String(
      raw.id
      || raw.plan_item_id
      || raw.item_id
      || generationRequest?.reproduction_plan_item_id
      || `item-${index + 1}`,
    ).trim();
    return {
      id,
      index: Number.isInteger(Number(raw.index)) && Number(raw.index) >= 0
        ? Number(raw.index)
        : index,
      kind: String(raw.kind || (
        generationRequest?.category === "video" ? "video_shot_regenerate" : "image_inpaint"
      )) as ReproductionRemediationMode,
      shot_id: nullableString(raw.shot_id || generationRequest?.shot_id),
      finding_ids: uniquePositiveIntegers(raw.finding_ids),
      status: String(raw.status || "planned").trim().toLowerCase(),
      generation_request: generationRequest,
      generation_task_id: positiveInteger(raw.generation_task_id || raw.task_id),
      asset_id: positiveInteger(raw.asset_id),
      asset_ref: nullableString(raw.asset_ref || raw.generated_asset_ref),
      error_code: nullableString(raw.error_code),
      error: nullableString(raw.error || raw.error_message),
    };
  });
}

function normalizeGenerationRequests(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.flatMap((item, index) => {
    const request = requestRecord(item);
    if (!request) return [];
    const identity = String(
      request.reproduction_plan_item_id
      || request.client_request_id
      || `request-${index}`,
    );
    if (seen.has(identity)) return [];
    seen.add(identity);
    return [request];
  });
}

export function normalizeReproductionRemediation(
  value: unknown,
  fallbackAssessmentId: unknown = null,
): ReproductionRemediation {
  const wrapped = record(value);
  const raw = Object.keys(record(wrapped.remediation)).length
    ? record(wrapped.remediation)
    : wrapped;
  const id = positiveInteger(raw.id || raw.remediation_id);
  const assessmentId = positiveInteger(raw.assessment_id || fallbackAssessmentId);
  const parentRevisionId = positiveInteger(raw.parent_revision_id);
  const mode = String(raw.mode || "").trim() as ReproductionRemediationMode;
  if (!id || !assessmentId || !parentRevisionId || !MODES.has(mode)) {
    throw new Error("服务端未返回有效的复刻纠偏计划");
  }

  const snapshotItems = normalizePlanItems(
    Array.isArray(raw.plan_snapshot) ? raw.plan_snapshot : [],
  );
  const dynamicItems = normalizePlanItems(
    Array.isArray(raw.plan_items) ? raw.plan_items : [],
  );
  const snapshotById = new Map(snapshotItems.map((item) => [item.id, item]));
  const dynamicById = new Map(dynamicItems.map((item) => [item.id, item]));
  const orderedIds = [
    ...snapshotItems.map((item) => item.id),
    ...dynamicItems.map((item) => item.id).filter((id) => !snapshotById.has(id)),
  ];
  const planItems = orderedIds.map((itemId, index) => {
    const snapshot = snapshotById.get(itemId);
    const dynamic = dynamicById.get(itemId);
    return {
      ...(snapshot || dynamic || {}),
      ...(dynamic || {}),
      id: itemId,
      index,
      generation_request: snapshot?.generation_request || dynamic?.generation_request || null,
    } as ReproductionRemediationPlanItem;
  });
  const generationRequests = normalizeGenerationRequests(
    Array.isArray(raw.generation_requests) && raw.generation_requests.length
      ? raw.generation_requests
      : planItems.map((item) => item.generation_request).filter(Boolean),
  );
  const generationTaskIds = uniquePositiveIntegers(raw.generation_task_ids);
  for (const item of planItems) {
    if (item.generation_task_id && !generationTaskIds.includes(item.generation_task_id)) {
      generationTaskIds.push(item.generation_task_id);
    }
  }

  return {
    id,
    assessment_id: assessmentId,
    correction_id: positiveInteger(raw.correction_id),
    parent_remediation_id: positiveInteger(raw.parent_remediation_id),
    parent_revision_id: parentRevisionId,
    applied_revision_id: positiveInteger(raw.applied_revision_id || raw.correction_revision_id),
    mode,
    status: normalizeStatus(raw.status),
    phase: nullableString(raw.phase),
    selected_finding_ids: uniquePositiveIntegers(raw.selected_finding_ids),
    selected_shot_ids: uniqueStrings(raw.selected_shot_ids),
    plan_items: planItems,
    generation_requests: generationRequests,
    generation_task_ids: generationTaskIds,
    composition_tool_run_id: positiveInteger(raw.composition_tool_run_id),
    composition_workflow_run_id: positiveInteger(
      raw.composition_workflow_run_id || raw.workflow_run_id,
    ),
    final_asset_id: positiveInteger(raw.final_asset_id),
    final_asset_ref: nullableString(raw.final_asset_ref)
      || (positiveInteger(raw.final_asset_id) ? `g.${positiveInteger(raw.final_asset_id)}` : null),
    successor_assessment_id: positiveInteger(raw.successor_assessment_id),
    auto_reassess: raw.auto_reassess !== false,
    error: nullableString(raw.error || raw.error_message),
    created_at: nullableString(raw.created_at),
    updated_at: nullableString(raw.updated_at),
    finished_at: nullableString(raw.finished_at),
  };
}

export function normalizeReproductionRemediationPage(
  value: unknown,
  fallbackAssessmentId: unknown = null,
): ReproductionRemediationPage {
  const raw = record(value);
  const source = Array.isArray(value) ? value : Array.isArray(raw.items) ? raw.items : [];
  const items = source.map((item) => normalizeReproductionRemediation(item, fallbackAssessmentId));
  return {
    items,
    total: Math.max(items.length, Number(raw.total) || 0),
    limit: Math.max(1, Number(raw.limit) || Math.max(items.length, 20)),
    offset: Math.max(0, Number(raw.offset) || 0),
  };
}

export function reproductionRemediationMode(
  assessment: Pick<ReproductionAssessment, "media_type"> | null | undefined,
): ReproductionRemediationMode | null {
  if (assessment?.media_type === "image") return "image_inpaint";
  if (assessment?.media_type === "video") return "video_shot_regenerate";
  return null;
}

export function executableReproductionFindingIds(
  value: ReproductionAssessment | null | undefined,
): number[] {
  if (!value || !["succeeded", "partial"].includes(value.status)) return [];
  return value.findings
    .filter((finding) => (
      finding.severity !== "info"
      && (value.media_type === "image" ? Boolean(finding.bbox) : Boolean(finding.shot_id))
    ))
    .map((finding) => finding.id);
}

export function reproductionRemediationCreatePayload({
  assessment,
  idempotencyKey,
  parentRevisionId,
  findingIds,
  modelConfigId = null,
  promptPatch = "",
  negativePatch = "",
  params = {},
  videoComposition = null,
  autoReassess = true,
  parentRemediationId = null,
}: {
  assessment: Pick<ReproductionAssessment, "media_type" | "findings">;
  idempotencyKey: string;
  parentRevisionId: number;
  findingIds: number[];
  modelConfigId?: number | null;
  promptPatch?: string;
  negativePatch?: string;
  params?: Record<string, unknown>;
  videoComposition?: Record<string, unknown> | null;
  autoReassess?: boolean;
  parentRemediationId?: number | null;
}) {
  const mode = reproductionRemediationMode(assessment);
  const idempotency = String(idempotencyKey || "").trim();
  const parentRevision = positiveInteger(parentRevisionId);
  const selectedFindingIds = uniquePositiveIntegers(findingIds);
  const selected = new Set(selectedFindingIds);
  const selectedFindings = (assessment?.findings || []).filter((finding) => selected.has(finding.id));
  if (!mode || !idempotency || !parentRevision) throw new Error("纠偏计划缺少有效血缘");
  if (!selectedFindingIds.length || selectedFindings.length !== selectedFindingIds.length) {
    throw new Error("请至少选择一个当前评估中的可执行问题");
  }
  if (mode === "image_inpaint" && selectedFindings.some((finding) => !finding.bbox)) {
    throw new Error("局部重绘只能执行已定位图像区域的问题");
  }
  if (mode === "video_shot_regenerate" && selectedFindings.some((finding) => !finding.shot_id)) {
    throw new Error("逐镜重生成需要每个问题都关联稳定 shot_id");
  }
  const parsedModelConfigId = positiveInteger(modelConfigId);
  if (!parsedModelConfigId) throw new Error("请先选择可用的生成模型");
  const selectedShotIds = mode === "video_shot_regenerate"
    ? [...new Set(selectedFindings.map((finding) => String(finding.shot_id || "").trim()).filter(Boolean))]
    : [];
  return {
    idempotency_key: idempotency,
    parent_revision_id: parentRevision,
    parent_remediation_id: positiveInteger(parentRemediationId),
    selected_finding_ids: selectedFindingIds,
    selected_shot_ids: selectedShotIds,
    mode,
    model_config_id: parsedModelConfigId,
    prompt_patch: String(promptPatch || "").trim() || null,
    negative_prompt_patch: String(negativePatch || "").trim() || null,
    params: record(params),
    video_composition: mode === "video_shot_regenerate" && videoComposition
      ? record(videoComposition)
      : null,
    auto_reassess: Boolean(autoReassess),
  };
}

function planItemId(request: Record<string, unknown>): string {
  return String(request.reproduction_plan_item_id || "").trim();
}

export function reproductionRemediationPendingGenerationRequests(
  remediation: ReproductionRemediation | null | undefined,
): Record<string, unknown>[] {
  if (!remediation || TERMINAL_STATUSES.has(remediation.status)) return [];
  const planItems = new Map(remediation.plan_items.map((item) => [item.id, item]));
  const explicitlyPending = remediation.generation_requests.filter((request) => {
    const itemId = planItemId(request);
    const item = itemId ? planItems.get(itemId) : null;
    return !positiveInteger(request.generation_task_id || request.task_id)
      && !item?.generation_task_id
      && !["submitted", "queued", "running", "succeeded"].includes(String(item?.status || ""));
  });
  const hasExplicitTaskMapping = remediation.plan_items.some((item) => item.generation_task_id);
  if (hasExplicitTaskMapping || !remediation.generation_task_ids.length) return explicitlyPending;
  return explicitlyPending.slice(Math.min(remediation.generation_task_ids.length, explicitlyPending.length));
}

export function reproductionRemediationExecutionIssue(
  remediation: ReproductionRemediation | null | undefined,
): string | null {
  if (!remediation) return "请先创建纠偏执行计划";
  if (TERMINAL_STATUSES.has(remediation.status)) {
    return remediation.status === "succeeded" ? "该纠偏计划已完成" : "该纠偏计划已终止";
  }
  if (ACTIVE_STATUSES.has(remediation.status)) return "纠偏任务正在执行";
  const pending = reproductionRemediationPendingGenerationRequests(remediation);
  if (!pending.length) {
    return remediation.generation_task_ids.length
      ? "已提交生成任务，请刷新纠偏状态"
      : "服务端未返回可执行的生成请求";
  }
  for (const request of pending) {
    if (!planItemId(request)) return "生成请求缺少稳定计划项标识，为避免重复扣费已阻止执行";
    if (positiveInteger(request.reproduction_remediation_id) !== remediation.id) {
      return "生成请求与当前纠偏计划不匹配";
    }
    if (!String(request.client_request_id || "").trim()) {
      return "生成请求缺少幂等标识，已阻止付费执行";
    }
    if (!["image", "video"].includes(String(request.category || ""))) {
      return "生成请求缺少有效媒体类型";
    }
  }
  return null;
}

export function isReproductionRemediationActive(
  remediation: ReproductionRemediation | null | undefined,
): boolean {
  return Boolean(remediation && ACTIVE_STATUSES.has(remediation.status));
}

export function isReproductionRemediationTerminal(
  remediation: ReproductionRemediation | null | undefined,
): boolean {
  return Boolean(remediation && TERMINAL_STATUSES.has(remediation.status));
}

export function selectedReproductionFindings(
  assessment: Pick<ReproductionAssessment, "findings"> | null | undefined,
  findingIds: Iterable<number>,
): ReproductionFinding[] {
  const selected = new Set([...findingIds]);
  return (assessment?.findings || []).filter((finding) => selected.has(finding.id));
}
