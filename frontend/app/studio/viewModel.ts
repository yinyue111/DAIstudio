"use client";

import { CREATION_MODES, EDIT_STYLE_KEYS, RATIOS, creationModeLabel } from "./constants";
import {
  boundedImageCount,
  boundedVideoDuration,
  estimateImageCredits,
  estimateVideoFinalCredits,
  imageSizeFor,
  shouldUseImageReference,
  videoDurationLimit,
  videoRatioOptions,
} from "./helpers";
import { shouldBlockNewGeneration } from "./taskConcurrency";

export function studioCreationFacts({ creationMode, imageEditProductMode, editSubjectMode }) {
  const category = creationMode === "video" || creationMode === "video_edit" ? "video" : "image";
  const isEditMode = creationMode === "image_edit" || creationMode === "video_edit";
  const isImageEditMode = creationMode === "image_edit";
  const subjectMode = creationMode === "video_edit"
    ? (editSubjectMode === "portrait" ? "portrait" : "product")
    : imageEditProductMode
      ? (editSubjectMode === "portrait" ? "portrait" : "product")
      : "general";
  return {
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode: subjectMode === "product",
    portraitGenerationMode: subjectMode === "portrait",
  };
}

export function modelEnabledForConfig(cfg, kind) {
  if (!cfg) return true;
  const modelUse = kind === "video" || kind === "video_edit" ? "video" : "image";
  return cfg.models?.[modelUse]?.enabled !== false;
}

