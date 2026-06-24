"use client";

import {
  IMAGE_QUALITY_PRESETS,
  RATIOS,
  TERMINAL_TASK_STATUSES,
  VIDEO_RATIO_KEYS,
} from "./constants.js";

export function assetDims(a) {
  if (!a) return null;
  let w = Number(a.width), h = Number(a.height);
  if (!w || !h) { w = Number(a.thumb_width); h = Number(a.thumb_height); }
  if (!w || !h) return null;
  return { width: w, height: h };
}

export function selectedLabel(a) {
  if (!a) return "";
  const dims = assetDims(a);
  const dimText = dims ? `${dims.width}x${dims.height}` : "未识别尺寸";
  if (a.original_url) return `已本地化 · ${dimText}`;
  if (a.original_thumb) return `视频封面 · ${dimText}`;
  if (a.url?.includes("/api/uploads/upload/")) return `上传图片 · ${dimText}`;
  if (a.url?.includes("/api/uploads/upload_video/")) return `上传视频 · ${dimText}`;
  return `链接素材 · ${dimText}`;
}

export function mediaThumbSrc(a) {
  return a?.display_thumb || a?.display_url || a?.thumb || a?.url || "";
}

export function assetSignature(a) {
  if (!a) return "";
  return [a.type || "", a.url || "", a.thumb || ""].join("|");
}

export function buildSourceAssetMeta(asset) {
  if (!asset) return null;
  const isUploaded = String(asset.url || "").includes("/api/uploads/");
  return {
    selected_type: asset.type || "",
    selected_url: asset.url || "",
    selected_thumb: asset.thumb || "",
    original_url: asset.original_url || "",
    original_thumb: asset.original_thumb || "",
    source_page_url: asset.source_page_url || "",
    source_captured_at: asset.source_captured_at || "",
  };
}

export function composePromptFromStructured(structured, fallbackText = "") {
  const fallback = String(fallbackText || "").trim();
  if (!structured || !Object.keys(structured).length) return fallback;
  const finalText = String(structured.final_text || structured["final_text"] || fallbackText || "").trim();
  const order = [
    "主体", "商品服装", "细节特征", "场景背景", "广告目标", "风格", "景别", "构图",
    "视角镜头", "视角构图", "主体动作", "镜头运动", "运动节奏", "剪辑节奏", "时序分镜",
    "字幕卖点", "光线", "色调配色", "材质纹理", "氛围情绪", "后期质感", "转场", "一致性约束",
  ];
  const skip = new Set(["负向", "标签", "文字水印", "时长建议", "final_text"]);
  const parts = [];
  for (const k of order) if (structured[k] && !skip.has(k)) parts.push(structured[k]);
  for (const k of Object.keys(structured))
    if (!order.includes(k) && !skip.has(k) && structured[k]) parts.push(structured[k]);
  let text = parts.join(", ");
  if (structured["标签"]) text += (text ? ", " : "") + structured["标签"];
  return text.trim() || finalText;
}

export function composeStyleTransferPrompt(structured, fallbackText = "", { video = false } = {}) {
  const fallback = String(fallbackText || "").trim();
  if (!structured || !Object.keys(structured).length) return fallback;
  const allow = video
    ? [
        "场景背景", "广告目标", "风格", "视角构图", "镜头运动", "剪辑节奏",
        "时序分镜", "字幕卖点", "光线", "色调配色", "材质纹理", "氛围情绪",
        "转场", "时长建议", "后期质感", "标签",
      ]
    : [
        "场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图",
        "光线", "色调配色", "材质纹理", "氛围情绪", "后期质感", "标签",
      ];
  const parts = [];
  for (const key of allow) {
    const value = String(structured[key] || "").trim();
    if (value && value !== "无" && value !== "未见" && value !== "不确定") parts.push(value);
  }
  return parts.join(", ").trim() || fallback;
}

export function ratioKeyForSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || ""));
  if (!m) return null;
  return nearestRatio(Number(m[1]), Number(m[2]));
}

