"use client";

import { useEffect, useRef } from "react";
import AssetMedia, {
  assetPreviewLabel,
  assetPreviewSrc,
  canDownloadAsset,
  isAssetTakenDown,
} from "./AssetMedia";

function fallbackText(asset) {
  if (isAssetTakenDown(asset)) return "素材已下架，不能继续预览、解锁或下载。";
  return asset?.unlocked ? "预览暂不可用，请稍后重试。" : "预览暂不可用，请先解锁后再下载。";
}

function focusableElements(root) {
  if (!root) return [];
  return Array.from(
    root.querySelectorAll(
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true");
}

export default function AssetPreviewDialog({
  asset,
  onClose,
  onError,
  meta = "",
  children,
}) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const src = assetPreviewSrc(asset);
  const takenDown = isAssetTakenDown(asset);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusableElements(dialogRef.current);
      if (!items.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      const previous = previousFocusRef.current;
      if (previous && typeof previous.focus === "function") previous.focus();
    };
  }, [onClose]);

  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm outline-none"
      role="dialog"
      aria-modal="true"
      aria-label="素材预览"
      onClick={onClose}
    >
      <div className="panel max-h-[92vh] w-full max-w-3xl overflow-auto p-3" onClick={(e) => e.stopPropagation()}>
        {!src ? (
          <div className="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog">
            {fallbackText(asset)}
          </div>
        ) : (
          <AssetMedia
            asset={asset}
            interactive
            controls
            autoPlay
            muted={false}
            className="mx-auto max-h-[76vh] max-w-full rounded-xl2 object-contain"
            fallbackClassName="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog"
            onError={onError}
          />
        )}
        <div className="mt-3 flex flex-col gap-2 text-sm sm:flex-row sm:items-center sm:justify-between">
          <span className="min-w-0 text-fog">
            {assetPreviewLabel(asset)}
            {meta}
          </span>
          <div className="flex flex-wrap gap-2 sm:justify-end">
            {children?.({ asset, takenDown, canDownload: canDownloadAsset(asset) })}
            <button type="button" onClick={onClose} className="btn-secondary btn-sm">关闭</button>
          </div>
        </div>
      </div>
    </div>
  );
}
