"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";

export function assetPreviewSrc(asset) {
  return asset?.preview_url || asset?.hd_url || "";
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
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setPlaybackUrl("");
    setError("");
    if (!interactive || asset?.type !== "video" || !asset.unlocked) return;
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
  }, [asset?.id, asset?.type, asset?.unlocked, interactive]);

  const src = assetDisplaySrc(asset, { playbackUrl });
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
        预览暂不可用
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
