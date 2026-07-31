"use client";

import type { AppConfig, Asset, Category, CreationModeKey, ProductPixelLockMode, ProductVideoStrategy, ReverseVideoAnalysis, SubjectMode, Task } from "../../lib/types";
import type { GenerationQuotePayload } from "../../lib/api";
import { MAX_PRODUCT_DETAIL_IMAGES, RATIOS } from "./constants";
import { buildEditNegativePrompt, buildEditPrompt } from "./editPrompt";
import {
  assetDims,
  assetSignature,
  boundedImageCount,
  boundedVideoDuration,
  buildSourceAssetMeta,
  composeEvidenceBackedVideoTransferPrompt,
  composePromptFromStructured,
  imageSizeFor,
  shouldUseImageReference,
  styleTransferStructured,
  visualStructuredFields,
  visualStructuredFieldOrder,
  videoDurationLimit,
  videoRatioOptions,
} from "./helpers";
import { normalizeProductVideoStrategyKey } from "./productVideoStrategy";

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
    optimizer_model_config_id?: number | null;
    optimization_direction?: string;
    optimization_kind?: string;
    compiler_metadata?: Record<string, unknown> | null;
    change_summary?: string[];
    warnings?: string[];
  };
  negative?: string;
  promptDirty?: boolean;
  promptSourceSignature?: string;
  selected?: Asset | null;
  lastFrameAsset?: Asset | null;
  firstLastFrameEnabled?: boolean;
  productAsset?: Asset | null;
  productDetailAssets?: Asset[];
  productProfile?: { structured?: Record<string, unknown>; final_text?: string } | null;
  productProfileSource?: string;
  variationSource?: Asset | null;
  structured?: Record<string, unknown>;
  structuredSource?: string;
  reverseVideoAnalysis?: ReverseVideoAnalysis | null;
  analysisFocus?: string;
  ratio: string;
  imageQuality: string;
  n: number | string;
  seed?: string;
  editMaskMode?: "protect_subject" | "center_box" | "off";
  productPixelLockMode?: ProductPixelLockMode;
  vDuration: number | string;
  vResolution: string;
  providerVideoEditMode?: boolean;
  productVideoTemplate?: ProductVideoStrategy | string;
  modelConfigId?: number | null;
  reverseOperationId?: number | null;
  reverseRevisionId?: number | null;
}

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

export function orderedProductDetailUrls(
  productAsset: Asset | null | undefined,
  details: Asset[] | string[] | null | undefined,
): string[] {
  const mainUrl = cleanText(productAsset?.url);
  const rows = Array.isArray(details) ? details : [];
  if (rows.length > 0 && !mainUrl) throw new Error("请先选择产品主题图，再添加产品细节图。");
  if (rows.length > MAX_PRODUCT_DETAIL_IMAGES) {
    throw new Error(`产品细节图最多 ${MAX_PRODUCT_DETAIL_IMAGES} 张，请移除后再生成。`);
  }
  const urls = rows.map((item) => cleanText(typeof item === "string" ? item : item?.url));
  if (urls.some((url) => !url)) throw new Error("产品细节图存在不可用素材，请移除后重新选择。");
  if (rows.some((item) => typeof item !== "string" && item?.type && item.type !== "image")) {
    throw new Error("产品细节图只支持图片素材。");
  }
  const unique = new Set(urls);
  if (unique.size !== urls.length || (mainUrl && unique.has(mainUrl))) {
    throw new Error("产品主题图和细节图不能重复，请调整后再生成。");
  }
  return urls;
}

