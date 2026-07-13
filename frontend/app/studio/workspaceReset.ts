export function clearWorkspaceContent(current: Record<string, unknown>) {
  return {
    ...current,
    prompt: "",
    negative: "",
    url: "",
    parsing: false,
    uploading: false,
    reversing: false,
    assets: [],
    selected: null,
    productAsset: null,
    productProfile: null,
    productProfileSource: "",
    productProfiling: false,
    subjectProtection: null,
    subjectProtectionLoading: false,
    subjectProtectionSource: "",
    variationSource: null,
    structured: {},
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
