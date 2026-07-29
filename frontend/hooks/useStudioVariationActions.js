"use client";

import { applyStudioVariationTransferState } from "../lib/studioSession";
import { assetVariationSourceUrl } from "../app/studio/assetActions";
import { RATIOS } from "../app/studio/constants";
import { nearestRatio } from "../app/studio/helpers";

export default function useStudioVariationActions({
  creationMode,
  ratio,
  workspace,
  showNegative,
  refOpen,
  structOpen,
  studioUiStateRef,
  variationRestoreContextRef,
  setCreationMode,
  setWorkspacePatch,
  setStructOpen,
  setRefOpen,
  setShowNegative,
  setMsg,
}) {
  function createImageVariation(asset) {
    const sourceUrl = assetVariationSourceUrl(asset, { respectUnlock: true });
    if (!sourceUrl) {
      setMsg("当前图片暂不可作为变体来源，请先确认预览可用。");
      return;
    }
    const nextAsset = {
      ...asset,
      type: "image",
      url: sourceUrl,
      thumb: asset.preview_url || sourceUrl,
    };
    const dims = asset.width && asset.height ? nearestRatio(asset.width, asset.height, RATIOS) : ratio;
    setCreationMode("image_edit");
    setWorkspacePatch({
      prompt: "基于这张图生成同主体、同风格的近似变体；保留主体结构、构图、光线、色调和广告质感，只做轻微差异化，不新增无关主体。",
      negative: "",
      imageEditProductMode: false,
      editSubjectMode: "general",
      productAsset: nextAsset,
      variationSource: nextAsset,
      selected: null,
      assets: [],
      structured: {},
      structuredBaseline: {},
      structuredDirty: false,
      structuredSource: "",
      promptSourceSignature: "",
      promptDirty: true,
      negativeTouched: false,
      ratio: dims,
    }, "image_edit");
    setStructOpen(false);
    setRefOpen(false);
    setShowNegative(false);
    setMsg("已切到图片编辑，可直接生成同款变体，也可以先微调提示词。");
    if (typeof window !== "undefined") {
      window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
    }
  }

  function applyVariationDraft(draft) {
    const asset = draft?.asset;
    const sourceUrl = assetVariationSourceUrl(asset, { respectUnlock: true });
    if (!asset || asset.type !== "image" || !sourceUrl) return false;
    const nextAsset = {
      ...asset,
      type: "image",
      url: sourceUrl,
      thumb: asset.preview_url || asset.thumb || sourceUrl,
    };
    const dims = asset.width && asset.height ? nearestRatio(asset.width, asset.height, RATIOS) : "1:1";
    const workspacePatch = {
      prompt: draft.prompt || "基于这张图生成同主体、同构图、同光线和同广告质感的近似变体；保留主体结构、产品文字、Logo、比例和核心视觉，只做轻微差异化。",
      negative: "",
      imageEditProductMode: false,
      editSubjectMode: "general",
      productAsset: nextAsset,
      productProfile: null,
      productProfileSource: "",
      portraitProfile: null,
      portraitProfileSource: "",
      productProfiling: false,
      variationSource: nextAsset,
      selected: null,
      assets: [],
      structured: {},
      structuredBaseline: {},
      structuredDirty: false,
      structuredSource: "",
      promptSourceSignature: "",
      promptDirty: true,
      negativeTouched: false,
      ratio: dims,
    };
    const liveState = studioUiStateRef.current || {
      workspaces: { image_edit: workspace },
      creationMode,
      showNegative,
      refOpen,
      structOpen,
    };
    const restoreContext = variationRestoreContextRef.current || {
      baseline: liveState,
      current: liveState,
    };
    const transition = applyStudioVariationTransferState(
      restoreContext.baseline,
      restoreContext.current,
      {
        metadataPatch: {
          creationMode: "image_edit",
          showNegative: false,
          refOpen: false,
          structOpen: false,
        },
        workspacePatch,
      },
    );
    if (!transition.applied) return false;
    setCreationMode(transition.state.creationMode);
    setWorkspacePatch(transition.workspacePatch, "image_edit");
    setStructOpen(transition.state.structOpen);
    setRefOpen(transition.state.refOpen);
    setShowNegative(transition.state.showNegative);
    setMsg("已带入历史图片，可直接生成变体，也可以先微调提示词。");
    return true;
  }

  return {
    createImageVariation,
    applyVariationDraft,
  };
}
