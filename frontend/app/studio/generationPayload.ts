"use client";

import type { AppConfig, Asset, Category, CreationModeKey, SubjectMode, Task } from "../../lib/types";
import { RATIOS } from "./constants";
import { buildEditNegativePrompt, buildEditPrompt } from "./editPrompt";
import {
  assetDims,
  assetSignature,
  boundedImageCount,
  boundedVideoDuration,
  buildSourceAssetMeta,
  composePromptFromStructured,
  composeStyleTransferPrompt,
  imageSizeFor,
  styleTransferStructured,
  videoDurationLimit,
  videoRatioOptions,
} from "./helpers";

function effectiveSubjectFlags({ isEditMode, subjectMode }) {
  const effectiveSubjectMode = isEditMode ? subjectMode : "";
  const portraitMode = effectiveSubjectMode === "portrait";
  const productMode = effectiveSubjectMode === "product";
  return {
    effectiveSubjectMode,
    portraitMode,
    productMode,
    subjectModeParam: portraitMode || productMode ? effectiveSubjectMode : "",
  };
}

type GenerationStage = "preview" | "final";

interface BuildGenerationPayloadInput {
  stage?: GenerationStage;
  task?: Task | null;
  cfg?: Partial<AppConfig> | null;
  category: Category;
  creationMode: CreationModeKey;
  isEditMode?: boolean;
  isImageEditMode?: boolean;
  subjectMode?: SubjectMode | "general";
  prompt?: string;
  negative?: string;
  promptDirty?: boolean;
  promptSourceSignature?: string;
  selected?: Asset | null;
  productAsset?: Asset | null;
  productProfile?: { structured?: Record<string, unknown>; final_text?: string } | null;
  productProfileSource?: string;
  variationSource?: Asset | null;
  structured?: Record<string, unknown>;
  structuredSource?: string;
  ratio: string;
  imageQuality: string;
  n: number | string;
  seed?: string;
  editMaskMode?: "protect_subject" | "center_box" | "off";
  vDuration: number | string;
  vResolution: string;
  videoProductLockMode?: "free" | "locked";
}

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

function productProfileText(profile: { structured?: Record<string, unknown>; final_text?: string } | null | undefined): string {
  if (!profile) return "";
  const direct = cleanText(profile.final_text || profile.structured?.final_text);
  if (direct) return direct;
  const structured = profile.structured || {};
  const keys = [
    "档案类型", "产品品类", "品牌Logo", "包装文字", "包装结构", "主色材质",
    "形状比例", "关键图案", "卖点摘要", "展示角度", "主角约束", "不可改项",
  ];
  return keys
    .map((key) => {
      const text = cleanText(structured[key]);
      return text ? `${key}: ${text}` : "";
    })
    .filter(Boolean)
    .join("；");
}

