export function assetVariationSourceUrl(asset, { respectUnlock = false } = {}) {
  if (!asset || asset.type !== "image") return "";
  if (respectUnlock) {
    return asset.unlocked ? (asset.hd_url || asset.preview_url || "") : (asset.preview_url || "");
  }
  return asset.hd_url || asset.preview_url || asset.url || "";
}
