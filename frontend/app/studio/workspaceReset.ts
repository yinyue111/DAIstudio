export function clearWorkspaceContent(current: Record<string, unknown>) {
  return {
    ...current,
    prompt: "",
    negative: "",
    url: "",
    appliedUrl: "",
    parsing: false,
    uploading: false,
    reversing: false,
    reverseOperation: null,
    profileOperation: null,
    assets: [],
    selected: null,
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