function promptInputParts(value: BuildGenerationPayloadInput["prompt"]) {
  if (value && typeof value === "object") {
    const text = cleanText(value.text || value.optimized_text || value.raw_text);
    return {
      text,
      rawText: cleanText(value.raw_text || text),
      optimizedText: cleanText(value.optimized_text),
      optimizerModelId: cleanText(value.optimizer_model_id),
      optimizerModelConfigId: Number(value.optimizer_model_config_id) || null,
      optimizationDirection: cleanText(value.optimization_direction),
      optimizationKind: cleanText(value.optimization_kind),
      compilerMetadata: value.compiler_metadata && typeof value.compiler_metadata === "object"
        ? { ...value.compiler_metadata }
        : null,
      changeSummary: Array.isArray(value.change_summary) ? value.change_summary.map(cleanText).filter(Boolean) : [],
      warnings: Array.isArray(value.warnings) ? value.warnings.map(cleanText).filter(Boolean) : [],
    };
  }
  const text = cleanText(value);
  return {
    text,
    rawText: text,
    optimizedText: "",
    optimizerModelId: "",
    optimizerModelConfigId: null,
    optimizationDirection: "",
    optimizationKind: "",
    compilerMetadata: null,
    changeSummary: [],
    warnings: [],
  };
}

function buildSubjectProfileText(
  profile: { structured?: Record<string, unknown>; final_text?: string } | null | undefined,
  target: "product_profile" | "portrait_profile",
): string {
  if (!profile) return "";
  const structured = profile.structured || {};
  const visual = visualStructuredFields(structured, target);
  const parts: string[] = [];
  let used = 0;
  for (const key of visualStructuredFieldOrder(target)) {
    const text = cleanText(visual[key]).slice(0, 72).replace(/[。；;，,、\s]+$/g, "");
    if (!text) continue;
    const part = `${key}: ${text}`;
    const separator = parts.length ? 1 : 0;
    if (used + separator + part.length > 420) continue;
    parts.push(part);
    used += separator + part.length;
  }
  return parts.join("；");
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
  lastFrameAsset = null,
  firstLastFrameEnabled = false,
  productAsset = null,
  productDetailAssets = [],
  productProfile = null,
  productProfileSource = "",
  variationSource = null,
  structured = {},
  structuredSource = "",
  reverseVideoAnalysis = null,
  analysisFocus = "",
  ratio,
  imageQuality,
  n,
  seed = "",
  editMaskMode = "protect_subject",
  productPixelLockMode = "auto",
  vDuration,
  vResolution,
  providerVideoEditMode = false,
  productVideoTemplate = "prompt_driven",
  modelConfigId = null,
  reverseOperationId = null,
  reverseRevisionId = null,
}: BuildGenerationPayloadInput) {
  const isFinal = stage === "final" && Boolean(task);
  const effCategory = isFinal ? task.category : category;
  const parentParams = isFinal ? (task?.params || {}) : {};
  const finalRatioKey = parentParams.target_ratio || parentParams.ratio || ratio;
  const finalResolution = parentParams.target_resolution || parentParams.resolution || vResolution;
  const finalDuration = parentParams.target_duration || parentParams.duration || vDuration;
  const maxVideoDuration = videoDurationLimit(cfg?.video_duration_max_seconds);
  const minVideoDuration = Number(cfg?.video_duration_min_seconds) || 1;
  const ratioPool = effCategory === "video" ? videoRatioOptions() : RATIOS;
  const ratioOption = ratioPool.find((r) => r.key === finalRatioKey) || ratioPool[0];
  const imageSize = imageSizeFor(ratioOption, imageQuality, cfg?.image_size_max_dim || 2048);
  const parentProductMode = Boolean(
    isFinal
    && (
      cleanText(parentParams.subject_mode) === "product"
      || cleanText(parentParams.product_reference_image)
    )
  );
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
  const requestedLastFrameAsset = (
    !isFinal
    && effCategory === "video"
    && creationMode === "video"
    && subjectMode === "general"
    && firstLastFrameEnabled
  ) ? lastFrameAsset : null;
  const firstFrameUrl = isFinal
    ? cleanText(parentParams.first_frame_image || parentParams.reference_image_url)
    : cleanText(refImage);
  const lastFrameUrl = isFinal
    ? cleanText(parentParams.last_frame_image)
    : cleanText(requestedLastFrameAsset?.url);
  if (requestedLastFrameAsset) {
    if (requestedLastFrameAsset.type !== "image") throw new Error("视频尾帧只支持图片素材。");
    if (!lastFrameUrl) throw new Error("视频尾帧素材不可用，请重新上传或选择。");
  }
  const firstLastFramePair = effCategory === "video" && Boolean(lastFrameUrl);
  if (firstLastFramePair) {
    if (!firstFrameUrl) throw new Error("请先上传视频首帧，再添加尾帧。");
    if (!isFinal && sourceAsset?.type !== "image") throw new Error("视频首尾帧模式的主素材必须是图片。");
    if (firstFrameUrl === lastFrameUrl) throw new Error("视频首帧和尾帧不能使用同一张图片。");
  }
  const styleReferenceAsset = variationSource || selected;
  const styleReferenceUrl = (isEditMode || directProductVideo) && styleReferenceAsset
    ? (styleReferenceAsset.type === "video" ? styleReferenceAsset.thumb : styleReferenceAsset.url)
    : null;
  const productDetailUrls = orderedProductDetailUrls(
    isFinal ? ({ url: cleanText(parentParams.product_reference_image), type: "image" } as Asset) : productAsset,
    isFinal ? (parentParams.product_detail_images as string[] | undefined) : productDetailAssets,
  );
  const parentSubjectMode = cleanText(parentParams.subject_mode);
  const {
    effectiveSubjectMode,
    portraitMode,
    productMode,
    subjectModeParam,
  } = isFinal
    ? {
        effectiveSubjectMode: parentProductMode
          ? "product"
          : parentSubjectMode === "portrait" ? "portrait" : "",
        portraitMode: !parentProductMode && parentSubjectMode === "portrait",
        productMode: parentProductMode,
        subjectModeParam: parentProductMode
          ? "product"
          : parentSubjectMode === "portrait" ? "portrait" : "",
      }
    : effectiveSubjectFlags({ isEditMode, directProductVideo, subjectMode });
  const requestedProductVideoTemplate = isFinal
    ? (parentParams.product_video_template ?? "prompt_driven")
    : productVideoTemplate;
  const normalizedProductVideoTemplate = productMode
    ? normalizeProductVideoStrategyKey(requestedProductVideoTemplate)
    : null;
  if (productMode && !normalizedProductVideoTemplate) {
    throw new Error("产品视频策略无效，请重新选择后再生成。");
  }
  const productLockMode = isFinal
    ? (cleanText(parentParams.product_lock_mode) || "locked")
    : "locked";
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
  const subjectProfileTarget = portraitMode ? "portrait_profile" : "product_profile";
  const subjectProfileText = buildSubjectProfileText(effectiveProductProfile, subjectProfileTarget);
  const subjectProfileNegative = cleanText(effectiveProductProfile?.structured?.["负向"]);
  const effectiveEditNegative = [...new Set(
    [editNegative, subjectProfileNegative].map(cleanText).filter(Boolean),
  )].join("，");
  const subjectProfileLabel = portraitMode ? "人物身份档案" : "产品身份档案";
  const effectiveStructured = (
    structuredSource && structuredSource !== styleSignature
      ? {}
      : structured
  );
  const structuredVisualTarget = (
    effCategory === "video" && reverseVideoAnalysis?.analysis_mode === "image_motion"
      ? "image_to_video"
      : effCategory
  );
  const promptStructured = visualStructuredFields(
    isEditMode
      ? styleTransferStructured(effectiveStructured, { video: effCategory === "video", subject: effectiveSubjectMode })
      : effectiveStructured,
    structuredVisualTarget,
  );
  const promptSourceStale = Boolean(promptSourceSignature && promptSourceSignature !== styleSignature && !promptDirty);
  const promptParts = promptInputParts(prompt);
  const promptText = promptSourceStale ? "" : promptParts.text;
  const transferPromptText = (
    isEditMode && effCategory === "video" && (productMode || portraitMode)
      ? composeEvidenceBackedVideoTransferPrompt(
          effectiveStructured,
          reverseVideoAnalysis,
          effectiveSubjectMode,
        )
      : ""
  );
  const structuredText = transferPromptText || composePromptFromStructured(
    promptStructured,
    "",
    { target: structuredVisualTarget },
  );
  const baseFinalText = (
    promptDirty
      ? (promptText || structuredText)
      : (
          isEditMode && effCategory === "video" && (productMode || portraitMode)
            ? (transferPromptText || promptText || structuredText)
            : (promptText || structuredText)
        )
  ) || "生成同风格的新素材";
  const finalText = isEditMode && effCategory === "image"
    ? buildEditPrompt(baseFinalText, {
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
    analysisFocus,
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
      subject_profile_summary: subjectProfileText,
    } : {}),
    style_reference: styleReferenceAsset ? buildSourceAssetMeta(styleReferenceAsset) : null,
    ...(requestedLastFrameAsset ? { last_frame: buildSourceAssetMeta(requestedLastFrameAsset) } : {}),
    product_subject: productAsset ? buildSourceAssetMeta(productAsset) : null,
    product_details: productDetailAssets.map(buildSourceAssetMeta),
    ...(variationSource ? {
      variation_source: buildSourceAssetMeta(variationSource),
      variation_of_asset_id: variationSource.id || null,
    } : {}),
  } : null;

  const payload: GenerationQuotePayload = {
    ...(modelConfigId ? { model_config_id: Number(modelConfigId) } : {}),
    ...(Number(reverseOperationId) > 0 && Number(reverseRevisionId) > 0 ? {
      reverse_operation_id: Number(reverseOperationId),
      reverse_revision_id: Number(reverseRevisionId),
    } : {}),
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
      ...(promptParts.optimizedText ? {
        input_mode: "optimized",
        raw_text: promptParts.rawText || promptText,
        optimized_text: promptParts.optimizedText,
        ...(promptParts.optimizerModelId ? { optimizer_model_id: promptParts.optimizerModelId } : {}),
        ...(promptParts.optimizerModelConfigId ? { optimizer_model_config_id: promptParts.optimizerModelConfigId } : {}),
        ...(promptParts.optimizationDirection ? { optimization_direction: promptParts.optimizationDirection } : {}),
        ...(promptParts.optimizationKind ? { optimization_kind: promptParts.optimizationKind } : {}),
        ...(promptParts.compilerMetadata ? { compiler_metadata: promptParts.compilerMetadata } : {}),
        ...(promptParts.changeSummary.length ? { optimization_change_summary: promptParts.changeSummary } : {}),
        ...(promptParts.warnings.length ? { optimization_warnings: promptParts.warnings } : {}),
        ...(effCategory === "video" ? { assembled_text: finalText } : {}),
      } : effCategory === "video" ? {
        input_mode: promptParts.optimizedText
          ? "optimized"
          : (promptDirty ? "direct_input" : "structured_reverse"),
        raw_text: promptParts.rawText || promptText,
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
            ...(effectiveEditNegative ? { negative_prompt: effectiveEditNegative } : {}),
          }
        : providerVideoEditMode ? {} : {
            duration: boundedVideoDuration(finalDuration, maxVideoDuration, minVideoDuration),
            resolution: finalResolution,
            target_resolution: finalResolution,
            ratio: ratioOption.key,
            ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
            ...(firstLastFramePair
              ? { first_frame_image: firstFrameUrl, last_frame_image: lastFrameUrl }
              : (productMode && refImage
                  ? { product_reference_image: refImage }
                  : (isFinal && parentParams.product_reference_image
                      ? { product_reference_image: parentParams.product_reference_image }
                      : (refImage && !portraitMode ? { reference_image_url: refImage } : {})))),
            ...(productDetailUrls.length ? { product_detail_images: productDetailUrls } : {}),
            ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
            ...(portraitMode && refImage ? { character_reference_image: refImage } : {}),
            ...(subjectModeParam ? { subject_mode: subjectModeParam } : {}),
            ...((isFinal || isEditMode || directProductVideo) && productMode ? {
              product_lock_mode: productLockMode,
              product_video_template: normalizedProductVideoTemplate,
            } : {}),
            ...(effectiveEditNegative ? { negative_prompt: effectiveEditNegative } : {}),
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
