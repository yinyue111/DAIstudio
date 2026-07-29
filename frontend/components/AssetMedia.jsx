"use client";

import { useEffect, useState } from "react";
import { API_BASE, api, assetDownloadObjectUrl } from "../lib/api";
import { normalizeLoopbackPlatformMediaUrl } from "../lib/platformMedia";
import { isStoredUserAsset } from "../lib/unifiedAssets";

const PLATFORM_MEDIA_PREFIXES = ["/media/", "/api/uploads/"];

function isPlatformMediaPath(pathname) {
  return PLATFORM_MEDIA_PREFIXES.some((prefix) => String(pathname || "").startsWith(prefix));
}

function mediaAllowlistOrigins() {
  const origins = new Set();
  if (typeof window !== "undefined") origins.add(window.location.origin);
  if (API_BASE) {
    try {
      origins.add(new URL(API_BASE, typeof window !== "undefined" ? window.location.origin : undefined).origin);
    } catch (e) {
      // Invalid operator-provided media origins are ignored by the allowlist.
    }
  }
  for (const source of String(process.env.NEXT_PUBLIC_MEDIA_SRC || "").split(/[,\s]+/)) {
    if (!source || source === "'self'" || source === "self") continue;
    try {
      origins.add(new URL(source).origin);
    } catch (e) {
      // Invalid operator-provided media origins are ignored by the allowlist.
    }
  }
  return origins;
}

export function safeAssetMediaSrc(src) {
  const value = String(src || "").trim();
  if (!value) return "";
  if (value.includes("\\")) return "";
  if (value.startsWith("//")) return "";
  if (value.startsWith("blob:") || value.startsWith("/")) return value;
  if (typeof window === "undefined") return "";
  try {
    const url = new URL(value, window.location.origin);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    const origins = mediaAllowlistOrigins();
    if (origins.has(url.origin)) return url.toString();
    const normalized = normalizeLoopbackPlatformMediaUrl(url, {
      apiBase: API_BASE,
      pageOrigin: window.location.origin,
    });
    if (!normalized) return "";
    return origins.has(new URL(normalized).origin) ? normalized : "";
  } catch (e) {
    return "";
  }
}

export function assetPreviewSrc(asset) {
  if (asset?.type === "video") {
    return safeAssetMediaSrc(
      asset?.display_thumb
      || asset?.preview_url
      || asset?.thumb
      || asset?.display_url
      || asset?.hd_url
      || asset?.url
      || "",
    );
  }
  return safeAssetMediaSrc(
    asset?.display_url
    || asset?.display_thumb
    || asset?.preview_url
    || asset?.thumb
    || asset?.hd_url
    || asset?.url
    || "",
  );
}

export function isAssetTakenDown(asset) {
  return asset?.moderation_status && asset.moderation_status !== "active";
}

export function assetUnavailableText(asset) {
  if (isAssetTakenDown(asset)) return "素材已下架";
  return "预览暂不可用";
}

export function assetDisplaySrc(asset, { playbackUrl = "", interactive = false } = {}) {
  if (isStoredUserAsset(asset)) {
    if (asset?.type === "video") {
      return safeAssetMediaSrc(
        interactive
          ? (playbackUrl || asset.url || asset.hd_url || asset.preview_url || asset.thumb || "")
          : (asset.preview_url || asset.thumb || asset.url || asset.hd_url || ""),
      );
    }
    return safeAssetMediaSrc(
      playbackUrl || asset.url || asset.hd_url || asset.preview_url || asset.thumb || "",
    );
  }
  if (asset?.type === "video" && asset.unlocked) {
    return safeAssetMediaSrc(playbackUrl || asset.preview_url || asset.hd_url || "");
  }
  return assetPreviewSrc(asset);
}

export function shouldRenderVideo(asset, { playbackUrl = "", interactive = false } = {}) {
  if (asset?.type !== "video") return false;
  if (isStoredUserAsset(asset)) {
    const playableUrl = playbackUrl || asset.url || asset.hd_url || "";
    if (interactive) return Boolean(playableUrl);
    const posterOrPreview = asset.preview_url || asset.thumb || "";
    if (posterOrPreview) return /\.(mp4|webm|mov)(\?|$)/i.test(posterOrPreview);
    return /\.(mp4|webm|mov)(\?|$)/i.test(playableUrl);
  }
  if (playbackUrl) return true;
  return Boolean(
    asset.preview_url
    && (
      asset.preview_url.includes("/media/video_preview/")
      || /\.(mp4|webm|mov)(\?|$)/i.test(asset.preview_url)
    ),
  );
}

export function isPreviewVideoAsset(asset) {
  return asset?.type === "video" && asset?.unlocked && !asset?.hd_url;
}

export function canDownloadAsset(asset) {
  return Boolean(
    asset?.available !== false
    && !isAssetTakenDown(asset)
    && (isStoredUserAsset(asset) || asset?.unlocked),
  );
}

export function assetPreviewLabel(asset) {
  if (isAssetTakenDown(asset)) return "素材已下架";
  if (asset?.origin === "fetched") return "素材抓取 · 可用于创作";
  if (asset?.origin === "uploaded") return "我的上传 · 可用于创作";
  if (!asset?.unlocked) return "预览 · 带水印";
  return isPreviewVideoAsset(asset) ? "视频 · 可下载" : "预览 · 已解锁，可下载";
}

export function isAudioAsset(asset) {
  return asset?.type === "audio";
}

