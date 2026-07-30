"use client";

import { useEffect } from "react";
import useStudioModelSelection from "../useStudioModelSelection";
import useStudioQuoteConfirmation from "../useStudioQuoteConfirmation";
import { RATIOS, VIDEO_RATIO_KEYS } from "../../app/studio/constants";
import { boundedVideoDuration, nearestRatio, videoRatioOptions } from "../../app/studio/helpers";
import { buildStudioModelContext } from "../../app/studio/studioModelContext";

export default function useStudioModelDomain(foundation) {
  const {
    cfg,
    me,
    notify,
    category,
    creationMode,
    selected,
    productAsset,
    productDetailAssets,
    subjectMode,
    productGenerationMode,
    lastFrameAsset,
    productVideoTemplate,
    pendingReverseResult,
    workspaceReverseOperation,
    reverseAppliedRevisionId,
    ratio,
    setRatio,
    vDuration,
    setVDuration,
    vResolution,
    setVResolution,
    setWorkspacePatch,
    setMsg,
  } = foundation;
  const selection = useStudioModelSelection({
    cfg,
    category,
    creationMode,
    selected,
    productAsset,
    productDetailAssets,
    subjectMode,
    ownerId: me?.id,
    notify,
  });
  const context = buildStudioModelContext({
    cfg,
    category,
    creationMode,
    subjectMode,
    productGenerationMode,
    selected,
    lastFrameAsset,
    productAsset,
    productDetailAssets,
    productVideoTemplate,
    generationModelOptions: selection.generationModelOptions,
    selectedGenerationModel: selection.selectedGenerationModel,
    reverseOperationId: pendingReverseResult?.operation_id
      || pendingReverseResult?.operationId
      || workspaceReverseOperation?.id
      || null,
    reverseRevisionId: reverseAppliedRevisionId,
    reverseSourceSignature: pendingReverseResult?.source_signature
      || pendingReverseResult?.sourceSignature
      || workspaceReverseOperation?.request_context?.source_signature
      || workspaceReverseOperation?.workspace_snapshot_v3?.source_signature
      || workspaceReverseOperation?.workspace_snapshot_v2?.source_signature
      || "",
  });
  const quote = useStudioQuoteConfirmation({ balanceCredits: me?.balance_credits });

  useEffect(() => {
    if (!lastFrameAsset || !selection.selectedGenerationModelConfigId) return;
    if (context.firstLastFrameEnabled) return;
    setWorkspacePatch({ lastFrameAsset: null });
    if (category === "video" && creationMode === "video") {
      setMsg("当前模式或模型不支持首尾帧，已清除尾帧素材。");
    }
  }, [
    category,
    creationMode,
    context.firstLastFrameEnabled,
    lastFrameAsset,
    selection.selectedGenerationModelConfigId,
  ]);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((item) => item.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

  useEffect(() => {
    if (category !== "video") return;
    const nextDuration = boundedVideoDuration(
      vDuration,
      context.maxVideoDuration,
      context.minVideoDuration,
    );
    if (Number(vDuration) !== nextDuration) setVDuration(nextDuration);
  }, [
    category,
    context.maxVideoDuration,
    context.minVideoDuration,
    selection.selectedGenerationModelConfigId,
    vDuration,
  ]);

  useEffect(() => {
    if (category !== "video" || context.videoResolutions.includes(vResolution)) return;
    const nextResolution = context.videoResolutions.includes("720p")
      ? "720p"
      : context.videoResolutions[0];
    if (nextResolution) setVResolution(nextResolution);
  }, [
    category,
    context.videoResolutions,
    selection.selectedGenerationModelConfigId,
    vResolution,
  ]);

  return {
    ...selection,
    ...context,
    ...quote,
  };
}
