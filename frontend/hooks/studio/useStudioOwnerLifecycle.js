"use client";

import useStudioDeepLinkBootstrap from "../useStudioDeepLinkBootstrap";
import useStudioOwnerSession from "../useStudioOwnerSession";
import useStudioRuntimeBootstrap from "../useStudioRuntimeBootstrap";

export default function useStudioOwnerLifecycle(
  foundation,
  model,
  prompt,
  task,
  reverse,
  reference,
  generation,
) {
  const {
    me,
    cfg,
    workspaces,
    creationMode,
    showNegative,
    refOpen,
    structOpen,
    toolWorkflowsEnabled,
    router,
    setMe,
    setCfg,
    setWorkspaces,
    setCreationMode,
    setShowNegative,
    setRefOpen,
    setStructOpen,
    setWorkspacePatch,
    setMsg,
    notify,
    refs,
  } = foundation;

  function resetStudioOwnerResources() {
    task.resetOwnerTracking();
    reference.resetOwnerMediaUpload();
    reverse.resetOwnerReferenceParsing();
    reference.cancelOwnerProfileOperationsAndReset();
    generation.resetOwnerGenerationSubmit();
    task.resetOwnerAssetActions();
  }

  const ownerSession = useStudioOwnerSession({
    me,
    cfg,
    workspaces,
    creationMode,
    showNegative,
    refOpen,
    structOpen,
    effectiveModelSelections: model.effectiveModelSelections,
    refs: {
      cloudDraftLoadedRef: refs.cloudDraftLoadedRef,
      studioInitSeqRef: refs.studioInitSeqRef,
      studioDraftClockRef: refs.studioDraftClockRef,
      studioOwnerSessionCoordinatorRef: refs.studioOwnerSessionCoordinatorRef,
      studioOwnerSessionRef: refs.studioOwnerSessionRef,
      latestMeRequestRef: refs.latestMeRequestRef,
      studioOwnerUserIdRef: refs.studioOwnerUserIdRef,
      initialStudioUiStateRef: refs.initialStudioUiStateRef,
      cloudDraftWriterRef: refs.cloudDraftWriterRef,
      studioUiStateRef: refs.studioUiStateRef,
      variationRestoreContextRef: refs.variationRestoreContextRef,
      reverseResumeOperationsRef: refs.reverseResumeOperationsRef,
      localReverseDraftRef: refs.localReverseDraftRef,
      loadWorksSeqRef: task.loadWorksSeqRef,
      taskRef: task.taskRef,
      workspacesRef: refs.workspacesRef,
    },
    setWorkspaces,
    setCreationMode,
    setShowNegative,
    setRefOpen,
    setStructOpen,
    setWorks: task.setWorks,
    setWorksError: task.setWorksError,
    setLightbox: task.setLightbox,
    setMsg,
    resetOwnerResources: resetStudioOwnerResources,
    applyModelSelectionPreferences: model.applyModelSelectionPreferences,
    applyVariationDraft: reference.applyVariationDraft,
    loadWorks: task.loadWorks,
  });

  useStudioDeepLinkBootstrap({
    ownerId: me?.id,
    cfg,
    workspaces,
    toolWorkflowsEnabled,
    allModelOptions: model.allModelOptions,
    modelOptionsForSelection: model.modelOptionsForSelection,
    creationMode,
    cloudDraftLoadedRef: refs.cloudDraftLoadedRef,
    workflowBootstrapRef: refs.workflowBootstrapRef,
    catalogModelBootstrapRef: refs.catalogModelBootstrapRef,
    reverseTaskBootstrapRef: refs.reverseTaskBootstrapRef,
    setCreationMode,
    setWorkspacePatch,
    setRefOpen,
    setStructOpen,
    setMsg,
    notify,
    changeModelSelection: prompt.changeModelSelection,
    openRecentReverseOperation: reverse.openRecentReverseOperation,
  });

  useStudioRuntimeBootstrap({
    router,
    setMe,
    setCfg,
    setMsg,
    studioInitSeqRef: refs.studioInitSeqRef,
    studioOwnerSessionCoordinatorRef: refs.studioOwnerSessionCoordinatorRef,
    stopAllTracking: task.stopAllTracking,
    revokeUploadedObjectUrls: reference.revokeUploadedObjectUrls,
    revokeProductObjectUrls: reference.revokeProductObjectUrls,
  });

  return {
    ...ownerSession,
    resetStudioOwnerResources,
  };
}
