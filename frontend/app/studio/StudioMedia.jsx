"use client";

import { useEffect, useState } from "react";
import { authenticatedObjectUrl } from "../../lib/api";
import AssetMedia, {
  assetPreviewLabel,
  assetPreviewSrc,
  assetUnavailableText,
  canDownloadAsset,
  isAssetTakenDown,
} from "../../components/AssetMedia";
import { assetDims, mediaAspectStyle, mediaThumbSrc } from "./helpers";

export function srcOf(a) {
  return assetPreviewSrc(a);
}

function isProtectedUploadSrc(src) {
  return String(src || "").includes("/api/uploads/");
}

function isPlatformSrc(src) {
  const value = String(src || "");
  if (!value) return false;
  if (value.startsWith("//")) return false;
  if (value.startsWith("blob:") || value.startsWith("/")) return true;
  if (typeof window === "undefined") return false;
  try {
    return new URL(value, window.location.origin).origin === window.location.origin;
  } catch (e) {
    return false;
  }
}

function PreviewLoading() {
  return (
    <div className="flex h-full w-full items-center justify-center bg-base2 text-[10px] text-fog">
      加载预览…
    </div>
  );
}

export function ReferenceAssetPreview({ asset, compact = false }) {
  const [failed, setFailed] = useState(false);
  const [secureSrc, setSecureSrc] = useState("");
  const videoRawSrc = asset?.type === "video" ? (asset?.display_url || asset?.url || "") : "";
  const videoPosterSrc = asset?.type === "video" ? (asset?.display_thumb || asset?.thumb || "") : "";
  const rawSrc = asset?.type === "video" ? (videoRawSrc || videoPosterSrc || mediaThumbSrc(asset)) : mediaThumbSrc(asset);
  const protectedSrc = isProtectedUploadSrc(rawSrc);
  useEffect(() => {
    setFailed(false);
    setSecureSrc("");
    if (!rawSrc || !isProtectedUploadSrc(rawSrc)) return;
    let cancelled = false;
    let objectUrl = "";
    authenticatedObjectUrl(rawSrc)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSecureSrc(url);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rawSrc]);
  if (failed) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-1 bg-base2 text-[10px] text-fog">
        <span>{asset?.type === "video" ? "🎬" : "图片"}</span>
        <span>预览不可用</span>
      </div>
    );
  }
  if (asset?.type === "video") {
    if (protectedSrc && !secureSrc) return <PreviewLoading />;
    const src = secureSrc || rawSrc;
    const canRenderVideo = Boolean(src && (String(src).startsWith("blob:") || protectedSrc || isPlatformSrc(src)));
    const videoSrc = canRenderVideo ? src : "";
    if (videoSrc) {
      return (
        <video
          src={videoSrc}
          controls={!compact}
          muted
          playsInline
          preload="metadata"
          poster={videoPosterSrc || undefined}
          className="h-full w-full bg-black/20 object-contain"
          onError={() => setFailed(true)}
        />
      );
    }
    if (videoPosterSrc) {
      return <img src={videoPosterSrc} alt="" loading="lazy" className="h-full w-full bg-black/20 object-contain" onError={() => setFailed(true)} />;
    }
    return <div className="flex h-full w-full items-center justify-center text-fog">🎬</div>;
  }
  if (protectedSrc && !secureSrc) return <PreviewLoading />;
  const src = secureSrc || rawSrc;
  if (!src) return <div className="flex h-full w-full items-center justify-center bg-base2 text-xs text-fog">无预览</div>;
  return <img src={src} alt="" loading="lazy" className="h-full w-full bg-black/20 object-contain" onError={() => setFailed(true)} />;
}

function isPreviewVideoAsset(a) {
  return a?.type === "video" && a?.unlocked && !a?.hd_url;
}

