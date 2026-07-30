import { useEffect } from "react";

import { api } from "../lib/api";
import { errorMessage, reportBackgroundError } from "../lib/errorHandling";
import { startSubjectProtectionPreview } from "../lib/studioSubjectProtection";
import { assetSignature } from "../app/studio/helpers";

export default function useSubjectProtectionPreview({
  creationMode,
  isImageEditMode,
  productGenerationMode,
  portraitGenerationMode,
  productAsset,
  editMaskMode,
  maskEditSupported = true,
  setWorkspacePatch,
}) {
  const productAssetSignature = assetSignature(productAsset);

  useEffect(() => {
    const mode = creationMode;
    const shouldPreview = (
      isImageEditMode
      && productGenerationMode
      && !portraitGenerationMode
      && productAsset?.url
      && maskEditSupported
    );
    if (!shouldPreview) {
      setWorkspacePatch({
        subjectProtection: null,
        subjectProtectionLoading: false,
        subjectProtectionSource: "",
      }, mode);
      return undefined;
    }
    const requestedMode = editMaskMode || "protect_subject";
    const source = `${productAssetSignature}|${requestedMode}`;
    setWorkspacePatch({
      subjectProtectionLoading: true,
      subjectProtectionSource: source,
    }, mode);
    const request = startSubjectProtectionPreview({
      load: () => api.subjectProtectionPreview(productAsset.url, requestedMode),
      onSuccess: (preview) => {
        setWorkspacePatch((current) => {
          const stillCurrent = (
            assetSignature(current.productAsset) === productAssetSignature
            && (current.editMaskMode || "protect_subject") === requestedMode
          );
          if (!stillCurrent) return {};
          return {
            subjectProtection: preview,
            subjectProtectionLoading: false,
            subjectProtectionSource: source,
          };
        }, mode);
      },
      onError: (error) => {
        reportBackgroundError(error, "subject protection preview");
        setWorkspacePatch((current) => {
          const stillCurrent = (
            assetSignature(current.productAsset) === productAssetSignature
            && (current.editMaskMode || "protect_subject") === requestedMode
          );
          if (!stillCurrent) return {};
          return {
            subjectProtection: {
              mode: "none",
              confidence: 0,
              bbox: null,
              width: 0,
              height: 0,
              mask_data_uri: null,
              will_send_mask: false,
              pixel_lock_recommended: false,
              risk_level: "high",
              title: "主体保护预检失败",
              message: errorMessage(error, "无法预检主体保护，请稍后重试或使用透明 PNG。"),
              recommendations: ["可先继续生成，但产品文字和边缘稳定性会下降。"],
            },
            subjectProtectionLoading: false,
            subjectProtectionSource: source,
          };
        }, mode);
      },
    });
    return request.cancel;
  }, [
    creationMode,
    isImageEditMode,
    productGenerationMode,
    portraitGenerationMode,
    productAssetSignature,
    productAsset?.url,
    editMaskMode,
    maskEditSupported,
  ]);
}
