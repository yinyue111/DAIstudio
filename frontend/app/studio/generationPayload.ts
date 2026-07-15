"use client";

import type { AppConfig, Asset, Category, CreationModeKey, ProductPixelLockMode, SubjectMode, Task } from "../../lib/types";
import { RATIOS } from "./constants";
import { buildEditNegativePrompt, buildEditPrompt } from "./editPrompt";
import {
  assetDims,
  assetSignature,
  boundedImageCount,
  boundedVideoDuration,
  buildSourceAssetMeta,
  composePromptFromStructured,
  composeSafeVideoTransferPrompt,
  composeStyleTransferPrompt,
  imageSizeFor,
  shouldUseImageReference,
  styleTransferStructured,
  videoDurationLimit,
  videoRatioOptions,
} from "./helpers";

function effectiveSubjectFlags({ isEditMode, directProductVideo, subjectMode }) {
  const effectiveSubjectMode = isEditMode || directProductVideo ? subjectMode : "";
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
  prompt?: string | {
    text?: string;
    raw_text?: string;
    optimized_text?: string;
    optimizer_model_id?: string;
  };
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
  productPixelLockMode?: ProductPixelLockMode;
  vDuration: number | string;
  vResolution: string;
}

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

function promptInputParts(value: BuildGenerationPayloadInput["prompt"]) {
  if (value && typeof value === "object") {
    const text = cleanText(value.text || value.optimized_text || value.raw_text);
    return {
      text,
      rawText: cleanText(value.raw_text || text),
      optimizedText: cleanText(value.optimized_text),
      optimizerModelId: cleanText(value.optimizer_model_id),
    };
  }
  const text = cleanText(value);
  return { text, rawText: text, optimizedText: "", optimizerModelId: "" };
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
  productPixelLockMode = "auto",
  vDuration,
  vResolution,
}: BuildGenerationPayloadInput) {
  const isFinal = stage === "final" && Boolean(task);
  const effCategory = isFinal ? task.category : category;
  const parentParams = isFinal ? (task?.params || {}) : {};
  const finalRatioKey = parentParams.target_ratio || parentParams.ratio || ratio;
  const finalResolution = parentParams.target_resolution || parentParams.resolution || vResolution;
  const finalDuration = parentParams.target_duration || parentParams.duration || vDuration;
  const maxVideoDuration = videoDurationLimit(cfg?.video_duration_max_seconds);
  const ratioPool = effCategory === "video" ? videoRatioOptions() : RATIOS;
  const ratioOption = ratioPool.find((r) => r.key === finalRatioKey) || ratioPool[0];
  const imageSize = imageSizeFor(ratioOption, imageQuality, cfg?.image_size_max_dim || 2048);
  const directProductVideo = Boolean(
    !isFinal
    && effCategory === "video"
    && creationMode === "video"
    && subjectMode === "product"
    && productAsset,
  );
  const sourceAsset = isFinal ? null : (isEditMode || directProductVideo ? productAsset : selected);
  const dims = sourceAsset ? assetDims(sourceAsset) : null;
  const refImage = sourceAsset ? (sourceAsset.type === "video" ? sourceAsset.thumb : sourceAsset.url) : null;
  const styleReferenceAsset = variationSource || selected;
  const styleReferenceUrl = (isEditMode || directProductVideo) && styleReferenceAsset
    ? (styleReferenceAsset.type === "video" ? styleReferenceAsset.thumb : styleReferenceAsset.url)
    : null;
  const {
    effectiveSubjectMode,
    portraitMode,
    productMode,
    subjectModeParam,
  } = effectiveSubjectFlags({ isEditMode, directProductVideo, subjectMode });
  const editNegative = effCategory === "video"
    ? cleanText(negative)
    : buildEditNegativePrompt(negative, {
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
  const promptParts = promptInputParts(prompt);
  const promptText = promptSourceStale ? "" : promptParts.text;
  const transferPromptText = (
    isEditMode && effCategory === "video" && (productMode || portraitMode)
      ? composeSafeVideoTransferPrompt(effectiveStructured, effectiveSubjectMode)
      : ""
  );
  const structuredText = isEditMode
    ? (
        effCategory === "video" && (productMode || portraitMode)
          ? (transferPromptText || composePromptFromStructured(promptStructured))
          : composeStyleTransferPrompt(effectiveStructured, "", { video: effCategory === "video", subject: effectiveSubjectMode })
      )
    : composePromptFromStructured(effectiveStructured);
  const baseFinalText = (
    promptDirty
      ? (promptText || structuredText)
      : (
          isEditMode && effCategory === "video" && (productMode || portraitMode)
            ? (transferPromptText || structuredText || promptText)
            : (promptText || structuredText)
        )
  ) || "生成同风格的新素材";
  const profiledBaseFinalText = (
    isEditMode
    && effCategory === "image"
    && (productMode || portraitMode)
    && subjectProfileText
      ? [
          `${subjectProfileLabel}: ${subjectProfileText}`,
          baseFinalText,
        ].filter(Boolean).join("；")
      : baseFinalText
  );
  const finalText = isEditMode && effCategory === "image"
    ? buildEditPrompt(profiledBaseFinalText, {
      video: false,
      hasStyleReference: Boolean(styleReferenceAsset),
      generalEdit: isImageEditMode && effectiveSubjectMode === "general",
      subject: effectiveSubjectMode,
    })
    : baseFinalText;

  const useRefImage = shouldUseImageReference({
    isFinal,
    sourceAsset,
    isEditMode,
    structured: effectiveStructured,
  });
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
      ...(promptDirty && promptText
        ? { user_instruction: promptText }
        : (
            isEditMode && effCategory === "video" && (productMode || portraitMode) && finalText
              ? { user_instruction: finalText }
              : {}
          )),
      ...(effCategory === "video" ? {
        input_mode: promptParts.optimizedText
          ? "optimized"
          : (promptDirty ? "direct_input" : "structured_reverse"),
        raw_text: promptParts.rawText || promptText,
        ...(promptParts.optimizedText ? { optimized_text: promptParts.optimizedText } : {}),
        ...(promptParts.optimizerModelId ? { optimizer_model_id: promptParts.optimizerModelId } : {}),
        assembled_text: finalText,
      } : {}),
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
            ...(useRefImage && refImage ? { reference_image_url: refImage } : {}),
            ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
            ...(portraitMode && refImage ? { character_reference_image: refImage } : {}),
            ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
            ...(variationSource?.id ? { variation_of_asset_id: Number(variationSource.id) } : {}),
            ...(isImageEditMode && productMode ? { edit_mask_mode: editMaskMode || "protect_subject" } : {}),
            ...(isImageEditMode && productMode ? { product_pixel_lock: editMaskMode === "off" ? "off" : (productPixelLockMode || "auto") } : {}),
            ...(editNegative ? { negative_prompt: editNegative } : {}),
          }
        : {
            duration: boundedVideoDuration(finalDuration, maxVideoDuration),
            resolution: finalResolution,
            target_resolution: finalResolution,
            ratio: ratioOption.key,
            ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
            ...(productMode && refImage
              ? { product_reference_image: refImage }
              : (refImage ? { reference_image_url: refImage } : {})),
            ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
            ...(portraitMode && refImage ? { character_reference_image: refImage } : {}),
            ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
            ...((isEditMode || directProductVideo) && productMode ? {
              product_lock_mode: "locked",
              product_video_template: "prompt_driven",
            } : {}),
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
