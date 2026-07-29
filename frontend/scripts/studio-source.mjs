import { readFileSync } from "node:fs";
import { dirname } from "node:path";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const STUDIO_SOURCE_FILES = [
  "app/page.jsx",
  "app/studio/StudioWorkspaceRoot.jsx",
  "app/studio/StudioWorkspaceView.jsx",
  "app/studio/buildStudioPresentation.js",
  "app/studio/StudioCreationConsole.jsx",
  "app/studio/StudioAssetPickerDialog.jsx",
  "app/studio/StudioPromptSaveControls.jsx",
  "app/studio/StudioResultsSection.jsx",
  "app/studio/studioModelContext.ts",
  "hooks/useStudioModelSelection.js",
  "hooks/useStudioDeepLinkBootstrap.js",
  "hooks/useStudioRuntimeBootstrap.js",
  "hooks/useSubjectProtectionPreview.js",
  "hooks/useReverseResultReviewContext.js",
  "hooks/useStudioOwnerSession.js",
  "hooks/studioOwnerRestore.js",
  "hooks/studio/useStudioSharedRefs.js",
  "hooks/studio/useStudioFoundation.js",
  "hooks/studio/useStudioModelDomain.js",
  "hooks/studio/useStudioTaskDomain.js",
  "hooks/studio/useStudioPromptDomain.js",
  "hooks/studio/useStudioReverseDomain.js",
  "hooks/studio/useStudioReferenceDomain.js",
  "hooks/studio/useStudioGenerationDomain.js",
  "hooks/studio/useStudioOwnerLifecycle.js",
];

export function readStudioSource(root) {
  return STUDIO_SOURCE_FILES
    .map((relativePath) => readFileSync(join(root, relativePath), "utf8"))
    .join("\n");
}

export function readStudioSourceFromUrl(importMetaUrl) {
  return readStudioSource(dirname(dirname(fileURLToPath(importMetaUrl))));
}
