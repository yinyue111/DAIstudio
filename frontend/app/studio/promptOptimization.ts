export type PromptOptimizationDirection =
  | "faithful"
  | "concise"
  | "expand"
  | "commercial"
  | "cinematic"
  | "model_adaptation"
  | "constraints"
  | "translate"
  | "target_model_adaptation";

export const PROMPT_OPTIMIZATION_DIRECTIONS: ReadonlyArray<{
  key: PromptOptimizationDirection;
  label: string;
  description: string;
}> = [
  { key: "faithful", label: "忠实整理", description: "整理表达，不改变原始需求。" },
  { key: "concise", label: "精简压缩", description: "保留约束，删除重复和空话。" },
  { key: "expand", label: "细节扩写", description: "补充可执行的视觉和镜头细节。" },
  { key: "commercial", label: "商业增强", description: "强化卖点、主体层级和广告表达。" },
  { key: "cinematic", label: "电影化", description: "增强机位、运镜、光影和节奏。" },
  { key: "target_model_adaptation", label: "模型适配", description: "编译到当前生成模型，不重新反推素材。" },
  { key: "constraints", label: "约束强化", description: "补全主体保护、禁改项和负向要求。" },
  { key: "translate", label: "翻译转换", description: "中英文转换并保留术语和字面约束。" },
];

export type PromptOptimizationContext = {
  creationMode: string;
  category: string;
  subjectMode: string;
  productGenerationMode: boolean;
  promptText?: string;
  duration?: number | string;
  aspectRatio?: string;
  resolution?: string;
  productLockMode?: string;
  productVideoTemplate?: string;
  referenceSignature?: string;
  subjectProfileSource?: string;
  targetModelId?: string;
  targetModelProvider?: string;
  optimizationDirection?: PromptOptimizationDirection;
  optimizationTargetLanguage?: "zh-CN" | "en";
};

export type PromptOptimizationRequest = {
  id: number;
  contextKey: string;
};

export function promptOptimizationContextKey(context: PromptOptimizationContext): string {
  return JSON.stringify([
    String(context.creationMode || ""),
    String(context.category || ""),
    String(context.subjectMode || ""),
    Boolean(context.productGenerationMode),
    String(context.promptText || "").trim(),
    String(context.duration || ""),
    String(context.aspectRatio || ""),
    String(context.resolution || ""),
    String(context.productLockMode || ""),
    String(context.productVideoTemplate || ""),
    String(context.referenceSignature || ""),
    String(context.subjectProfileSource || ""),
    String(context.targetModelId || ""),
    String(context.targetModelProvider || ""),
    String(context.optimizationDirection || "faithful"),
    String(context.optimizationTargetLanguage || "en"),
  ]);
}

export function isPromptOptimizationResultCurrent(
  request: PromptOptimizationRequest,
  currentRequestId: number | undefined,
  currentContextKey: string | undefined,
): boolean {
  return request.id === currentRequestId && request.contextKey === currentContextKey;
}

export type PromptOptimizationSegment = {
  id: string;
  field_path: string;
  original: unknown;
  suggestion: unknown;
  changed: boolean;
};

export type PromptOptimizationProposal = {
  proposal_id: number;
  proposal_version: number;
  original: Record<string, unknown>;
  suggestion: Record<string, unknown>;
  segments: PromptOptimizationSegment[];
};

export type PromptOptimizationDisplayProposal = PromptOptimizationProposal & {
  raw_text: string;
  optimized_text: string;
  model_name: string;
  direction: PromptOptimizationDirection;
  optimization_kind?: "rewrite" | "model_compile";
  [key: string]: unknown;
};

export function promptOptimizationDisplayProposal(
  value: Record<string, unknown>,
): PromptOptimizationDisplayProposal {
  const original = value.original && typeof value.original === "object" && !Array.isArray(value.original)
    ? value.original as Record<string, unknown>
    : {};
  const suggestion = value.suggestion && typeof value.suggestion === "object" && !Array.isArray(value.suggestion)
    ? value.suggestion as Record<string, unknown>
    : {};
  const provenance = value.provenance && typeof value.provenance === "object" && !Array.isArray(value.provenance)
    ? value.provenance as Record<string, unknown>
    : {};
  const compilerProfile = value.compiler_profile
    && typeof value.compiler_profile === "object"
    && !Array.isArray(value.compiler_profile)
    ? value.compiler_profile as Record<string, unknown>
    : {};
  const direction = String(value.mode || value.direction || "faithful") as PromptOptimizationDirection;
  return {
    ...value,
    proposal_id: Number(value.proposal_id),
    proposal_version: Number(value.proposal_version),
    original,
    suggestion,
    segments: Array.isArray(value.segments)
      ? value.segments as PromptOptimizationSegment[]
      : [],
    raw_text: String(value.raw_text ?? original.final_text ?? ""),
    optimized_text: String(value.optimized_text ?? suggestion.final_text ?? ""),
    model_name: String(
      value.model_name
      ?? compilerProfile.model_id
      ?? provenance.optimizer_model_id
      ?? "提示词模型",
    ),
    direction,
  };
}

