import { api } from "../lib/api";
import { assetReferenceUrl } from "../lib/unifiedAssets";
import { buildGenerationPayload } from "../app/studio/generationPayload";
import { generateClientRequestId } from "../app/studio/generationRequestId";
import { validateImageEvidenceMaskPreflight } from "../app/studio/imageEvidenceReview";
import { resolveProductVideoStrategySelection } from "../app/studio/productVideoStrategy";
import { shouldBlockNewGeneration } from "../app/studio/taskConcurrency";
import { generationRequiresPendingUpload } from "../app/studio/generationUploadPolicy";
import {
  modelRequirements,
  modelOptionSupports,
  validateMultiReferenceSelection,
} from "../app/studio/StudioModelSelector";
import { videoModelRequiresFirstFrame } from "../app/studio/studioModelContext";
import { assetSignature } from "../app/studio/helpers";

export function validateGenerationSubmission({
  uploading,
  uploadingRole,
  parsing,
  reversing,
  productProfiling,
  structuredDirty,
  modelOption,
  productVideoTemplate,
  category,
  creationMode,
  subjectMode,
  firstLastFrameEnabled,
  lastFrameAsset,
  selected,
  productAsset,
  productDetailAssets = [],
  variationSource = null,
  task,
  isEditMode,
  isImageEditMode,
  prompt,
  structured,
  reverseOperationId,
  reverseRevisionId,
  reverseSourceSignature = "",
  reviewedImageEvidence,
  reverseEvidenceOperation,
}) {
  if (generationRequiresPendingUpload({ uploading, uploadingRole, selected })) {
    return { ok: false, message: "本次生成需要的素材仍在上传中，请等待上传完成后再生成。" };
  }
  if (parsing || reversing) {
    return { ok: false, message: "参考素材仍在抓取或反推中，请等待提示词完成后再生成。" };
  }
  if (productProfiling) return { ok: false, message: "主体信息仍在识别中，请稍后再生成。" };
  if (structuredDirty) {
    return { ok: false, message: "结构化维度与手工提示词存在冲突，请先应用结构修改或撤销结构修改。" };
  }

  const productVideoStrategy = resolveProductVideoStrategySelection(
    modelOption?.capabilities,
    productVideoTemplate,
  );
  const analysisOnlySourceVideo = Boolean(
    category === "video"
    && selected?.type === "video"
    && Number(reverseOperationId) > 0
    && Number(reverseRevisionId) > 0
    && Boolean(reverseSourceSignature)
    && reverseSourceSignature === assetSignature(selected)
  );
  const videoRequiresFirstFrame = Boolean(
    category === "video" && videoModelRequiresFirstFrame(modelOption),
  );
  const requirements = modelRequirements({
    use: category,
    creationMode,
    selected,
    productAsset,
    subjectMode,
    analysisOnlySourceVideo,
  });
  const independentSubjectReference = Boolean(
    category === "video"
    && productAsset
    && ["product", "portrait"].includes(subjectMode)
  );
  const modelSupportsFirstLastFrame = Boolean(
    category === "video"
    && creationMode === "video"
    && subjectMode === "general"
    && modelOption?.capabilities
    && !Array.isArray(modelOption.capabilities)
    && modelOption.capabilities.first_last_frame === true
  );
  const effectiveLastFrameAsset = firstLastFrameEnabled && modelSupportsFirstLastFrame
    ? lastFrameAsset
    : null;
  if (
    independentSubjectReference
    && !modelOptionSupports(modelOption, [["reference_image", "multi_reference"]])
  ) {
    return {
      ok: false,
      message: subjectMode === "portrait"
        ? "当前视频模型不支持独立人物参考图，请切换支持人物参考的模型后再生成。"
        : "当前视频模型不支持独立产品主题图，请切换支持产品参考的模型后再生成。",
    };
  }
  if (!modelOptionSupports(modelOption, requirements)) {
    if (videoRequiresFirstFrame && selected?.type !== "image") {
      return {
        ok: false,
        message: "当前视频模型必须使用图片首帧，请上传视频首帧或切换模型。",
      };
    }
    if (category === "video" && selected?.type === "video" && !analysisOnlySourceVideo) {
      return {
        ok: false,
        message: "当前视频模型不支持视频输入；请切换支持视频参考或视频编辑的模型，或先完成视频反推并应用反推版本后再生成。",
      };
    }
    return {
      ok: false,
      message: `${category === "video" ? "当前视频" : "当前图片"}模型不支持本次生成所需的素材方式，请切换模型后再生成。`,
    };
  }
  if (category === "video" && subjectMode === "product" && !productVideoStrategy.supported) {
    return { ok: false, message: "当前视频模型未提供可用的产品视频策略，请切换模型后再生成。" };
  }
  if (effectiveLastFrameAsset) {
    const firstFrameUrl = assetReferenceUrl(selected);
    const lastFrameUrl = assetReferenceUrl(effectiveLastFrameAsset);
    if (selected?.type !== "image") {
      return { ok: false, message: "首尾帧模式需要先选择图片作为视频首帧。" };
    }
    if (effectiveLastFrameAsset.type !== "image" || !lastFrameUrl) {
      return { ok: false, message: "视频尾帧只支持可用的图片素材，请重新上传或选择。" };
    }
    if (firstFrameUrl === lastFrameUrl) {
      return { ok: false, message: "视频首帧和尾帧不能使用同一张图片。" };
    }
  }
  const directProductVideo = Boolean(
    category === "video"
    && creationMode === "video"
    && subjectMode === "product"
    && productAsset
  );
  const sourceAsset = isEditMode || directProductVideo ? productAsset : selected;
  const styleReferenceAsset = isEditMode || directProductVideo
    ? (variationSource || selected)
    : null;
  const imageReferenceUrls = [
    sourceAsset,
    styleReferenceAsset,
    variationSource,
    ...productDetailAssets,
    effectiveLastFrameAsset,
  ]
    .filter((asset) => asset?.type === "image")
    .map(assetReferenceUrl)
    .filter(Boolean);
  const uniqueImageReferenceCount = new Set(imageReferenceUrls).size;
  const pureFirstLastFramePair = Boolean(
    effectiveLastFrameAsset
    && selected?.type === "image"
    && !productAsset
    && !styleReferenceAsset
    && productDetailAssets.length === 0
    && uniqueImageReferenceCount === 2
  );
  if (uniqueImageReferenceCount > 1 && !pureFirstLastFramePair) {
    const validation = validateMultiReferenceSelection(
      modelOption,
      uniqueImageReferenceCount,
      uniqueImageReferenceCount - 1,
    );
    if (!validation.ok) return { ok: false, message: validation.message };
  }
  if (shouldBlockNewGeneration(task, category)) {
    return {
      ok: false,
      message: category === "image" ? "" : "当前视频任务仍在生成中，请等待完成后再发起新的视频生成。",
    };
  }
  if (isEditMode && !productAsset) {
    return {
      ok: false,
      message: subjectMode === "portrait"
        ? "请先上传人物照片"
        : subjectMode === "product"
          ? "请先上传产品主体图片"
          : isImageEditMode
            ? "请先上传要编辑的图片"
            : "请先上传主体图片",
    };
  }
  if (isEditMode && !String(prompt || "").trim() && Object.keys(structured || {}).length === 0) {
    return {
      ok: false,
      message: isImageEditMode
        ? "请输入图片编辑要求"
        : "请先反推风格参考或输入希望迁移的风格提示词",
    };
  }
  if (!isEditMode && !String(prompt || "").trim() && !selected) {
    return { ok: false, message: "请输入提示词，或从参考反推" };
  }
  if (category === "image" && Number(reverseOperationId) > 0 && Number(reverseRevisionId) > 0) {
    const maskPreflight = validateImageEvidenceMaskPreflight(
      reviewedImageEvidence,
      reverseEvidenceOperation,
      assetReferenceUrl(isEditMode ? productAsset : selected),
    );
    if (!maskPreflight.ok) return maskPreflight;
  }
  return { ok: true, productVideoStrategy, effectiveLastFrameAsset };
}

