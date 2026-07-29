"use client";

import { useEffect } from "react";
import useMediaUpload from "../useMediaUpload";
import useStudioVariationActions from "../useStudioVariationActions";
import useStudioWorkspaceActions from "../useStudioWorkspaceActions";
import useSubjectProtectionPreview from "../useSubjectProtectionPreview";

export default function useStudioReferenceDomain(foundation, model, prompt, reverse) {
  const {
    cfg,
    creationMode,
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
    productAsset,
    productDetailAssets,
    productProfile,
    portraitProfile,
    profileOperation,
    uploading,
    structured,
    structuredBaseline,
    structuredSource,
    selected,
    workspace,
    showNegative,
    refOpen,
    structOpen,
    setCreationMode,
    setWorkspacePatch,
    setWorkspaces,
    setStructOpen,
    setRefOpen,
    setShowNegative,
    setMsg,
    getOwnerSession,
    refs,
  } = foundation;

  const variation = useStudioVariationActions({
    creationMode,
    ratio: foundation.ratio,
    workspace,
    showNegative,
    refOpen,
    structOpen,
    studioUiStateRef: refs.studioUiStateRef,
    variationRestoreContextRef: refs.variationRestoreContextRef,
    setCreationMode,
    setWorkspacePatch,
    setStructOpen,
    setRefOpen,
    setShowNegative,
    setMsg,
  });

  useSubjectProtectionPreview({
    creationMode,
    isImageEditMode,
    productGenerationMode,
    portraitGenerationMode,
    productAsset,
    editMaskMode: foundation.editMaskMode,
    setWorkspacePatch,
  });

  const media = useMediaUpload({
    cfg,
    creationMode,
    category,
    isEditMode,
    subjectMode,
    productAsset,
    productDetailAssets,
    productDetailLimit: model.productDetailLimit,
    visionModelConfigId: model.selectedVisionModelConfigId,
    uploading,
    setMsg,
    setWorkspacePatch,
    setCreationMode,
    setRefOpen,
    bumpRefVersion: reverse.bumpRefVersion,
    isRefVersionCurrent: reverse.isRefVersionCurrent,
    bumpReverseRequest: reverse.bumpReverseRequest,
    isModeVisible: reverse.isModeVisible,
    selectAssetForMode: reverse.selectAssetForMode,
    subjectProfilePendingRequestRef: refs.subjectProfilePendingRequestRef,
    subjectProfileResultCacheRef: refs.subjectProfileResultCacheRef,
    subjectProfileOperationsRef: refs.subjectProfileOperationsRef,
    profileOperation,
    cancelRecoveredProfileOperation: reverse.cancelRecoveredProfileOperationForMode,
    trackProfileReverseOperation: reverse.trackProfileReverseOperation,
    getOwnerSession,
    requestQuoteConfirmation: model.requestQuoteConfirmation,
  });

  const workspaceActions = useStudioWorkspaceActions({
    creationMode,
    isEditMode,
    category,
    subjectMode,
    structured,
    structuredBaseline,
    structuredSource,
    prompt: foundation.prompt,
    selected,
    refs: {
      productUploadInputRef: media.productUploadInputRef,
      imageUploadInputRef: media.imageUploadInputRef,
      lastFrameUploadInputRef: media.lastFrameUploadInputRef,
      videoUploadInputRef: media.videoUploadInputRef,
      subjectProfilePendingRequestRef: refs.subjectProfilePendingRequestRef,
      subjectProfileResultCacheRef: refs.subjectProfileResultCacheRef,
      variationRestoreContextRef: refs.variationRestoreContextRef,
    },
    cancelRecoveredProfileOperationForMode: reverse.cancelRecoveredProfileOperationForMode,
    cancelProfileOperation: media.cancelProfileOperation,
    cancelReverseOperationForMode: reverse.cancelReverseOperationForMode,
    cancelAllReverseOperations: reverse.cancelAllReverseOperations,
    cancelAllProfileOperations: media.cancelAllProfileOperations,
    bumpProductUploadRequest: media.bumpProductUploadRequest,
    bumpRefVersion: reverse.bumpRefVersion,
    bumpParseRequest: reverse.bumpParseRequest,
    bumpUploadRequest: media.bumpUploadRequest,
    bumpReverseRequest: reverse.bumpReverseRequest,
    revokeProductObjectUrl: media.revokeProductObjectUrl,
    revokeUploadedObjectUrls: media.revokeUploadedObjectUrls,
    clearSelectedForMode: reverse.clearSelectedForMode,
    clearPromptOptimizationMode: prompt.clearMode,
    resetAllPromptOptimizations: prompt.resetAll,
    resetOwnerReferenceParsing: reverse.resetOwnerReferenceParsing,
    resetOwnerMediaUpload: media.resetOwnerMediaUpload,
    setWorkspacePatch,
    setWorkspaces,
    setStructOpen,
    setMsg,
  });

  useEffect(() => {
    refs.revokeUploadedObjectUrlsRef.current = media.revokeUploadedObjectUrls;
  }, [media.revokeUploadedObjectUrls, refs.revokeUploadedObjectUrlsRef]);

  return {
    ...variation,
    ...media,
    ...workspaceActions,
    productProfile,
    portraitProfile,
  };
}
