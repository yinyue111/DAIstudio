"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import {
  assetReferenceUrl,
  normalizeUnifiedAssetPage,
  unifiedAssetKey,
} from "../lib/unifiedAssets";
import AssetMedia from "./AssetMedia";

const PAGE_SIZE = 48;
const EMPTY_LIST = [];
const ROLE_LABELS = {
  product_theme: "选择产品主题图",
  product_detail: "选择产品细节图",
  style: "选择风格参考",
  first_frame: "选择视频首帧",
  last_frame: "选择视频尾帧",
};

function isAssetExcluded(asset, excludedRefSet, excludedUrlSet) {
  const ref = unifiedAssetKey(asset);
  const url = assetReferenceUrl(asset);
  return Boolean(
    (ref && excludedRefSet.has(ref))
    || (url && excludedUrlSet.has(url)),
  );
}

export default function AssetPickerDialog({
  open,
  role = "product_theme",
  multiple = false,
  maxSelection = 1,
  selected = [],
  mediaType = "image",
  excludedRefs = EMPTY_LIST,
  excludedUrls = EMPTY_LIST,
  onConfirm,
  onClose,
  onUploadRequest,
}) {
  const dialogRef = useRef(null);
  const [origin, setOrigin] = useState("all");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [items, setItems] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState(() => new Map());
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!open) return;
    const initial = new Map(
      (selected || []).map((asset) => [unifiedAssetKey(asset), asset]).filter(([key]) => key),
    );
    setSelection(initial);
    setOrigin("all");
    setFavoriteOnly(false);
  }, [open, role]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    api.meAssets({
      origin,
      type: mediaType,
      favorite: favoriteOnly ? true : "",
      retention: "all",
      limit: PAGE_SIZE,
      cursor: "",
      offset: 0,
    }).then((payload) => {
      if (cancelled) return;
      const page = normalizeUnifiedAssetPage(payload);
      setItems(page.items.filter((asset) => assetReferenceUrl(asset)));
      setNextCursor(page.nextCursor);
    }).catch((err) => {
      if (!cancelled) setError(err.message || "资产加载失败");
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [open, origin, favoriteOnly, mediaType, reloadToken]);

  useEffect(() => {
    if (!open) return undefined;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose?.();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus?.();
    };
  }, [open, onClose]);

  const excludedRefSet = useMemo(
    () => new Set((excludedRefs || []).map((value) => String(value || "").trim()).filter(Boolean)),
    [excludedRefs],
  );
  const excludedUrlSet = useMemo(
    () => new Set((excludedUrls || []).map((value) => String(value || "").trim()).filter(Boolean)),
    [excludedUrls],
  );
  const visibleItems = useMemo(
    () => items.filter((asset) => !isAssetExcluded(asset, excludedRefSet, excludedUrlSet)),
    [items, excludedRefSet, excludedUrlSet],
  );
  const selectedItems = useMemo(
    () => [...selection.values()].filter(
      (asset) => !isAssetExcluded(asset, excludedRefSet, excludedUrlSet),
    ),
    [selection, excludedRefSet, excludedUrlSet],
  );
  const exclusionsActive = excludedRefSet.size > 0 || excludedUrlSet.size > 0;
  if (!open) return null;

  function toggle(asset) {
    const key = unifiedAssetKey(asset);
    if (!key || isAssetExcluded(asset, excludedRefSet, excludedUrlSet)) return;
    setSelection((current) => {
      const next = new Map(current);
      if (next.has(key)) {
        next.delete(key);
        return next;
      }
      if (!multiple) return new Map([[key, asset]]);
      const selectableCount = [...next.values()].filter(
        (item) => !isAssetExcluded(item, excludedRefSet, excludedUrlSet),
      ).length;
      if (selectableCount >= maxSelection) return next;
      next.set(key, asset);
      return next;
    });
  }

  async function loadMore() {
    if (loading || !nextCursor) return;
    setLoading(true);
    setError("");
    try {
      const payload = await api.meAssets({
        origin,
        type: mediaType,
        favorite: favoriteOnly ? true : "",
        retention: "all",
        limit: PAGE_SIZE,
        cursor: nextCursor,
        offset: 0,
      });
      const page = normalizeUnifiedAssetPage(payload);
      setItems((current) => {
        const seen = new Set(current.map(unifiedAssetKey));
        return [
          ...current,
          ...page.items.filter((asset) => assetReferenceUrl(asset) && !seen.has(unifiedAssetKey(asset))),
        ];
      });
      setNextCursor(page.nextCursor);
    } catch (err) {
      setError(err.message || "资产加载失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/75 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <section
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="asset-picker-title"
        className="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-t-xl3 border border-line bg-base2 shadow-pop outline-none sm:rounded-xl3"
      >
        <header className="flex min-h-16 items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <h2 id="asset-picker-title" className="truncate text-base font-display font-semibold text-snow">
              {ROLE_LABELS[role] || "选择资产"}
            </h2>
            <p className="mt-0.5 text-xs text-fog">
              已选择 {selectedItems.length}/{Math.max(1, maxSelection)}
            </p>
          </div>
          <button type="button" onClick={onClose} className="btn-ghost btn-sm" aria-label="关闭资产选择器">
            关闭
          </button>
        </header>

        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
          <div className="inline-flex gap-1 rounded-full border border-line bg-white/5 p-1" aria-label="资产来源">
            {[["all", "全部"], ["uploaded", "我的上传"], ["generated", "生成作品"]].map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setOrigin(value)}
                className={origin === value ? "chip chip-active" : "chip"}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="chip cursor-pointer gap-2 px-3 py-2">
            <input
              type="checkbox"
              checked={favoriteOnly}
              onChange={(event) => setFavoriteOnly(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand"
            />
            仅收藏
          </label>
          {onUploadRequest && (
            <button type="button" onClick={onUploadRequest} className="btn-secondary btn-sm ml-auto">
              上传新素材
            </button>
          )}
        </div>

        <div className="min-h-64 flex-1 overflow-y-auto p-4 sm:p-5">
          {role === "product_detail" && exclusionsActive && (
            <p className="mb-3 text-xs text-fog" role="status">
              已排除当前产品主题图，避免与细节图重复。
            </p>
          )}
          {error && (
            <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
              <span>{error}</span>
              <button type="button" onClick={() => setReloadToken((value) => value + 1)} className="btn-ghost btn-sm">重试</button>
            </div>
          )}
          {!items.length && loading ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {Array.from({ length: 8 }).map((_, index) => (
                <div key={index} className="skeleton aspect-square" />
              ))}
            </div>
          ) : !visibleItems.length ? (
            <div className="flex min-h-64 flex-col items-center justify-center gap-3 text-center">
              <p className="text-sm text-mist">
                {role === "product_detail" && exclusionsActive
                  ? "当前筛选下没有其他可用细节图"
                  : "当前筛选下没有可用资产"}
              </p>
              {onUploadRequest && <button type="button" onClick={onUploadRequest} className="btn-primary btn-sm">上传素材</button>}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-5">
              {visibleItems.map((asset) => {
                const key = unifiedAssetKey(asset);
                const checked = selection.has(key);
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => toggle(asset)}
                    aria-pressed={checked}
                    className={`group relative aspect-square overflow-hidden rounded-xl2 border bg-black/20 text-left transition ${
                      checked ? "border-brand ring-2 ring-brand/35" : "border-line hover:border-line2"
                    }`}
                  >
                    <AssetMedia
                      asset={asset}
                      className="h-full w-full object-cover transition duration-200 group-hover:scale-[1.03]"
                      fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
                    />
                    <span className="badge absolute left-2 top-2 bg-black/70 text-white">
                      {asset.origin === "uploaded" ? "上传" : "生成"}
                    </span>
                    <span className={`absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full border text-xs font-bold ${
                      checked ? "border-brand bg-brand text-white" : "border-white/30 bg-black/60 text-white"
                    }`} aria-hidden>
                      {checked ? "✓" : "+"}
                    </span>
                    {asset.filename && (
                      <span className="absolute inset-x-0 bottom-0 truncate bg-black/70 px-2 py-1.5 text-[11px] text-white">
                        {asset.filename}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
          {nextCursor && (
            <div className="mt-5 text-center">
              <button type="button" onClick={loadMore} disabled={loading} className="btn-secondary btn-sm">
                {loading ? "加载中…" : "加载更多"}
              </button>
            </div>
          )}
        </div>

        <footer className="flex min-h-16 items-center justify-end gap-2 border-t border-line px-4 py-3 sm:px-5">
          <button type="button" onClick={onClose} className="btn-secondary btn-sm">取消</button>
          <button
            type="button"
            onClick={() => onConfirm?.(selectedItems)}
            disabled={!selectedItems.length}
            className="btn-primary btn-sm"
          >
            确认选择{selectedItems.length ? ` ${selectedItems.length}` : ""}
          </button>
        </footer>
      </section>
    </div>
  );
}
