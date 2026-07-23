import { normalizeImageEvidence, type ImageEvidenceItem } from "./imageEvidence";
import { normalizeRecoveredVisualPrompt } from "./helpers";

export type ReverseResultMediaType = "image" | "video";

export type ReverseResultApplicationMode =
  | "replace"
  | "append"
  | "structure_only"
  | "parameters_only";

export type ReverseResultWorkspace = Record<string, unknown>;

export interface PendingReverseResult {
  kind: "pending_reverse_result";
  mediaType: ReverseResultMediaType;
  prompt: string;
  negative: string;
  structured: Record<string, unknown>;
  imageEvidence: ImageEvidenceItem[];
  videoAnalysis: Record<string, unknown> | null;
  parameters: {
    ratio: string | null;
    vDuration: number | null;
    vResolution: string | null;
  };
  sourceSignature: string;
}

export interface NormalizePendingReverseResultOptions {
  mediaType?: ReverseResultMediaType;
  sourceSignature?: string;
}

export type ReverseResultParameterField = "ratio" | "vDuration" | "vResolution";

export interface ReverseResultApplicationSelection {
  prompt?: boolean;
  negative?: boolean;
  structuredKeys?: string[];
  parameterFields?: ReverseResultParameterField[];
  imageEvidence?: boolean;
  videoAnalysis?: boolean;
}

export interface ResolvedReverseResultApplicationSelection {
  prompt: boolean;
  negative: boolean;
  structuredKeys: string[];
  parameterFields: ReverseResultParameterField[];
  imageEvidence: boolean;
  videoAnalysis: boolean;
}

export type ReverseResultWorkspaceField =
  | "prompt"
  | "negative"
  | "structured"
  | "structuredBaseline"
  | "ratio"
  | "vDuration"
  | "vResolution"
  | "promptSourceSignature"
  | "structuredSource"
  | "promptDirty"
  | "negativeTouched"
  | "structuredDirty"
  | "reverseVideoAnalysis";

interface ReverseResultUndoValue {
  present: boolean;
  value?: unknown;
}

export interface ReverseResultUndoToken {
  version: 1;
  mode: ReverseResultApplicationMode;
  changedFields: ReverseResultWorkspaceField[];
  before: Partial<Record<ReverseResultWorkspaceField, ReverseResultUndoValue>>;
  after?: Partial<Record<ReverseResultWorkspaceField, ReverseResultUndoValue>>;
}

export interface ReverseResultApplication<T extends ReverseResultWorkspace = ReverseResultWorkspace> {
  workspace: T;
  changedFields: ReverseResultWorkspaceField[];
  undo: ReverseResultUndoToken | null;
}

export type ReverseResultApplicationMergeStatus = "applied" | "unchanged" | "conflict";

export interface ReverseResultApplicationMerge<
  T extends ReverseResultWorkspace = ReverseResultWorkspace,
> extends ReverseResultApplication<T> {
  status: ReverseResultApplicationMergeStatus;
  pending: boolean;
  conflictFields: ReverseResultWorkspaceField[];
  pendingFields: ReverseResultWorkspaceField[];
}

export type ReverseResultAction =
  | {
      type: ReverseResultApplicationMode;
      pending: PendingReverseResult;
      selection?: ReverseResultApplicationSelection;
    }
  | {
      type: "undo";
      undo: ReverseResultUndoToken;
    };

const REVERSIBLE_FIELDS = new Set<ReverseResultWorkspaceField>([
  "prompt",
  "negative",
  "structured",
  "structuredBaseline",
  "ratio",
  "vDuration",
  "vResolution",
  "promptSourceSignature",
  "structuredSource",
  "promptDirty",
  "negativeTouched",
  "structuredDirty",
  "reverseVideoAnalysis",
]);

const FORBIDDEN_RECORD_KEYS = new Set(["__proto__", "prototype", "constructor"]);
const CLAUSE_SEPARATOR = /[\n\r,，;；]+/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cloneValue<T>(value: T): T {
  if (Array.isArray(value)) return value.map((item) => cloneValue(item)) as T;
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !FORBIDDEN_RECORD_KEYS.has(key))
      .map(([key, item]) => [key, cloneValue(item)]),
  ) as T;
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => valuesEqual(item, right[index]));
  }
  if (!isRecord(left) || !isRecord(right)) return false;
  const leftKeys = Object.keys(left).filter((key) => !FORBIDDEN_RECORD_KEYS.has(key)).sort();
  const rightKeys = Object.keys(right).filter((key) => !FORBIDDEN_RECORD_KEYS.has(key)).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => (
      key === rightKeys[index] && valuesEqual(left[key], right[key])
    ));
}

