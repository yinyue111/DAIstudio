import { assetReferenceUrl } from "../../lib/unifiedAssets";
import {
  modelRequirements,
  modelOptionSupports,
  resolveVideoSubjectImageInputMode,
  strictMultiReferenceLimit,
  VIDEO_IMAGE_INPUT_MODE,
  validateMultiReferenceSelection,
} from "./StudioModelSelector";
import { MAX_PRODUCT_DETAIL_IMAGES } from "./constants";
import { assetSignature, videoDurationLimit, videoDurationMinimum } from "./helpers";
import { resolveProductVideoStrategySelection } from "./productVideoStrategy";

export function videoModelRequiresFirstFrame(modelOption) {
  if (!modelOption) return false;
  const textRequirements = modelRequirements({
    use: "video",
    creationMode: "video",
    selected: null,
    productAsset: null,
    subjectMode: "general",
  });
  const firstFrameRequirements = modelRequirements({
    use: "video",
    creationMode: "video",
    selected: { type: "image" },
    productAsset: null,
    subjectMode: "general",
  });
  return (
    !modelOptionSupports(modelOption, textRequirements)
    && modelOptionSupports(modelOption, firstFrameRequirements)
  );
}

export function buildStudioModelContext({
  cfg,
  category,
  creationMode,
  subjectMode,
  productGenerationMode,
  selected,
  lastFrameAsset,
  productAsset,
  productDetailAssets = [],
  productVideoTemplate,
  generationModelOptions,
  selectedGenerationModel,
  reverseOperationId = null,
  reverseRevisionId = null,
  reverseSourceSignature = "",
}) {
  const selectedCapabilities = (
    selectedGenerationModel?.capabilities
    && !Array.isArray(selectedGenerationModel.capabilities)
  ) ? selectedGenerationModel.capabilities : {};
  const maskEditSupported = modelOptionSupports(
    selectedGenerationModel,
    [["mask_edit", "image_mask", "inpainting"]],
  );
  const modelDeclaresFirstLastFrame = Boolean(
    selectedGenerationModel?.capabilities
    && !Array.isArray(selectedGenerationModel.capabilities)
    && selectedGenerationModel.capabilities.first_last_frame === true,
  );
  const firstLastFrameEnabled = Boolean(
    category === "video"
    && creationMode === "video"
    && subjectMode === "general"
    && modelDeclaresFirstLastFrame,
  );
  const effectiveLastFrameAsset = firstLastFrameEnabled ? lastFrameAsset : null;
  const analysisOnlySourceVideo = Boolean(
    category === "video"
    && selected?.type === "video"
    && Number(reverseOperationId) > 0
    && Number(reverseRevisionId) > 0
    && Boolean(reverseSourceSignature)
    && reverseSourceSignature === assetSignature(selected)
  );
  const videoRequiresFirstFrame = Boolean(
    category === "video"
    && creationMode === "video"
    && subjectMode === "general"
    && videoModelRequiresFirstFrame(selectedGenerationModel)
  );
  const referenceSignature = [
    assetSignature(productAsset),
    assetSignature(selected),
    assetSignature(effectiveLastFrameAsset),
  ].join("|");
  const productVideoStrategySelection = resolveProductVideoStrategySelection(
    selectedGenerationModel?.capabilities,
    productVideoTemplate,
  );
  const effectiveProductVideoTemplate = productVideoStrategySelection.effectiveValue;
  const videoSubjectImageInputMode = resolveVideoSubjectImageInputMode(
    selectedGenerationModel,
    {
      category,
      creationMode,
      subjectMode,
      selected,
      productAsset,
      productDetailAssets,
    },
  );
  const productVideoStrategyUnsupported = Boolean(
    category === "video"
    && productGenerationMode
    && videoSubjectImageInputMode !== VIDEO_IMAGE_INPUT_MODE.FIRST_FRAME
    && !productVideoStrategySelection.supported,
  );
  const independentSubjectReferenceUnsupported = Boolean(
    videoSubjectImageInputMode === VIDEO_IMAGE_INPUT_MODE.UNSUPPORTED,
  );
  const productReferenceUnsupported = Boolean(
    independentSubjectReferenceUnsupported && subjectMode === "product",
  );
  const portraitReferenceUnsupported = Boolean(
    independentSubjectReferenceUnsupported && subjectMode === "portrait",
  );
  const generationRequirements = modelRequirements({
    use: category,
    creationMode,
    selected,
    productAsset,
    productDetailAssets,
    subjectMode,
    analysisOnlySourceVideo,
  });
  const generationInputUnsupported = Boolean(
    !modelOptionSupports(selectedGenerationModel, generationRequirements)
    && !(videoRequiresFirstFrame && selected?.type !== "image")
  );
  const declaredReferenceLimitUnsupported = Boolean(
    selectedGenerationModel?.selection_disabled === true,
  );
  const modelSwitchRequired = Boolean(
    independentSubjectReferenceUnsupported
    || generationInputUnsupported
    || productVideoStrategyUnsupported
    || declaredReferenceLimitUnsupported
  );
  const mediaLabel = category === "video" ? "视频" : "图片";
  const selectedModelName = String(
    selectedGenerationModel?.display_name || selectedGenerationModel?.model_id || `当前${mediaLabel}模型`,
  ).trim();
  const subjectImageReferenceCount = new Set(
    [productAsset, selected, ...productDetailAssets]
      .filter((asset) => asset && asset.type !== "video")
      .map(assetReferenceUrl)
      .filter(Boolean),
  ).size;
  const modelSwitchMessage = independentSubjectReferenceUnsupported
    ? subjectImageReferenceCount > 1
      ? `${selectedModelName} 仅支持单图首帧，本次需要 ${subjectImageReferenceCount} 张图片。请移除多余图片或切换支持多参考图的视频模型；系统不会自动切换模型。`
      : subjectMode === "portrait"
        ? `${selectedModelName} 不支持当前人物图片输入方式。请切换视频模型后再生成；系统不会自动切换模型。`
        : `${selectedModelName} 不支持当前产品图片输入方式。请切换视频模型后再生成；系统不会自动切换模型。`
    : generationInputUnsupported
      ? category === "video" && selected?.type === "video" && !analysisOnlySourceVideo
        ? `${selectedModelName} 不支持视频输入。请切换支持视频参考或视频编辑的模型，或先完成该素材的视频反推并应用反推版本；系统不会自动切换模型。`
        : `${selectedModelName} 不支持当前素材组合。请切换支持该能力的${mediaLabel}模型后再生成；系统不会自动切换模型。`
      : productVideoStrategyUnsupported
      ? `${selectedModelName} 未提供可用的产品视频策略。请切换视频模型后再生成；系统不会自动切换模型。`
      : declaredReferenceLimitUnsupported
        ? `${selectedModelName} ${selectedGenerationModel.selection_disabled_reason || "不支持当前参考素材组合"}。请切换${mediaLabel}模型后再生成；系统不会自动切换模型。`
      : "";
  const baseProductReferenceUrls = [productAsset, selected]
    .map(assetReferenceUrl)
    .filter(Boolean);
  const productDetailUrls = productDetailAssets.map(assetReferenceUrl).filter(Boolean);
  const totalProductReferenceCount = new Set([
    ...baseProductReferenceUrls,
    ...productDetailUrls,
  ]).size;
  const productDetailValidation = productAsset
    ? validateMultiReferenceSelection(
        selectedGenerationModel,
        totalProductReferenceCount,
        productDetailUrls.length,
      )
    : { ok: false, limit: 0, message: "请先选择产品主题图" };
  const productDetailModelLimit = strictMultiReferenceLimit(selectedGenerationModel);
  const productDetailLimit = Math.min(
    MAX_PRODUCT_DETAIL_IMAGES,
    Math.max(
      0,
      productDetailModelLimit
        - new Set(baseProductReferenceUrls).size
        - (productAsset ? 0 : 1),
    ),
  );
  const providerVideoEditMode = Boolean(
    category === "video"
    && selected?.type === "video"
    && !analysisOnlySourceVideo
    && selectedCapabilities.video_edit === true
    && selectedCapabilities.video_reference !== true
  );
  const independentReferenceMode = Boolean(
    category === "video"
    && (
      videoSubjectImageInputMode === VIDEO_IMAGE_INPUT_MODE.SUBJECT_REFERENCE
      || productDetailAssets.length > 0
    )
  );
  const configuredVideoMax = videoDurationLimit(cfg?.video_duration_max_seconds);
  const declaredVideoMax = Number(selectedCapabilities.max_duration_seconds);
  const declaredReferenceMax = independentReferenceMode
    ? Number(selectedCapabilities.max_reference_duration_seconds)
    : 0;
  const effectiveVideoMax = Math.min(
    configuredVideoMax,
    ...(declaredVideoMax > 0 ? [declaredVideoMax] : []),
    ...(declaredReferenceMax > 0 ? [declaredReferenceMax] : []),
  );
  const effectiveVideoMin = videoDurationMinimum(
    selectedCapabilities.min_duration_seconds,
    effectiveVideoMax,
  );
  const declaredVideoResolutions = Array.isArray(selectedCapabilities.resolutions)
    ? selectedCapabilities.resolutions
      .map((value) => String(value || "").trim().toLowerCase())
      .filter(Boolean)
    : [];
  const videoResolutions = declaredVideoResolutions.length
    ? [...new Set(declaredVideoResolutions)]
    : ["480p", "720p", "1080p"];
  const studioCfg = cfg ? {
    ...cfg,
    ...(category === "video" ? {
      video_duration_min_seconds: effectiveVideoMin,
      video_duration_max_seconds: effectiveVideoMax,
      video_resolutions: videoResolutions,
    } : {}),
    ...(cfg.model_options ? {
      models: {
        ...(cfg.models || {}),
        [category]: {
          ...(cfg.models?.[category] || {}),
          ...(selectedGenerationModel || {}),
          enabled: generationModelOptions.length > 0,
        },
      },
    } : {}),
  } : cfg;
  const targetModel = cfg?.models?.[category] || {};
  const targetGateway = cfg?.gateways?.[category] || {};

  return {
    firstLastFrameEnabled,
    effectiveLastFrameAsset,
    analysisOnlySourceVideo,
    reverseSourceSignature,
    videoRequiresFirstFrame,
    providerVideoEditMode,
    maskEditSupported,
    referenceSignature,
    productVideoStrategySelection,
    effectiveProductVideoTemplate,
    productVideoStrategyUnsupported,
    videoSubjectImageInputMode,
    independentSubjectReferenceUnsupported,
    productReferenceUnsupported,
    portraitReferenceUnsupported,
    generationInputUnsupported,
    declaredReferenceLimitUnsupported,
    modelSwitchRequired,
    modelSwitchMessage,
    productDetailValidation,
    productDetailLimit,
    minVideoDuration: effectiveVideoMin,
    maxVideoDuration: effectiveVideoMax,
    videoResolutions,
    studioCfg,
    targetModelId: selectedGenerationModel?.model_id
      || targetModel.model_id
      || targetGateway.model_id
      || "",
    targetModelProvider: selectedGenerationModel?.provider
      || targetModel.provider
      || targetGateway.provider
      || "",
  };
}