export function ResultCard({ a, onOpen, onUnlock, onDownload, onVariation, unlocking = false, fixedAspect = false }) {
  const src = srcOf(a);
  const ratioStyle = fixedAspect ? null : mediaAspectStyle(a);
  const takenDown = isAssetTakenDown(a);
  const canCreateVariation = a.type === "image" && Boolean(onVariation);
  const actionClassName = canCreateVariation ? "grid grid-cols-3 gap-1.5" : "flex gap-1.5";
  return (
    <div className="group flex h-full flex-col overflow-hidden rounded-xl2 border border-line bg-base2">
      <button
        type="button"
        aria-label={`${takenDown ? "查看素材状态" : "预览素材"} #${a.id}`}
        className={`relative block w-full cursor-zoom-in bg-black/20 text-left ${fixedAspect ? "aspect-[4/5] overflow-hidden" : ""}`}
        style={ratioStyle || undefined}
        onClick={onOpen}
      >
        {!src ? (
          <div className="absolute inset-0 flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
            {assetUnavailableText(a)}
          </div>
        ) : (
          <AssetMedia
            asset={a}
            className={`${fixedAspect ? "absolute inset-0" : ""} h-full w-full object-contain transition group-hover:scale-105`}
            fallbackClassName={`${fixedAspect ? "absolute inset-0" : ""} flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog`}
          />
        )}
        {takenDown && <span className="badge absolute left-1.5 top-1.5 bg-bad/80 text-white">已下架</span>}
      </button>
      <div className="mt-auto p-1.5">
        {takenDown ? (
          <div className="flex gap-1.5">
            <button type="button" onClick={onOpen} className="btn-secondary btn-sm flex-1">查看状态</button>
          </div>
        ) : a.unlocked ? (
          <div className={actionClassName}>
            <button type="button" onClick={onOpen} className={`btn-secondary btn-sm ${canCreateVariation ? "" : "flex-1"}`}>预览</button>
            {canCreateVariation && (
              <button type="button" onClick={() => onVariation(a)} className="btn-secondary btn-sm">变体</button>
            )}
            <button
              type="button"
              onClick={onDownload}
              disabled={unlocking || !canDownloadAsset(a)}
              className={canDownloadAsset(a) ? `btn-primary btn-sm ${canCreateVariation ? "" : "flex-1"}` : `btn-secondary btn-sm cursor-not-allowed opacity-70 ${canCreateVariation ? "" : "flex-1"}`}
            >
              {unlocking ? "处理中…" : "下载"}
            </button>
          </div>
        ) : (
          <div className={actionClassName}>
            <button type="button" onClick={onOpen} className={`btn-secondary btn-sm ${canCreateVariation ? "" : "flex-1"}`}>预览</button>
            {canCreateVariation && (
              <button type="button" onClick={() => onVariation(a)} className="btn-secondary btn-sm">变体</button>
            )}
            <button type="button" onClick={onUnlock} disabled={unlocking} className={`btn-primary btn-sm ${canCreateVariation ? "" : "flex-1"}`}>
              {unlocking ? "解锁中…" : "解锁"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function MasonryItem({ a, onOpen }) {
  const src = srcOf(a);
  const ratioStyle = mediaAspectStyle(a);
  const takenDown = isAssetTakenDown(a);
  return (
    <button
      onClick={onOpen}
      aria-label={`预览${a.type === "video" ? "视频" : "图片"}素材 #${a.id}`}
      className="group relative block w-full overflow-hidden rounded-xl2 border border-line bg-base2"
      style={ratioStyle}
    >
      {!src ? (
        <div className="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
          {assetUnavailableText(a)}
        </div>
      ) : (
        <AssetMedia
          asset={a}
          className="h-full w-full object-contain transition duration-300 group-hover:scale-[1.04]"
          fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
        />
      )}
      <div className="pointer-events-none absolute inset-0 flex items-end bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition group-hover:opacity-100">
        <span className="m-2 flex items-center gap-1 text-xs text-white/90">
          {takenDown ? "素材已下架" : a.unlocked ? "已解锁" : "点击预览 / 解锁"}
        </span>
      </div>
      {takenDown && <span className="badge absolute left-2 top-2 bg-bad/80 text-white">已下架</span>}
    </button>
  );
}

export function Lightbox({ a, onClose, onUnlock, onDownload, onVariation, unlocking = false }) {
  const src = srcOf(a);
  const takenDown = isAssetTakenDown(a);
  const dims = assetDims(a);
  const canCreateVariation = a.type === "image" && Boolean(onVariation) && !takenDown;
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="素材预览"
      onClick={onClose}
    >
      <div className="panel max-h-[92vh] w-full max-w-3xl overflow-auto p-3" onClick={(e) => e.stopPropagation()}>
        {!src ? (
          <div className="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog">
            {takenDown ? "素材已下架，不能继续预览、解锁或下载。" : a.unlocked ? "预览暂不可用，请稍后重试。" : "预览暂不可用，请先解锁后再下载。"}
          </div>
        ) : (
          <AssetMedia
            asset={a}
            interactive
            controls
            autoPlay
            muted={false}
            className="mx-auto max-h-[76vh] max-w-full rounded-xl2 object-contain"
            fallbackClassName="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog"
          />
        )}
        <div className="mt-3 flex flex-col gap-2 text-sm sm:flex-row sm:items-center sm:justify-between">
          <span className="min-w-0 text-fog">
            {assetPreviewLabel(a)}
            {dims ? ` · ${dims.width}×${dims.height}` : ""}
          </span>
          <div className="flex flex-wrap gap-2 sm:justify-end">
            {!takenDown && !a.unlocked && (
              <button onClick={onUnlock} disabled={unlocking} className="btn-primary btn-sm">
                {unlocking ? "解锁中…" : "解锁"}
              </button>
            )}
            {!takenDown && a.unlocked && (
              <button
                onClick={onDownload}
                disabled={unlocking || !canDownloadAsset(a)}
                className={canDownloadAsset(a) ? "btn-primary btn-sm" : "btn-secondary btn-sm cursor-not-allowed opacity-70"}
              >
                {unlocking ? "处理中…" : "下载"}
              </button>
            )}
            {canCreateVariation && (
              <button onClick={() => onVariation(a)} className="btn-secondary btn-sm">生成变体</button>
            )}
            <button onClick={onClose} className="btn-secondary btn-sm">关闭</button>
          </div>
        </div>
      </div>
    </div>
  );
}