export function prepareGenerationSubmission({
  stage,
  input,
  subjectProfile,
  productVideoStrategy,
  effectiveLastFrameAsset,
  pendingGenerateRequestRef,
}) {
  const { payload, ratioOption } = buildGenerationPayload({
    ...input,
    stage,
    task: null,
    lastFrameAsset: effectiveLastFrameAsset,
    firstLastFrameEnabled: Boolean(effectiveLastFrameAsset),
    productProfile: subjectProfile.productProfile,
    productProfileSource: subjectProfile.productProfileSource,
    productVideoTemplate: productVideoStrategy.effectiveValue || input.productVideoTemplate,
  });
  payload.client_request_id = generateClientRequestId(
    pendingGenerateRequestRef,
    stage,
    JSON.stringify(payload),
  );
  return { payload, ratioOption };
}

export async function executeGenerationSubmission(payload, requestQuoteConfirmation) {
  if (typeof requestQuoteConfirmation !== "function") {
    throw new Error("生成服务不可用，本次未执行生成。");
  }
  const confirmation = await requestQuoteConfirmation({
    kind: "generation",
    request: payload,
    clientRequestId: payload.client_request_id,
    execute: ({ request }) => api.generate(request),
  });
  if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
    throw confirmation.error || new Error("任务提交失败。");
  }
  return confirmation;
}
