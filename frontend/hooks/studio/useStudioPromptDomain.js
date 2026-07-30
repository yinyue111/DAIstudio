"use client";

import { useEffect, useRef } from "react";
import usePromptOptimization from "../usePromptOptimization";
import { assetSignature } from "../../app/studio/helpers";
import {
  buildStudioQuoteInputRevision,
} from "../../app/studio/viewModel";
import { normalizeProductVideoStrategyKey } from "../../app/studio/productVideoStrategy";

export default function useStudioPromptDomain(
  foundation,
  model,
  reverseOperationForPendingResult,
) {
  const {
    me,
    notify,
    creationMode,
    setCreationMode,
    category,
    subjectMode,
    productGenerationMode,
    prompt,
    negative,
    vDuration,
    ratio,
    vResolution,
    activeSubjectProfileSource,
    reverseAppliedRevisionId,
    workspace,
    setWorkspacePatch,
    setMsg,
    setPrompt,
    setEditSubjectMode,
    productVideoTemplate,
    setProductVideoTemplate,
    selected,
    productAsset,
    productDetailAssets,
    batchReverseAssets,
    structured,
    imageQuality,
    n,
    seed,
    editMaskMode,
    productPixelLockMode,
    reverseConfig,
    reverseSources,
    pendingReverseResult,
    workspaceReverseOperation,
    setPromptSaveCategory,
    refs,
  } = foundation;

  const optimization = usePromptOptimization({
    creationMode,
    category,
    subjectMode,
    productGenerationMode,
    prompt,
    vDuration,
    ratio,
    vResolution,
    effectiveProductVideoTemplate: model.effectiveProductVideoTemplate,
    referenceSignature: model.referenceSignature,
    activeSubjectProfileSource,
    targetModelId: model.targetModelId,
    targetModelProvider: model.targetModelProvider,
    selectedGenerationModelConfigId: model.selectedGenerationModelConfigId,
    selectedPromptModelConfigId: model.selectedPromptModelConfigId,
    selectedPromptModel: model.selectedPromptModel,
    reverseAppliedRevisionId,
    reverseOperationForPendingResult,
    me,
    refreshMe: foundation.refreshMe,
    requestQuoteConfirmation: model.requestQuoteConfirmation,
    workspace,
    workspacesRef: refs.workspacesRef,
    setWorkspacePatch,
    setMsg,
    notify,
    setPrompt,
    setEditSubjectMode,
    studioActionPendingRequestRef: refs.studioActionPendingRequestRef,
    generationModelOptions: model.generationModelOptions,
    modelSelectionsRef: model.modelSelectionsRef,
    changeModelSelection,
  });

  const quoteInputRevision = buildStudioQuoteInputRevision({
    owner_id: me?.id || null,
    creation_mode: creationMode,
    category,
    prompt: optimization.promptForGeneration,
    negative,
    selected: assetSignature(selected),
    last_frame_asset: assetSignature(model.effectiveLastFrameAsset),
    product_asset: assetSignature(productAsset),
    product_detail_assets: productDetailAssets.map(assetSignature),
    batch_reverse_assets: batchReverseAssets.map(assetSignature),
    structured,
    ratio,
    image_quality: imageQuality,
    count: n,
    seed,
    edit_mask_mode: model.maskEditSupported ? editMaskMode : "off",
    product_pixel_lock_mode: model.maskEditSupported ? productPixelLockMode : "off",
    video_duration: vDuration,
    video_resolution: vResolution,
    product_video_template: category === "video" && productGenerationMode
      ? model.effectiveProductVideoTemplate
      : null,
    reverse_config: reverseConfig,
    reverse_sources: reverseSources,
    model_selections: model.effectiveModelSelections,
    prompt_optimization_direction: optimization.setting.direction,
    prompt_optimization_target_language: optimization.setting.targetLanguage,
    reverse_operation_id: pendingReverseResult?.operation_id || workspaceReverseOperation?.id || null,
    reverse_revision_id: reverseAppliedRevisionId || null,
    pending_reverse_result: pendingReverseResult,
  });
  const previousQuoteInputRevisionRef = useRef(quoteInputRevision);

  useEffect(() => {
    if (previousQuoteInputRevisionRef.current === quoteInputRevision) return;
    previousQuoteInputRevisionRef.current = quoteInputRevision;
    model.invalidateQuote(
      "工作区输入或模型已变化，本次操作已取消，请重新执行。",
      { preserveKinds: ["generation"] },
    );
  }, [quoteInputRevision, model.invalidateQuote]);

  useEffect(() => {
    if (
      category !== "video"
      || !productGenerationMode
      || !model.productVideoStrategySelection.supported
      || !model.effectiveProductVideoTemplate
      || model.effectiveProductVideoTemplate === productVideoTemplate
    ) return;
    setProductVideoTemplate(model.effectiveProductVideoTemplate);
    optimization.invalidate(creationMode);
  }, [
    category,
    creationMode,
    productGenerationMode,
    model.productVideoStrategySelection.supported,
    model.effectiveProductVideoTemplate,
    productVideoTemplate,
  ]);

  useEffect(() => {
    setPromptSaveCategory(category === "video" ? "video" : "image");
  }, [category]);

  function changeModelSelection(use, modelConfigId) {
    model.selectModel(use, modelConfigId);
    if (use === "prompt" || use === category) optimization.invalidate();
  }

  function changeProductVideoTemplate(value) {
    const next = normalizeProductVideoStrategyKey(value);
    if (!next || !model.productVideoStrategySelection.strategies.includes(next)) return;
    if (next === productVideoTemplate) return;
    setProductVideoTemplate(next);
    optimization.invalidate();
  }

  function switchCreationMode(kind) {
    if (!model.modelEnabled(kind)) {
      const text = `${kind === "video" || kind === "video_edit" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`;
      setMsg(text);
      notify.warn(text);
      return;
    }
    setMsg("");
    optimization.invalidate(creationMode);
    setCreationMode(kind);
  }

  return {
    ...optimization,
    changeModelSelection,
    changeProductVideoTemplate,
    switchCreationMode,
    quoteInputRevision,
  };
}
