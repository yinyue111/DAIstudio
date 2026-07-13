"use client";

import type { AppConfig, ReverseVideoAnalysis, WorkspaceState } from "../../lib/types";
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
  "光线", "色调配色", "氛围情绪", "转场",
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
const VIDEO_TRANSFER_IDENTITY_KEYS = [
  "主体", "人像意图", "人物比例", "身材体态", "体态线条", "服装结构", "服装覆盖",
  "妆发五官", "商品服装", "细节特征", "标签", "品牌Logo", "包装文字", "不可改项",
];
const GENERIC_IDENTITY_WORDS = new Set([
  "product", "products", "video", "image", "model", "logo", "brand", "bottle", "package", "packaging",
]);
const UNSAFE_VIDEO_TRANSFER_IDENTITY_RE = /(?:原|参考)(?:片|视频|画面|素材)?(?:主体|商品|产品|品牌|人物|模特|人脸|身份|服装)|(?:original|reference)\s+(?:product|subject|brand|person|character|model|face|identity|clothing)/i;
const GENERIC_CJK_IDENTITY_PART_RE = /(?:上传|参考|原|视频|画面|素材|唯一|同一|主体|商品|产品|人物|模特|人脸|身份|服装|包装|文字|品牌|标志|正面|背面|侧面|顶部|底部|包含|显示|可见|保持|位于|置于|放置于|摆放于|放在|摆在|悬挂在|靠在|站在|坐在|处于|出现在|写有|印有|为)/g;
const CJK_SCENE_PLACEMENT_TAIL_RE = /(?:位于|置于|放置于|摆放于|放在|摆在|悬挂在|靠在|站在|坐在|处于|出现在).*$/g;
const EXPLICIT_CJK_IDENTITY_RE = /(?:品牌|标志|包装文字|正面文字|背面文字|侧面文字|写有|印有)/i;
const VIDEO_POST_PRODUCTION_CLAUSE_RE = /(^|[\n。；;，,])\s*(?:(?:温柔|轻柔|低沉)?\s*(?:男声|女声|女性|男性)?\s*)?(?:字幕|后期叠字|(?:画面(?:中)?)?文字(?:浮现|出现|显示)?|画面(?:中)?(?:浮现|出现|显示)文字|旁白|voiceover|后期配音|音效|sfx|环境音)\s*[:：]?\s*(?:["'“‘][^"'”’]+["'”’]|[^\n。；;，,]+)\s*(?:浮现|出现|显示)?\s*/gi;

function styleTransferKeys({ video = false, subject = "" } = {}) {
  const base = video ? VIDEO_STYLE_TRANSFER_KEYS : IMAGE_STYLE_TRANSFER_KEYS;
  if (video && (subject === "product" || subject === "portrait")) {
    return [
      "场景背景", "风格", "视角构图",
      ...VIDEO_SUBJECT_MOTION_KEYS,
      "镜头运动", "剪辑节奏", "光线", "色调配色",
      "氛围情绪", "转场", "时长建议", "后期质感", "平台质感",
    ];
  }
  if (subject === "product" || subject === "portrait") return base;
  return [...base, ...GENERAL_STYLE_TRANSFER_EXTRA_KEYS];
}

function rewriteVideoSubjectMotion(value, subject = "", structured = {}) {
  const text = String(value || "").trim();
  if (!text || text === "无" || text === "未见" || text === "不确定") return "";
  const safeMotion = hasReferenceIdentityLeak(text, structured) ? "" : text;
  if (subject === "product") {
    return [
      "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物",
      "复用参考片的展示节奏、入镜顺序、角度切换、慢速推拉、稳定特写和卖点展示等可迁移动作",
      safeMotion ? `具体可迁移动作：${safeMotion}` : "",
      "优先保持完整包装、Logo 和主要文字始终在画面内，避免裁切主体、侧面展示或快速旋转",
      "不要生成参考片里的原商品、原品牌、人物或服装",
    ].filter(Boolean).join("；");
  }
  if (subject === "portrait") {
    return [
      "上传人物作为唯一视频主体，替换参考片中的原人物身份",
      "复用参考片的动作节奏、走位、姿态变化、镜头调度和分镜顺序",
      safeMotion ? `具体可迁移动作：${safeMotion}` : "",
      "不要生成参考片里的原人物、人脸身份或品牌主体",
    ].filter(Boolean).join("；");
  }
  return text;
}

