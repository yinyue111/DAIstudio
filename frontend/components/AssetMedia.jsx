"use client";

import { useEffect, useState } from "react";
import { API_BASE, api, assetDownloadObjectUrl } from "../lib/api";

const PLATFORM_MEDIA_PREFIXES = ["/media/", "/api/uploads/"];
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function isPlatformMediaPath(pathname) {
  return PLATFORM_MEDIA_PREFIXES.some((prefix) => String(pathname || "").startsWith(prefix));
}

function isLoopbackHost(hostname) {
  return LOOPBACK_HOSTS.has(String(hostname || "").toLowerCase());
}

function portOf(url) {
  if (url.port) return url.port;
  if (url.protocol === "https:") return "443";
  if (url.protocol === "http:") return "80";
  return "";
}

function resolvedApiUrl() {
  if (!API_BASE || typeof window === "undefined") return null;
  try {
    return new URL(API_BASE, window.location.origin);
  } catch (e) {
    return null;
  }
}

function normalizeLoopbackPlatformMediaUrl(url) {
  if (!isPlatformMediaPath(url.pathname) || !isLoopbackHost(url.hostname)) return "";
  const apiUrl = resolvedApiUrl();
  if (!apiUrl || portOf(url) !== portOf(apiUrl)) return "";
  return `${apiUrl.origin}${url.pathname}${url.search}${url.hash}`;
}

function mediaAllowlistOrigins() {
  const origins = new Set();
  if (typeof window !== "undefined") origins.add(window.location.origin);
  if (API_BASE) {
    try { origins.add(new URL(API_BASE, typeof window !== "undefined" ? window.location.origin : undefined).origin); } catch (e) {}
  }
  for (const source of String(process.env.NEXT_PUBLIC_MEDIA_SRC || "").split(/\s+/)) {
    if (!source || source === "'self'" || source === "self") continue;
    try { origins.add(new URL(source).origin); } catch (e) {}
  }
  return origins;
}

export function safeAssetMediaSrc(src) {
  const value = String(src || "").trim();
  if (!value) return "";
  if (value.startsWith("//")) return "";
  if (value.startsWith("blob:") || value.startsWith("/")) return value;
  if (typeof window === "undefined") return "";
  try {
    const url = new URL(value, window.location.origin);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    const origins = mediaAllowlistOrigins();
    if (origins.has(url.origin)) return url.toString();
    const normalized = normalizeLoopbackPlatformMediaUrl(url);
    if (!normalized) return "";
    return origins.has(new URL(normalized).origin) ? normalized : "";
  } catch (e) {
    return "";
  }
}

export function assetPreviewSrc(asset) {
  return safeAssetMediaSrc(asset?.preview_url || asset?.hd_url || "");
}

export function isAssetTakenDown(asset) {
  return asset?.moderation_status && asset.moderation_status !== "active";
}

export function assetUnavailableText(asset) {
  if (isAssetTakenDown(asset)) return "素材已下架";
  return "预览暂不可用";
}

export function assetDisplaySrc(asset, { playbackUrl = "" } = {}) {
  if (asset?.type === "video" && asset.unlocked) {
    return safeAssetMediaSrc(playbackUrl || asset.preview_url || asset.hd_url || "");
  }
  return assetPreviewSrc(asset);
}

export function shouldRenderVideo(asset, { playbackUrl = "" } = {}) {
  if (asset?.type !== "video") return false;
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

export function isDownscaledImageAsset(asset) {
  return false;
}

export function canDownloadAsset(asset) {
  return Boolean(asset?.unlocked && !isAssetTakenDown(asset));
}

export function assetPreviewLabel(asset) {
  if (isAssetTakenDown(asset)) return "素材已下架";
  if (!asset?.unlocked) return "预览 · 带水印";
  return isPreviewVideoAsset(asset) ? "视频 · 可下载" : "预览 · 已解锁，可下载";
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
    api.playbackTicket(asset.id)
      .then((res) => {
        if (!cancelled) setPlaybackUrl(api.playbackUrl(asset.id, res.ticket));
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e.message || "视频加载失败");
          if (onError) onError(e);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [asset?.id, asset?.type, asset?.unlocked, asset?.hd_url, asset?.moderation_status, interactive]);

  const src = safeAssetMediaSrc(imagePreviewUrl || assetDisplaySrc(asset, { playbackUrl }));
  const renderVideo = shouldRenderVideo(asset, { playbackUrl });
  if (error) {
    return (
      <div className={fallbackClassName || className}>
        {error}
      </div>
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