function hasOwnField(
  workspace: ReverseResultWorkspace,
  field: ReverseResultWorkspaceField,
): boolean {
  return Object.prototype.hasOwnProperty.call(workspace, field);
}

function fieldState(
  workspace: ReverseResultWorkspace,
  field: ReverseResultWorkspaceField,
): ReverseResultUndoValue {
  const present = hasOwnField(workspace, field);
  return {
    present,
    ...(present ? { value: cloneValue(workspace[field]) } : {}),
  };
}

function fieldStatesEqual(
  left: ReverseResultWorkspace,
  right: ReverseResultWorkspace,
  field: ReverseResultWorkspaceField,
): boolean {
  const leftPresent = hasOwnField(left, field);
  const rightPresent = hasOwnField(right, field);
  return leftPresent === rightPresent
    && (!leftPresent || valuesEqual(left[field], right[field]));
}

function workspaceMatchesUndoValue(
  workspace: ReverseResultWorkspace,
  field: ReverseResultWorkspaceField,
  expected: ReverseResultUndoValue,
): boolean {
  const present = hasOwnField(workspace, field);
  return present === expected.present
    && (!present || valuesEqual(workspace[field], expected.value));
}

function undoFieldGroup(field: ReverseResultWorkspaceField): string {
  if (["prompt", "promptSourceSignature", "promptDirty"].includes(field)) return "prompt";
  if (["negative", "negativeTouched"].includes(field)) return "negative";
  if (["structured", "structuredBaseline", "structuredSource", "structuredDirty"].includes(field)) {
    return "structured";
  }
  return field;
}

function firstRecord(...values: unknown[]): Record<string, unknown> {
  return values.find(isRecord) as Record<string, unknown> || {};
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function normalizedText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (!Array.isArray(value)) return "";
  return value
    .map((item) => typeof item === "string" ? item.trim() : "")
    .filter(Boolean)
    .join("，");
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    const text = normalizedText(value);
    if (text) return text;
  }
  return "";
}

function positiveInteger(...values: unknown[]): number | null {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return Math.max(1, Math.round(parsed));
  }
  return null;
}

function cleanStructured(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  return cloneValue(Object.fromEntries(
    Object.entries(value).filter(([key]) => !FORBIDDEN_RECORD_KEYS.has(key)),
  ));
}

export function resolveReverseResultApplicationSelection(
  pending: PendingReverseResult,
  selection?: ReverseResultApplicationSelection,
): ResolvedReverseResultApplicationSelection {
  const structuredKeys = Object.keys(pending.structured)
    .filter((key) => !FORBIDDEN_RECORD_KEYS.has(key));
  const availableParameters: ReverseResultParameterField[] = [];
  if (pending.parameters.ratio) availableParameters.push("ratio");
  if (pending.mediaType === "video" && pending.parameters.vDuration !== null) {
    availableParameters.push("vDuration");
  }
  if (pending.mediaType === "video" && pending.parameters.vResolution) {
    availableParameters.push("vResolution");
  }

  if (selection === undefined) {
    return {
      prompt: Boolean(pending.prompt),
      negative: Boolean(pending.negative),
      structuredKeys,
      parameterFields: availableParameters,
      imageEvidence: pending.mediaType === "image"
        && confirmedImageEvidence(pending.imageEvidence).length > 0,
      videoAnalysis: pending.mediaType === "video" && Boolean(pending.videoAnalysis),
    };
  }

  const requestedStructured = new Set(
    Array.isArray(selection.structuredKeys)
      ? selection.structuredKeys.filter((key) => typeof key === "string")
      : [],
  );
  const requestedParameters = new Set(
    Array.isArray(selection.parameterFields) ? selection.parameterFields : [],
  );
  return {
    prompt: selection.prompt === true && Boolean(pending.prompt),
    negative: selection.negative === true && Boolean(pending.negative),
    structuredKeys: structuredKeys.filter((key) => requestedStructured.has(key)),
    parameterFields: availableParameters.filter((field) => requestedParameters.has(field)),
    imageEvidence: selection.imageEvidence === true
      && pending.mediaType === "image"
      && confirmedImageEvidence(pending.imageEvidence).length > 0,
    videoAnalysis: selection.videoAnalysis === true
      && pending.mediaType === "video"
      && Boolean(pending.videoAnalysis),
  };
}

