"use client";

import { api } from "../lib/api";
import { errorMessage } from "../lib/errorHandling";
import { reverseOperationResult } from "../lib/reverseOperations";
import { assetSignature } from "../app/studio/helpers";
import {
  buildReverseSnapshotV3,
  workspacePatchFromReverseSnapshot,
} from "../app/studio/reverseSnapshot";
import {
  reverseResultEnvelope,
  reverseResultPayload,
} from "../app/studio/reverseResultRevisions";

export default function useStudioRecipeActions({
  creationMode,
  category,
  subjectMode,
  workspace,
  activeSubjectProfile,
  effectiveModelSelections,
  selectedGenerationModelConfigId,
  selectedVisionModelConfigId,
  selectedPromptModelConfigId,
  effectiveProductVideoTemplate,
  reverseOperationForPendingResult,
  lastReversePromptRef,
  promptSaveTitle,
  promptSaveCategory,
  promptSaveFavorite,
  setWorkspacePatch,
  setReverseActionBusy,
  setMsg,
  notify,
}) {
  const {
    ratio,
    imageQuality,
    n,
    seed,
    vDuration,
    vResolution,
    productVideoTemplate,
    selected,
    productAsset,
    assets,
    reverseSources,
    structured,
    prompt,
    negative,
    promptDirty,
    videoAnalysisPreset,
    reverseVideoAnalysis,
    reverseConfig,
    pendingReverseResult,
    reverseResultTab,
    reverseResultSchemaVersion,
    reverseAppliedVersion,
    reverseAppliedRevisionId,
    reverseUndoSnapshot,
    reverseResultRevisions,
    reverseFeedback,
    creationRecipeId,
    creationRecipeVersion,
    creationRecipeShareSlug,
    creationRecipeSource,
    structuredSource,
    promptSourceSignature,
  } = workspace;

  function recipeGenerationSettings(overrides = {}) {
    return {
      model_selections: effectiveModelSelections,
      generation_model_config_id: selectedGenerationModelConfigId || null,
      vision_model_config_id: selectedVisionModelConfigId || null,
      prompt_model_config_id: selectedPromptModelConfigId || null,
      ratio,
      image_quality: imageQuality,
      count: n,
      seed,
      duration: vDuration,
      resolution: vResolution,
      product_video_template: effectiveProductVideoTemplate || productVideoTemplate,
      ...overrides,
    };
  }

  function currentReverseSnapshotV3() {
    const generation = recipeGenerationSettings();
    return {
      ...buildReverseSnapshotV3({
        creationMode,
        subjectMode,
        target: category,
        selected,
        productAsset,
        assets,
        sources: reverseSources,
        structured,
        finalText: prompt,
        promptDirty,
        videoAnalysisPreset,
        videoAnalysis: reverseVideoAnalysis,
        subjectProfile: activeSubjectProfile,
        reverseConfig,
        pendingResult: pendingReverseResult,
        resultTab: reverseResultTab,
        resultSchemaVersion: reverseResultSchemaVersion,
        appliedVersion: reverseAppliedVersion,
        appliedRevisionId: reverseAppliedRevisionId,
        undoSnapshot: reverseUndoSnapshot,
        resultRevisions: reverseResultRevisions,
        feedback: reverseFeedback,
        creationRecipeId,
        creationRecipeVersion,
        creationRecipeShareSlug,
        creationRecipeSource,
      }),
      model_selections: effectiveModelSelections,
      model_config_id: selectedVisionModelConfigId || null,
      generation,
    };
  }

  function recipePayload(sourceOperation = null) {
    const currentOperation = reverseOperationForPendingResult();
    const useCurrentWorkspace = !sourceOperation
      || (currentOperation?.id && String(sourceOperation.id) === String(currentOperation.id));
    if (useCurrentWorkspace) {
      return {
        schema_version: "creation-recipe.v1",
        reverse_snapshot_v3: currentReverseSnapshotV3(),
        prompt,
        negative,
        structured,
        reverse_result: reverseResultPayload(pendingReverseResult),
        generation: recipeGenerationSettings(),
      };
    }

    const operationResult = reverseOperationResult(sourceOperation) || {};
    const sourceSnapshot = sourceOperation.workspace_snapshot_v3 || sourceOperation.workspace_snapshot_v2 || {};
    const restored = workspacePatchFromReverseSnapshot(sourceSnapshot);
    const restoredWorkspace = restored?.workspace || {};
    const restoredCategory = sourceOperation.target === "video" ? "video" : "image";
    const sourceGeneration = sourceSnapshot.generation && typeof sourceSnapshot.generation === "object"
      ? sourceSnapshot.generation
      : {};
    const generation = recipeGenerationSettings({
      product_video_template: restoredWorkspace.productVideoTemplate || "prompt_driven",
      ...sourceGeneration,
    });
    const upgradedSnapshot = {
      ...buildReverseSnapshotV3({
        creationMode: restored?.creationMode || (restoredCategory === "video" ? "video" : "image"),
        subjectMode: restored?.subjectMode || "general",
        target: restoredCategory,
        selected: restoredWorkspace.selected,
        productAsset: restoredWorkspace.productAsset,
        assets: restoredWorkspace.assets || [],
        sources: sourceSnapshot.sources || [],
        structured: operationResult.structured || restoredWorkspace.structured || {},
        finalText: operationResult.final_text || restoredWorkspace.prompt || "",
        videoAnalysisPreset: sourceOperation.analysis_precision,
        videoAnalysis: operationResult.video_analysis || restoredWorkspace.reverseVideoAnalysis,
        reverseConfig: restoredWorkspace.reverseConfig,
        pendingResult: reverseResultEnvelope(sourceOperation, operationResult),
        resultSchemaVersion: sourceOperation.result_schema_version,
        appliedVersion: sourceOperation.applied_result_version,
        appliedRevisionId: restoredWorkspace.reverseAppliedRevisionId,
        resultRevisions: restoredWorkspace.reverseResultRevisions || [],
      }),
      model_selections: sourceSnapshot.model_selections || effectiveModelSelections,
      model_config_id: sourceOperation.model_config_id || null,
      generation,
    };
    return {
      schema_version: "creation-recipe.v1",
      reverse_snapshot_v3: upgradedSnapshot,
      prompt: String(operationResult.final_text || restoredWorkspace.prompt || ""),
      negative: String(operationResult.negative || operationResult.negative_prompt || restoredWorkspace.negative || ""),
      structured: operationResult.structured || restoredWorkspace.structured || {},
      reverse_result: operationResult,
      generation,
    };
  }

  async function saveCreationRecipe(sourceOperation = null, { reuseCurrent = true } = {}) {
    const operation = sourceOperation || reverseOperationForPendingResult();
    const payload = recipePayload(sourceOperation);
    const finalPrompt = String(payload.prompt || "").trim();
    if (!finalPrompt && !Object.keys(payload.structured || {}).length) {
      notify.warn("当前没有可保存的反推结果。");
      return;
    }
    setReverseActionBusy("recipe");
    try {
      if (reuseCurrent && creationRecipeId && (!creationRecipeSource || creationRecipeSource === "owner")) {
        const version = await api.createCreationRecipeVersion(creationRecipeId, { payload });
        setWorkspacePatch({
          creationRecipeVersion: version.version,
          creationRecipeShareSlug: "",
          creationRecipeSource: "owner",
        });
        setMsg(`创作配方已更新到版本 ${version.version}。`);
        notify.success(`创作配方已保存为版本 ${version.version}`);
        return;
      }
      const snapshot = payload.reverse_snapshot_v3 || {};
      const coverAssetUrl = snapshot.selected?.preview_url
        || snapshot.selected?.thumb
        || snapshot.selected?.url
        || null;
      const created = await api.createCreationRecipe({
        title: promptSaveTitle.trim() || (payload.reverse_snapshot_v3?.target === "video" ? "视频反推创作配方" : "图片反推创作配方"),
        category: payload.reverse_snapshot_v3?.target === "video" ? "video" : "image",
        visibility: "private",
        favorite: Boolean(promptSaveFavorite),
        source_operation_id: operation?.status === "succeeded" && ["image", "video"].includes(operation.target)
          ? Number(operation.id)
          : null,
        cover_asset_url: coverAssetUrl,
        payload,
      });
      if (reuseCurrent) setWorkspacePatch({
        creationRecipeId: created.id,
        creationRecipeVersion: created.current_version,
        creationRecipeShareSlug: "",
        creationRecipeSource: "owner",
      });
      setMsg("已保存完整创作配方，可在提示词库中恢复。");
      notify.success("创作配方已保存");
    } catch (error) {
      const text = errorMessage(error, "保存创作配方失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function saveReversePromptToLibrary() {
    const text = String(prompt || "").trim();
    if (!text) {
      setMsg("当前没有可保存的提示词。");
      notify.warn("当前没有可保存的提示词。");
      return;
    }
    const reverseSource = lastReversePromptRef.current?.[creationMode] || {};
    try {
      await api.createPromptHistory({
        title: promptSaveTitle.trim() || (category === "video" ? "反推视频提示词" : "反推图片提示词"),
        prompt: text,
        category: promptSaveCategory || (category === "video" ? "video" : "image"),
        source: "reverse",
        favorite: Boolean(promptSaveFavorite),
        params: {
          creation_mode: creationMode,
          subject_mode: subjectMode,
          source_signature: reverseSource.sourceSignature || structuredSource || promptSourceSignature || assetSignature(selected),
          structured,
          reverse_snapshot_v3: {
            ...currentReverseSnapshotV3(),
            final_text: text,
          },
        },
      });
      setMsg("已保存到“我的提示词”。");
      notify.success("已保存到“我的提示词”。");
    } catch (error) {
      const text = errorMessage(error, "保存提示词失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    }
  }

  return {
    recipePayload,
    saveCreationRecipe,
    saveReversePromptToLibrary,
  };
}
