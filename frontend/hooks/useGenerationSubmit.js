"use client";

import { useRef, useState } from "react";
import { api } from "../lib/api";
import {
  normalizeReverseOperation,
  reverseOperationFailure,
  reverseOperationResult,
} from "../lib/reverseOperations";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import { buildReverseOperationRequestSnapshotV2 } from "../app/studio/reverseSnapshot";
import { classifyGenerationError, reportBackgroundError } from "../lib/errorHandling";
import {
  clearPendingGenerateRequest,
  clearPendingReverseRequest,
  generateReverseClientRequestId,
  shouldKeepPendingReverseRequest,
} from "../app/studio/generationRequestId";
import { assetSignature, isRequestTimeoutError, isTerminalTaskStatus } from "../app/studio/helpers";
import {
  buildSubjectProfileRequestIdentity,
  cacheSubjectProfileResult,
  normalizeSubjectProfileResult,
  readCachedSubjectProfileResult,
} from "../lib/studioSubjectProfile";
import {
  executeGenerationSubmission,
  prepareGenerationSubmission,
  validateGenerationSubmission,
} from "./generationSubmitWorkflow";

function trackingLostError(operation) {
  return Object.assign(
    new Error(operation?.error || "主体档案仍在后台处理中，稍后会自动恢复进度"),
    { operation_id: operation?.id },
  );
}

function waitForTrackedProfileOperation(operation, {
  mode,
  trackProfileReverseOperation,
  context,
}) {
  if (typeof trackProfileReverseOperation !== "function") {
    return Promise.reject(new Error("主体档案任务跟踪器不可用"));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      callback(value);
    };
    try {
      Promise.resolve(trackProfileReverseOperation(operation, mode, {
        ...context,
        onUpdate: (nextOperation) => {
          context.onUpdate?.(nextOperation);
          if (nextOperation?.error_code === "tracking_lost") {
            finish(reject, trackingLostError(nextOperation));
          }
        },
        onSettled: (settledOperation) => {
          context.onSettled?.(settledOperation);
          finish(resolve, settledOperation);
        },
      })).catch((error) => finish(reject, error));
    } catch (error) {
      finish(reject, error);
    }
  });
}


function subjectProfilePatch(subjectMode, profile, source, busy = false) {
  return subjectMode === "portrait"
    ? {
        portraitProfile: profile,
        portraitProfileSource: source,
        productProfile: null,
        productProfileSource: "",
        productProfiling: busy,
        profileOperation: busy ? undefined : null,
      }
    : {
        productProfile: profile,
        productProfileSource: source,
        portraitProfile: null,
        portraitProfileSource: "",
        productProfiling: busy,
        profileOperation: busy ? undefined : null,
      };
}


