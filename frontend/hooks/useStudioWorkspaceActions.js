"use client";

import { errorMessage, reportBackgroundError } from "../lib/errorHandling";
import { CREATION_MODES } from "../app/studio/constants";
import {
  assetSignature,
  composePromptFromStructured,
  composeStyleTransferPrompt,
} from "../app/studio/helpers";
import {
  clearAllWorkspaceContent,
  clearWorkspaceContent,
} from "../app/studio/workspaceReset";

export default function useStudioWorkspaceActions({
  creationMode,
  isEditMode,
  category,
  subjectMode,
  structured,
  structuredBaseline,
  structuredSource,
  prompt,
  selected,
  refs,
  cancelRecoveredProfileOperationForMode,
  cancelProfileOperation,
  cancelReverseOperationForMode,
  cancelAllReverseOperations,
  cancelAllProfileOperations,
  bumpProductUploadRequest,
  bumpRefVersion,
  bumpParseRequest,
  bumpUploadRequest,
  bumpReverseRequest,
  revokeProductObjectUrl,
  revokeUploadedObjectUrls,
  clearSelectedForMode,
  clearPromptOptimizationMode,
  resetAllPromptOptimizations,
  resetOwnerReferenceParsing,
  resetOwnerMediaUpload,
  setWorkspacePatch,
  setWorkspaces,
  setStructOpen,
  setMsg,
}) {
  const {
    productUploadInputRef,
    imageUploadInputRef,
    lastFrameUploadInputRef,
    videoUploadInputRef,
    subjectProfilePendingRequestRef,
    subjectProfileResultCacheRef,
    variationRestoreContextRef,
  } = refs;

  async function clearProductAsset() {
    try {
      await cancelRecoveredProfileOperationForMode(creationMode);
      await cancelProfileOperation(creationMode);
    } catch (error) {
      reportBackgroundError(error, "cancel profile while clearing subject");
      setMsg(errorMessage(error, "主体档案任务取消失败，已保留当前素材。"));
      return;
    }
    bumpProductUploadRequest(creationMode);
    revokeProductObjectUrl(creationMode);
    setWorkspacePatch({
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
    });
    if (productUploadInputRef.current) productUploadInputRef.current.value = "";
  }

  async function clearRef() {
    const mode = creationMode;
    try {
      await cancelReverseOperationForMode(mode);
    } catch (error) {
      reportBackgroundError(error, "cancel reverse while clearing reference");
      setMsg(errorMessage(error, "反推任务取消失败，已保留当前参考素材。"));
      return;
    }
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpUploadRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
    if (lastFrameUploadInputRef.current) lastFrameUploadInputRef.current.value = "";
    if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
    clearSelectedForMode(mode);
    setWorkspacePatch((current) => ({
      selected: null,
      lastFrameAsset: null,
      assets: [],
      variationSource: null,
      url: "",
      appliedUrl: "",
      parsing: false,
      uploading: false,
      uploadingRole: null,
      reversing: false,
      structured: {},
      structuredBaseline: {},
      structuredDirty: false,
      structuredSource: "",
      reverseVideoAnalysis: null,
      reverseOperation: null,
      ...(current.promptSourceSignature && !current.promptDirty
        ? { prompt: "", promptSourceSignature: "", promptDirty: false }
        : { promptSourceSignature: "" }),
      ...(current.negativeTouched ? {} : { negative: "" }),
    }), mode);
  }

  function updateReferenceUrl(value) {
    const mode = creationMode;
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    setWorkspacePatch({ url: value, parsing: false }, mode);
  }

  function recompose() {
    const nextPrompt = isEditMode
      ? composeStyleTransferPrompt(structured, prompt, { video: category === "video", subject: subjectMode })
      : composePromptFromStructured(structured, prompt, { target: category });
    setWorkspacePatch({
      prompt: nextPrompt,
      promptDirty: false,
      structuredBaseline: structured,
      structuredDirty: false,
      promptSourceSignature: structuredSource || assetSignature(selected) || "",
    });
  }

  function undoStructuredChanges() {
    setWorkspacePatch({
      structured: structuredBaseline || {},
      structuredDirty: false,
    });
  }

  async function clearCurrentWorkspace() {
    const mode = creationMode;
    try {
      await cancelReverseOperationForMode(mode);
      await cancelRecoveredProfileOperationForMode(mode);
      await cancelProfileOperation(mode);
    } catch (error) {
      reportBackgroundError(error, "cancel reverse operation while clearing workspace");
      setMsg(errorMessage(error, "后台任务取消失败，已保留当前模式内容。"));
      return;
    }
    clearPromptOptimizationMode(mode);
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpUploadRequest(mode);
    bumpProductUploadRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    revokeProductObjectUrl(mode);
    clearSelectedForMode(mode);
    subjectProfilePendingRequestRef.current = null;
    subjectProfileResultCacheRef.current = null;
    variationRestoreContextRef.current = null;
    setWorkspacePatch((current) => clearWorkspaceContent(current), mode);
    setStructOpen(true);
    setMsg("");
  }

  async function clearAllWorkspaces() {
    if (!window.confirm("确认清空全部创作模式？所有模式中的提示词、素材和反推结果都会被移除。")) return;
    try {
      await cancelAllReverseOperations();
      await cancelAllProfileOperations();
    } catch (error) {
      reportBackgroundError(error, "cancel reverse operations while clearing all workspaces");
      setMsg(errorMessage(error, "部分后台任务取消失败，已保留全部模式内容。"));
      return;
    }
    resetAllPromptOptimizations(CREATION_MODES.map(({ key }) => key));
    resetOwnerReferenceParsing();
    resetOwnerMediaUpload();
    subjectProfilePendingRequestRef.current = null;
    subjectProfileResultCacheRef.current = null;
    variationRestoreContextRef.current = null;
    setWorkspaces(clearAllWorkspaceContent);
    setStructOpen(true);
    setMsg("");
  }

  return {
    clearProductAsset,
    clearRef,
    updateReferenceUrl,
    recompose,
    undoStructuredChanges,
    clearCurrentWorkspace,
    clearAllWorkspaces,
  };
}