export function confirmedImageEvidence(value: unknown): ImageEvidenceItem[] {
  return normalizeImageEvidence(value)
    .filter((item) => item.review_status === "confirmed")
    .map((item) => cloneValue(item));
}

export function selectedImageEvidenceForApplication(
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): ImageEvidenceItem[] {
  if (pending.mediaType !== "image" || !selection.imageEvidence) return [];
  return confirmedImageEvidence(pending.imageEvidence);
}

function inferMediaType(
  envelope: Record<string, unknown>,
  result: Record<string, unknown>,
  explicit?: ReverseResultMediaType,
): ReverseResultMediaType {
  if (explicit) return explicit;
  const declared = firstString(
    result.mediaType,
    result.media_type,
    result.source_type,
    envelope.mediaType,
    envelope.media_type,
    envelope.source_type,
  );
  if (declared === "video") return "video";
  const target = firstString(result.target, envelope.target);
  if (target === "video" || target === "image_to_video") return "video";
  if (isRecord(result.video_analysis) || isRecord(envelope.video_analysis)) return "video";
  return "image";
}

/**
 * Converts current and future image/video reverse envelopes into one UI-safe
 * pending result. Empty or invalid values are represented explicitly and are
 * treated as unavailable by the application functions.
 */
export function normalizePendingReverseResult(
  input: unknown,
  options: NormalizePendingReverseResultOptions = {},
): PendingReverseResult {
  const envelope = isRecord(input) ? input : {};
  const result = firstRecord(envelope.normalized_result, envelope.result, envelope);
  const structured = cleanStructured(result.structured);
  const analysis = firstRecord(result.video_analysis, envelope.video_analysis);
  const analysisSource = firstRecord(analysis.source);
  const parameters = firstRecord(
    result.parameters,
    result.generation_parameters,
    envelope.parameters,
    envelope.generation_parameters,
  );
  const mediaType = inferMediaType(envelope, result, options.mediaType);
  const reverseTarget = firstString(result.target, envelope.target) || mediaType;
  const providerPrompt = firstText(result.final_text, result.prompt, result.generation_prompt);
  const prompt = mediaType === "image"
    ? normalizeRecoveredVisualPrompt(structured, providerPrompt, {
        target: reverseTarget,
        preserveFallback: envelope.dirty === true || result.dirty === true,
      })
    : providerPrompt;
  const imageEvidence = mediaType === "image"
    ? normalizeImageEvidence(result.image_evidence)
    : [];
  const negative = firstText(
    result.negative,
    result.negative_prompt,
    result.negative_constraints,
    structured["负向"],
    structured.negative,
  );
  const sourceSignature = firstString(
    options.sourceSignature,
    result.sourceSignature,
    result.source_signature,
    envelope.sourceSignature,
    envelope.source_signature,
    isRecord(envelope.request_context) ? envelope.request_context.source_signature : "",
    isRecord(envelope.workspace_snapshot_v2) ? envelope.workspace_snapshot_v2.source_signature : "",
  );

  return {
    kind: "pending_reverse_result",
    mediaType,
    prompt,
    negative,
    structured,
    imageEvidence,
    videoAnalysis: Object.keys(analysis).length ? cloneValue(analysis) : null,
    parameters: {
      ratio: firstString(parameters.ratio, result.ratio, analysisSource.ratio) || null,
      vDuration: mediaType === "video"
        ? positiveInteger(
            parameters.vDuration,
            parameters.v_duration,
            parameters.duration,
            result.vDuration,
            result.v_duration,
            result.duration,
            analysisSource.duration_seconds,
          )
        : null,
      vResolution: mediaType === "video"
        ? firstString(
            parameters.vResolution,
            parameters.v_resolution,
            parameters.resolution,
            result.vResolution,
            result.v_resolution,
            result.resolution,
          ) || null
        : null,
    },
    sourceSignature,
  };
}

