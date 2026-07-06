"use client";

import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import {
  PARSE_POLL_INTERVAL_MS,
  PARSE_POLL_TIMEOUT_MS,
  RATIOS,
} from "../app/studio/constants";
import {
  assetDims,
  assetSignature,
  composePromptFromStructured,
  composeStyleTransferPrompt,
  isRequestTimeoutError,
  nearestRatio,
  videoRatioOptions,
} from "../app/studio/helpers";
import {
  clearPendingReverseRequest,
  generateReverseClientRequestId,
} from "../app/studio/generationRequestId";

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

export default function useReferenceParsing({
  creationMode,
  category,
  url,
  parsing,
  selected,
  prompt,
  negative,
  negativeTouched,
  videoAnalysisPreset,
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
}) {
  const selectedByModeRef = useRef({});
  const refVersionRef = useRef({});
  const parseRequestRef = useRef({});
  const reverseRequestRef = useRef({});
  const pendingReverseRequestRef = useRef({});
  const creationModeRef = useRef("image");
  const urlByModeRef = useRef({});

  urlByModeRef.current[creationMode] = String(url || "").trim();

  useEffect(() => {
    creationModeRef.current = creationMode;
    selectedByModeRef.current[creationMode] = selected;
  }, [creationMode, selected]);

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

  function selectAssetForMode(asset, mode = creationMode) {
    const targetMode = asset.type === "video" && mode === "image" ? "video" : mode;
    const previousSignature = assetSignature(selectedByModeRef.current[targetMode]);
    const nextSignature = assetSignature(asset);
    selectedByModeRef.current[targetMode] = asset;
    setWorkspacePatch((current) => ({
      selected: asset,
      ...(previousSignature !== nextSignature
        ? {
            structured: {},
            structuredSource: "",
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

  async function waitForParseResult(initial, refVersion, mode, targetUrl) {
    let current = initial;
    const startedAt = Date.now();
    while (current?.status === "queued" || current?.status === "running") {
      if (!isParseStillCurrent(mode, refVersion, targetUrl)) return null;
      if (Date.now() - startedAt > PARSE_POLL_TIMEOUT_MS) {
        return {
          ...current,
          status: "running",
          pending_timeout: true,
          error: "抓取仍在后台处理中，30 分钟内点击刷新或重新抓取会自动复用同一任务。",
        };
      }
      await sleep(PARSE_POLL_INTERVAL_MS);
      if (!isParseStillCurrent(mode, refVersion, targetUrl)) return null;
      current = await api.parseStatus(current.id);
    }
    return current;
  }

  async function doParse() {
    if (!url.trim() || parsing) return;
    const mode = creationMode;
    const targetUrl = url.trim();
    const reqId = bumpParseRequest(mode);
    const refVersion = bumpRefVersion(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrlsRef.current?.(mode);
    selectedByModeRef.current[mode] = null;
    setMsg("");
    setWorkspacePatch((current) => ({
      assets: [],
      selected: null,
      variationSource: null,
      structured: {},
      structuredSource: "",
      ...(current.promptSourceSignature && !current.promptDirty
        ? { prompt: "", promptSourceSignature: "", promptDirty: false }
        : { promptSourceSignature: "" }),
      ...(current.negativeTouched ? {} : { negative: "" }),
    }), mode);
    setWorkspacePatch({ parsing: true }, mode);
      setRefOpen(true);
    let clientRequestId = null;
    try {
      const first = await api.parse(targetUrl);
      const result = await waitForParseResult(first, refVersion, mode, targetUrl);
      if (!isParseStillCurrent(mode, refVersion, targetUrl)) return;
      if (!result) return;
      if (result.pending_timeout) {
        setMsg(result.error || "抓取仍在后台处理中，稍后刷新。");
        return;
      }
      if (result.status === "failed") throw new Error(result.error || "抓取失败，请稍后重试或更换链接");
      if (result.status !== "done") throw new Error("抓取状态异常，请稍后重试");
      setWorkspacePatch({ assets: result.assets || [] }, mode);
      if (!result.assets?.length && isModeVisible(mode)) setMsg("未在该页面发现可用素材");
    } catch (e) {
      if (isRefVersionCurrent(mode, refVersion) && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(parseRequestRef, mode, reqId)) setWorkspacePatch({ parsing: false }, mode);
    }
  }

  function pickAsset(asset) {
    selectAssetForMode(asset, creationMode);
  }

  async function doReverse() {
    if (!selected) return;
    const mode = creationMode;
    const targetCategory = category;
    const targetVideoPreset = videoAnalysisPreset;
    const targetNegativeTouched = negativeTouched;
    const startedPrompt = String(prompt || "");
    const startedNegative = String(negative || "");
    const target = selected;
    const targetSignature = assetSignature(target);
    const reqId = bumpReverseRequest(mode);
    const isCurrent = () => (
      isRequestCurrent(reverseRequestRef, mode, reqId)
      && assetSignature(selectedByModeRef.current[mode]) === targetSignature
    );
    setWorkspacePatch({ reversing: true }, mode);
    setMsg("");
    try {
      const isVideo = target.type === "video";
      const refUrl = isVideo ? target.url || target.thumb : target.url;
      const reverseTarget = isVideo ? "video" : targetCategory;
      const reverseSignature = JSON.stringify({
        refUrl,
        fallback: isVideo ? target.thumb : null,
        sourceType: target.type,
        target: reverseTarget,
        preset: isVideo ? targetVideoPreset : null,
      });
      clientRequestId = generateReverseClientRequestId(
        pendingReverseRequestRef,
        mode,
        reverseSignature,
      );
      const result = await api.reverse(
        refUrl,
        reverseTarget,
        isVideo ? target.thumb : null,
        target.type,
        isVideo ? targetVideoPreset : null,
        clientRequestId,
      );
      clearPendingReverseRequest(pendingReverseRequestRef, clientRequestId);
      if (!isCurrent()) return;
      const structured = result.structured || {};
      const reversePrompt = isEditMode
        ? composeStyleTransferPrompt(structured, result.final_text || "", {
            video: targetCategory === "video",
            subject: subjectMode,
          })
        : composePromptFromStructured(structured, result.final_text || "");
      setWorkspacePatch((current) => {
        const currentPrompt = String(current.prompt || "");
        const currentNegative = String(current.negative || "");
        const promptUnchanged = currentPrompt === startedPrompt && !current.promptDirty;
        const negativeUnchanged = currentNegative === startedNegative && !current.negativeTouched && !targetNegativeTouched;
        return {
          structured,
          structuredSource: targetSignature,
          ...(promptUnchanged
            ? { prompt: reversePrompt, promptSourceSignature: targetSignature, promptDirty: false }
            : {}),
          ...(structured["负向"] && negativeUnchanged ? { negative: structured["负向"] } : {}),
        };
      }, mode);
      setStructOpen(true);
      if (structured["负向"] && !targetNegativeTouched && isModeVisible(mode)) setShowNegative(true);
      if (typeof result.charged_credits === "number") {
        const suffix = result.reference_count > 1 ? `（${result.reference_count} 帧）` : "";
        if (isModeVisible(mode)) setMsg(`反推完成，已扣 ${result.charged_credits} 积分${suffix}`);
      }
      refreshMe();
    } catch (e) {
      if (!isRequestTimeoutError(e)) clearPendingReverseRequest(pendingReverseRequestRef, clientRequestId);
      if (isCurrent() && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(reverseRequestRef, mode, reqId)) setWorkspacePatch({ reversing: false }, mode);
    }
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
  };
}
