"use client";

import type { AppConfig } from "../../lib/types";
import {
  IMAGE_QUALITY_PRESETS,
  MAX_VIDEO_DURATION_SECONDS,
  RATIOS,
  TERMINAL_TASK_STATUSES,
  VIDEO_RATIO_KEYS,
} from "./constants";

type CreditConfig = Partial<AppConfig> & {
  pricing?: Record<string, any>;
  models?: Record<string, any>;
};
type ImagePricing = {
  edit_unit_costs?: Record<string, number>;
  unit_costs?: Record<string, number>;
};

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

export function composePromptFromStructured(
  structured,
  fallbackText = "",
  { preferFallback = false } = {},
) {
  const fallback = String(fallbackText || "").trim();
  if (preferFallback && fallback) return fallback;
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

export function isStructuredPortrait(structured) {
  return String(structured?.["图像类型"] || "").includes("人物");
}

export function shouldUseImageReference({
  isFinal = false,
  sourceAsset = null,
  isEditMode = false,
  structured = {},
} = {}) {
  return Boolean(
    !isFinal
    && sourceAsset?.type === "image"
    && (
      isEditMode
      || Object.keys(structured || {}).length === 0
      || isStructuredPortrait(structured)
    )
  );
}

const IMAGE_STYLE_TRANSFER_KEYS = [
  "场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图",
  "光线", "色调配色", "氛围情绪", "后期质感", "平台质感",
];

const VIDEO_STYLE_TRANSFER_KEYS = [
  "场景背景", "广告目标", "风格", "视角构图", "镜头运动", "剪辑节奏", "时序分镜",
  "字幕卖点", "光线", "色调配色", "氛围情绪", "转场",
  "时长建议", "后期质感", "平台质感",
];

const GENERAL_STYLE_TRANSFER_EXTRA_KEYS = ["材质纹理", "标签"];
const VIDEO_SUBJECT_MOTION_KEYS = ["可迁移主体动作", "主体动作", "产品展示方式"];
const VIDEO_SUBJECT_MOTION_TRANSFER_KEYS = new Set(["可迁移主体动作", "主体动作", "产品展示方式"]);

const REFERENCE_SUBJECT_KEYS = new Set([
  "图像类型", "反推重点", "主体", "人像意图", "人物比例", "身材体态",
  "体态线条", "服装结构", "服装覆盖", "身材曲线", "尺码三围", "露肤度",
  "妆发五官", "商品服装", "细节特征",
  "主体动作", "一致性约束", "负向", "文字水印", "final_text",
]);

function styleTransferKeys({ video = false, subject = "" } = {}) {
  const base = video ? VIDEO_STYLE_TRANSFER_KEYS : IMAGE_STYLE_TRANSFER_KEYS;
  if (video && (subject === "product" || subject === "portrait")) {
    return [
      "场景背景", "广告目标", "风格", "视角构图",
      ...VIDEO_SUBJECT_MOTION_KEYS,
      "镜头运动", "剪辑节奏", "时序分镜", "字幕卖点", "光线", "色调配色",
      "氛围情绪", "转场", "时长建议", "后期质感", "平台质感",
    ];
  }
  if (subject === "product" || subject === "portrait") return base;
  return [...base, ...GENERAL_STYLE_TRANSFER_EXTRA_KEYS];
}

function rewriteVideoSubjectMotion(value, subject = "") {
  const text = String(value || "").trim();
  if (!text || text === "无" || text === "未见" || text === "不确定") return "";
  if (subject === "product") {
    return [
      "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物",
      "复用参考片的展示节奏、入镜顺序、角度切换、慢速推拉、稳定特写和卖点展示等可迁移动作",
      "优先保持完整包装、Logo 和主要文字始终在画面内，避免裁切主体、侧面展示或快速旋转",
      "不要生成参考片里的原商品、原品牌、人物或服装",
    ].join("；");
  }
  if (subject === "portrait") {
    return [
      "上传人物作为唯一视频主体，替换参考片中的原人物身份",
      "复用参考片的动作节奏、走位、姿态变化、镜头调度和分镜顺序",
      "不要生成参考片里的原人物、人脸身份或品牌主体",
    ].join("；");
  }
  return text;
}

export function styleTransferStructured(structured, { video = false, subject = "" } = {}) {
  if (!structured || !Object.keys(structured).length) return {};
  const allowed = new Set(styleTransferKeys({ video, subject }));
  const out = {};
  for (const key of Object.keys(structured)) {
    if (video && (subject === "product" || subject === "portrait") && VIDEO_SUBJECT_MOTION_TRANSFER_KEYS.has(key)) {
      const value = rewriteVideoSubjectMotion(structured[key], subject);
      if (value) out[key] = value;
      continue;
    }
    if (!allowed.has(key) || REFERENCE_SUBJECT_KEYS.has(key)) continue;
    const value = String(structured[key] || "").trim();
    if (value && value !== "无" && value !== "未见" && value !== "不确定") out[key] = value;
  }
  return out;
}

export function composeStyleTransferPrompt(structured, fallbackText = "", { video = false, subject = "" } = {}) {
  const fallback = String(fallbackText || "").trim();
  if (!structured || !Object.keys(structured).length) return fallback;
  const transferStructured = styleTransferStructured(structured, { video, subject });
  const parts = [];
  for (const key of styleTransferKeys({ video, subject })) {
    const value = String(transferStructured[key] || "").trim();
    if (value) parts.push(value);
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
  if (maxSide > 1280) return "2k";
  return "1k";
}

export function imageSizeFor(ratio, quality, maxDim = 2048) {
  const preset = IMAGE_QUALITY_PRESETS.find((q) => q.key === quality) || IMAGE_QUALITY_PRESETS[0];
  const maxSide = Math.min(Number(maxDim) || 2048, preset.maxSide);
  const maxArea = maxSide * maxSide;
  const areaSide = Math.sqrt(maxArea / (ratio.w * ratio.h));
  const scale = Math.min(maxSide / Math.max(ratio.w, ratio.h), areaSide);
  const width = roundImageDim(ratio.w * scale, maxDim);
  const height = roundImageDim(ratio.h * scale, maxDim);
  return `${width}x${height}`;
}

export function roundImageDim(value, maxDim = 2048) {
  const limit = Number(maxDim) || 2048;
  const capped = Math.max(64, Math.min(limit, Math.round(value)));
  return Math.max(64, Math.min(limit, Math.floor(capped / 16) * 16));
}

export function boundedImageCount(value, max = 8) {
  const limit = Math.max(1, Number(max) || 8);
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 1;
  return Math.max(1, Math.min(limit, parsed));
}

export function videoDurationLimit(max = MAX_VIDEO_DURATION_SECONDS) {
  const configured = Math.max(1, Number(max) || MAX_VIDEO_DURATION_SECONDS);
  return Math.min(MAX_VIDEO_DURATION_SECONDS, configured);
}

export function boundedVideoDuration(value, max = MAX_VIDEO_DURATION_SECONDS) {
  const limit = videoDurationLimit(max);
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

export function imageQualityTierForSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || ""));
  if (!m) return "1k";
  const maxSide = Math.max(Number(m[1]), Number(m[2]));
  const area = Number(m[1]) * Number(m[2]);
  if (maxSide > 2560 || area >= 3840 * 2160 * 0.9) return "4k";
  if (maxSide > 1280) return "2k";
  return "1k";
}