function clauseKey(value: string): string {
  return value
    .trim()
    .replace(/\s+/g, " ")
    .replace(/[.。!！?？]+$/g, "")
    .toLocaleLowerCase();
}

function clauses(value: string): string[] {
  return value
    .split(CLAUSE_SEPARATOR)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** Appends only clauses not already present, preserving the existing text. */
export function appendUniqueReverseText(
  current: unknown,
  incoming: unknown,
  separator = "，",
): string {
  const currentText = typeof current === "string" ? current : "";
  const incomingText = normalizedText(incoming);
  if (!incomingText) return currentText;
  const existingKeys = new Set(clauses(currentText).map(clauseKey));
  const additions: string[] = [];
  for (const clause of clauses(incomingText)) {
    const key = clauseKey(clause);
    if (!key || existingKeys.has(key)) continue;
    existingKeys.add(key);
    additions.push(clause);
  }
  if (!additions.length) return currentText;
  const base = currentText.trim();
  return base ? `${base}${separator}${additions.join(separator)}` : additions.join(separator);
}

function setPatchField(
  workspace: ReverseResultWorkspace,
  patch: Partial<Record<ReverseResultWorkspaceField, unknown>>,
  field: ReverseResultWorkspaceField,
  value: unknown,
) {
  if (!valuesEqual(workspace[field], value)) patch[field] = cloneValue(value);
}

interface SelectedStructuredApplication {
  value: Record<string, unknown>;
  baseline: Record<string, unknown>;
  source: string;
  dirty: boolean;
}

function selectedStructuredApplication(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): SelectedStructuredApplication | null {
  const incoming = pending.structured;
  const incomingKeys = Object.keys(incoming);
  if (!incomingKeys.length) return null;
  const selectedKeys = new Set(
    selection.structuredKeys.filter((key) => !FORBIDDEN_RECORD_KEYS.has(key)),
  );
  const selectedEntries = incomingKeys
    .filter((key) => selectedKeys.has(key))
    .map((key) => [key, cloneValue(incoming[key])] as const);
  if (!selectedEntries.length) return null;
  if (selectedEntries.length === incomingKeys.length) {
    return {
      value: incoming,
      baseline: incoming,
      source: pending.sourceSignature,
      dirty: false,
    };
  }
  const value = {
    ...cleanStructured(workspace.structured),
    ...Object.fromEntries(selectedEntries),
  };
  const baseline = {
    ...cleanStructured(workspace.structuredBaseline),
    ...Object.fromEntries(selectedEntries),
  };
  return {
    value,
    baseline,
    source: "",
    dirty: !valuesEqual(value, baseline),
  };
}

function replacePatch(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): Partial<Record<ReverseResultWorkspaceField, unknown>> {
  const patch: Partial<Record<ReverseResultWorkspaceField, unknown>> = {};
  if (selection.prompt && pending.prompt) {
    setPatchField(workspace, patch, "prompt", pending.prompt);
    setPatchField(workspace, patch, "promptSourceSignature", pending.sourceSignature);
    setPatchField(workspace, patch, "promptDirty", false);
  }
  if (selection.negative && pending.negative) {
    setPatchField(workspace, patch, "negative", pending.negative);
    setPatchField(workspace, patch, "negativeTouched", false);
  }
  const structured = selectedStructuredApplication(workspace, pending, selection);
  if (structured) {
    setPatchField(workspace, patch, "structured", structured.value);
    setPatchField(workspace, patch, "structuredBaseline", structured.baseline);
    setPatchField(workspace, patch, "structuredSource", structured.source);
    setPatchField(workspace, patch, "structuredDirty", structured.dirty);
  }
  if (selection.videoAnalysis && pending.mediaType === "video" && pending.videoAnalysis) {
    setPatchField(workspace, patch, "reverseVideoAnalysis", pending.videoAnalysis);
  }
  Object.assign(patch, parametersPatch(workspace, pending, selection));
  return patch;
}

function appendPatch(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): Partial<Record<ReverseResultWorkspaceField, unknown>> {
  const patch: Partial<Record<ReverseResultWorkspaceField, unknown>> = {};
  const prompt = !selection.prompt
    ? (typeof workspace.prompt === "string" ? workspace.prompt : "")
    : appendUniqueReverseText(workspace.prompt, pending.prompt);
  if (!valuesEqual(prompt, workspace.prompt)) {
    setPatchField(workspace, patch, "prompt", prompt);
    setPatchField(workspace, patch, "promptSourceSignature", "");
    setPatchField(workspace, patch, "promptDirty", true);
  }
  const negative = !selection.negative
    ? (typeof workspace.negative === "string" ? workspace.negative : "")
    : appendUniqueReverseText(workspace.negative, pending.negative);
  if (!valuesEqual(negative, workspace.negative)) {
    setPatchField(workspace, patch, "negative", negative);
    setPatchField(workspace, patch, "negativeTouched", true);
  }
  return patch;
}

function structurePatch(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): Partial<Record<ReverseResultWorkspaceField, unknown>> {
  const patch: Partial<Record<ReverseResultWorkspaceField, unknown>> = {};
  const structured = selectedStructuredApplication(workspace, pending, selection);
  if (structured) {
    setPatchField(workspace, patch, "structured", structured.value);
    setPatchField(workspace, patch, "structuredBaseline", structured.baseline);
    setPatchField(workspace, patch, "structuredSource", structured.source);
    setPatchField(workspace, patch, "structuredDirty", structured.dirty);
  }
  if (selection.videoAnalysis && pending.mediaType === "video" && pending.videoAnalysis) {
    setPatchField(workspace, patch, "reverseVideoAnalysis", pending.videoAnalysis);
  }
  return patch;
}

function parametersPatch(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  selection: ResolvedReverseResultApplicationSelection,
): Partial<Record<ReverseResultWorkspaceField, unknown>> {
  const patch: Partial<Record<ReverseResultWorkspaceField, unknown>> = {};
  const selected = new Set(selection.parameterFields);
  if (selected.has("ratio") && pending.parameters.ratio) {
    setPatchField(workspace, patch, "ratio", pending.parameters.ratio);
  }
  if (selected.has("vDuration") && pending.mediaType === "video" && pending.parameters.vDuration !== null) {
    setPatchField(workspace, patch, "vDuration", pending.parameters.vDuration);
  }
  if (selected.has("vResolution") && pending.mediaType === "video" && pending.parameters.vResolution) {
    setPatchField(workspace, patch, "vResolution", pending.parameters.vResolution);
  }
  return patch;
}

function patchForMode(
  workspace: ReverseResultWorkspace,
  pending: PendingReverseResult,
  mode: ReverseResultApplicationMode,
  selection: ResolvedReverseResultApplicationSelection,
) {
  if (mode === "append") return appendPatch(workspace, pending, selection);
  if (mode === "structure_only") return structurePatch(workspace, pending, selection);
  if (mode === "parameters_only") return parametersPatch(workspace, pending, selection);
  return replacePatch(workspace, pending, selection);
}

export function applyReverseResultApplication<T extends ReverseResultWorkspace>(
  workspace: T,
  pending: PendingReverseResult,
  mode: ReverseResultApplicationMode,
  selection?: ReverseResultApplicationSelection,
): ReverseResultApplication<T> {
  const resolvedSelection = resolveReverseResultApplicationSelection(pending, selection);
  const patch = patchForMode(workspace, pending, mode, resolvedSelection);
  const changedFields = Object.keys(patch) as ReverseResultWorkspaceField[];
  if (!changedFields.length) return { workspace, changedFields: [], undo: null };

  const before: ReverseResultUndoToken["before"] = {};
  for (const field of changedFields) {
    const present = Object.prototype.hasOwnProperty.call(workspace, field);
    before[field] = {
      present,
      ...(present ? { value: cloneValue(workspace[field]) } : {}),
    };
  }
  const next: ReverseResultWorkspace = { ...workspace };
  for (const field of changedFields) next[field] = cloneValue(patch[field]);
  return {
    workspace: next as T,
    changedFields,
    undo: { version: 1, mode, changedFields, before },
  };
}

/**
 * Commits a server-confirmed application onto the latest workspace without
 * replaying the stale request-time workspace. Selected fields are committed
 * atomically: if any of them changed while the request was pending, none are
 * overwritten and the application remains pending for explicit review.
 */
export function mergeConfirmedReverseResultApplication<
  T extends ReverseResultWorkspace,
>(
  currentWorkspace: T,
  requestWorkspace: T,
  application: ReverseResultApplication<T>,
): ReverseResultApplicationMerge<T> {
  const seen = new Set<ReverseResultWorkspaceField>();
  const actualFields = (Array.isArray(application.changedFields) ? application.changedFields : [])
    .filter((field): field is ReverseResultWorkspaceField => {
      if (!REVERSIBLE_FIELDS.has(field) || seen.has(field)) return false;
      seen.add(field);
      return !fieldStatesEqual(requestWorkspace, application.workspace, field);
    });
  if (!actualFields.length) {
    return {
      status: "unchanged",
      pending: false,
      workspace: currentWorkspace,
      changedFields: [],
      conflictFields: [],
      pendingFields: [],
      undo: null,
    };
  }

  const fieldsToApply = actualFields.filter(
    (field) => !fieldStatesEqual(currentWorkspace, application.workspace, field),
  );
  const conflictFields = fieldsToApply.filter(
    (field) => !fieldStatesEqual(currentWorkspace, requestWorkspace, field),
  );
  if (conflictFields.length) {
    return {
      status: "conflict",
      pending: true,
      workspace: currentWorkspace,
      changedFields: [],
      conflictFields,
      pendingFields: fieldsToApply,
      undo: null,
    };
  }
  if (!fieldsToApply.length) {
    return {
      status: "unchanged",
      pending: false,
      workspace: currentWorkspace,
      changedFields: [],
      conflictFields: [],
      pendingFields: [],
      undo: null,
    };
  }

  const next: ReverseResultWorkspace = { ...currentWorkspace };
  const before: ReverseResultUndoToken["before"] = {};
  const after: NonNullable<ReverseResultUndoToken["after"]> = {};
  for (const field of fieldsToApply) {
    before[field] = fieldState(currentWorkspace, field);
    const desiredPresent = hasOwnField(application.workspace, field);
    if (desiredPresent) next[field] = cloneValue(application.workspace[field]);
    else delete next[field];
    after[field] = fieldState(next, field);
  }
  return {
    status: "applied",
    pending: false,
    workspace: next as T,
    changedFields: fieldsToApply,
    conflictFields: [],
    pendingFields: [],
    undo: {
      version: 1,
      mode: application.undo?.mode || "replace",
      changedFields: fieldsToApply,
      before,
      after,
    },
  };
}

export function undoReverseResultApplication<T extends ReverseResultWorkspace>(
  workspace: T,
  token: ReverseResultUndoToken,
): ReverseResultApplication<T> {
  if (!token || token.version !== 1 || !Array.isArray(token.changedFields)) {
    return { workspace, changedFields: [], undo: null };
  }
  const next: ReverseResultWorkspace = { ...workspace };
  const restored: ReverseResultWorkspaceField[] = [];
  const staleGroups = new Set<string>();
  for (const field of token.changedFields) {
    if (!REVERSIBLE_FIELDS.has(field)) continue;
    const after = token.after?.[field];
    if (after && !workspaceMatchesUndoValue(workspace, field, after)) {
      staleGroups.add(undoFieldGroup(field));
    }
  }
  for (const field of token.changedFields) {
    if (!REVERSIBLE_FIELDS.has(field)) continue;
    if (staleGroups.has(undoFieldGroup(field))) continue;
    const before = token.before?.[field];
    if (!before) continue;
    const currentlyPresent = hasOwnField(next, field);
    if (before.present) {
      if (!currentlyPresent || !valuesEqual(next[field], before.value)) restored.push(field);
      next[field] = cloneValue(before.value);
    } else {
      if (currentlyPresent) restored.push(field);
      delete next[field];
    }
  }
  if (!restored.length) return { workspace, changedFields: [], undo: null };
  return { workspace: next as T, changedFields: restored, undo: null };
}

export function applyReverseResultAction<T extends ReverseResultWorkspace>(
  workspace: T,
  action: ReverseResultAction,
): ReverseResultApplication<T> {
  return action.type === "undo"
    ? undoReverseResultApplication(workspace, action.undo)
    : applyReverseResultApplication(workspace, action.pending, action.type, action.selection);
}