export type PromptOptimizationWorkspace = {
  prompt: string;
  negative: string;
  structured: Record<string, unknown>;
  ratio: string;
  vDuration: number | string;
  vResolution: string;
};

export type PromptOptimizationUndo = {
  proposalId: number;
  before: PromptOptimizationWorkspace;
  after: PromptOptimizationWorkspace;
  changedFields: Array<keyof PromptOptimizationWorkspace>;
};

const PARAMETER_WORKSPACE_FIELDS = {
  aspect_ratio: "ratio",
  ratio: "ratio",
  duration: "vDuration",
  vDuration: "vDuration",
  resolution: "vResolution",
  vResolution: "vResolution",
} as const;

function cloneValue<T>(value: T): T {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, cloneValue(item)]),
    ) as T;
  }
  return value;
}

function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => deepEqual(item, right[index]));
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord).sort();
  const rightKeys = Object.keys(rightRecord).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => (
      key === rightKeys[index] && deepEqual(leftRecord[key], rightRecord[key])
    ));
}

function workspaceSnapshot(workspace: Partial<PromptOptimizationWorkspace>): PromptOptimizationWorkspace {
  return {
    prompt: String(workspace.prompt || ""),
    negative: String(workspace.negative || ""),
    structured: workspace.structured && typeof workspace.structured === "object" && !Array.isArray(workspace.structured)
      ? cloneValue(workspace.structured)
      : {},
    ratio: String(workspace.ratio || ""),
    vDuration: workspace.vDuration ?? "",
    vResolution: String(workspace.vResolution || ""),
  };
}

function pathValue(payload: unknown, fieldPath: string): { exists: boolean; value: unknown } {
  let current = payload;
  for (const part of fieldPath.split(".")) {
    if (!current || typeof current !== "object" || Array.isArray(current) || !(part in current)) {
      return { exists: false, value: undefined };
    }
    current = (current as Record<string, unknown>)[part];
  }
  return { exists: true, value: current };
}

function setStructuredPath(
  structured: Record<string, unknown>,
  parts: string[],
  authoritative: { exists: boolean; value: unknown },
): Record<string, unknown> {
  const next = cloneValue(structured);
  let current = next;
  for (const part of parts.slice(0, -1)) {
    const child = current[part];
    current[part] = child && typeof child === "object" && !Array.isArray(child)
      ? cloneValue(child as Record<string, unknown>)
      : {};
    current = current[part] as Record<string, unknown>;
  }
  const finalPart = parts.at(-1);
  if (!finalPart) return next;
  if (authoritative.exists) current[finalPart] = cloneValue(authoritative.value);
  else delete current[finalPart];
  return next;
}

function workspaceFieldForPath(fieldPath: string): keyof PromptOptimizationWorkspace | null {
  if (fieldPath === "final_text") return "prompt";
  if (fieldPath === "negative" || fieldPath === "negative_prompt") return "negative";
  if (fieldPath === "structured" || fieldPath.startsWith("structured.")) return "structured";
  const parameter = fieldPath.match(/^parameters\.([^\.]+)$/)?.[1];
  return parameter && parameter in PARAMETER_WORKSPACE_FIELDS
    ? PARAMETER_WORKSPACE_FIELDS[parameter as keyof typeof PARAMETER_WORKSPACE_FIELDS]
    : null;
}

export function isSupportedPromptOptimizationSegment(fieldPath: unknown): boolean {
  const path = String(fieldPath || "");
  if (path === "final_text" || path === "negative" || path === "negative_prompt" || path === "structured") {
    return true;
  }
  if (/^structured\.[^\.]+(?:\.[^\.]+)*$/.test(path)) return true;
  return workspaceFieldForPath(path) !== null;
}

function currentSegmentValue(workspace: PromptOptimizationWorkspace, fieldPath: string): unknown {
  if (fieldPath === "final_text") return workspace.prompt;
  if (fieldPath === "negative" || fieldPath === "negative_prompt") return workspace.negative;
  if (fieldPath === "structured") return workspace.structured;
  if (fieldPath.startsWith("structured.")) {
    return pathValue(workspace.structured, fieldPath.slice("structured.".length)).value;
  }
  const field = workspaceFieldForPath(fieldPath);
  return field ? workspace[field] : undefined;
}