async function resolveGenerationSubjectProfile({
  cfg,
  creationMode,
  subjectMode,
  isEditMode,
  productAsset,
  productProfile,
  productProfileSource,
  portraitProfile,
  portraitProfileSource,
  visionModelConfigId,
  productAssetSignatureByModeRef,
  profileResultCacheRef,
  pendingProfileReverseRequestRef,
  profileOperationsRef,
  requestQuoteConfirmation,
  trackProfileReverseOperation,
  isCurrent,
  setWorkspacePatch,
  setMsg,
  refreshMe,
  clearGenerateRequest,
}) {
  let resolvedProductProfile = subjectMode === "portrait" ? portraitProfile : productProfile;
  let resolvedProductProfileSource = subjectMode === "portrait"
    ? portraitProfileSource
    : productProfileSource;
  const directProductVideo = creationMode === "video" && subjectMode === "product";
  const needsSubjectProfile = (
    (isEditMode || directProductVideo)
    && productAsset?.url
    && (subjectMode === "product" || subjectMode === "portrait")
  );
  if (needsSubjectProfile && cfg?.model_options && !visionModelConfigId) {
    setMsg("当前没有支持主体档案识别的反推模型，请联系管理员配置后再生成。");
    return { status: "aborted" };
  }

  const productSignature = assetSignature(productAsset);
  const isProductAssetStillCurrent = () => (
    productAssetSignatureByModeRef.current[creationMode] === productSignature
  );
  if (needsSubjectProfile && (!resolvedProductProfile || resolvedProductProfileSource !== productSignature)) {
    setWorkspacePatch?.(subjectProfilePatch(subjectMode, null, "", true), creationMode);
    const requestIdentity = buildSubjectProfileRequestIdentity({
      mode: creationMode,
      subjectMode,
      assetUrl: productAsset.url,
      assetSignature: productSignature,
      modelConfigId: visionModelConfigId,
    });
    const cachedProfile = readCachedSubjectProfileResult(
      profileResultCacheRef,
      requestIdentity,
    );
    if (cachedProfile) {
      resolvedProductProfile = cachedProfile;
      resolvedProductProfileSource = productSignature;
      setWorkspacePatch?.(subjectProfilePatch(
        subjectMode,
        resolvedProductProfile,
        resolvedProductProfileSource,
      ), creationMode);
    } else {
      const profileRequestId = generateReverseClientRequestId(
        pendingProfileReverseRequestRef,
        requestIdentity.scope,
        requestIdentity.signature,
      );
      const operationContext = {
        creationMode,
        subjectMode,
        productAsset,
        productSignature,
        visionModelConfigId,
        requestIdentity,
        profileRequestId,
        pendingProfileReverseRequestRef,
        profileResultCacheRef,
        profileOperationsRef,
        requestQuoteConfirmation,
        trackProfileReverseOperation,
        isCurrent,
        isProductAssetStillCurrent,
        setWorkspacePatch,
        setMsg,
        refreshMe,
        clearGenerateRequest,
      };
      try {
        const resolved = await runTrackedSubjectProfileOperation(operationContext);
        if (resolved.status !== "ready") return resolved;
        resolvedProductProfile = resolved.profile;
        resolvedProductProfileSource = productSignature;
      } catch (e) {
        return handleSubjectProfileFailure(e, operationContext);
      }
    }
  }
  if ((isEditMode || directProductVideo) && productAsset?.url && !isProductAssetStillCurrent()) {
    setMsg("主体图片已变更，请重新点击生成。");
    clearGenerateRequest();
    return { status: "aborted" };
  }
  return {
    status: "ready",
    productProfile: resolvedProductProfile,
    productProfileSource: resolvedProductProfileSource,
  };
}


