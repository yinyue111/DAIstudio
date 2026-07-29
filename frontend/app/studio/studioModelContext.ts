import { assetReferenceUrl } from "../../lib/unifiedAssets";
import {
  modelOptionSupports,
  strictMultiReferenceLimit,
  validateMultiReferenceSelection,
} from "./StudioModelSelector";
import { MAX_PRODUCT_DETAIL_IMAGES } from "./constants";
import { assetSignature } from "./helpers";
import { resolveProductVideoStrategySelection } from "./productVideoStrategy";

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
}) {
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
  const productVideoStrategyUnsupported = Boolean(
    category === "video"
    && productGenerationMode
    && !productVideoStrategySelection.supported,
  );
  const productReferenceUnsupported = Boolean(
    category === "video"
    && productGenerationMode
    && productAsset
    && !modelOptionSupports(selectedGenerationModel, [["reference_image", "multi_reference"]]),
  );
  const videoModelSwitchRequired = Boolean(
    productReferenceUnsupported || productVideoStrategyUnsupported,
  );
  const selectedVideoModelName = String(
    selectedGenerationModel?.display_name || selectedGenerationModel?.model_id || "当前视频模型",
  ).trim();
  const videoModelSwitchMessage = productReferenceUnsupported
    ? `${selectedVideoModelName} 不支持独立产品主题图。请切换到支持产品参考的视频模型后再生成；系统不会自动切换模型。`
    : productVideoStrategyUnsupported
      ? `${selectedVideoModelName} 未提供可用的产品视频策略。请切换视频模型后再生成；系统不会自动切换模型。`
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
  const studioCfg = cfg?.model_options ? {
    ...cfg,
    models: {
      ...(cfg.models || {}),
      [category]: {
        ...(cfg.models?.[category] || {}),
        ...(selectedGenerationModel || {}),
        enabled: generationModelOptions.length > 0,
      },
    },
  } : cfg;
  const targetModel = cfg?.models?.[category] || {};
  const targetGateway = cfg?.gateways?.[category] || {};

  return {
    firstLastFrameEnabled,
    effectiveLastFrameAsset,
    referenceSignature,
    productVideoStrategySelection,
    effectiveProductVideoTemplate,
    productVideoStrategyUnsupported,
    productReferenceUnsupported,
    videoModelSwitchRequired,
    videoModelSwitchMessage,
    productDetailValidation,
    productDetailLimit,
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
