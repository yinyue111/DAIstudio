const OMITTED_REVERSE_DIAGNOSTIC_KEYS = new Set([
  "binary",
  "data_uri",
  "image_evidence_analyzers",
  "languages",
  "provider_final_text",
  "raw_payload",
  "raw_response",
  "request_context",
  "source_fingerprints",
  "usage",
  "workspace_snapshot_v2",
  "workspace_snapshot_v3",
]);

const MAX_DRAFT_ARRAY_ITEMS: Record<string, number> = {
  analysis_gaps: 128,
  image_evidence: 128,
  observations: 128,
  ocr_tracks: 256,
  sampled_frames: 96,
  shots: 128,
  tracks: 256,
};

const MAX_DRAFT_STRING_LENGTH = 24_000;
const MAX_DRAFT_DEPTH = 16;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function compactDraftValue(value: unknown, key = "", depth = 0): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value === "string") {
    if (value.startsWith("data:") && value.length > 2048) return null;
    return value.length > MAX_DRAFT_STRING_LENGTH
      ? value.slice(0, MAX_DRAFT_STRING_LENGTH)
      : value;
  }
  if (typeof value !== "object") return value;
  if (depth >= MAX_DRAFT_DEPTH) return null;
  if (Array.isArray(value)) {
    const limit = MAX_DRAFT_ARRAY_ITEMS[key] || 128;
    return value
      .slice(0, limit)
      .map((item) => compactDraftValue(item, key, depth + 1));
  }
  if (!isRecord(value)) return null;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([childKey]) => !OMITTED_REVERSE_DIAGNOSTIC_KEYS.has(childKey))
      .map(([childKey, childValue]) => [
        childKey,
        compactDraftValue(childValue, childKey, depth + 1),
      ]),
  );
}

export function compactReverseResultForStudioDraft(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  const compacted = compactDraftValue(value);
  return isRecord(compacted) ? compacted : null;
}

export function compactPendingReverseResultForStudioDraft(
  value: unknown,
): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  const resultKey = isRecord(value.payload) ? "payload" : isRecord(value.result) ? "result" : null;
  const result = resultKey ? compactReverseResultForStudioDraft(value[resultKey]) : null;
  const compacted: Record<string, unknown> = Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== "payload" && key !== "result")
      .map(([key, item]) => [key, compactDraftValue(item, key, 1)]),
  );
  if (resultKey && result) compacted[resultKey] = result;
  return compacted;
}

export function compactReverseOperationForStudioDraft(
  value: unknown,
): Record<string, unknown> | null {
  if (!isRecord(value) || value.id === null || value.id === undefined) return null;
  const compacted: Record<string, unknown> = {};
  for (const key of [
    "id",
    "status",
    "target",
    "source_type",
    "phase",
    "progress",
    "analysis_focus",
    "analysis_precision",
    "output_purpose",
    "include_audio",
    "result_schema_version",
    "applied_result_version",
    "retry_of_operation_id",
    "cost_frozen",
    "cost_settled",
    "confirmation_expires_at",
    "cancel_requested",
    "error_code",
    "error",
    "expired",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "model_config_id",
    "model_name",
    "model_id",
  ]) {
    if (value[key] !== undefined) compacted[key] = compactDraftValue(value[key], key, 1);
  }
  return compacted;
}

export function compactStudioDraftValue(value: unknown): unknown {
  return compactDraftValue(value);
}

export function studioDraftByteSize(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}