async function runTrackedSubjectProfileOperation({
  creationMode,
  subjectMode,
  productAsset,
  productSignature,
  visionModelConfigId,
  requestIdentity,
  profileRequestId,
  pendingProfileReverseRequestRef,
  profileResultCacheRef,
  profileOperationsRef,
  requestQuoteConfirmation,
  trackProfileReverseOperation,
  isCurrent,
  isProductAssetStillCurrent,
  setWorkspacePatch,
  setMsg,
  refreshMe,
  clearGenerateRequest,
}) {
  const profileTarget = subjectMode === "portrait" ? "portrait_profile" : "product_profile";
  const profileRequest = {
    asset_url: productAsset.url,
    target: profileTarget,
    source_type: "image",
    ...(visionModelConfigId ? { model_config_id: Number(visionModelConfigId) } : {}),
    client_request_id: profileRequestId,
    workspace_snapshot_v2: {
      ...buildReverseOperationRequestSnapshotV2({
        creationMode,
        subjectMode,
        target: profileTarget,
        productAsset,
      }),
      source_signature: productSignature,
      ...(visionModelConfigId ? { model_config_id: Number(visionModelConfigId) } : {}),
    },
  };
  if (typeof requestQuoteConfirmation !== "function") {
    throw new Error("主体识别服务不可用，本次未识别主体档案。");
  }
  const profileConfirmation = await requestQuoteConfirmation({
    kind: "reverse",
    request: profileRequest,
    clientRequestId: profileRequestId,
    execute: ({ request }) => api.createReverseOperation(request),
  });
  if (profileConfirmation.status !== "executed") {
    clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
    setWorkspacePatch?.(subjectProfilePatch(subjectMode, null, ""), creationMode);
    if (["quote_failed", "execution_failed"].includes(profileConfirmation.status)) {
      throw profileConfirmation.error || new Error("主体档案提交失败。");
    }
    if (profileConfirmation.status === "invalidated") {
      setMsg(profileConfirmation.reason || "主体素材或模型已变化，请重新点击生成。");
    }
    clearGenerateRequest();
    return { status: "aborted" };
  }

  const createdOperation = normalizeReverseOperation(profileConfirmation.result);
  profileOperationsRef.current[creationMode] = createdOperation;
  const settledOperation = await waitForTrackedProfileOperation(createdOperation, {
    mode: creationMode,
    trackProfileReverseOperation,
    context: {
      clientRequestId: profileRequestId,
      targetSignature: productSignature,
      sourceAsset: productAsset,
      onUpdate: (operation) => {
        profileOperationsRef.current[creationMode] = operation;
        if (isCurrent()) setWorkspacePatch?.({ profileOperation: operation }, creationMode);
      },
      onSettled: (operation) => {
        delete profileOperationsRef.current[creationMode];
        if (["succeeded", "failed", "canceled"].includes(operation.status)) {
          clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
        }
      },
    },
  });
  if (settledOperation.status !== "succeeded") {
    throw reverseOperationFailure(settledOperation);
  }
  const profile = reverseOperationResult(settledOperation);
  if (!profile) throw new Error("主体档案任务完成但未返回结果");
  if (!isCurrent()) return { status: "aborted" };
  if (!isProductAssetStillCurrent()) {
    clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
    setWorkspacePatch?.({ productProfiling: false }, creationMode);
    setMsg("主体图片已变更，请重新点击生成。");
    clearGenerateRequest();
    return { status: "aborted" };
  }

  clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
  delete profileOperationsRef.current[creationMode];
  const resolvedProductProfile = normalizeSubjectProfileResult(profile);
  cacheSubjectProfileResult(
    profileResultCacheRef,
    requestIdentity,
    profileRequestId,
    resolvedProductProfile,
  );
  setWorkspacePatch?.(subjectProfilePatch(
    subjectMode,
    resolvedProductProfile,
    productSignature,
  ), creationMode);
  refreshMe();
  return { status: "ready", profile: resolvedProductProfile };
}


function handleSubjectProfileFailure(e, {
  creationMode,
  subjectMode,
  profileRequestId,
  pendingProfileReverseRequestRef,
  profileOperationsRef,
  isCurrent,
  isProductAssetStillCurrent,
  setWorkspacePatch,
  setMsg,
  clearGenerateRequest,
}) {
  if (!isCurrent()) return { status: "aborted" };
  const keepTracking = shouldKeepPendingReverseRequest(e);
  if (!isRequestTimeoutError(e) && !keepTracking) {
    clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
  }
  if (keepTracking) {
    const activeOperation = profileOperationsRef.current[creationMode];
    if (activeOperation) {
      setWorkspacePatch?.({
        productProfiling: true,
        profileOperation: activeOperation,
      }, creationMode);
      setMsg(`${e.message}，可刷新页面继续查看进度。`);
    }
    clearGenerateRequest();
    return { status: "aborted" };
  }
  delete profileOperationsRef.current[creationMode];
  if (!isProductAssetStillCurrent()) {
    setWorkspacePatch?.({ productProfiling: false }, creationMode);
    setMsg("主体图片已变更，请重新点击生成。");
    clearGenerateRequest();
    return { status: "aborted" };
  }
  setWorkspacePatch?.(subjectProfilePatch(subjectMode, null, ""), creationMode);
  setMsg(`主体档案识别失败，请重新点击生成或更换更清晰的主体图片后再试：${e.message}`);
  clearGenerateRequest();
  return { status: "aborted" };
}


