import { CREATION_MODES } from "./constants";
import {
  normalizedPromptDraft,
  positiveRevisionId,
} from "./reverseResultRevisions";

const GENERATION_SOURCE_REVISION_SOURCES = new Set(["applied"]);

export function parsePromptDraft(raw: unknown) {
  if (raw && typeof raw === "object") return normalizedPromptDraft(raw);
  try {
    const parsed = JSON.parse(String(raw || ""));
    if (parsed && typeof parsed === "object") return normalizedPromptDraft(parsed);
  } catch (_error) {
    // Legacy prompt drafts were stored as plain strings.
  }
  return {
    prompt: String(raw || ""),
    negative: "",
    structured: {},
    generation: {},
    category: "",
    creationMode: "",
    creationRecipeId: null,
    creationRecipeVersion: null,
    creationRecipeShareSlug: "",
    creationRecipeSource: "",
    savedAt: 0,
    reverseSnapshot: null,
    legacyReverse: false,
  };
}

export function findGenerationSourceRevision(
  revisions: unknown,
  {
    revisionId,
    version,
    operationId,
  }: { revisionId?: unknown; version?: unknown; operationId?: unknown },
) {
  const id = positiveRevisionId(revisionId);
  const appliedVersion = positiveRevisionId(version);
  const ownerOperationId = positiveRevisionId(operationId);
  const candidates = (Array.isArray(revisions) ? revisions : []).filter((revision) => (
    GENERATION_SOURCE_REVISION_SOURCES.has(String(revision?.source || ""))
    && (!ownerOperationId || Number(revision?.operation_id) === ownerOperationId)
  ));
  return (id ? candidates.find((revision) => Number(revision?.id) === id) : null)
    || (appliedVersion
      ? candidates.find((revision) => Number(revision?.version) === appliedVersion)
      : null)
    || null;
}

export function promptDraftMode(draft: { creationMode?: string; category?: string }) {
  if (draft.creationMode && CREATION_MODES.some((item) => item.key === draft.creationMode)) {
    return draft.creationMode;
  }
  return draft.category === "video" ? "video" : "image";
}
