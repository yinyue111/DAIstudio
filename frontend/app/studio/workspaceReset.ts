import { DEFAULT_REVERSE_CONFIG } from "./reverseConfig";

export function clearReverseLineage() {
  return {
    reversing: false,
    reverseOperation: null,
    pendingReverseResult: null,
    reverseResultTab: "draft",
    reverseResultSchemaVersion: "",
    reverseAppliedVersion: null,
    reverseAppliedRevisionId: null,
    reverseUndoSnapshot: null,
    reverseApplyConflict: null,
    reverseResultRevisions: [],
    reverseFeedback: null,
  };
}

export function clearWorkspaceContent(current: Record<string, unknown>) {
  return {
    ...current,
    prompt: "",
    negative: "",
    url: "",
    appliedUrl: "",
    parsing: false,
    uploading: false,
    uploadingRole: null,
    ...clearReverseLineage(),
    profileOperation: null,
    assets: [],
    selected: null,
    lastFrameAsset: null,
    productAsset: null,
    productDetailAssets: [],
    productProfile: null,
    productProfileSource: "",
    portraitProfile: null,
    portraitProfileSource: "",
    productProfiling: false,
    subjectProtection: null,
    subjectProtectionLoading: false,
    subjectProtectionSource: "",
    variationSource: null,
    structured: {},
    structuredBaseline: {},
    structuredDirty: false,
    structuredSource: "",
    reverseVideoAnalysis: null,
    reverseSources: [],
    reverseConfig: { ...DEFAULT_REVERSE_CONFIG, source_ranges: [], custom_keyframes: [] },
    batchReverseAssets: [],
    creationRecipeId: null,
    creationRecipeVersion: null,
    creationRecipeShareSlug: "",
    creationRecipeSource: "",
    promptSourceSignature: "",
    negativeTouched: false,
    promptDirty: false,
  };
}

export function clearAllWorkspaceContent(
  workspaces: Record<string, Record<string, unknown>>,
) {
  return Object.fromEntries(
    Object.entries(workspaces).map(([mode, workspace]) => [
      mode,
      clearWorkspaceContent(workspace),
    ]),
  );
}
