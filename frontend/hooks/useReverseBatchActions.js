"use client";

import { api } from "../lib/api";
import { errorMessage } from "../lib/errorHandling";
import { buildReverseBatchItemPayload, MAX_REVERSE_BATCH_ITEMS } from "../lib/reverseBatches";
import { assetReferenceUrl, dedupeAssets, unifiedAssetKey } from "../lib/unifiedAssets";
import { assetSignature } from "../app/studio/helpers";
import { normalizeReverseConfig, validateReverseConfig } from "../app/studio/reverseConfig";
import { buildReverseOperationRequestSnapshotV3 } from "../app/studio/reverseSnapshot";

export default function useReverseBatchActions({
  category,
  creationMode,
  reverseConfig,
  batchReverseAssets,
  effectiveModelSelections,
  selectedVisionModelConfigId,
  requestQuoteConfirmation,
  createReverseBatch,
  recipePayload,
  refreshMe,
  setWorkspacePatch,
  setReverseActionBusy,
  setMsg,
  notify,
}) {
  function toggleBatchReverseAsset(asset) {
    if (!assetReferenceUrl(asset)) return;
    if (category === "image" && asset.type !== "image") {
      notify.warn("图片反推批次只能选择图片素材。");
      return;
    }
    setWorkspacePatch((current) => {
      const existing = current.batchReverseAssets || [];
      const key = unifiedAssetKey(asset) || assetReferenceUrl(asset);
      const found = existing.some((item) => (
        (unifiedAssetKey(item) || assetReferenceUrl(item)) === key
      ));
      if (found) {
        return { batchReverseAssets: existing.filter((item) => (
          (unifiedAssetKey(item) || assetReferenceUrl(item)) !== key
        )) };
      }
      if (existing.length >= MAX_REVERSE_BATCH_ITEMS) {
        notify.warn(`单次最多选择 ${MAX_REVERSE_BATCH_ITEMS} 个素材。`);
        return {};
      }
      return { batchReverseAssets: dedupeAssets([...existing, asset], MAX_REVERSE_BATCH_ITEMS) };
    });
  }

  function removeBatchReverseAsset(asset) {
    const key = unifiedAssetKey(asset) || assetReferenceUrl(asset);
    setWorkspacePatch((current) => ({
      batchReverseAssets: (current.batchReverseAssets || []).filter((item) => (
        (unifiedAssetKey(item) || assetReferenceUrl(item)) !== key
      )),
    }));
  }

  async function startReverseBatch(itemOverrides = {}, overrideCapability = { supported: false, fields: [], audio_policies: [] }) {
    const selectedAssets = dedupeAssets(batchReverseAssets, MAX_REVERSE_BATCH_ITEMS);
    if (selectedAssets.length < 2) {
      notify.warn("请至少选择 2 个素材进行批量反推。");
      return;
    }
    if (category === "image" && selectedAssets.some((asset) => asset.type !== "image")) {
      notify.warn("图片反推批次不能包含视频素材。");
      return;
    }
    const everyVideo = selectedAssets.every((asset) => asset.type === "video");
    const normalizedConfig = normalizeReverseConfig({
      ...(reverseConfig || {}),
      source_range: null,
      source_ranges: [],
      custom_keyframes: [],
      include_audio: everyVideo && Boolean(reverseConfig?.include_audio),
    }, { category });
    for (const asset of selectedAssets) {
      const validation = validateReverseConfig(normalizedConfig, {
        category,
        selectedType: asset.type,
        duration: asset.duration,
      });
      if (!validation.valid) {
        notify.warn(validation.errors[0]?.message || "批量反推设置不适用于所选素材。");
        return;
      }
    }
    const clientRequestId = `reverse-batch-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    const items = selectedAssets.map((asset) => {
      const url = assetReferenceUrl(asset);
      const sources = [{
        asset_url: url,
        source_type: asset.type,
        role: "primary",
        ...(Number.isInteger(Number(asset.id)) && Number(asset.id) > 0 ? { asset_id: Number(asset.id) } : {}),
      }];
      const item = {
        asset_url: url,
        source_type: asset.type,
        fallback_image: asset.type === "video" ? (asset.thumb || asset.preview_url || null) : null,
        sources,
        workspace_snapshot_v3: {
          ...buildReverseOperationRequestSnapshotV3({
            creationMode,
            subjectMode: "general",
            target: category,
            selected: asset,
            assets: selectedAssets,
            sources,
            videoAnalysisPreset: normalizedConfig.analysis_precision,
            reverseConfig: normalizedConfig,
          }),
          source_signature: assetSignature(asset),
          model_selections: effectiveModelSelections,
          ...(selectedVisionModelConfigId ? { model_config_id: selectedVisionModelConfigId } : {}),
        },
      };
      return buildReverseBatchItemPayload(
        item,
        itemOverrides[unifiedAssetKey(asset) || assetReferenceUrl(asset)],
        overrideCapability,
      );
    });
    try {
      const batchRequest = {
        client_request_id: clientRequestId,
        name: `${category === "video" ? "视频" : "图片"}批量反推 · ${selectedAssets.length} 项`,
        target: category,
        analysis_focus: normalizedConfig.analysis_focus,
        analysis_precision: normalizedConfig.analysis_precision,
        output_purpose: normalizedConfig.output_purpose,
        custom_instruction: normalizedConfig.custom_instruction || null,
        include_audio: normalizedConfig.include_audio,
        model_config_id: selectedVisionModelConfigId || null,
        items,
      };
      const confirmation = await requestQuoteConfirmation({
        kind: "reverse_batch",
        request: batchRequest,
        clientRequestId,
        execute: ({ request: confirmed }) => createReverseBatch(confirmed),
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("批量反推提交失败。");
        }
        return;
      }
      setMsg(`已创建 ${selectedAssets.length} 项批量反推，结果会逐项返回。`);
      notify.success("批量反推已开始");
      refreshMe();
    } catch (error) {
      const text = errorMessage(error, "创建批量反推失败");
      setMsg(text);
      notify.error(text);
    }
  }

  async function saveReverseBatchRecipes(operations) {
    const successful = (operations || []).filter((operation) => operation?.status === "succeeded");
    if (!successful.length) return;
    setReverseActionBusy("batch-recipes");
    try {
      const results = await Promise.allSettled(successful.map((operation, index) => {
        const payload = recipePayload(operation);
        const snapshot = payload.reverse_snapshot_v3 || {};
        const coverAssetUrl = snapshot.selected?.preview_url
          || snapshot.selected?.thumb
          || snapshot.selected?.url
          || null;
        return api.createCreationRecipe({
          title: `${operation.target === "video" ? "视频" : "图片"}批量反推配方 ${index + 1}`,
          category: operation.target === "video" ? "video" : "image",
          visibility: "private",
          favorite: false,
          source_operation_id: Number(operation.id),
          cover_asset_url: coverAssetUrl,
          payload,
        });
      }));
      const saved = results.filter((item) => item.status === "fulfilled").length;
      const failed = results.length - saved;
      setMsg(`批量保存完成：成功 ${saved}，失败 ${failed}。`);
      if (failed) notify.warn(`已保存 ${saved} 个配方，${failed} 个保存失败。`);
      else notify.success(`已保存 ${saved} 个创作配方`);
    } finally {
      setReverseActionBusy("");
    }
  }

  return {
    toggleBatchReverseAsset,
    removeBatchReverseAsset,
    startReverseBatch,
    saveReverseBatchRecipes,
  };
}
