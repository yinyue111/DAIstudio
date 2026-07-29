"use client";

import { useEffect } from "react";
import useStudioModelSelection from "../useStudioModelSelection";
import useStudioQuoteConfirmation from "../useStudioQuoteConfirmation";
import { RATIOS, VIDEO_RATIO_KEYS } from "../../app/studio/constants";
import { nearestRatio, videoRatioOptions } from "../../app/studio/helpers";
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
    subjectMode,
    productGenerationMode,
    lastFrameAsset,
    productDetailAssets,
    productVideoTemplate,
    ratio,
    setRatio,
    setWorkspacePatch,
    setMsg,
  } = foundation;
  const selection = useStudioModelSelection({
    cfg,
    category,
    creationMode,
    selected,
    productAsset,
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

  return {
    ...selection,
    ...context,
    ...quote,
  };
}