export function audioAssetSrc(asset) {
  if (!isAudioAsset(asset)) return "";
  return safeAssetMediaSrc(asset?.url || asset?.hd_url || asset?.preview_url || "");
}

export function formatAudioDuration(value) {
  const seconds = Math.round(Number(value) || 0);
  if (seconds <= 0) return "";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function AudioCard({ asset, src, className, fallbackClassName, interactive, controls, autoPlay, onError }) {
  const name = String(asset?.filename || asset?.title || "").trim() || "音频素材";
  const duration = formatAudioDuration(asset?.duration_seconds ?? asset?.duration);
  const showPlayer = Boolean(src && (interactive || controls));
  return (
    <div className={`${fallbackClassName || className || ""} flex flex-col items-center justify-center gap-2 px-3 py-4 text-center`}>
      <span aria-hidden="true" className="flex h-10 w-10 items-end justify-center gap-[3px] rounded-full bg-white/10 px-2.5 py-2 text-mist">
        {[45, 75, 100, 60, 85].map((height, index) => (
          <span key={index} className="w-[3px] rounded-full bg-current opacity-80" style={{ height: `${height}%` }} />
        ))}
      </span>
      <span className="max-w-full truncate text-xs text-mist" title={name}>{name}</span>
      {duration && <span className="text-[11px] text-fog">{duration}</span>}
      {showPlayer && (
        <audio
          src={src}
          controls
          autoPlay={autoPlay}
          preload="metadata"
          className="w-full max-w-[240px]"
          onError={onError}
        />
      )}
    </div>
  );
}

function VideoPoster({ src, className, fallbackClassName, onError }) {
  return (
    <div className={`relative ${fallbackClassName || className || ""}`}>
      <img
        src={safeAssetMediaSrc(src)}
        alt=""
        loading="lazy"
        className="h-full w-full object-cover"
        onError={onError}
      />
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/10">
        <span className="flex h-10 w-10 items-center justify-center rounded-full bg-black/55 text-white shadow-lg ring-1 ring-white/30">
          <span className="ml-0.5 block h-0 w-0 border-y-[7px] border-l-[11px] border-y-transparent border-l-current" />
        </span>
      </div>
    </div>
  );
}

export default function AssetMedia({
  asset,
  interactive = false,
  className = "",
  fallbackClassName = "",
  autoPlay = false,
  controls = false,
  muted = true,
  playsInline = true,
  onError,
}) {
  const [playbackUrl, setPlaybackUrl] = useState("");
  const [imagePreviewUrl, setImagePreviewUrl] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setPlaybackUrl("");
    setImagePreviewUrl("");
    setError("");
    if (!interactive || !asset?.unlocked || isAssetTakenDown(asset)) return;
    if (isStoredUserAsset(asset)) return;
    if (asset.type === "image" && asset.hd_url) {
      let objectUrl = "";
      assetDownloadObjectUrl(asset.id)
        .then((url) => {
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          objectUrl = url;
          setImagePreviewUrl(url);
        })
        .catch((e) => {
          if (!cancelled) {
            if (!asset.preview_url) setError(e.message || "预览加载失败");
            if (onError) onError(e);
          }
        });
      return () => {
        cancelled = true;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
      };
    }
    if (asset.type !== "video") return;
    setPlaybackUrl(api.playbackUrl(asset.id));
    return () => {
      cancelled = true;
    };
  }, [asset?.id, asset?.origin, asset?.type, asset?.unlocked, asset?.hd_url, asset?.moderation_status, interactive]);

  const src = safeAssetMediaSrc(
    imagePreviewUrl || assetDisplaySrc(asset, { playbackUrl, interactive }),
  );
  const renderVideo = shouldRenderVideo(asset, { playbackUrl, interactive });
  if (error) {
    return (
      <div className={fallbackClassName || className}>
        {error}
      </div>
    );
  }
  if (isAudioAsset(asset)) {
    if (isAssetTakenDown(asset)) {
      return (
        <div className={fallbackClassName || className}>
          {assetUnavailableText(asset)}
        </div>
      );
    }
    return (
      <AudioCard
        asset={asset}
        src={audioAssetSrc(asset)}
        className={className}
        fallbackClassName={fallbackClassName}
        interactive={interactive}
        controls={controls}
        autoPlay={autoPlay}
        onError={(e) => {
          setError("音频加载失败，请下载后播放");
          if (onError) onError(e);
        }}
      />
    );
  }
  if (!src) {
    return (
      <div className={fallbackClassName || className}>
        {assetUnavailableText(asset)}
      </div>
    );
  }
  if (renderVideo) {
    return (
      <video
        src={src}
        controls={controls}
        autoPlay={autoPlay}
        muted={muted}
        playsInline={playsInline}
        preload={interactive ? "metadata" : "metadata"}
        className={className}
        onError={(e) => {
          setError("视频加载失败，请重新打开或下载查看");
          if (onError) onError(e);
        }}
      />
    );
  }
  if (asset?.type === "video" && src) {
    return (
      <VideoPoster
        src={src}
        className={className}
        fallbackClassName={fallbackClassName}
        onError={(e) => {
          setError("预览加载失败");
          if (onError) onError(e);
        }}
      />
    );
  }
  return (
    <img
      src={src}
      alt=""
      loading={interactive ? undefined : "lazy"}
      className={className}
      onError={(e) => {
        setError("预览加载失败");
        if (onError) onError(e);
      }}
    />
  );
}
