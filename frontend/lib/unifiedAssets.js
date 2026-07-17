export function unifiedAssetKey(asset) {
  if (!asset) return "";
  if (asset.asset_ref) return String(asset.asset_ref);
  if (asset.origin === "generated" && asset.id != null) return `g.${asset.id}`;
  return String(asset.id ?? asset.url ?? asset.preview_url ?? "");
}

export function normalizeUnifiedAsset(raw) {
  const asset = raw && typeof raw === "object" ? raw : {};
  const origin = asset.origin === "uploaded" ? "uploaded" : "generated";
  const preview = asset.preview_url || asset.thumb || asset.url || asset.hd_url || "";
  const original = asset.url || asset.hd_url || preview;
  const assetRef = String(
    asset.asset_ref
    || (origin === "generated" && asset.id != null ? `g.${asset.id}` : ""),
  );
  const generatedId = origin === "generated" && /^g\.\d+$/.test(assetRef)
    ? Number(assetRef.slice(2))
    : asset.id;
  return {
    ...asset,
    id: generatedId,
    asset_ref: assetRef,
    origin,
    type: asset.type === "video" ? "video" : "image",
    url: original,
    thumb: asset.thumb || preview || null,
    preview_url: preview || null,
    hd_url: origin === "uploaded" ? original : (asset.hd_url || null),
    unlocked: origin === "uploaded" ? true : Boolean(asset.unlocked),
    available: asset.available !== false,
    favorite: Boolean(asset.favorite),
    retained: Boolean(asset.retained),
    created_at: asset.created_at || "",
    retention_expires_at: asset.retention_expires_at || asset.expires_at || null,
  };
}

export function normalizeUnifiedAssetPage(payload) {
  const items = Array.isArray(payload) ? payload : (Array.isArray(payload?.items) ? payload.items : []);
  return {
    items: items.map(normalizeUnifiedAsset).filter((asset) => unifiedAssetKey(asset)),
    total: Number(Array.isArray(payload) ? items.length : payload?.total ?? items.length) || 0,
    stats: payload && !Array.isArray(payload) && payload.stats && typeof payload.stats === "object"
      ? payload.stats
      : null,
    nextCursor: payload && !Array.isArray(payload) ? (payload.next_cursor || null) : null,
  };
}

export function uploadedAssetOptimistic(raw, file) {
  return normalizeUnifiedAsset({
    ...raw,
    asset_ref: `client.${Date.now()}.${Math.random().toString(36).slice(2)}`,
    origin: "uploaded",
    filename: file?.name || raw?.filename || "",
    bytes: file?.size ?? raw?.bytes ?? null,
    created_at: new Date().toISOString(),
    favorite: false,
    retained: false,
  });
}

export function assetReferenceUrl(asset) {
  if (!asset || asset.available === false) return "";
  return String(asset.url || asset.hd_url || asset.preview_url || asset.thumb || "").trim();
}

export function dedupeAssets(assets, limit = Infinity) {
  const seen = new Set();
  const result = [];
  for (const asset of assets || []) {
    const normalized = normalizeUnifiedAsset(asset);
    const key = assetReferenceUrl(normalized) || unifiedAssetKey(normalized);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push(normalized);
    if (result.length >= limit) break;
  }
  return result;
}
