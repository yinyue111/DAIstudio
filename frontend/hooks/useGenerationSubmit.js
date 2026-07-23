"use client";

import { useRef, useState } from "react";
import { api } from "../lib/api";
import {
  normalizeReverseOperation,
  reverseOperationFailure,
  reverseOperationResult,
} from "../lib/reverseOperations";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import { buildGenerationPayload } from "../app/studio/generationPayload";
import { resolveProductVideoStrategySelection } from "../app/studio/productVideoStrategy";
import { validateImageEvidenceMaskPreflight } from "../app/studio/imageEvidenceReview";
import { buildReverseOperationRequestSnapshotV2 } from "../app/studio/reverseSnapshot";
import { classifyGenerationError, reportBackgroundError } from "../lib/errorHandling";
import {
  clearPendingGenerateRequest,
  clearPendingReverseRequest,
  generateClientRequestId,
  generateReverseClientRequestId,
  shouldKeepPendingReverseRequest,
} from "../app/studio/generationRequestId";
import { shouldBlockNewGeneration } from "../app/studio/taskConcurrency";
import { validateMultiReferenceSelection } from "../app/studio/StudioModelSelector";
import { assetReferenceUrl } from "../lib/unifiedAssets";
import { assetSignature, isRequestTimeoutError, isTerminalTaskStatus } from "../app/studio/helpers";
import {
  buildSubjectProfileRequestIdentity,
  cacheSubjectProfileResult,
  normalizeSubjectProfileResult,
  readCachedSubjectProfileResult,
} from "../lib/studioSubjectProfile";

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

  async function submit(stage = "preview") {
    if (submitting) return;
    if (uploading) {
      setMsg("素材仍在上传中，请等待上传完成后再生成。");
      return;
    }
    if (parsing || reversing) {
      setMsg("参考素材仍在抓取或反推中，请等待提示词完成后再生成。");
      return;
    }
    if (productProfiling) {
      setMsg("主体信息仍在识别中，请稍后再生成。");
      return;
    }
    if (structuredDirty) {
      setMsg("结构化维度与手工提示词存在冲突，请先应用结构修改或撤销结构修改。");
      return;
    }
    const productVideoStrategy = resolveProductVideoStrategySelection(
      modelOption?.capabilities,
      productVideoTemplate,
    );
    const modelSupportsFirstLastFrame = Boolean(
      category === "video"
      && creationMode === "video"
      && subjectMode === "general"
      && modelOption?.capabilities
      && !Array.isArray(modelOption.capabilities)
      && modelOption.capabilities.first_last_frame === true,
    );
    const effectiveLastFrameAsset = firstLastFrameEnabled && modelSupportsFirstLastFrame
      ? lastFrameAsset
      : null;
    if (category === "video" && subjectMode === "product" && !productVideoStrategy.supported) {
      setMsg("当前视频模型未提供可用的产品视频策略，请切换模型后再生成。");
      return;
    }
    if (effectiveLastFrameAsset) {
      const firstFrameUrl = assetReferenceUrl(selected);
      const lastFrameUrl = assetReferenceUrl(effectiveLastFrameAsset);
      if (selected?.type !== "image") {
        setMsg("首尾帧模式需要先选择图片作为视频首帧。");
        return;
      }
      if (effectiveLastFrameAsset.type !== "image" || !lastFrameUrl) {
        setMsg("视频尾帧只支持可用的图片素材，请重新上传或选择。");
        return;
      }
      if (firstFrameUrl === lastFrameUrl) {
        setMsg("视频首帧和尾帧不能使用同一张图片。");
        return;
      }
    }
    if (productDetailAssets.length) {
      const urls = [productAsset, ...productDetailAssets, selected]
        .map(assetReferenceUrl)
        .filter(Boolean);
      const validation = validateMultiReferenceSelection(
        modelOption,
        new Set(urls).size,
        productDetailAssets.length,
      );
      if (!validation.ok) {
        setMsg(validation.message);
        return;
      }
    }
    if (shouldBlockNewGeneration(task, category)) {
      setMsg(category === "image" ? "" : "当前视频任务仍在生成中，请等待完成后再发起新的视频生成。");
      return;
    }
    if (isEditMode && !productAsset) {
      setMsg(
        subjectMode === "portrait"
          ? "请先上传人物照片"
          : subjectMode === "product"
            ? "请先上传产品主体图片"
            : isImageEditMode
              ? "请先上传要编辑的图片"
              : "请先上传主体图片",
      );
      return;
    }
    if (isEditMode && !String(prompt || "").trim() && Object.keys(structured || {}).length === 0) {
      setMsg(isImageEditMode ? "请输入图片编辑要求" : "请先反推风格参考或输入希望迁移的风格提示词");
      return;
    }
    if (!isEditMode && !String(prompt || "").trim() && !selected) {
      setMsg("请输入提示词，或从参考反推");
      return;
    }
    if (
      category === "image"
      && Number(reverseOperationId) > 0
      && Number(reverseRevisionId) > 0
    ) {
      const maskPreflight = validateImageEvidenceMaskPreflight(
        reviewedImageEvidence,
        reverseEvidenceOperation,
        assetReferenceUrl(isEditMode ? productAsset : selected),
      );
      if (!maskPreflight.ok) {
        setMsg(maskPreflight.message);
        return;
      }
    }

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
      const effCategory = category;
      if (!modelEnabled(effCategory)) {
        setMsg(`${effCategory === "video" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
        return;
      }
      let resolvedProductProfile = subjectMode === "portrait" ? portraitProfile : productProfile;
      let resolvedProductProfileSource = subjectMode === "portrait" ? portraitProfileSource : productProfileSource;
      const subjectProfilePatch = (profile, source, busy = false) => (
        subjectMode === "portrait"
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
            }
      );
      const directProductVideo = creationMode === "video" && subjectMode === "product";
      const needsSubjectProfile = (
        (isEditMode || directProductVideo)
        && productAsset?.url
        && (subjectMode === "product" || subjectMode === "portrait")
      );
      if (needsSubjectProfile && cfg?.model_options && !visionModelConfigId) {
        setMsg("当前没有支持主体档案识别的反推模型，请联系管理员配置后再生成。");
        return;
      }
      const productSignature = assetSignature(productAsset);
      const isProductAssetStillCurrent = () => (
        productAssetSignatureByModeRef.current[creationMode] === productSignature
      );
      if (needsSubjectProfile && (!resolvedProductProfile || resolvedProductProfileSource !== productSignature)) {
        setWorkspacePatch?.(subjectProfilePatch(null, "", true), creationMode);
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
            resolvedProductProfile,
            resolvedProductProfileSource,
          ), creationMode);
        } else {
          const profileRequestId = generateReverseClientRequestId(
            pendingProfileReverseRequestRef,
            requestIdentity.scope,
            requestIdentity.signature,
          );
          try {
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
              setWorkspacePatch?.(subjectProfilePatch(null, ""), creationMode);
              if (["quote_failed", "execution_failed"].includes(profileConfirmation.status)) {
                throw profileConfirmation.error || new Error("主体档案提交失败。");
              }
              if (profileConfirmation.status === "invalidated") {
                setMsg(profileConfirmation.reason || "主体素材或模型已变化，请重新点击生成。");
              }
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
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
            if (!isCurrent()) return;
            if (!isProductAssetStillCurrent()) {
              clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
              setWorkspacePatch?.({ productProfiling: false }, creationMode);
              setMsg("主体图片已变更，请重新点击生成。");
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
            }
            clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
            delete profileOperationsRef.current[creationMode];
            resolvedProductProfile = normalizeSubjectProfileResult(profile);
            cacheSubjectProfileResult(
              profileResultCacheRef,
              requestIdentity,
              profileRequestId,
              resolvedProductProfile,
            );
            resolvedProductProfileSource = productSignature;
            setWorkspacePatch?.(subjectProfilePatch(
              resolvedProductProfile,
              resolvedProductProfileSource,
            ), creationMode);
            refreshMe();
          } catch (e) {
            if (!isCurrent()) return;
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
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
            }
            delete profileOperationsRef.current[creationMode];
            if (!isProductAssetStillCurrent()) {
              setWorkspacePatch?.({ productProfiling: false }, creationMode);
              setMsg("主体图片已变更，请重新点击生成。");
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
            }
            resolvedProductProfile = null;
            resolvedProductProfileSource = "";
            setWorkspacePatch?.(subjectProfilePatch(null, ""), creationMode);
            setMsg(
              shouldKeepPendingReverseRequest(e)
                ? `${e.message}，请稍后再次点击生成。`
                : `主体档案识别失败，请重新点击生成或更换更清晰的主体图片后再试：${e.message}`,
            );
            clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
            return;
          }
        }
      }
      if ((isEditMode || directProductVideo) && productAsset?.url && !isProductAssetStillCurrent()) {
        setMsg("主体图片已变更，请重新点击生成。");
        clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
        return;
      }
      const { payload, ratioOption } = buildGenerationPayload({
        stage,
        task: null,
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
        lastFrameAsset: effectiveLastFrameAsset,
        firstLastFrameEnabled: Boolean(effectiveLastFrameAsset),
        productAsset,
        productDetailAssets,
        productProfile: resolvedProductProfile,
        productProfileSource: resolvedProductProfileSource,
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
        productVideoTemplate: productVideoStrategy.effectiveValue || productVideoTemplate,
        modelConfigId,
        reverseOperationId,
        reverseRevisionId,
      });
      payload.client_request_id = generateClientRequestId(
        pendingGenerateRequestRef,
        stage,
        JSON.stringify(payload),
      );
      requestId = payload.client_request_id;

      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("生成服务不可用，本次未执行生成。");
      }
      const confirmation = await requestQuoteConfirmation({
        kind: "generation",
        request: payload,
        clientRequestId: payload.client_request_id,
        execute: ({ request }) => api.generate(request),
      });
      if (confirmation.status !== "executed") {
        clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("任务提交失败。");
        }
        if (confirmation.status === "invalidated") {
          setMsg(confirmation.reason || "参数已变化，请重新点击生成。");
        }
        return;
      }
      const submitted = {
        task: confirmation.result,
        quote: confirmation.quote?.raw,
        payload: confirmation.request,
      };
      const nextTask = submitted.task;
      if (!isCurrent()) return;
      clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
      const previousTask = task;
      if (
        previousTask
        && previousTask.id !== nextTask.id
        && previousTask.category === "image"
        && !isTerminalTaskStatus(previousTask.status)
      ) {
        trackBackgroundTask(previousTask);
      }
      setTask(nextTask);
      onGenerationSubmitted?.({ task: nextTask, payload: submitted.payload, stage });
      setTrackingLost(false);
      setRunningSnapshot({
        category: effCategory,
        n: effCategory === "image" ? Number(payload.params?.n || n || 1) : 1,
        ratio: ratioOption,
      });
      scrollToResults();
      refreshMe();
      startTracking(nextTask.id);
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
