"use client";

import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseOperation,
  reverseOperationRequestSignature,
  reverseOperationResult,
} from "../lib/reverseOperations";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import {
  PARSE_POLL_INTERVAL_MS,
  PARSE_POLL_TIMEOUT_MS,
  RATIOS,
} from "../app/studio/constants";
import {
  assetDims,
  assetSignature,
  composeEvidenceBackedVideoGenerationDraft,
  composeEvidenceBackedVideoTransferPrompt,
  composePromptFromStructured,
  composeStyleTransferPrompt,
  isRequestTimeoutError,
  nearestRatio,
  visualStructuredFields,
  videoRatioOptions,
} from "../app/studio/helpers";
import {
  clearPendingReverseRequest,
  generateReverseClientRequestId,
  shouldKeepPendingReverseRequest,
} from "../app/studio/generationRequestId";
import {
  buildReverseOperationRequestSnapshotV3,
  workspacePatchFromReverseSnapshot,
} from "../app/studio/reverseSnapshot";
import { clearReverseLineage } from "../app/studio/workspaceReset";
import {
  normalizeReverseConfig,
  reverseConfigForSourceChange,
  validateReverseConfig,
} from "../app/studio/reverseConfig";
import useReverseOperationTracking from "./useReverseOperationTracking";

function bumpRequest(ref, mode) {
  const next = Number(ref.current[mode] || 0) + 1;
  ref.current[mode] = next;
  return next;
}