export function qualityKeyForSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || ""));
  if (!m) return "1k";
  const maxSide = Math.max(Number(m[1]), Number(m[2]));
  if (maxSide >= 3500) return "4k";
  if (maxSide >= 1500) return "2k";
  return "1k";
}

export function imageSizeFor(ratio, quality, maxDim = 4096) {
  const preset = IMAGE_QUALITY_PRESETS.find((q) => q.key === quality) || IMAGE_QUALITY_PRESETS[0];
  const maxSide = Math.min(Number(maxDim) || 4096, preset.maxSide);
  if (ratio.w >= ratio.h) {
    const width = maxSide;
    const height = roundImageDim((maxSide * ratio.h) / ratio.w, maxDim);
    return `${width}x${height}`;
  }
  const height = maxSide;
  const width = roundImageDim((maxSide * ratio.w) / ratio.h, maxDim);
  return `${width}x${height}`;
}

export function roundImageDim(value, maxDim = 4096) {
  const capped = Math.max(64, Math.min(Number(maxDim) || 4096, Math.round(value)));
  return Math.max(64, Math.min(Number(maxDim) || 4096, Math.round(capped / 8) * 8));
}

export function boundedImageCount(value, max = 8) {
  const limit = Math.max(1, Number(max) || 8);
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 1;
  return Math.max(1, Math.min(limit, parsed));
}

export function boundedVideoDuration(value, max = 900) {
  const limit = Math.max(1, Number(max) || 900);
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 5;
  return Math.max(1, Math.min(limit, parsed));
}

export function formatDuration(seconds) {
  const s = Math.max(1, Number.parseInt(seconds, 10) || 1);
  if (s < 60) return `${s}s`;
  const minutes = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${minutes}m${rest}s` : `${minutes}min`;
}

export function videoRatioOptions() {
  return RATIOS.filter((r) => VIDEO_RATIO_KEYS.has(r.key));
}

export function nearestRatio(w, h, options = RATIOS) {
  const r = w / h;
  let best = options[0] || RATIOS[0], diff = Infinity;
  for (const x of options) {
    const d = Math.abs(r - x.w / x.h);
    if (d < diff) { diff = d; best = x; }
  }
  return best.key;
}

export function mediaAspectStyle(a, fallback = "1 / 1") {
  const dims = assetDims(a);
  if (!dims) return fallback ? { aspectRatio: fallback } : null;
  return { aspectRatio: `${dims.width} / ${dims.height}` };
}

export function isTerminalTaskStatus(s) {
  return TERMINAL_TASK_STATUSES.has(s);
}

export function statusZh(s) {
  return { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败", needs_review: "待确认" }[s] || s;
}

export function statusStyle(s) {
  return {
    queued: "bg-white/10 text-mist",
    running: "bg-aqua/15 text-aqua",
    succeeded: "bg-ok/15 text-ok",
    failed: "bg-bad/15 text-bad",
    needs_review: "bg-warn/15 text-warn",
  }[s] || "bg-white/10 text-mist";
}

export function isRequestTimeoutError(e) {
  return String(e?.message || "").includes("请求超时");
}

export function taskResultTitle(task, runningSnapshot) {
  if (!task) return "本次生成";
  if (task.category === "image") {
    const assetsCount = Number(task.assets?.length || 0);
    const saved = Number(task.saved_count ?? assetsCount);
    const snapshotCount = runningSnapshot?.category === "image" ? runningSnapshot.n : null;
    const requestedValue = task.requested_count ?? snapshotCount ?? assetsCount;
    const requested = Number(requestedValue || 1);
    if (task.partial) return `本次生成 · ${saved}/${requested} 张`;
    if (!isTerminalTaskStatus(task.status)) return `本次生成 · ${requested} 张`;
    return `本次生成 · ${saved || requested} 张`;
  }
  if (task.category === "video") {
    return task.stage === "final" ? "本次生成 · 完整视频" : "本次生成 · 视频预览";
  }
  return "本次生成";
}
