import AssetPickerDialog from "../../components/AssetPickerDialog";
import { MAX_REVERSE_BATCH_ITEMS } from "../../lib/reverseBatches";
import { assetReferenceUrl, dedupeAssets, unifiedAssetKey } from "../../lib/unifiedAssets";

export default function StudioAssetPickerDialog({
  request,
  reverseBatchEnabled,
  category,
  productDetailLimit,
  productDetailAssets,
  productAsset,
  effectiveLastFrameAsset,
  selected,
  batchReverseAssets,
  uploadInputRefs,
  setRequest,
  setWorkspacePatch,
  selectProductAsset,
  selectLastFrameAsset,
  selectReferenceAsset,
  setMsg,
}) {
  const role = request?.role;

  function requestUpload() {
    setRequest(null);
    if (["storyboard_shot", "storyboard_audio"].includes(role)) {
      uploadInputRefs.video?.current?.click();
    } else if (role === "product_detail") {
      uploadInputRefs.productDetail?.current?.click();
    } else if (role === "last_frame") {
      uploadInputRefs.lastFrame?.current?.click();
    } else if (role === "reverse_source") {
      const inputRef = request?.mediaType === "video" ? uploadInputRefs.video : uploadInputRefs.image;
      inputRef?.current?.click();
    } else {
      uploadInputRefs.product?.current?.click();
    }
  }

  function confirmSelection(picked) {
    if (typeof request?.onConfirm === "function") {
      request.onConfirm(picked);
    } else if (role === "reverse_batch" && reverseBatchEnabled) {
      setWorkspacePatch({
        batchReverseAssets: dedupeAssets(picked, MAX_REVERSE_BATCH_ITEMS),
      });
    } else if (role === "product_theme") {
      const next = picked[0];
      if (next) {
        const url = assetReferenceUrl(next);
        const clean = { ...next, url };
        setWorkspacePatch((current) => ({
          productDetailAssets: (current.productDetailAssets || [])
            .filter((item) => assetReferenceUrl(item) !== url),
        }));
        selectProductAsset(clean);
      }
    } else if (role === "last_frame") {
      const next = picked[0];
      const url = assetReferenceUrl(next);
      if (next && url) selectLastFrameAsset({ ...next, url });
    } else if (role === "reverse_source") {
      const next = picked[0];
      const url = assetReferenceUrl(next);
      if (next && url) selectReferenceAsset({ ...next, url });
    } else {
      if (!productAsset) {
        setMsg("请先选择产品主题图");
        setRequest(null);
        return;
      }
      setWorkspacePatch((current) => {
        const mainUrl = assetReferenceUrl(current.productAsset);
        const existing = current.productDetailAssets || [];
        const combined = dedupeAssets([...existing, ...picked]);
        const next = combined.filter((item) => assetReferenceUrl(item) !== mainUrl);
        if (next.length > productDetailLimit) return {};
        return { productDetailAssets: next };
      });
    }
    setRequest(null);
  }

  return (
    <AssetPickerDialog
      open={Boolean(request && (reverseBatchEnabled || role !== "reverse_batch"))}
      role={role || "product_theme"}
      multiple={request?.multiple ?? ["product_detail", "reverse_batch"].includes(role)}
      maxSelection={request?.maxSelection ?? (role === "reverse_batch"
        ? MAX_REVERSE_BATCH_ITEMS
        : role === "product_detail"
          ? Math.max(0, productDetailLimit - productDetailAssets.length)
          : 1)}
      selected={request?.selected || (role === "product_theme"
        ? (productAsset ? [productAsset] : [])
        : role === "last_frame"
          ? (effectiveLastFrameAsset ? [effectiveLastFrameAsset] : [])
          : role === "reverse_source"
            ? (selected ? [selected] : [])
          : role === "reverse_batch"
            ? batchReverseAssets
            : [])}
      mediaType={request?.mediaType || (
        role === "reverse_batch" && category === "video" ? "all" : "image"
      )}
      excludedRefs={role === "product_detail" && productAsset
        ? [unifiedAssetKey(productAsset)]
        : role === "last_frame" && selected
          ? [unifiedAssetKey(selected)]
          : []}
      excludedUrls={role === "product_detail" && productAsset
        ? [assetReferenceUrl(productAsset)]
        : role === "last_frame" && selected
          ? [assetReferenceUrl(selected)]
          : []}
      onClose={() => setRequest(null)}
      onUploadRequest={role === "reverse_batch" ? undefined : requestUpload}
      onConfirm={confirmSelection}
    />
  );
}