export function buildStudioDerivedViewState({
  cfg,
  creationMode,
  category,
  isEditMode,
  isImageEditMode,
  subjectMode,
  productGenerationMode,
  portraitGenerationMode,
  task,
  submitting,
  selected,
  productAsset,
  structured,
  prompt,
  ratio,
  imageQuality,
  n,
  vDuration,
  vResolution,
  videoAnalysisPreset,
}) {
  const blockingGeneration = shouldBlockNewGeneration(task, category);
  const running = blockingGeneration;
  const reverseCost = cfg?.models?.vision?.cost_credits || 0;
  const reverseImageCost = cfg?.reverse?.image_cost ?? reverseCost;
  const reverseVideoPresets = Array.isArray(cfg?.reverse?.video_presets) ? cfg.reverse.video_presets : [];
  const reverseVideoPreset = reverseVideoPresets.find((p) => p.key === videoAnalysisPreset) || reverseVideoPresets[0] || null;
  const reverseVideoFrameCount = Number(reverseVideoPreset?.max_frames || cfg?.reverse?.video_frame_count || 1);
  const reverseVideoMaxCost = reverseVideoPreset?.max_cost ?? cfg?.reverse?.video_max_cost ?? (reverseCost * reverseVideoFrameCount);
  const selectedReverseCost = selected?.type === "video" ? reverseVideoMaxCost : reverseImageCost;
  const selectedReverseCostLabel = selected?.type === "video" && reverseVideoFrameCount > 1
    ? `${selectedReverseCost}积分(最多${reverseVideoFrameCount}帧)`
    : `${selectedReverseCost}积分`;
  const reverseEnabled = cfg?.features?.reverse_prompt_enabled !== false;
  const ratioOptions = category === "video" ? videoRatioOptions() : RATIOS;
  const ratioOption = ratioOptions.find((r) => r.key === ratio) || ratioOptions[0];
  const maxImageN = Number(cfg?.image_n_max || 8);
  const imageCount = boundedImageCount(n, maxImageN);
  const maxVideoDuration = videoDurationLimit(cfg?.video_duration_max_seconds);
  const videoDuration = boundedVideoDuration(vDuration, maxVideoDuration);
  const currentImageSize = imageSizeFor(ratioOption, imageQuality, cfg?.image_size_max_dim || 2048);
  const taskParams = task?.params || {};
  const imageReferenceSource = isEditMode ? productAsset : selected;
  const usesImageReference = shouldUseImageReference({
    sourceAsset: imageReferenceSource,
    isEditMode,
    structured,
  });
  const videoFinalCost = Number(task?.final_cost_estimate || 0)
    || estimateVideoFinalCredits(cfg, {
      resolution: taskParams.target_resolution || taskParams.resolution || vResolution,
      duration: taskParams.target_duration || taskParams.duration || videoDuration,
    });
  const estCost = category === "image"
    ? estimateImageCredits(cfg, {
        size: currentImageSize,
        count: imageCount,
        edit: isEditMode || usesImageReference,
      })
    : videoFinalCost;
  const currentModelEnabled = modelEnabledForConfig(cfg, creationMode);
  const currentGatewayMock = cfg?.gateways?.[category]?.mock_mode ?? cfg?.mock_mode;
  const gatewayStatus = cfg === null
    ? "检测生成网关中"
    : !currentModelEnabled
      ? `${creationModeLabel(creationMode)}模型未启用`
      : currentGatewayMock
        ? `${creationModeLabel(creationMode)}演示模式 · 占位素材`
        : `${creationModeLabel(creationMode)}网关已连接`;

  const promptPlaceholder = isEditMode
    ? (isImageEditMode
        ? (portraitGenerationMode
            ? "描述要把这张人像生成成什么风格：证件照、写真、职业形象、小红书封面、广告场景、光线和妆造… 人物身份会强保护。⌘/Ctrl + Enter 生成"
            : productGenerationMode
              ? "描述产品图要生成成什么商业素材：电商主图、小红书种草、广告场景、背景光线、卖点氛围… 产品身份会强保护。⌘/Ctrl + Enter 生成"
              : "描述要如何编辑这张图：替换背景、增加文案、调整光线、改变风格、保留主体细节… ⌘/Ctrl + Enter 生成")
        : portraitGenerationMode
          ? `${creationModeLabel(creationMode)}：先在右侧选择目标视频/风格参考并反推，再上传人物照片；这里可补充服装、动作、镜头和身份保留要求…`
          : `${creationModeLabel(creationMode)}：先在右侧选择风格参考并反推，再上传产品主体图；这里可补充必须保留或强化的卖点…`)
    : category === "video"
      ? "描述你想要的视频：主体 / 动作 / 镜头运动 / 光线 / 节奏… ⌘/Ctrl + Enter 生成"
      : "描述你想要的画面：主体 / 风格 / 光线 / 色调 / 构图… ⌘/Ctrl + Enter 生成";
  const editStyleKeys = (EDIT_STYLE_KEYS[category] || EDIT_STYLE_KEYS.image)
    .filter((key) => String(structured?.[key] || "").trim());
  const promptReady = Boolean(String(prompt || "").trim() || editStyleKeys.length);
  const editReadySteps = isImageEditMode
    ? [
        { label: portraitGenerationMode ? "人像照片" : (productGenerationMode ? "产品图" : "编辑源"), ready: Boolean(productAsset), readyText: "已上传", pendingText: "待上传" },
        { label: portraitGenerationMode ? "写真要求" : (productGenerationMode ? "生产要求" : "编辑要求"), ready: promptReady, readyText: "已填写", pendingText: "待填写" },
        { label: "风格参考", ready: Boolean(selected), readyText: "已选择", pendingText: "可选" },
      ]
    : [
        { label: portraitGenerationMode ? "目标视频/风格" : "风格参考", ready: Boolean(selected), readyText: "已选择", pendingText: "待选择" },
        { label: portraitGenerationMode ? "人物照片" : "产品主体", ready: Boolean(productAsset), readyText: "已上传", pendingText: "待上传" },
        { label: "风格提示", ready: promptReady, readyText: "已就绪", pendingText: "待补充" },
      ];
  const submitLabel = submitting || running
    ? "生成中…"
    : creationMode === "video"
      ? "生成成片 ▶"
      : creationMode === "video_edit"
        ? "生成成片 ▶"
        : creationMode === "image_edit"
          ? (portraitGenerationMode ? "人像生成 ✦" : productGenerationMode ? "产品生成 ✦" : "编辑生成 ✦")
          : "立即生成 ✦";

  return {
    creationModes: CREATION_MODES,
    blockingGeneration,
    running,
    reverseVideoPresets,
    selectedReverseCost,
    selectedReverseCostLabel,
    reverseEnabled,
    ratioOptions,
    ratioOption,
    maxImageN,
    imageCount,
    maxVideoDuration,
    videoDuration,
    currentImageSize,
    videoFinalCost,
    estCost,
    currentModelEnabled,
    gatewayStatus,
    promptPlaceholder,
    editStyleKeys,
    editReadySteps,
    submitLabel,
    subjectMode,
  };
}