export function buildGenerationPayload({
  stage = "preview",
  task = null,
  cfg = null,
  category,
  creationMode,
  isEditMode = false,
  isImageEditMode = false,
  subjectMode = "general",
  prompt = "",
  negative = "",
  promptDirty = false,
  promptSourceSignature = "",
  selected = null,
  productAsset = null,
  productProfile = null,
  productProfileSource = "",
  variationSource = null,
  structured = {},
  structuredSource = "",
  ratio,
  imageQuality,
  n,
  seed = "",
  editMaskMode = "protect_subject",
  vDuration,
  vResolution,
  videoProductLockMode = "free",
}: BuildGenerationPayloadInput) {
  const isFinal = stage === "final" && task;
  const effCategory = isFinal ? task.category : category;
  const parentParams = isFinal ? (task?.params || {}) : {};
  const finalRatioKey = parentParams.target_ratio || parentParams.ratio || ratio;
  const finalResolution = parentParams.target_resolution || parentParams.resolution || vResolution;
  const finalDuration = parentParams.target_duration || parentParams.duration || vDuration;
  const maxVideoDuration = videoDurationLimit(cfg?.video_duration_max_seconds);
  const ratioPool = effCategory === "video" ? videoRatioOptions() : RATIOS;
  const ratioOption = ratioPool.find((r) => r.key === finalRatioKey) || ratioPool[0];
  const imageSize = imageSizeFor(ratioOption, imageQuality, cfg?.image_size_max_dim || 2048);
  const sourceAsset = isFinal ? null : (isEditMode ? productAsset : selected);
  const dims = sourceAsset ? assetDims(sourceAsset) : null;
  const refImage = sourceAsset ? (sourceAsset.type === "video" ? sourceAsset.thumb : sourceAsset.url) : null;
  const styleReferenceAsset = variationSource || selected;
  const styleReferenceUrl = isEditMode && styleReferenceAsset
    ? (styleReferenceAsset.type === "video" ? styleReferenceAsset.thumb : styleReferenceAsset.url)
    : null;
  const {
    effectiveSubjectMode,
    portraitMode,
    productMode,
    subjectModeParam,
  } = effectiveSubjectFlags({ isEditMode, subjectMode });
  const editNegative = buildEditNegativePrompt(negative, {
    productMode,
    portraitMode,
    editMode: isEditMode,
  });
  const styleSignature = assetSignature(selected);
  const productSignature = assetSignature(productAsset);
  const effectiveProductProfile = (
    productAsset
    && productProfile
    && productProfileSource
    && productProfileSource === productSignature
      ? productProfile
      : null
  );
  const subjectProfileText = productProfileText(effectiveProductProfile);
  const subjectProfileLabel = portraitMode ? "人物身份档案" : "产品身份档案";
  const effectiveStructured = (
    structuredSource && structuredSource !== styleSignature
      ? {}
      : structured
  );
  const promptStructured = isEditMode
    ? styleTransferStructured(effectiveStructured, { video: effCategory === "video", subject: effectiveSubjectMode })
    : effectiveStructured;
  const promptSourceStale = Boolean(promptSourceSignature && promptSourceSignature !== styleSignature && !promptDirty);
  const promptText = promptSourceStale ? "" : String(prompt || "").trim();
  const structuredText = isEditMode
    ? composeStyleTransferPrompt(effectiveStructured, "", { video: effCategory === "video", subject: effectiveSubjectMode })
    : composePromptFromStructured(effectiveStructured);
  const baseFinalText = (
    promptDirty
      ? [structuredText, promptText].filter(Boolean).join("；")
      : (structuredText || promptText)
  ) || "生成同风格的新素材";
  const profiledBaseFinalText = (
    isEditMode
    && (productMode || portraitMode)
    && subjectProfileText
      ? [
          `${subjectProfileLabel}: ${subjectProfileText}`,
          baseFinalText,
        ].filter(Boolean).join("；")
      : baseFinalText
  );
  const finalText = isEditMode
    ? buildEditPrompt(profiledBaseFinalText, {
      video: effCategory === "video",
      hasStyleReference: Boolean(styleReferenceAsset),
      generalEdit: isImageEditMode && effectiveSubjectMode === "general",
      subject: effectiveSubjectMode,
    })
    : baseFinalText;

  const useRefImage = !isFinal && sourceAsset && sourceAsset.type === "image" && (
    isEditMode || Object.keys(effectiveStructured).length === 0
  );
  const useRefVideo = !isFinal && sourceAsset && sourceAsset.type === "video" && Object.keys(effectiveStructured).length === 0;
  const sourceAssetMeta = sourceAsset ? {
    ...buildSourceAssetMeta(sourceAsset),
    mode: creationMode,
    product_generation_mode: productMode,
    portrait_generation_mode: portraitMode,
    ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
    ...(subjectProfileText ? {
      subject_profile_source: productProfileSource,
      subject_profile_summary: subjectProfileText.slice(0, 1200),
    } : {}),
    style_reference: styleReferenceAsset ? buildSourceAssetMeta(styleReferenceAsset) : null,
    product_subject: productAsset ? buildSourceAssetMeta(productAsset) : null,
    ...(variationSource ? {
      variation_source: buildSourceAssetMeta(variationSource),
      variation_of_asset_id: variationSource.id || null,
    } : {}),
  } : null;

  const payload = {
    source_asset_url: sourceAsset ? sourceAsset.url : null,
    source_type: sourceAsset ? sourceAsset.type : "image",
    source_asset_meta: sourceAssetMeta,
    category: effCategory,
    stage,
    parent_task_id: isFinal ? task.id : null,
    prompt: {
      ...(Object.keys(promptStructured).length ? promptStructured : {}),
      ...(subjectProfileText && (productMode || portraitMode) ? { [subjectProfileLabel]: subjectProfileText } : {}),
      ...(isEditMode && promptDirty && promptText ? { user_instruction: promptText } : {}),
      final_text: finalText,
      ...(useRefImage ? { instruction: finalText || promptText || "参考所选图生成同款风格的新素材" } : {}),
      ...(useRefVideo ? { instruction: promptText || "参考所选视频的主体、动作和镜头节奏生成同款视频" } : {}),
    },
    params:
      effCategory === "image"
        ? {
            n: boundedImageCount(n, cfg?.image_n_max || 8),
            size: imageSize,
            ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
            ...(seed !== "" ? { seed: Number(seed) } : {}),
            ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
            ...(portraitMode && refImage ? { character_reference_image: refImage } : {}),
            ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
            ...(variationSource?.id ? { variation_of_asset_id: Number(variationSource.id) } : {}),
            ...(isImageEditMode && productMode ? { edit_mask_mode: editMaskMode || "protect_subject" } : {}),
            ...(editNegative ? { negative_prompt: editNegative } : {}),
          }
        : {
            duration: boundedVideoDuration(finalDuration, maxVideoDuration),
            resolution: finalResolution,
            target_resolution: finalResolution,
            ratio: ratioOption.key,
            ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
            ...(refImage ? { reference_image_url: refImage } : {}),
            ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
            ...(portraitMode && refImage ? { character_reference_image: refImage } : {}),
            ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
            ...(isEditMode && productMode ? { product_lock_mode: videoProductLockMode === "locked" ? "locked" : "free" } : {}),
            ...(editNegative ? { negative_prompt: editNegative } : {}),
          },
  };

  return {
    payload,
    effCategory,
    ratioOption,
    imageSize,
    sourceAsset,
    effectiveStructured,
    finalText,
    subject: {
      mode: effectiveSubjectMode,
      portraitMode,
      productMode,
      subjectModeParam,
    },
  };
}