function authoritativeSegmentValue(
  result: Record<string, unknown>,
  fieldPath: string,
): { exists: boolean; value: unknown } {
  const exact = pathValue(result, fieldPath);
  if (exact.exists) return exact;
  if (fieldPath === "negative") return pathValue(result, "negative_prompt");
  if (fieldPath === "negative_prompt") return pathValue(result, "negative");
  return exact;
}

function selectedSupportedSegments(
  proposal: PromptOptimizationProposal,
  result: Record<string, unknown>,
  acceptedSegmentIds?: string[],
): PromptOptimizationSegment[] {
  const accepted = Array.isArray(acceptedSegmentIds) ? new Set(acceptedSegmentIds) : null;
  return (proposal.segments || []).filter((segment) => {
    if (!segment.changed || !isSupportedPromptOptimizationSegment(segment.field_path)) return false;
    if (accepted) return accepted.has(segment.id);
    const authoritative = authoritativeSegmentValue(result, segment.field_path);
    return deepEqual(authoritative.exists ? authoritative.value : null, segment.suggestion ?? null);
  });
}

export function applyPromptOptimizationDecision(
  currentWorkspace: Partial<PromptOptimizationWorkspace>,
  proposal: PromptOptimizationProposal,
  decisionResult: Record<string, unknown> | null | undefined,
  acceptedSegmentIds?: string[],
): {
  applied: boolean;
  stale: boolean;
  workspace: PromptOptimizationWorkspace;
  changedFields: Array<keyof PromptOptimizationWorkspace>;
  undo: PromptOptimizationUndo | null;
} {
  const before = workspaceSnapshot(currentWorkspace);
  if (!decisionResult || typeof decisionResult !== "object") {
    return { applied: false, stale: false, workspace: before, changedFields: [], undo: null };
  }
  const selected = selectedSupportedSegments(proposal, decisionResult, acceptedSegmentIds);
  const stale = selected.some((segment) => !deepEqual(
    currentSegmentValue(before, segment.field_path) ?? null,
    segment.original ?? null,
  ));
  if (stale) {
    return { applied: false, stale: true, workspace: before, changedFields: [], undo: null };
  }

  const after = workspaceSnapshot(before);
  for (const segment of selected) {
    const path = segment.field_path;
    const authoritative = authoritativeSegmentValue(decisionResult, path);
    const field = workspaceFieldForPath(path);
    if (!field) continue;
    if (path === "structured") {
      after.structured = authoritative.exists
        && authoritative.value
        && typeof authoritative.value === "object"
        && !Array.isArray(authoritative.value)
        ? cloneValue(authoritative.value as Record<string, unknown>)
        : {};
    } else if (path.startsWith("structured.")) {
      after.structured = setStructuredPath(
        after.structured,
        path.slice("structured.".length).split("."),
        authoritative,
      );
    } else if (field === "prompt") {
      after.prompt = authoritative.exists ? String(authoritative.value || "") : "";
    } else if (field === "negative") {
      after.negative = authoritative.exists ? String(authoritative.value || "") : "";
    } else if (field === "ratio") {
      after.ratio = authoritative.exists ? String(authoritative.value || "") : "";
    } else if (field === "vDuration") {
      after.vDuration = authoritative.exists ? (authoritative.value as number | string) ?? "" : "";
    } else if (field === "vResolution") {
      after.vResolution = authoritative.exists ? String(authoritative.value || "") : "";
    }
  }

  const changedFields = (Object.keys(before) as Array<keyof PromptOptimizationWorkspace>)
    .filter((field) => !deepEqual(before[field], after[field]));
  if (changedFields.length === 0) {
    return { applied: false, stale: false, workspace: before, changedFields, undo: null };
  }
  return {
    applied: true,
    stale: false,
    workspace: after,
    changedFields,
    undo: {
      proposalId: proposal.proposal_id,
      before: cloneValue(before),
      after: cloneValue(after),
      changedFields,
    },
  };
}

export function undoPromptOptimization(
  currentWorkspace: Partial<PromptOptimizationWorkspace>,
  undo: PromptOptimizationUndo | null | undefined,
): { applied: boolean; stale: boolean; workspace: PromptOptimizationWorkspace } {
  const current = workspaceSnapshot(currentWorkspace);
  if (!undo) return { applied: false, stale: false, workspace: current };
  if (!deepEqual(current, undo.after)) {
    return { applied: false, stale: true, workspace: current };
  }
  return { applied: true, stale: false, workspace: cloneValue(undo.before) };
}