export function styleTransferStructured(structured, { video = false, subject = "" } = {}) {
  if (!structured || !Object.keys(structured).length) return {};
  const allowed = new Set(styleTransferKeys({ video, subject }));
  const out = {};
  const motionCandidates = VIDEO_SUBJECT_MOTION_KEYS.map((key) => ({
    key,
    value: String(structured[key] || "").trim(),
  })).filter(({ value }) => value && value !== "无" && value !== "未见" && value !== "不确定");
  const preferredMotionKey = (
    motionCandidates.find(({ value }) => !hasReferenceIdentityLeak(value, structured))
    || motionCandidates[0]
  )?.key;
  for (const key of Object.keys(structured)) {
    if (video && (subject === "product" || subject === "portrait") && VIDEO_SUBJECT_MOTION_TRANSFER_KEYS.has(key)) {
      if (key !== preferredMotionKey) continue;
      const value = rewriteVideoSubjectMotion(structured[key], subject, structured);
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
  const seen = new Set();
  for (const key of styleTransferKeys({ video, subject })) {
    const value = String(transferStructured[key] || "").trim();
    const identity = value.replace(/\s+/g, "").toLowerCase();
    if (identity && !seen.has(identity)) {
      seen.add(identity);
      parts.push(value);
    }
  }
  return parts.join(", ").trim() || fallback;
}

function referenceIdentityTokens(structured) {
  const tokens = new Set();
  for (const key of VIDEO_TRANSFER_IDENTITY_KEYS) {
    const value = String(structured?.[key] || "");
    for (const token of value.match(/[A-Za-z][A-Za-z0-9_-]{2,}/g) || []) {
      const normalized = token.toLowerCase();
      if (!GENERIC_IDENTITY_WORDS.has(normalized)) tokens.add(normalized);
    }
    for (const segment of value.match(/[\u3400-\u9fff]{2,}/g) || []) {
      const preserveIdentitySuffix = EXPLICIT_CJK_IDENTITY_RE.test(segment);
      const distinctive = (preserveIdentitySuffix
        ? segment
        : segment.replace(CJK_SCENE_PLACEMENT_TAIL_RE, ""))
        .replace(GENERIC_CJK_IDENTITY_PART_RE, " ")
        .replace(/\s+/g, "")
        .trim();
      if (!distinctive) continue;
      if (distinctive.length <= 4) {
        tokens.add(distinctive);
        continue;
      }
      for (let index = 0; index <= distinctive.length - 4; index += 1) {
        tokens.add(distinctive.slice(index, index + 4));
      }
    }
  }
  return tokens;
}

function hasReferenceIdentityLeak(value, structured) {
  const candidate = String(value || "").trim();
  if (!candidate) return false;
  if (UNSAFE_VIDEO_TRANSFER_IDENTITY_RE.test(candidate)) return true;
  const normalized = candidate.toLowerCase();
  for (const token of referenceIdentityTokens(structured)) {
    if (normalized.includes(String(token).toLowerCase())) return true;
  }
  return false;
}

function stripVideoPostProductionClauses(value) {
  return String(value || "")
    .replace(VIDEO_POST_PRODUCTION_CLAUSE_RE, "$1")
    .replace(/([。；;，,])\s*(?=[。；;，,])/g, "")
    .replace(/^[\s。；;，,]+|[\s。；;，,]+$/g, "")
    .trim();
}

export function composeSafeVideoTransferPrompt(structured, subject = "") {
  const fallback = composeStyleTransferPrompt(structured, "", { video: true, subject });
  const candidate = stripVideoPostProductionClauses(structured?.["迁移生成指令"]);
  if (!candidate) return fallback;
  if (hasReferenceIdentityLeak(candidate, structured)) return fallback;
  return candidate;
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

type ReverseVideoWorkspacePatchArgs = {
  analysis?: ReverseVideoAnalysis | null;
  current?: Partial<WorkspaceState>;
  startedRatio?: string;
  startedDuration?: number;
  maxDuration?: number;
  productVideo?: boolean;
};

export function reverseVideoWorkspacePatch({
  analysis,
  current = {},
  startedRatio,
  startedDuration,
  maxDuration = MAX_VIDEO_DURATION_SECONDS,
  productVideo = false,
}: ReverseVideoWorkspacePatchArgs = {}): Partial<WorkspaceState> {
  if (!analysis || typeof analysis !== "object") return { reverseVideoAnalysis: null };
  const patch: Partial<WorkspaceState> = { reverseVideoAnalysis: analysis };
  const source = analysis.source || {};
  const width = Number(source.width);
  const height = Number(source.height);
  const exactRatio = videoRatioOptions().find((item) => item.key === source.ratio)?.key;
  const inferredRatio = width > 0 && height > 0
    ? nearestRatio(width, height, videoRatioOptions())
    : null;
  if (String(current.ratio || "") === String(startedRatio || "") && (exactRatio || inferredRatio)) {
    patch.ratio = exactRatio || inferredRatio;
  }

  const sourceDuration = Number(source.duration_seconds);
  if (
    Number.isFinite(sourceDuration)
    && sourceDuration > 0
    && Number(current.vDuration) === Number(startedDuration)
  ) {
    patch.vDuration = boundedVideoDuration(Math.round(sourceDuration), maxDuration);
  }

  const multiShot = Array.isArray(analysis.shots) && analysis.shots.length > 1;
  if (
    productVideo
    && (multiShot || sourceDuration > 5)
    && current.videoProductLockMode === "locked"
    && (!current.videoProductTemplate || current.videoProductTemplate === "stable_showcase")
  ) {
    patch.videoProductLockMode = "free";
    patch.videoProductTemplate = "reference_sequence";
  }
  return patch;
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
