"use client";

import { useRef, useState } from "react";
import { api } from "../lib/api";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import { buildGenerationPayload } from "../app/studio/generationPayload";
import { classifyGenerationError } from "../lib/errorHandling";
import {
  clearPendingGenerateRequest,
  clearPendingReverseRequest,
  generateClientRequestId,
  generateReverseClientRequestId,
} from "../app/studio/generationRequestId";
import { shouldBlockNewGeneration } from "../app/studio/taskConcurrency";
import { assetSignature, isRequestTimeoutError, isTerminalTaskStatus } from "../app/studio/helpers";
import {
  buildSubjectProfileRequestIdentity,
  cacheSubjectProfileResult,
  normalizeSubjectProfileResult,
  readCachedSubjectProfileResult,
} from "../lib/studioSubjectProfile";

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
  productAsset,
  productProfile,
  productProfileSource,
  variationSource,
  structured,
  structuredSource,
  ratio,
  imageQuality,
  n,
  seed,
  editMaskMode,
  productPixelLockMode,
  vDuration,
  vResolution,
  videoProductLockMode,
  videoProductTemplate,
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
  getOwnerSession,
  subjectProfilePendingRequestRef = null,
  subjectProfileResultCacheRef = null,
}) {
  const [submitting, setSubmitting] = useState(false);
  const pendingGenerateRequestRef = useRef(null);
  const fallbackSubjectProfilePendingRequestRef = useRef(null);
  const fallbackSubjectProfileResultCacheRef = useRef(null);
  const pendingProfileReverseRequestRef = subjectProfilePendingRequestRef || fallbackSubjectProfilePendingRequestRef;
  const profileResultCacheRef = subjectProfileResultCacheRef || fallbackSubjectProfileResultCacheRef;
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
    if (shouldBlockNewGeneration(task, category)) {
      setMsg(category === "image" ? "" : "当前视频任务仍在生成中，请等待完成后再发起新的视频生成。");
      return;
    }
    if (isEditMode && !productAsset) {
      setMsg(isImageEditMode ? "请先上传要编辑的图片" : "请先上传产品主体图片");
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
      let resolvedProductProfile = productProfile;
      let resolvedProductProfileSource = productProfileSource;
      const needsSubjectProfile = (
        isEditMode
        && productAsset?.url
        && (subjectMode === "product" || subjectMode === "portrait")
      );
      const productSignature = assetSignature(productAsset);
      const isProductAssetStillCurrent = () => (
        productAssetSignatureByModeRef.current[creationMode] === productSignature
      );
      if (needsSubjectProfile && (!resolvedProductProfile || resolvedProductProfileSource !== productSignature)) {
        setWorkspacePatch?.({ productProfiling: true, productProfile: null, productProfileSource: "" }, creationMode);
        const requestIdentity = buildSubjectProfileRequestIdentity({
          mode: creationMode,
          subjectMode,
          assetUrl: productAsset.url,
          assetSignature: productSignature,
        });
        const cachedProfile = readCachedSubjectProfileResult(
          profileResultCacheRef,
          requestIdentity,
        );
        if (cachedProfile) {
          resolvedProductProfile = cachedProfile;
          resolvedProductProfileSource = productSignature;
          setWorkspacePatch?.({
            productProfile: resolvedProductProfile,
            productProfileSource: resolvedProductProfileSource,
            productProfiling: false,
          }, creationMode);
        } else {
          const profileRequestId = generateReverseClientRequestId(
            pendingProfileReverseRequestRef,
            requestIdentity.scope,
            requestIdentity.signature,
          );
          try {
            const profile = await api.reverse(
              productAsset.url,
              "product_profile",
              null,
              "image",
              null,
              profileRequestId,
            );
            if (!isCurrent()) return;
            if (!isProductAssetStillCurrent()) {
              clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
              setWorkspacePatch?.({ productProfiling: false }, creationMode);
              setMsg("主体图片已变更，请重新点击生成。");
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
            }
            clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
            resolvedProductProfile = normalizeSubjectProfileResult(profile);
            cacheSubjectProfileResult(
              profileResultCacheRef,
              requestIdentity,
              profileRequestId,
              resolvedProductProfile,
            );
            resolvedProductProfileSource = productSignature;
            setWorkspacePatch?.({
              productProfile: resolvedProductProfile,
              productProfileSource: resolvedProductProfileSource,
              productProfiling: false,
            }, creationMode);
            refreshMe();
          } catch (e) {
            if (!isCurrent()) return;
            if (!isRequestTimeoutError(e)) {
              clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
            }
            if (!isProductAssetStillCurrent()) {
              setWorkspacePatch?.({ productProfiling: false }, creationMode);
              setMsg("主体图片已变更，请重新点击生成。");
              clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
              return;
            }
            resolvedProductProfile = null;
            resolvedProductProfileSource = "";
            setWorkspacePatch?.({
              productProfile: null,
              productProfileSource: "",
              productProfiling: false,
            }, creationMode);
            setMsg(`主体档案识别失败，请重新点击生成或更换更清晰的主体图片后再试：${e.message}`);
            clearPendingGenerateRequest(pendingGenerateRequestRef, requestId);
            return;
          }
        }
      }
      if (isEditMode && productAsset?.url && !isProductAssetStillCurrent()) {
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
        productAsset,
        productProfile: resolvedProductProfile,
        productProfileSource: resolvedProductProfileSource,
        variationSource,
        structured,
        structuredSource,
        ratio,
        imageQuality,
        n,
        seed,
        editMaskMode,
        productPixelLockMode,
        vDuration,
        vResolution,
        videoProductLockMode,
        videoProductTemplate,
      });
      payload.client_request_id = generateClientRequestId(
        pendingGenerateRequestRef,
        stage,
        JSON.stringify(payload),
      );
      requestId = payload.client_request_id;

      const nextTask = await api.generate(payload);
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
        setMsg(`${e.message}。任务可能已提交，重新点击会复用同一次请求，避免重复扣费。`);
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