export function estimateImageCredits(cfg: CreditConfig | null | undefined, { size, count = 1, edit = false }: { size?: string; count?: number; edit?: boolean } = {}) {
  const tier = imageQualityTierForSize(size);
  const pricing = (cfg?.pricing?.image || {}) as ImagePricing;
  const table = edit ? pricing.edit_unit_costs : pricing.unit_costs;
  const fallback = Number(cfg?.models?.image?.cost_credits || 0);
  const unit = Number(table?.[tier] ?? fallback);
  return Math.max(0, unit) * Math.max(1, Number(count) || 1);
}

export function estimateVideoFinalCredits(cfg, { resolution = "720p", duration = 5 } = {}) {
  const perSecond = cfg?.pricing?.video?.per_second || {};
  const unit = Number(perSecond?.[resolution] ?? cfg?.models?.video?.final_cost ?? cfg?.models?.video?.cost_credits ?? 0);
  return Math.max(0, unit) * Math.max(1, Number(duration) || 1);
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
  return { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败", needs_review: "待确认", canceled: "已取消", unknown: "状态未知" }[s] || s;
}

export function statusStyle(s) {
  return {
    queued: "bg-white/10 text-mist",
    running: "bg-aqua/15 text-aqua",
    succeeded: "bg-ok/15 text-ok",
    failed: "bg-bad/15 text-bad",
    needs_review: "bg-warn/15 text-warn",
    canceled: "bg-white/10 text-fog",
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
    return "本次生成 · 视频";
  }
  return "本次生成";
}