function adoptSubmittedGeneration({
  confirmation,
  payload,
  ratioOption,
  stage,
  category,
  imageCount,
  previousTask,
  trackBackgroundTask,
  setTask,
  onGenerationSubmitted,
  setTrackingLost,
  setRunningSnapshot,
  scrollToResults,
  refreshMe,
  startTracking,
}) {
  const nextTask = confirmation.result;
  if (
    previousTask
    && previousTask.id !== nextTask.id
    && previousTask.category === "image"
    && !isTerminalTaskStatus(previousTask.status)
  ) {
    trackBackgroundTask(previousTask);
  }
  setTask(nextTask);
  onGenerationSubmitted?.({ task: nextTask, payload: confirmation.request, stage });
  setTrackingLost(false);
  setRunningSnapshot({
    category,
    n: category === "image" ? Number(payload.params?.n || imageCount || 1) : 1,
    ratio: ratioOption,
  });
  scrollToResults();
  refreshMe();
  startTracking(nextTask.id);
}


export default function useGenerationSubmit({
  cfg,
  task,
  category,
  creationMode,
  isEditMode,
  isImageEditMode,
  subjectMode,
  parsing = false,
  uploading = false,
  uploadingRole = null,
  reversing = false,
  productProfiling = false,
  prompt,
  negative,
  promptDirty,
  promptSourceSignature,
  selected,
  lastFrameAsset = null,
  firstLastFrameEnabled = false,
  productAsset,
  productDetailAssets = [],
  productProfile,
  productProfileSource,
  portraitProfile = null,
  portraitProfileSource = "",
  variationSource,
  structured,
  structuredDirty = false,
  structuredSource,
  reverseVideoAnalysis = null,
  analysisFocus = "",
  ratio,
  imageQuality,
  n,
  seed,
  editMaskMode,
  productPixelLockMode,
  vDuration,
  vResolution,
  productVideoTemplate = "prompt_driven",
  modelConfigId = null,
  reverseOperationId = null,
  reverseRevisionId = null,
  reviewedImageEvidence = null,
  reverseEvidenceOperation = null,
  modelOption = null,
  visionModelConfigId = null,
  resultsRef,
  modelEnabled,
  setMsg,
  setTask,
  setRunningSnapshot,
  setTrackingLost,
  setWorkspacePatch,
  trackBackgroundTask,
  refreshMe,
  startTracking,
  onGenerationSubmitted = null,
  getOwnerSession,
  subjectProfilePendingRequestRef = null,
  subjectProfileResultCacheRef = null,
  subjectProfileOperationsRef = null,
  trackProfileReverseOperation = null,
  requestQuoteConfirmation = null,
}) {
  const [submitting, setSubmitting] = useState(false);
  const pendingGenerateRequestRef = useRef(null);
  const fallbackSubjectProfilePendingRequestRef = useRef(null);
  const fallbackSubjectProfileResultCacheRef = useRef(null);
  const fallbackSubjectProfileOperationsRef = useRef({});
  const pendingProfileReverseRequestRef = subjectProfilePendingRequestRef || fallbackSubjectProfilePendingRequestRef;
  const profileResultCacheRef = subjectProfileResultCacheRef || fallbackSubjectProfileResultCacheRef;
  const profileOperationsRef = subjectProfileOperationsRef || fallbackSubjectProfileOperationsRef;
  const productAssetSignatureByModeRef = useRef({});
  const ownerRequestRef = useRef(0);
  const getOwnerSessionRef = useRef(getOwnerSession);
  const ownerRequestContextRef = useRef(null);

  productAssetSignatureByModeRef.current[creationMode] = assetSignature(productAsset);
  getOwnerSessionRef.current = getOwnerSession;
  if (!ownerRequestContextRef.current) {
    ownerRequestContextRef.current = createStudioOwnerRequestContext(
      () => getOwnerSessionRef.current?.(),
    );
  }

  function scrollToResults() {
    if (typeof window === "undefined") return;
    const run = () => {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    window.requestAnimationFrame(() => window.requestAnimationFrame(run));
  }

  const generationRequestInput = {
    cfg,
    category,
    creationMode,
    isEditMode,
    isImageEditMode,
    subjectMode,
    prompt,
    negative,
    promptDirty,
    promptSourceSignature,
    selected,
    productAsset,
    productDetailAssets,
    variationSource,
    structured,
    structuredSource,
    reverseVideoAnalysis,
    analysisFocus,
    ratio,
    imageQuality,
    n,
    seed,
    editMaskMode,
    productPixelLockMode,
    vDuration,
    vResolution,
    productVideoTemplate,
    modelConfigId,
    reverseOperationId,
    reverseRevisionId,
  };

  async function submit(stage = "preview") {
    if (submitting) return;
    const preflight = validateGenerationSubmission({
      ...generationRequestInput,
      uploading,
      uploadingRole,
      parsing,
      reversing,
      productProfiling,
      structuredDirty,
      modelOption,
      firstLastFrameEnabled,
      lastFrameAsset,
      task,
      reviewedImageEvidence,
      reverseEvidenceOperation,
    });
    if (!preflight.ok) {
      setMsg(preflight.message);
      return;
    }
    const { productVideoStrategy, effectiveLastFrameAsset } = preflight;

    setSubmitting(true);
    setMsg("");
    const ownerRequest = ownerRequestContextRef.current.capture();
    const ownerRequestId = ++ownerRequestRef.current;
    const isCurrent = () => (
      ownerRequest.isCurrent()
      && ownerRequestRef.current === ownerRequestId
    );
    let requestId = null;
    try {
      if (!modelEnabled(category)) {
        setMsg(`${category === "video" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
        return;
      }
      const subjectProfile = await resolveGenerationSubjectProfile({
        cfg,
        creationMode,
        subjectMode,
        isEditMode,
        productAsset,
        productProfile,
        productProfileSource,
        portraitProfile,
        portraitProfileSource,
        visionModelConfigId,
        productAssetSignatureByModeRef,
        profileResultCacheRef,
        pendingProfileReverseRequestRef,
        profileOperationsRef,
        requestQuoteConfirmation,
        trackProfileReverseOperation,
        isCurrent,
        setWorkspacePatch,
        setMsg,
        refreshMe,
        clearGenerateRequest: () => clearPendingGenerateRequest(
          pendingGenerateRequestRef,
          requestId,
        ),
      });
      if (subjectProfile.status !== "ready") return;
      const { payload, ratioOption } = prepareGenerationSubmission({
        stage,
        input: generationRequestInput,
        subjectProfile,
        productVideoStrategy,
        effectiveLastFrameAsset,
        pendingGenerateRequestRef,
      });
      requestId = payload.client_request_id;

      const confirmation = await executeGenerationSubmission(payload, requestQuoteConfirmation);
      if (confirmation.status !== "executed") {
        clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
        if (confirmation.status === "invalidated") {
          setMsg(confirmation.reason || "参数已变化，请重新点击生成。");
        }
        return;
      }
      if (!isCurrent()) return;
      clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
      adoptSubmittedGeneration({
        confirmation,
        payload,
        ratioOption,
        stage,
        category,
        imageCount: n,
        previousTask: task,
        trackBackgroundTask,
        setTask,
        onGenerationSubmitted,
        setTrackingLost,
        setRunningSnapshot,
        scrollToResults,
        refreshMe,
        startTracking,
      });
    } catch (e) {
      if (!isCurrent()) return;
      if (isRequestTimeoutError(e)) {
        setMsg(`${e.message}。任务可能仍在处理中，重新点击会复用同一次请求，避免重复扣费。`);
      } else {
        clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
        setMsg(classifyGenerationError(e, { category }).message);
      }
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  }

  function resetOwnerGenerationSubmit() {
    ownerRequestContextRef.current.invalidate();
    ownerRequestRef.current += 1;
    pendingGenerateRequestRef.current = null;
    pendingProfileReverseRequestRef.current = null;
    profileResultCacheRef.current = null;
    productAssetSignatureByModeRef.current = {};
    setSubmitting(false);
  }

  return {
    submitting,
    submit,
    resetOwnerGenerationSubmit,
  };
}
