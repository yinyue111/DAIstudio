"use client";

import { useEffect, useState } from "react";
import { api, assetDownloadObjectUrl } from "../lib/api";

export function assetPreviewSrc(asset) {
  return asset?.preview_url || asset?.hd_url || "";
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
    return playbackUrl || asset.preview_url || asset.hd_url || "";
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

function VideoPoster({ src, className, fallbackClassName, onError }) {
  return (
    <div className={`relative ${fallbackClassName || className || ""}`}>
      <img
        src={src}
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
            if (!asset.preview_url) setError(e.message || "高清预览加载失败");
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
          setError(e.message || "视频预览加载失败");
          if (onError) onError(e);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [asset?.id, asset?.type, asset?.unlocked, asset?.hd_url, asset?.moderation_status, interactive]);

  const src = imagePreviewUrl || assetDisplaySrc(asset, { playbackUrl });
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
          setError("视频预览加载失败，请重新打开或下载查看");
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
