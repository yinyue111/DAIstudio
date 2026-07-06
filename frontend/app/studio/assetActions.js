export function confirmAssetUnlock(asset, me, cfg) {
  const type = asset?.type === "video" ? "视频" : "图片";
  const balance = Number(me?.balance_credits ?? 0);
  const cost = Number(asset?.unlock_cost ?? cfg?.models?.[asset?.type]?.unlock_cost ?? 0);
  return window.confirm(`解锁${type}将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`);
}

export function assetVariationSourceUrl(asset, { respectUnlock = false } = {}) {
  if (!asset || asset.type !== "image") return "";
  if (respectUnlock) {
    return asset.unlocked ? (asset.hd_url || asset.preview_url || "") : (asset.preview_url || "");
  }
  return asset.hd_url || asset.preview_url || asset.url || "";
}
