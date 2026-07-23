"use client";

import { useEffect, useState } from "react";
import { authenticatedObjectUrl } from "../../lib/api";
import AssetMedia, { assetPreviewSrc, assetUnavailableText, canDownloadAsset, isAssetTakenDown } from "../../components/AssetMedia";
import AssetPreviewDialog from "../../components/AssetPreviewDialog";
import { reportBackgroundError } from "../../lib/errorHandling";
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

export function referenceVideoRawSrc(asset) {
  if (asset?.type !== "video") return "";
  return String(asset?.display_url || asset?.url || "").trim();
}

export function useReferenceVideoSource(asset) {
  const rawSrc = referenceVideoRawSrc(asset);
  const [source, setSource] = useState({
    src: "",
    status: "idle",
    message: "",
  });

  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    if (!rawSrc) {
      setSource({
        src: "",
        status: asset?.type === "video" ? "unavailable" : "idle",
        message: asset?.type === "video" ? "当前素材只有封面，无法读取视频时间线。" : "",
      });
      return undefined;
    }
    if (!isProtectedUploadSrc(rawSrc)) {
      if (!isPlatformSrc(rawSrc)) {
        setSource({
          src: "",
          status: "unavailable",
          message: "当前视频源不允许浏览器逐帧读取，请先上传到平台素材库。",
        });
      } else {
        setSource({ src: rawSrc, status: "ready", message: "" });
      }
      return undefined;
    }

    setSource({ src: "", status: "loading", message: "正在加载受保护视频…" });
    authenticatedObjectUrl(rawSrc)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSource({ src: url, status: "ready", message: "" });
      })
      .catch((error) => {
        reportBackgroundError(error, "load protected video timeline source");
        if (!cancelled) {
          setSource({
            src: "",
            status: "unavailable",
            message: error?.message || "视频源加载失败，暂时无法读取时间线。",
          });
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [asset?.type, rawSrc]);

  return source;
}

function PreviewLoading() {
  return (
    <div className="flex h-full w-full items-center justify-center bg-base2 text-[10px] text-fog">
      加载预览…
    </div>
  );
}

export function ReferenceAssetPreview({ asset, compact = false }) {
  const [imageFailed, setImageFailed] = useState(false);
  const [videoPlaybackFailed, setVideoPlaybackFailed] = useState(false);
  const [posterFailed, setPosterFailed] = useState(false);
  const [secureSrc, setSecureSrc] = useState("");
  const [securePosterSrc, setSecurePosterSrc] = useState("");
  const videoRawSrc = asset?.type === "video" ? (asset?.display_url || asset?.url || "") : "";
  const videoPosterSrc = asset?.type === "video" ? (asset?.display_thumb || asset?.thumb || "") : "";
  const rawSrc = asset?.type === "video" ? (videoRawSrc || videoPosterSrc || mediaThumbSrc(asset)) : mediaThumbSrc(asset);
  const protectedSrc = isProtectedUploadSrc(rawSrc);
  const protectedPosterSrc = isProtectedUploadSrc(videoPosterSrc);
  useEffect(() => {
    setImageFailed(false);
    setVideoPlaybackFailed(false);
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
      .catch((e) => {
        reportBackgroundError(e, "load protected reference preview");
        if (!cancelled) {
          if (asset?.type === "video") setVideoPlaybackFailed(true);
          else setImageFailed(true);
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [asset?.type, rawSrc]);
  useEffect(() => {
    setPosterFailed(false);
    setSecurePosterSrc("");
    if (!videoPosterSrc || !protectedPosterSrc) return;
    let cancelled = false;
    let objectUrl = "";
    authenticatedObjectUrl(videoPosterSrc)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSecurePosterSrc(url);
      })
      .catch((e) => {
        reportBackgroundError(e, "load protected reference poster");
        if (!cancelled) setPosterFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [protectedPosterSrc, videoPosterSrc]);
  if (imageFailed) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-1 bg-base2 text-[10px] text-fog">
        <span>{asset?.type === "video" ? "🎬" : "图片"}</span>
        <span>预览不可用</span>
      </div>
    );
  }
  if (asset?.type === "video") {
    if (protectedSrc && !secureSrc && !videoPlaybackFailed) return <PreviewLoading />;
    const src = secureSrc || rawSrc;
    const posterSrc = securePosterSrc || (protectedPosterSrc ? "" : videoPosterSrc);
    const rawIsPosterOnly = !videoRawSrc && rawSrc === videoPosterSrc;
    const canRenderVideo = Boolean(!rawIsPosterOnly && src && (String(src).startsWith("blob:") || protectedSrc || isPlatformSrc(src)));
    const videoSrc = canRenderVideo && !videoPlaybackFailed ? src : "";
    if (videoSrc) {
      return (
        <video
          src={videoSrc}
          controls={!compact}
          muted
          playsInline
          preload="metadata"
          poster={posterSrc || undefined}
          className="h-full w-full bg-black/20 object-contain"
          onError={() => setVideoPlaybackFailed(true)}
        />
      );
    }
    if (protectedPosterSrc && !securePosterSrc && !posterFailed) return <PreviewLoading />;
    if (posterSrc && !posterFailed) {
      return <img src={posterSrc} alt="" loading="lazy" className="h-full w-full bg-black/20 object-contain" onError={() => setPosterFailed(true)} />;
    }
    return <div className="flex h-full w-full items-center justify-center text-fog">🎬</div>;
  }
  if (protectedSrc && !secureSrc) return <PreviewLoading />;
  const src = secureSrc || rawSrc;
  if (!src) return <div className="flex h-full w-full items-center justify-center bg-base2 text-xs text-fog">无预览</div>;
  return <img src={src} alt="" loading="lazy" className="h-full w-full bg-black/20 object-contain" onError={() => setImageFailed(true)} />;
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
  const takenDown = isAssetTakenDown(a);
  const dims = assetDims(a);
  const canCreateVariation = a.type === "image" && Boolean(onVariation) && !takenDown;
  return (
    <AssetPreviewDialog
      asset={a}
      onClose={onClose}
      meta={dims ? ` · ${dims.width}×${dims.height}` : ""}
    >
      {() => (
        <>
          {!takenDown && !a.unlocked && (
            <button type="button" onClick={onUnlock} disabled={unlocking} className="btn-primary btn-sm">
                {unlocking ? "解锁中…" : "解锁"}
            </button>
          )}
          {!takenDown && a.unlocked && (
            <button
              type="button"
              onClick={onDownload}
              disabled={unlocking || !canDownloadAsset(a)}
              className={canDownloadAsset(a) ? "btn-primary btn-sm" : "btn-secondary btn-sm cursor-not-allowed opacity-70"}
            >
                {unlocking ? "处理中…" : "下载"}
            </button>
          )}
          {canCreateVariation && (
            <button type="button" onClick={() => onVariation(a)} className="btn-secondary btn-sm">生成变体</button>
          )}
        </>
      )}
    </AssetPreviewDialog>
  );
}