function isRequestCurrent(ref, mode, id) {
  return Number(ref.current[mode] || 0) === id;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function assetIdField(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? { asset_id: parsed } : {};
}

function reverseWorkspaceSnapshot(operation) {
  return operation?.workspace_snapshot_v3 || operation?.workspace_snapshot_v2 || {};
}

export async function cancelStaleReverseOperation(
  operation,
  cancelOperation = (operationId) => api.cancelReverseOperation(operationId),
  reportError = reportBackgroundError,
) {
  if (!operation?.id) return false;
  try {
    await cancelOperation(operation.id);
    return true;
  } catch (error) {
    reportError(error, "cancel stale reverse operation after confirmed execution");
    return false;
  }
}

function recoveredSourceAsset(operation, restoredWorkspace, target) {
  const profileTarget = ["product_profile", "portrait_profile"].includes(target);
  const restored = profileTarget ? restoredWorkspace?.productAsset : restoredWorkspace?.selected;
  if (restored) return restored;
  const snapshot = reverseWorkspaceSnapshot(operation);
  const requestContext = operation?.request_context || {};
  const signature = String(snapshot.source_signature || requestContext.source_signature || "");
  const [signatureType, signatureUrl, ...signatureThumb] = signature.split("|");
  const url = signatureUrl || String(requestContext.asset_url || "");
  if (!url) return null;
  const thumb = signatureThumb.join("|") || String(requestContext.fallback_image || "");
  return {
    type: ["image", "video"].includes(signatureType) ? signatureType : (operation?.source_type || "image"),
    url,
    ...(thumb ? { thumb } : {}),
  };
}

export default function useReferenceParsing({
  creationMode,
  category,
  url,
  parsing,
  selected,
  assets = [],
  productAsset = null,
  productProfile = null,
  structured: workspaceStructured = {},
  reverseVideoAnalysis: workspaceVideoAnalysis = null,
  workspaceReverseOperation = null,
  prompt,
  negative,
  negativeTouched,
  videoAnalysisPreset,
  reverseConfig = null,
  reverseSources = [],
  modelConfigId = null,
  modelSelections = null,
  ratio,
  vDuration,
  videoDurationMaxSeconds,
  isEditMode = false,
  subjectMode = "",
  setMsg,
  setWorkspacePatch,
  setCreationMode,
  setRatio,
  setStructOpen,
  setShowNegative,
  setRefOpen,
  refreshMe,
  revokeUploadedObjectUrlsRef,
  getOwnerSession,
  ownerKey = "",
  resumeOperations = [],
  requestQuoteConfirmation = null,
}) {
  const selectedByModeRef = useRef({});
  const profileAssetByModeRef = useRef({});
  const refVersionRef = useRef({});
  const parseRequestRef = useRef({});
  const reverseRequestRef = useRef({});
  const pendingReverseRequestRef = useRef({});
  const lastReversePromptRef = useRef({});
  const reverseContextsRef = useRef({});
  const creationModeRef = useRef("image");
  const urlByModeRef = useRef({});
  const getOwnerSessionRef = useRef(getOwnerSession);
  const ownerRequestContextRef = useRef(null);

  urlByModeRef.current[creationMode] = String(url || "").trim();
  getOwnerSessionRef.current = getOwnerSession;
  if (!ownerRequestContextRef.current) {
    ownerRequestContextRef.current = createStudioOwnerRequestContext(
      () => getOwnerSessionRef.current?.(),
    );
  }
  for (const candidate of resumeOperations) {
    if (candidate?.mode && candidate.sourceAsset) {
      const targetRef = String(candidate.trackingKey || "").endsWith("::profile")
        ? profileAssetByModeRef
        : selectedByModeRef;
      if (!Object.prototype.hasOwnProperty.call(targetRef.current, candidate.mode)) {
        targetRef.current[candidate.mode] = candidate.sourceAsset;
      }
    }
  }

  function clearReverseRuntimeContext(mode, context = null) {
    if (context?.clientRequestId) {
      clearPendingReverseRequest(pendingReverseRequestRef, context.clientRequestId);
    }
    if (!context || reverseContextsRef.current[mode] === context) {
      delete reverseContextsRef.current[mode];
    }
  }

  function bindRecoveredOperationWorkspace(operation, mode, runtimeContext = null) {
    if (!runtimeContext?.recovered) return;
    const snapshot = reverseWorkspaceSnapshot(operation);
    const restored = workspacePatchFromReverseSnapshot(snapshot);
    const restoredWorkspace = restored?.workspace || {};
    const operationTarget = operation.target || snapshot.target || "image";
    const profileTarget = ["product_profile", "portrait_profile"].includes(operationTarget);
    const sourceAsset = recoveredSourceAsset(operation, restoredWorkspace, operationTarget);
    const sourceSignature = (
      runtimeContext.targetSignature
      || operation.request_context?.source_signature
      || snapshot.source_signature
      || assetSignature(sourceAsset)
    );
    runtimeContext.targetSignature = sourceSignature;
    runtimeContext.targetCategory ||= operationTarget === "video" ? "video" : "image";
    runtimeContext.targetVideoPreset ||= operation.request_context?.video_analysis_preset || "standard";
    runtimeContext.startedPrompt ??= snapshot.final_text ?? restoredWorkspace.prompt ?? "";
    runtimeContext.subjectMode ||= snapshot.subject_mode || restored?.subjectMode || "";
    runtimeContext.isEditMode ??= ["image_edit", "video_edit"].includes(mode);
    reverseContextsRef.current[mode] = runtimeContext;
    if (sourceAsset) {
      (profileTarget ? profileAssetByModeRef : selectedByModeRef).current[mode] = sourceAsset;
    }

    setWorkspacePatch((current) => {
      const operationField = profileTarget ? "profileOperation" : "reverseOperation";
      const existingOperation = current[operationField];
      if (
        existingOperation?.id
        && String(existingOperation.id) !== String(operation.id)
        && ["queued", "running", "needs_confirmation"].includes(existingOperation.status)
      ) return {};
      const currentSource = profileTarget ? current.productAsset : current.selected;
      if (
        currentSource
        && sourceSignature
        && assetSignature(currentSource) !== sourceSignature
      ) return {};
      if (profileTarget) {
        return {
          productAsset: current.productAsset || sourceAsset,
          editSubjectMode: restored?.subjectMode || current.editSubjectMode,
          imageEditProductMode: ["product", "portrait"].includes(restored?.subjectMode),
          profileOperation: operation,
          productProfiling: ["queued", "running"].includes(operation.status),
        };
      }
      const shouldRestoreSource = !current.selected && Boolean(sourceAsset);
      return {
        ...(shouldRestoreSource ? {
          ...restoredWorkspace,
          selected: sourceAsset,
          editSubjectMode: restored?.subjectMode || current.editSubjectMode,
          imageEditProductMode: ["product", "portrait"].includes(restored?.subjectMode),
        } : {}),
        reverseOperation: operation,
        reversing: ["queued", "running"].includes(operation.status),
      };
    }, mode);
  }

  function applyReverseOperationResult(operation, mode, runtimeContext = null) {
    const result = reverseOperationResult(operation);
    if (!result) {
      setWorkspacePatch((current) => {
        if (
          current.reverseOperation?.id
          && String(current.reverseOperation.id) !== String(operation.id)
        ) return {};
        return {
          reversing: false,
          reverseOperation: operation,
          pendingReverseResult: null,
          reverseResultRevisions: [],
          reverseAppliedVersion: null,
          reverseAppliedRevisionId: null,
          reverseFeedback: null,
          reverseResultSchemaVersion: "",
          reverseUndoSnapshot: null,
          reverseApplyConflict: null,
        };
      }, mode);
      if (isModeVisible(mode)) setMsg("反推任务完成但未返回可用结果");
      clearReverseRuntimeContext(mode, runtimeContext || reverseContextsRef.current[mode] || null);
      return;
    }
    const snapshot = reverseWorkspaceSnapshot(operation);
    const context = runtimeContext || reverseContextsRef.current[mode] || {};
    const operationTarget = operation.target || snapshot.target || "image";
    const targetSignature = (
      context.targetSignature
      || operation.request_context?.source_signature
      || snapshot.source_signature
      || assetSignature(snapshot.selected)
      || ""
    );
    if (["product_profile", "portrait_profile"].includes(operationTarget)) {
      const resolvedProfile = {
        structured: result.structured && typeof result.structured === "object" ? result.structured : {},
        final_text: String(result.final_text || ""),
      };
      setWorkspacePatch((current) => {
        const currentSource = assetSignature(current.productAsset);
        if (!currentSource || (targetSignature && currentSource !== targetSignature)) {
          return { productProfiling: false, profileOperation: null };
        }
        return operationTarget === "portrait_profile" ? {
          portraitProfile: resolvedProfile,
          portraitProfileSource: targetSignature || currentSource,
          productProfile: null,
          productProfileSource: "",
          productProfiling: false,
          profileOperation: null,
        } : {
          productProfile: resolvedProfile,
          productProfileSource: targetSignature || currentSource,
          portraitProfile: null,
          portraitProfileSource: "",
          productProfiling: false,
          profileOperation: null,
        };
      }, mode);
      if (isModeVisible(mode)) setMsg(operationTarget === "portrait_profile" ? "人物身份档案识别完成。" : "产品身份档案识别完成。");
      refreshMe();
      return;
    }
    if (
      targetSignature
      && assetSignature(selectedByModeRef.current[mode]) !== targetSignature
    ) {
      setWorkspacePatch((current) => (
        current.reverseOperation?.id
        && String(current.reverseOperation.id) !== String(operation.id)
          ? {}
          : { reversing: false, reverseOperation: null }
      ), mode);
      clearReverseRuntimeContext(mode, context);
      return;
    }
    const targetCategory = context.targetCategory || (operationTarget === "video" ? "video" : "image");
    const targetSubjectMode = context.subjectMode || snapshot.subject_mode || "";
    const targetIsEditMode = context.isEditMode ?? ["image_edit", "video_edit"].includes(mode);
    const target = selectedByModeRef.current[mode];
    const isVideo = operationTarget === "video" || target?.type === "video" || targetCategory === "video";
    const structured = result.structured && typeof result.structured === "object" ? result.structured : {};
    const videoAnalysis = isVideo && result.video_analysis && typeof result.video_analysis === "object"
      ? result.video_analysis
      : null;
    const visualStructured = visualStructuredFields(
      structured,
      isVideo ? "video" : (operationTarget || targetCategory),
    );
    const validatedFinalText = String(result.final_text || "").trim();
    const evidenceTransferPrompt = composeEvidenceBackedVideoTransferPrompt(
      structured,
      videoAnalysis,
      targetSubjectMode,
    );
    const evidenceGenerationDraft = isVideo
      ? composeEvidenceBackedVideoGenerationDraft(
          structured,
          videoAnalysis,
          result.provider_final_text,
        )
      : "";
    const reversePrompt = targetIsEditMode
      ? (
          isVideo
            ? (
                ["product", "portrait"].includes(targetSubjectMode)
                  ? evidenceTransferPrompt
                  : composePromptFromStructured(visualStructured, "", { target: "video" })
              )
            : composeStyleTransferPrompt(structured, "", {
                video: false,
                subject: targetSubjectMode,
              })
        )
      : evidenceGenerationDraft || validatedFinalText || composePromptFromStructured(visualStructured, "", {
          target: operationTarget || targetCategory,
        });
    lastReversePromptRef.current[mode] = {
      prompt: reversePrompt,
      structured,
      sourceSignature: targetSignature,
      category: targetCategory,
      operationId: operation.id,
    };
    const source = videoAnalysis?.source && typeof videoAnalysis.source === "object"
      ? videoAnalysis.source
      : {};
    const pendingResult = {
      kind: "pending_reverse_review",
      operation_id: operation.id,
      category: isVideo ? "video" : "image",
      target: operationTarget,
      source_signature: targetSignature,
      received_at: new Date().toISOString(),
      dirty: false,
      result: {
        ...result,
        structured,
        final_text: reversePrompt,
        video_analysis: videoAnalysis,
        shots: Array.isArray(result.shots) ? result.shots : [],
        generation_parameters: {
          ...(result.generation_parameters && typeof result.generation_parameters === "object"
            ? result.generation_parameters
            : {}),
          ...(source.ratio ? { ratio: source.ratio } : {}),
          ...(Number.isFinite(Number(source.duration_seconds))
            ? { duration: Math.max(1, Math.round(Number(source.duration_seconds))) }
            : {}),
        },
      },
    };
    setWorkspacePatch({
      reversing: false,
      reverseOperation: operation,
      pendingReverseResult: pendingResult,
      reverseResultTab: isVideo ? "report" : "draft",
    }, mode);
    if (isModeVisible(mode)) {
      const cost = Number(operation.cost_settled ?? result.charged_credits ?? 0);
      const suffix = Number(result.reference_count || 0) > 1 ? `（${result.reference_count} 帧）` : "";
      setMsg(`反推完成，已结算 ${cost} 积分${suffix}。请预览后选择应用方式。`);
    }
    clearReverseRuntimeContext(mode, context);
    refreshMe();
  }

  function handleReverseOperationUpdate(operation, mode, runtimeContext = null) {
    bindRecoveredOperationWorkspace(operation, mode, runtimeContext);
    const operationTarget = operation.target || reverseWorkspaceSnapshot(operation).target || "";
    if (["product_profile", "portrait_profile"].includes(operationTarget)) {
      runtimeContext?.onUpdate?.(operation);
      setWorkspacePatch({
        productProfiling: ["queued", "running"].includes(operation.status),
        profileOperation: operation,
      }, mode);
      return;
    }
    setWorkspacePatch({
      reverseOperation: operation,
      reversing: operation.status === "queued" || operation.status === "running",
    }, mode);
  }

  function handleReverseOperationSettled(operation, mode, runtimeContext) {
    const context = runtimeContext || reverseContextsRef.current[mode] || null;
    const operationTarget = operation.target || reverseWorkspaceSnapshot(operation).target || "";
    const isProfileOperation = ["product_profile", "portrait_profile"].includes(operationTarget);
    if (isProfileOperation) runtimeContext?.onSettled?.(operation);
    const sourceRef = isProfileOperation ? profileAssetByModeRef : selectedByModeRef;
    if (
      context?.targetSignature
      && assetSignature(sourceRef.current[mode]) !== context.targetSignature
    ) {
      setWorkspacePatch((current) => {
        const operationField = isProfileOperation ? "profileOperation" : "reverseOperation";
        if (
          current[operationField]?.id
          && String(current[operationField].id) !== String(operation.id)
        ) return {};
        return isProfileOperation
          ? { productProfiling: false, profileOperation: null }
          : { reversing: false, reverseOperation: null };
      }, mode);
      clearReverseRuntimeContext(mode, context);
      return;
    }
    if (operation.status === "succeeded") {
      applyReverseOperationResult(operation, mode, context);
      return;
    }
    if (isProfileOperation) {
      setWorkspacePatch({ productProfiling: false, profileOperation: null }, mode);
    } else {
      setWorkspacePatch((current) => {
        if (
          current.reverseOperation?.id
          && String(current.reverseOperation.id) !== String(operation.id)
        ) return {};
        return {
          reverseOperation: operation,
          reversing: false,
          pendingReverseResult: null,
          reverseResultRevisions: [],
          reverseAppliedVersion: null,
          reverseAppliedRevisionId: null,
          reverseFeedback: null,
          reverseResultSchemaVersion: "",
          reverseUndoSnapshot: null,
          reverseApplyConflict: null,
        };
      }, mode);
    }
    if (operation.status === "needs_confirmation") {
      if (isModeVisible(mode)) setMsg("视频抽帧失败，需要确认是否改用封面单帧分析。");
      return;
    }
    clearReverseRuntimeContext(mode, context);
    if (isModeVisible(mode) && !isProfileOperation) {
      setMsg(operation.status === "canceled" ? "反推任务已取消，冻结积分已退回。" : (operation.error || "反推失败，冻结积分已退回。"));
    }
    refreshMe();
  }

  const {
    operationsByMode,
    startTracking: startReverseTracking,
    cancelOperation: cancelTrackedReverseOperation,
    confirmCover: confirmTrackedReverseCover,
    stopTracker: stopReverseTracker,
    resetOwner: resetOwnerReverseTracking,
  } = useReverseOperationTracking({
    ownerKey,
    resumeOperations,
    onOperationUpdate: handleReverseOperationUpdate,
    onOperationSettled: handleReverseOperationSettled,
  });

  useEffect(() => {
    creationModeRef.current = creationMode;
    selectedByModeRef.current[creationMode] = selected;
  }, [creationMode, selected]);

  useEffect(() => {
    profileAssetByModeRef.current[creationMode] = productAsset;
  }, [creationMode, productAsset]);

  function bumpRefVersion(mode = creationMode) {
    const next = Number(refVersionRef.current[mode] || 0) + 1;
    refVersionRef.current[mode] = next;
    return next;
  }

  function isRefVersionCurrent(mode, version) {
    return Number(refVersionRef.current[mode] || 0) === version;
  }

  function isParseStillCurrent(mode, refVersion, targetUrl) {
    return isRefVersionCurrent(mode, refVersion)
      && String(urlByModeRef.current[mode] || "") === String(targetUrl || "").trim();
  }

  function isModeVisible(mode) {
    return creationModeRef.current === mode;
  }

  function bumpParseRequest(mode = creationMode) {
    return bumpRequest(parseRequestRef, mode);
  }

  function bumpReverseRequest(mode = creationMode) {
    return bumpRequest(reverseRequestRef, mode);
  }

  function clearSelectedForMode(mode = creationMode) {
    selectedByModeRef.current[mode] = null;
  }

  async function selectAssetForMode(asset, mode = creationMode) {
    const targetMode = asset.type === "video" && mode === "image" ? "video" : mode;
    const previousSignature = assetSignature(selectedByModeRef.current[targetMode]);
    const nextSignature = assetSignature(asset);
    if (previousSignature !== nextSignature) {
      await cancelReverseOperationForMode(targetMode);
    }
    selectedByModeRef.current[targetMode] = asset;
    setWorkspacePatch((current) => ({
      selected: asset,
      appliedUrl: String(asset.source_page_url || "").trim(),
      ...(previousSignature !== nextSignature
        ? {
            ...clearReverseLineage(),
            structured: {},
            structuredBaseline: {},
            structuredDirty: false,
            structuredSource: "",
            reverseVideoAnalysis: null,
            reverseConfig: reverseConfigForSourceChange(current.reverseConfig),
            ...(
              current.promptSourceSignature
              && current.promptSourceSignature !== nextSignature
                ? (
                    current.promptDirty
                      ? { promptSourceSignature: "" }
                      : { prompt: "", promptSourceSignature: "", promptDirty: false }
                  )
                : {}
            ),
            ...(current.negativeTouched ? {} : { negative: "" }),
          }
        : {}),
    }), targetMode);
    if (previousSignature !== nextSignature) {
      bumpReverseRequest(targetMode);
      setWorkspacePatch({ reversing: false }, targetMode);
    }
    const dims = assetDims(asset);
    if (targetMode !== mode) setCreationMode(targetMode);
    if (asset.type === "video" && dims) {
      setRatio(nearestRatio(dims.width, dims.height, videoRatioOptions()), targetMode);
    } else if (dims) {
      setRatio(nearestRatio(dims.width, dims.height), targetMode);
    }
  }

  async function waitForParseResult(initial, refVersion, mode, targetUrl, ownerRequest) {
    let current = initial;
    const startedAt = Date.now();
    while (current?.status === "queued" || current?.status === "running") {
      if (
        !ownerRequest.isCurrent()
        || !isParseStillCurrent(mode, refVersion, targetUrl)
      ) return null;
      if (Date.now() - startedAt > PARSE_POLL_TIMEOUT_MS) {
        return {
          ...current,
          status: "running",
          pending_timeout: true,
          error: "抓取仍在后台处理中，30 分钟内点击刷新或重新抓取会自动复用同一任务。",
        };
      }
      await sleep(PARSE_POLL_INTERVAL_MS);
      if (
        !ownerRequest.isCurrent()
        || !isParseStillCurrent(mode, refVersion, targetUrl)
      ) return null;
      current = await api.parseStatus(current.id);
    }
    return current;
  }

  async function doParse() {
    if (!url.trim() || parsing) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    const targetUrl = url.trim();
    const reqId = bumpParseRequest(mode);
    const refVersion = bumpRefVersion(mode);
    setMsg("");
    setWorkspacePatch({ parsing: true }, mode);
    setRefOpen(true);
    try {
      const first = await api.parse(targetUrl);
      if (!ownerRequest.isCurrent()) return;
      const result = await waitForParseResult(first, refVersion, mode, targetUrl, ownerRequest);
      if (
        !ownerRequest.isCurrent()
        || !isParseStillCurrent(mode, refVersion, targetUrl)
      ) return;
      if (!result) return;
      if (result.pending_timeout) {
        setMsg(result.error || "抓取仍在后台处理中，稍后刷新。");
        return;
      }
      if (result.status === "failed") throw new Error(result.error || "抓取失败，请稍后重试或更换链接");
      if (result.status !== "done") throw new Error("抓取状态异常，请稍后重试");
      const parsedAssets = Array.isArray(result.assets) ? result.assets : [];
      if (!parsedAssets.length) {
        if (isModeVisible(mode)) setMsg("未在该页面发现可用素材，已保留当前工作区。");
        return;
      }
      await cancelReverseOperationForMode(mode);
      bumpReverseRequest(mode);
      revokeUploadedObjectUrlsRef.current?.(mode);
      selectedByModeRef.current[mode] = null;
      setWorkspacePatch((current) => ({
        appliedUrl: targetUrl,
        assets: parsedAssets,
        selected: null,
        variationSource: null,
        structured: {},
        structuredBaseline: {},
        structuredDirty: false,
        structuredSource: "",
        reverseVideoAnalysis: null,
        ...clearReverseLineage(),
        ...(current.promptSourceSignature && !current.promptDirty
          ? { prompt: "", promptSourceSignature: "", promptDirty: false }
          : { promptSourceSignature: "" }),
        ...(current.negativeTouched ? {} : { negative: "" }),
      }), mode);
    } catch (e) {
      if (
        ownerRequest.isCurrent()
        && isRefVersionCurrent(mode, refVersion)
        && isModeVisible(mode)
      ) setMsg(e.message);
    } finally {
      if (
        ownerRequest.isCurrent()
        && isRequestCurrent(parseRequestRef, mode, reqId)
      ) setWorkspacePatch({ parsing: false }, mode);
    }
  }

  function pickAsset(asset) {
    return selectAssetForMode(asset, creationMode).catch((error) => {
      reportBackgroundError(error, "cancel reverse before selecting reference");
      return null;
    });
  }

  async function doReverse() {
    if (!selected) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    const targetCategory = category;
    const rawConfig = {
      ...(reverseConfig || {}),
      analysis_precision: reverseConfig?.analysis_precision || videoAnalysisPreset || "standard",
    };
    const configValidation = validateReverseConfig(rawConfig, {
      category: category === "video" ? "video" : "image",
      selectedType: selected.type,
      duration: selected.duration,
    });
    if (!configValidation.valid) {
      setMsg(configValidation.errors[0]?.message || "反推设置不完整，请检查后重试。");
      return;
    }
    const normalizedConfig = configValidation.value;
    const targetVideoPreset = normalizedConfig.analysis_precision;
    const targetNegativeTouched = negativeTouched;
    const startedPrompt = String(prompt || "");
    const startedNegative = String(negative || "");
    const startedRatio = ratio;
    const startedDuration = vDuration;
    const target = selected;
    const targetSignature = assetSignature(target);
    const existingOperation = operationsByMode[mode] || workspaceReverseOperation;
    if (existingOperation?.status === "needs_confirmation") {
      setMsg("请先确认使用封面分析或取消当前反推任务。");
      return;
    }
    const reqId = bumpReverseRequest(mode);
    let clientRequestId = null;
    const isCurrent = () => (
      ownerRequest.isCurrent()
      && isRequestCurrent(reverseRequestRef, mode, reqId)
      && assetSignature(selectedByModeRef.current[mode]) === targetSignature
    );
    setWorkspacePatch({ reversing: true }, mode);
    setMsg("");
    try {
      const isVideo = target.type === "video";
      const isVideoTarget = targetCategory === "video";
      const refUrl = isVideo ? target.url || target.thumb : target.url;
      const reverseTarget = isVideo ? "video" : targetCategory;
      const configuredSources = Array.isArray(reverseSources)
        ? reverseSources.filter((source) => source?.role !== "primary" && source?.asset_url)
        : [];
      const sources = [
        {
          asset_url: refUrl,
          source_type: target.type,
          role: "primary",
          ...assetIdField(target.id),
        },
        ...configuredSources,
        ...(configuredSources.length === 0 && productAsset && productAsset.type === "image" && assetSignature(productAsset) !== targetSignature
          ? [{
              asset_url: productAsset.url || productAsset.preview_url || productAsset.thumb,
              source_type: "image",
              role: subjectMode === "portrait" ? "subject" : "product",
              ...assetIdField(productAsset.id),
            }]
          : []),
      ].filter((source, index, rows) => (
        source.asset_url
        && rows.findIndex((item) => item.asset_url === source.asset_url && item.role === source.role) === index
      ));
      const workspaceSnapshot = {
        ...buildReverseOperationRequestSnapshotV3({
          creationMode: mode,
          subjectMode,
          target: reverseTarget,
          selected: target,
          productAsset,
          assets,
          sources,
          videoAnalysisPreset: isVideoTarget ? targetVideoPreset : "standard",
          reverseConfig: normalizedConfig,
          subjectProfile: productProfile,
        }),
        source_signature: targetSignature,
        ...(modelConfigId ? { model_config_id: Number(modelConfigId) } : {}),
        ...(modelSelections ? { model_selections: { ...modelSelections } } : {}),
      };
      const reverseBody = {
        asset_url: refUrl,
        sources,
        target: reverseTarget,
        fallback_image: isVideo ? target.thumb : null,
        source_type: target.type,
        video_analysis_preset: targetVideoPreset,
        analysis_precision: targetVideoPreset,
        analysis_focus: normalizedConfig.analysis_focus,
        output_purpose: normalizedConfig.output_purpose,
        custom_instruction: normalizedConfig.custom_instruction || null,
        source_range: selected.type === "video" ? normalizedConfig.source_range : null,
        source_ranges: selected.type === "video" ? normalizedConfig.source_ranges : [],
        custom_keyframes: selected.type === "video" ? normalizedConfig.custom_keyframes : [],
        include_audio: selected.type === "video" && normalizedConfig.include_audio,
        workspace_snapshot_v3: workspaceSnapshot,
        ...(modelConfigId ? { model_config_id: Number(modelConfigId) } : {}),
      };
      clientRequestId = generateReverseClientRequestId(
        pendingReverseRequestRef,
        mode,
        reverseOperationRequestSignature(reverseBody),
      );
      const context = {
        clientRequestId,
        targetSignature,
        targetCategory,
        targetVideoPreset,
        targetNegativeTouched,
        startedPrompt,
        startedNegative,
        startedRatio,
        startedDuration,
        isEditMode,
        subjectMode,
      };
      reverseContextsRef.current[mode] = context;
      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("反推服务不可用，本次未执行反推。");
      }
      const executableRequest = {
        ...reverseBody,
        client_request_id: clientRequestId,
      };
      const confirmation = await requestQuoteConfirmation({
        kind: "reverse",
        request: executableRequest,
        clientRequestId,
        execute: ({ request }) => api.createReverseOperation(request),
      });
      if (confirmation.status !== "executed") {
        clearPendingReverseRequest(pendingReverseRequestRef, clientRequestId);
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("反推提交失败。");
        }
        if (confirmation.status === "invalidated" && isModeVisible(mode)) {
          setMsg(confirmation.reason || "反推参数已变化，请重新执行。");
        }
        setWorkspacePatch({ reversing: false }, mode);
        delete reverseContextsRef.current[mode];
        return;
      }
      const operation = normalizeReverseOperation(confirmation.result);
      if (!isCurrent()) {
        await cancelStaleReverseOperation(operation);
        clearPendingReverseRequest(pendingReverseRequestRef, clientRequestId);
        if (reverseContextsRef.current[mode] === context) delete reverseContextsRef.current[mode];
        return;
      }
      setWorkspacePatch({ reverseOperation: operation, reversing: true,
        pendingReverseResult: null,
        reverseResultRevisions: [],
        reverseAppliedVersion: null,
        reverseAppliedRevisionId: null,
        reverseFeedback: null,
        reverseResultSchemaVersion: "",
        reverseUndoSnapshot: null,
        reverseApplyConflict: null,
      }, mode);
      await startReverseTracking(operation, { mode, context });
    } catch (e) {
      if (
        isCurrent()
        && !isRequestTimeoutError(e)
        && !shouldKeepPendingReverseRequest(e)
      ) {
        clearPendingReverseRequest(pendingReverseRequestRef, clientRequestId);
      }
      if (isCurrent() && isModeVisible(mode)) setMsg(e.message);
      if (isCurrent()) setWorkspacePatch({ reversing: false }, mode);
      if (reverseContextsRef.current[mode]?.clientRequestId === clientRequestId) {
        delete reverseContextsRef.current[mode];
      }
    }
  }

  function savedOperationId(trackingKey) {
    if (operationsByMode[trackingKey]?.id) return operationsByMode[trackingKey].id;
    if (trackingKey === creationMode && workspaceReverseOperation?.id) {
      return workspaceReverseOperation.id;
    }
    return resumeOperations.find((candidate) => candidate?.trackingKey === trackingKey)?.operation?.id || null;
  }

  async function confirmReverseCover(fallbackFile = null) {
    const mode = creationMode;
    let fallbackImage = selected?.thumb || null;
    if (fallbackFile) {
      if (!fallbackFile.type?.startsWith("image/")) throw new Error("请选择图片文件作为备用封面");
      setMsg("正在上传备用封面…");
      const uploaded = await api.uploadImage(fallbackFile);
      fallbackImage = uploaded?.url || uploaded?.preview_url || null;
      if (!fallbackImage) throw new Error("备用封面上传成功但未返回可用地址");
    }
    return confirmTrackedReverseCover(
      mode,
      fallbackImage,
      savedOperationId(mode),
    );
  }

  function cancelReverseOperationForMode(mode = creationMode) {
    return cancelTrackedReverseOperation(mode, savedOperationId(mode)).catch((error) => {
      if (isModeVisible(mode)) setMsg(error.message || "反推任务取消失败");
      throw error;
    });
  }

  function cancelRecoveredProfileOperationForMode(mode = creationMode) {
    const trackingKey = `${mode}::profile`;
    return cancelTrackedReverseOperation(trackingKey, savedOperationId(trackingKey)).catch((error) => {
      if (isModeVisible(mode)) setMsg(error.message || "主体档案任务取消失败");
      throw error;
    }).then((operation) => {
      stopReverseTracker(trackingKey);
      return operation;
    });
  }

  function trackProfileReverseOperation(operation, mode = creationMode, context = {}) {
    const trackingKey = `${mode}::profile`;
    if (context.sourceAsset) profileAssetByModeRef.current[mode] = context.sourceAsset;
    return startReverseTracking(operation, {
      mode,
      trackingKey,
      context: {
        ...context,
        targetSignature: context.targetSignature || assetSignature(profileAssetByModeRef.current[mode]),
      },
    });
  }

  function trackReverseOperation(operation, mode = creationMode, context = {}) {
    return startReverseTracking(operation, {
      mode,
      context: {
        recovered: true,
        ...context,
      },
    });
  }

  function cancelAllReverseOperations() {
    const candidates = new Map();
    for (const [trackingKey, operation] of Object.entries(operationsByMode)) {
      if (operation?.id) candidates.set(trackingKey, operation);
    }
    for (const candidate of resumeOperations) {
      const trackingKey = candidate?.trackingKey || candidate?.mode;
      if (trackingKey && candidate?.operation?.id && !candidates.has(trackingKey)) {
        candidates.set(trackingKey, candidate.operation);
      }
    }
    return Promise.allSettled(
      [...candidates.entries()]
        .filter(([, operation]) => ["queued", "running", "needs_confirmation"].includes(operation.status))
        .map(([trackingKey, operation]) => cancelTrackedReverseOperation(trackingKey, operation.id)),
    ).then((results) => {
      const failure = results.find((result) => result.status === "rejected");
      if (failure) throw failure.reason;
      return results;
    });
  }

  function resetOwnerReferenceParsing() {
    cancelAllReverseOperations().catch((error) => {
      reportBackgroundError(error, "cancel reverse operations while resetting owner");
    });
    resetOwnerReverseTracking();
    ownerRequestContextRef.current.invalidate();
    selectedByModeRef.current = {};
    profileAssetByModeRef.current = {};
    refVersionRef.current = {};
    parseRequestRef.current = {};
    reverseRequestRef.current = {};
    pendingReverseRequestRef.current = {};
    lastReversePromptRef.current = {};
    reverseContextsRef.current = {};
    urlByModeRef.current = {};
  }

  return {
    bumpRefVersion,
    isRefVersionCurrent,
    bumpParseRequest,
    bumpReverseRequest,
    isModeVisible,
    clearSelectedForMode,
    selectAssetForMode,
    pickAsset,
    doParse,
    doReverse,
    confirmReverseCover,
    cancelReverseOperationForMode,
    cancelRecoveredProfileOperationForMode,
    trackReverseOperation,
    trackProfileReverseOperation,
    cancelAllReverseOperations,
    reverseOperation: operationsByMode[creationMode] || null,
    lastReversePromptRef,
    resetOwnerReferenceParsing,
  };
}
