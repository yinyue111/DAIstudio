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

const VISUAL_PROMPT_LIMITS = {
  image: 420,
  video: 220,
  product_profile: 420,
  portrait_profile: 420,
  image_to_video: 280,
};
const VISUAL_CLAUSE_LIMITS = {
  image: 56,
  video: 64,
  product_profile: 72,
  portrait_profile: 72,
  image_to_video: 64,
};
const IMAGE_VISUAL_FIELD_LIMITS = {
  "主体": 48,
  "商品服装": 52,
  "妆发五官": 48,
  "服装结构": 48,
  "人物比例": 42,
  "身材体态": 42,
  "场景背景": 60,
  "构图": 50,
  "光线": 58,
  "色调配色": 48,
  "视角镜头": 48,
  "景别": 28,
  "风格": 32,
  "材质纹理": 40,
  "文字版式": 48,
  "氛围情绪": 28,
  "后期质感": 36,
  "一致性约束": 48,
  "细节特征": 36,
};
const VISUAL_PLACEHOLDER_VALUES = new Set([
  "无", "暂无", "无相关内容", "不适用", "未见", "未见明确卖点", "未见明确广告目标",
  "不确定", "未知", "未分析", "未支持", "无法确认", "无法判断", "证据不足",
  "不清晰", "看不清", "未识别",
]);

function stripUncertainVisualSentences(value) {
  const chunks = String(value || "").split(/([；;。.!！?？\n]+)/g);
  const direct = [];
  for (let index = 0; index < chunks.length; index += 2) {
    const sentence = String(chunks[index] || "").trim();
    const delimiter = String(chunks[index + 1] || "");
    if (
      sentence
      && !/(?:不确定|无法确认|无法判断|证据不足|未见|未识别|看不清|不清晰|疑似|(?<!不)猜测|(?<!不)推测|(?<!尽)可能(?!性)|possibly|maybe|unclear|might)/i.test(sentence)
    ) {
      direct.push(`${sentence}${delimiter}`);
    }
  }
  return direct.join("");
}

function stripVisualAnalysisScaffolding(value) {
  const text = String(value || "")
    .replace(
      /(^|[；;。.!！?？,，\n])\s*(?:未知|不确定项?|无法确认|无法判断|证据不足)\s*[:：]\s*[^；;。.!！?？\n]*(?:[；;。.!！?？]|$)/gi,
      "$1",
    )
    .replace(
      /(?:直接可见事实|视觉估计|视觉推断|观察事实|事实层|估计层|高置信(?:度)?推断|模型推断|低置信(?:度)?推断)\s*[:：]\s*/gi,
      "",
    )
    .replace(
      /(?:第\s*[一二三四五六七八九十\d]+\s*张\s*)?参考\s*(?:图|图片|素材)?\s*[一二三四五六七八九十\d]+\s*(?:中(?!央)|为|呈现|采用|的)?\s*/gi,
      "",
    )
    .replace(
      /(?:[,，、]\s*)?(?:(?:可|适合)(?:用于|迁移为)?\s*)?(?:小红书|抖音|tiktok|instagram|pinterest|社(?:交)?媒体(?:品牌)?(?:广告)?素材|社媒(?:品牌)?(?:广告)?素材|品牌网页广告|网页广告|电商(?:详情页|主图|海报|素材|包装视觉升级)|发布平台|发布渠道|平台归因)[^；;。.!！?？\n]*/gi,
      "",
    )
    .replace(
      /\b(?:masterpiece|best quality|high quality|ultra quality|ultra[- ]?detailed|highly detailed|extremely detailed|insanely detailed|ultra[- ]?high resolution|high[- ]?resolution|hi[- ]?res|premium texture|award[- ]?winning|trending on artstation|uhd|(?:4|8|16)k(?: resolution| quality)?)\b|(?:杰作|最佳质量|顶级质量|超高质量|高质量|专业级品质|广告级品质|商业级品质|超高清|高清画质|超清画质|高分辨率|超高分辨率|极致细节|细节拉满|获奖作品|顶级画质|顶级品质)/gi,
      "",
    )
    .replace(/(?:视觉估计\s*(?:接近|约为|约)?|接近|约为|约)\s*#[0-9a-f]{3,8}(?:\s*(?:-|~|至|到)\s*#[0-9a-f]{3,8})?/gi, "")
    .replace(/(?:约|大约)?(?:占(?:画面)?\s*)?\d+(?:\.\d+)?\s*%(?:\s*(?:-|~|至|到)\s*\d+(?:\.\d+)?\s*%)?/gi, "")
    .replace(/(?:[,，、]\s*)?(?:置信度|可信度)(?:为|约为)?\s*(?:高|中等?|低|\d+(?:\.\d+)?\s*%)/gi, "");
  return stripUncertainVisualSentences(text)
    .replace(/([,，;；、])(?:\s*[,，;；、])+/g, "$1")
    .replace(/(^|[；;。.!！?？])\s*[,，、]+/g, "$1")
    .replace(/(^|[；;。.!！?？,，、])\s*的(?=\S)/g, "$1")
    .replace(/\s+/g, " ")
    .replace(/^[,，;；、\s]+|[,，;；、\s]+$/g, "");
}

function cleanVisualStructuredValue(value) {
  if (typeof value !== "string") return "";
  const cleaned = stripVisualAnalysisScaffolding(value);
  const text = cleaned.replace(/[。.!！?？,，;；:：、\s]+$/g, "");
  if (!text || VISUAL_PLACEHOLDER_VALUES.has(text)) return "";
  const tokens = text
    .split(/[/|、,，;；]|或/g)
    .map((item) => item.trim())
    .filter(Boolean);
  if (tokens.length && tokens.every((item) => VISUAL_PLACEHOLDER_VALUES.has(item))) return "";
  return cleaned.replace(/[；;\s]+$/g, "");
}

function truncateVisualClause(value, limit) {
  if (value.length <= limit) return value;
  let candidate = value.slice(0, limit);
  const cut = Math.max(...["。", "；", ";", "，", ","].map((mark) => candidate.lastIndexOf(mark)));
  if (cut >= Math.max(12, Math.floor(limit / 2))) candidate = candidate.slice(0, cut);
  return candidate.replace(/[。；;，,、\s]+$/g, "");
}

function compactVisualPrompt(parts, target = "image") {
  const limit = VISUAL_PROMPT_LIMITS[target] || VISUAL_PROMPT_LIMITS.image;
  const clauseLimit = VISUAL_CLAUSE_LIMITS[target] || VISUAL_CLAUSE_LIMITS.image;
  const result = [];
  const seen = new Set();
  const clauses = parts.flatMap((part) => String(part || "").split(/[；;\n]+/));
  for (const rawPart of clauses) {
    let part = cleanVisualStructuredValue(rawPart);
    if (!part) continue;
    part = truncateVisualClause(part, clauseLimit);
    const identity = part.replace(/[。；;，,、\s]+$/g, "");
    if (!identity || seen.has(identity)) continue;
    const used = result.reduce((total, item) => total + item.length, 0) + result.length;
    const remaining = limit - used;
    if (remaining <= 0) break;
    if (part.length > remaining) {
      continue;
    }
    result.push(part);
    seen.add(identity);
  }
  return result.join("；").trim();
}

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
  { preferFallback = false, target = "image" } = {},
) {
  const fallback = String(fallbackText || "").trim();
  if (preferFallback && fallback) return fallback;
  if (!structured || !Object.keys(structured).length) return fallback;
  const finalText = String(structured.final_text || structured["final_text"] || fallbackText || "").trim();
  const order = visualStructuredFieldOrder(target);
  const parts = [];
  for (const key of order) {
    let value = cleanVisualStructuredValue(structured[key]);
    if (target === "image" && value) {
      value = truncateVisualClause(value, IMAGE_VISUAL_FIELD_LIMITS[key] || VISUAL_CLAUSE_LIMITS.image);
    }
    if (value) parts.push(value);
  }
  const text = compactVisualPrompt(parts, target);
  return text.trim() || finalText;
}

const LEGACY_VISUAL_PROMPT_NOISE_RE = /(?:直接可见事实|视觉估计|视觉推断|观察事实|事实层|估计层|高置信(?:度)?推断|模型推断|低置信(?:度)?推断|(?:未知|不确定项?|无法确认|证据不足)\s*[:：]|参考\s*(?:图|图片|素材)?\s*[一二三四五六七八九十\d]+|masterpiece|best\s+quality|high\s+quality|ultra[- ]?detailed|high[- ]?resolution|uhd|(?:4|8|16)k(?:\s+(?:resolution|quality))?|高质量|超高质量|广告级品质|专业级品质|平台质感|小红书|抖音|社媒品牌|品牌网页广告|发布平台|发布渠道)/i;

/** Recompile old provider prose only when it contains known analysis/output noise. */
export function normalizeRecoveredVisualPrompt(
  structured,
  fallbackText = "",
  { target = "image", preserveFallback = false } = {},
) {
  const fallback = String(fallbackText || "").trim();
  if (preserveFallback || !fallback || !LEGACY_VISUAL_PROMPT_NOISE_RE.test(fallback)) {
    return fallback || composePromptFromStructured(structured, "", { target });
  }
  return composePromptFromStructured(structured, "", { target })
    || cleanVisualStructuredValue(fallback);
}

const VISUAL_STRUCTURED_FIELDS = {
  image: [
    "主体", "商品服装", "妆发五官", "服装结构", "人物比例", "身材体态",
    "场景背景", "构图", "光线", "色调配色", "视角镜头", "景别", "风格",
    "材质纹理", "文字版式", "氛围情绪", "后期质感", "一致性约束",
    "细节特征", "人像意图", "体态线条", "服装覆盖",
  ],
  video: [
    "主体", "人像意图", "人物比例", "身材体态", "体态线条", "服装结构", "服装覆盖", "妆发五官",
    "商品服装", "细节特征", "场景背景", "广告目标", "风格", "视角构图",
    "光线", "色调配色", "材质纹理", "氛围情绪",
    "一致性约束",
  ],
  product_profile: [
    "产品品类", "品牌Logo", "包装文字", "包装结构", "主色材质", "形状比例",
    "关键图案", "卖点摘要", "展示角度", "主角约束", "不可改项", "可迁移项",
  ],
  portrait_profile: [
    "年龄语境", "脸型五官", "妆发", "肤质", "体型比例", "姿态表情", "服装", "配饰",
    "身份稳定特征", "不可改项", "可调整项",
  ],
  image_to_video: [
    "主体", "主体运动设计", "镜头运动设计", "时序设计", "静态观察", "场景背景",
    "视角构图", "光线", "色调配色", "材质纹理", "可动元素", "一致性约束",
  ],
};

export function visualStructuredFieldOrder(target = "image") {
  return VISUAL_STRUCTURED_FIELDS[target] || VISUAL_STRUCTURED_FIELDS.image;
}

export function visualStructuredFields(structured, target = "image") {
  if (!structured || typeof structured !== "object") return {};
  const allowed = new Set(visualStructuredFieldOrder(target));
  return Object.fromEntries(
    Object.entries(structured)
      .map(([key, value]) => [key, cleanVisualStructuredValue(value)])
      .filter(([key, value]) => allowed.has(key) && Boolean(value)),
  );
}

const VIDEO_EVIDENCE_SHOT_FIELDS = [
  "visual", "subject_tracking", "pose", "action", "camera", "lighting", "transition", "ocr", "audio_cue",
];
const VIDEO_DRAFT_SHOT_LABELS = {
  visual: "画面",
  subject_tracking: "主体追踪",
  pose: "姿态",
  action: "动作",
  camera: "运镜",
  lighting: "光线",
  transition: "转场",
  ocr: "画面文字",
  audio_cue: "声音",
};

const VIDEO_GENERATION_OUTPUT_SPEC_RE = /(?:输出规格|视频规格|生成规格|源视频规格)\s*[:：]\s*[^\n；;。]*/gi;
const VIDEO_GENERATION_RESOLUTION_RE = /(^|\D)\d{3,4}\s*[x×]\s*\d{3,4}(?!\d)/gi;
const VIDEO_GENERATION_RATIO_RE = /(^|\D)\d{1,2}\s*:\s*\d{1,2}(?!\d)\s*(?:画幅|比例|竖版|横版)?/gi;
const VIDEO_GENERATION_TIMESTAMP_LIST_RE = /(?:在|于)\s*(?:约\s*)?\d+(?:\.\d+)?\s*(?:秒|s)(?:\s*[、,，]\s*\d+(?:\.\d+)?\s*(?:秒|s))*\s*(?:等)?\s*/gi;
const VIDEO_GENERATION_TIME_RANGE_RE = /(^|\D)\d+(?:\.\d+)?\s*(?:-|–|—|~|至|到)\s*\d+(?:\.\d+)?\s*(?:秒|s)(?![A-Za-z])/gi;
const VIDEO_GENERATION_RUNTIME_RE = /(?:总时长|视频时长|成片时长|全片时长|片长|建议生成)\s*(?:为|约|建议)?\s*\d+(?:\.\d+)?\s*(?:秒钟?|seconds?|secs?|s)(?![A-Za-z])|\d+(?:\.\d+)?\s*秒钟?\s*(?=(?:视频|广告|短片|成片))/gi;
const VIDEO_GENERATION_RELATIVE_SECONDS_RE = /前\s*\d+(?:\.\d+)?\s*秒(?:内)?|\d+(?:\.\d+)?\s*秒(?:钟)?后|(?:持续|停留)\s*(?:约\s*)?\d+(?:\.\d+)?\s*秒(?:钟)?/gi;
const VIDEO_GENERATION_EXACT_BPM_RE = /(?:节拍(?:约|为)?\s*)?\d+(?:\.\d+)?\s*BPM/gi;
const VIDEO_GENERATION_UNCLASSIFIED_TRANSIENT_RE = /(?:检测到\s*)?\d+\s*个?\s*未分类瞬态声|未分类瞬态声/gi;
const VIDEO_GENERATION_RAW_AUDIO_SIGNAL_RE = /检测到节拍点|节拍点秒数\s*=\s*[^；;。\n]+|持续音乐可能性不确定|未检测到明显持续音乐/gi;
const LEGACY_VIDEO_GENERATION_PARAMETER_RE = /(?:输出规格|视频规格|生成规格|源视频规格)\s*[:：]|\d{3,4}\s*[x×]\s*\d{3,4}|\d{1,2}\s*:\s*\d{1,2}|\d+(?:\.\d+)?\s*(?:秒钟?|seconds?|secs?)|镜头\d+\s*[（(][^）)]*[）)]|\d+(?:\.\d+)?\s*BPM|未分类瞬态声/i;

export function normalizeLegacyVideoGenerationPrompt(value) {
  let text = String(value || "")
    .replace(
      /^\s*(?:输出规格|视频规格|生成规格|源视频规格)\s*[:：][^\n]*(?:\n|$)/gim,
      "",
    )
    .replace(
      /(镜头\d+)\s*[（(]\s*\d+(?:\.\d+)?\s*(?:-|–|—|~|至|到)\s*\d+(?:\.\d+)?\s*(?:秒|s)\s*[）)]\s*[:：]?/gi,
      "$1：",
    )
    .replace(VIDEO_GENERATION_TIMESTAMP_LIST_RE, "");
  text = stripNonExecutableAudioSignals(text)
    .replace(VIDEO_GENERATION_OUTPUT_SPEC_RE, "")
    .replace(VIDEO_GENERATION_RESOLUTION_RE, "$1")
    .replace(VIDEO_GENERATION_RATIO_RE, "$1")
    .replace(VIDEO_GENERATION_TIME_RANGE_RE, "$1")
    .replace(VIDEO_GENERATION_RUNTIME_RE, "")
    .replace(VIDEO_GENERATION_RELATIVE_SECONDS_RE, (token) => {
      if (token.trimStart().startsWith("前")) return "开场";
      if (/后\s*$/.test(token)) return "随后";
      return "";
    });
  return text
    .replace(/([,，;；、])(?:\s*[,，;；、])+/g, "$1")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^[ \t,，;；、。]+|[ \t,，;；、。]+$/g, "")
    .trim();
}

function cleanVideoGenerationValue(value) {
  let text = cleanVisualStructuredValue(value);
  text = text.replace(VIDEO_GENERATION_OUTPUT_SPEC_RE, "");
  text = text.replace(VIDEO_GENERATION_RESOLUTION_RE, "$1");
  text = text.replace(VIDEO_GENERATION_RATIO_RE, "$1");
  text = text.replace(VIDEO_GENERATION_TIMESTAMP_LIST_RE, "");
  text = text.replace(VIDEO_GENERATION_TIME_RANGE_RE, "$1");
  text = text.replace(VIDEO_GENERATION_RUNTIME_RE, "");
  text = text.replace(VIDEO_GENERATION_RELATIVE_SECONDS_RE, (token) => {
    if (token.trimStart().startsWith("前")) return "开场";
    if (/后\s*$/.test(token)) return "随后";
    return "";
  });
  return text
    .replace(/([,，;；、])(?:\s*[,，;；、])+/g, "$1")
    .replace(/\s+/g, " ")
    .replace(/^[\s,，;；。、]+|[\s,，;；。、]+$/g, "");
}

function stripNonExecutableAudioSignals(value) {
  return String(value || "")
    .replace(/检测到持续音乐可能性较高/gi, "持续背景音乐")
    .replace(VIDEO_GENERATION_EXACT_BPM_RE, "")
    .replace(VIDEO_GENERATION_UNCLASSIFIED_TRANSIENT_RE, "")
    .replace(VIDEO_GENERATION_RAW_AUDIO_SIGNAL_RE, "")
    .replace(/([,，;；、])(?:\s*[,，;；、])+/g, "$1")
    .replace(/(^|[；;\n])\s*(?:声音|音效)\s*[:：][ \t,，;；。、]*(?=[；;\n]|$)/g, "$1")
    .replace(/^[\s,，;；。、]+|[\s,，;；。、]+$/g, "");
}

function cleanVideoGenerationAudioValue(value) {
  return stripNonExecutableAudioSignals(cleanVideoGenerationValue(value));
}
const VIDEO_DRAFT_FIELD_LABELS = {
  主体: "主体",
  人像意图: "人物",
  人物比例: "人物比例",
  身材体态: "体态",
  体态线条: "体态线条",
  服装结构: "服装",
  服装覆盖: "服装覆盖",
  妆发五官: "妆发",
  商品服装: "产品/服装",
  细节特征: "关键细节",
  场景背景: "场景",
  广告目标: "叙事目标",
  风格: "风格",
  视角构图: "构图",
  光线: "光线",
  色调配色: "配色",
  材质纹理: "材质",
  氛围情绪: "氛围",
  一致性约束: "连续性约束",
  字幕卖点: "画面字幕",
  旁白: "旁白/对白",
  音效: "声音",
};

function composeCanonicalVideoDraft(
  initialParts,
  structured,
  videoAnalysis,
  { excludeReferenceIdentity = false, includePostProduction = true } = {},
) {
  const parts = [...initialParts].filter(Boolean);
  const source = videoAnalysis?.source && typeof videoAnalysis.source === "object"
    ? videoAnalysis.source
    : {};
  const shots = Array.isArray(videoAnalysis?.shots) ? videoAnalysis.shots : [];
  const sourceDuration = Math.max(
    Number(source.duration_seconds) || 0,
    ...shots.map((shot) => Number(shot?.end_seconds) || 0),
  );
  if (shots.length > 3) {
    parts.push(
      `复刻执行：参考片含 ${shots.length} 个高密度剪辑镜头；建议逐镜独立生成，单镜含多步动作或内部硬切时再按一段一个主动作/可见结果拆分，最后按原镜头顺序剪辑。`,
    );
  } else if (sourceDuration > MAX_VIDEO_DURATION_SECONDS) {
    parts.push("复刻执行：参考片时间线较长，建议逐镜分别生成后按顺序合成，不压缩、不省略原镜头。");
  }
  const frameTimes = new Map(
    (Array.isArray(videoAnalysis?.sampled_frames) ? videoAnalysis.sampled_frames : [])
      .map((frame) => [Number(frame?.index), Number(frame?.timestamp_seconds)])
      .filter(([index, timestamp]) => Number.isInteger(index) && Number.isFinite(timestamp)),
  );
  for (let index = 0; index < shots.length; index += 1) {
    const shot = shots[index];
    if (!shot || typeof shot !== "object") continue;
    const confidence = Number(shot.confidence);
    const evidenceIndices = Array.isArray(shot.evidence_frame_indices)
      ? [...new Set(shot.evidence_frame_indices.map(Number))].filter((value) => frameTimes.has(value))
      : [];
    if (!evidenceIndices.length || !Number.isFinite(confidence) || confidence <= 0) continue;
    const hasCrossFrameEvidence = new Set(evidenceIndices.map((value) => frameTimes.get(value))).size >= 2;
    const allowedFields = (
      hasCrossFrameEvidence
        ? VIDEO_EVIDENCE_SHOT_FIELDS
        : ["visual", "lighting", "ocr", "audio_cue"]
    ).filter((key) => includePostProduction || !["ocr", "audio_cue"].includes(key));
    const details = allowedFields
      .map((key) => {
        const value = key === "audio_cue"
          ? cleanVideoGenerationAudioValue(shot[key])
          : cleanVideoGenerationValue(shot[key]);
        if (!value || (excludeReferenceIdentity && hasReferenceIdentityLeak(value, structured))) return "";
        return `${VIDEO_DRAFT_SHOT_LABELS[key]}：${value}`;
      })
      .filter(Boolean);
    if (!details.length) continue;
    const segmentIndex = Number(shot.source_segment_index);
    const segment = Number.isInteger(segmentIndex) && segmentIndex > 0 ? `片段${segmentIndex} ` : "";
    parts.push(`${segment}镜头${index + 1}：${[...new Set(details)].join("；")}`);
  }
  return [...new Set(parts)].join("\n").trim();
}

export function composeEvidenceBackedVideoGenerationDraft(
  structured,
  videoAnalysis = null,
  providerFinalText = "",
) {
  const visual = visualStructuredFields(structured, "video");
  const parts = visualStructuredFieldOrder("video")
    .map((key) => {
      const value = cleanVideoGenerationValue(visual[key]);
      return value ? `${VIDEO_DRAFT_FIELD_LABELS[key] || key}：${value}` : "";
    })
    .filter(Boolean);
  for (const key of ["字幕卖点", "旁白", "音效"]) {
    const value = key === "音效"
      ? cleanVideoGenerationAudioValue(structured?.[key])
      : cleanVideoGenerationValue(structured?.[key]);
    if (value) parts.push(`${VIDEO_DRAFT_FIELD_LABELS[key]}：${value}`);
  }
  const draft = composeCanonicalVideoDraft(parts, structured, videoAnalysis);
  if (draft.includes("镜头")) return draft;
  const providerNarrative = cleanVideoGenerationAudioValue(providerFinalText);
  return [...new Set([draft, providerNarrative].filter(Boolean))].join("\n").trim();
}

export function withEvidenceBackedVideoGenerationDraft(result, target = "video") {
  if (target !== "video" || !result || typeof result !== "object") return result;
  const structured = result.structured && typeof result.structured === "object"
    ? result.structured
    : {};
  const storedAnalysis = result.video_analysis && typeof result.video_analysis === "object"
    ? result.video_analysis
    : {};
  const videoAnalysis = Array.isArray(storedAnalysis.shots)
    ? storedAnalysis
    : { ...storedAnalysis, shots: Array.isArray(result.shots) ? result.shots : [] };
  const draft = composeEvidenceBackedVideoGenerationDraft(
    structured,
    videoAnalysis,
    result.provider_final_text,
  );
  const storedFinalText = String(result.final_text || "").trim();
  const normalizedStoredFinalText = normalizeLegacyVideoGenerationPrompt(storedFinalText);
  const hasLegacyParameters = LEGACY_VIDEO_GENERATION_PARAMETER_RE.test(storedFinalText);
  const hasRebuiltShots = /(?:^|\n)(?:片段\d+\s*)?镜头\d+：/.test(draft);
  const useDraft = Boolean(
    draft
    && (
      draft.length > normalizedStoredFinalText.length
      || (hasLegacyParameters && hasRebuiltShots)
    )
  );
  const finalText = useDraft ? draft : normalizedStoredFinalText;
  return finalText && finalText !== storedFinalText
    ? { ...result, final_text: finalText }
    : result;
}

function videoSubjectTransferRules(subject = "") {
  if (subject === "product") {
    return [
      "上传产品作为唯一视频主体，替换参考片中的原主体/原商品/人物",
      "动作过程中保持同一 SKU 的包装结构、Logo、可见文字、颜色和材质纹理连续一致",
      "不要生成参考片里的原商品、原品牌、人物或服装",
    ];
  }
  if (subject === "portrait") {
    return [
      "上传人物作为唯一视频主体，替换参考片中的原人物身份",
      "动作过程中保持人脸、发型、体型比例和身份稳定特征连续一致",
      "不要生成参考片里的原人物、人脸身份或品牌主体",
    ];
  }
  return [];
}

export function composeEvidenceBackedVideoTransferPrompt(
  structured,
  videoAnalysis = null,
  subject = "",
) {
  const staticStructured = visualStructuredFields(
    styleTransferStructured(structured, { video: true, subject }),
    "video",
  );
  const parts = [
    ...videoSubjectTransferRules(subject),
    ...visualStructuredFieldOrder("video")
      .map((key) => cleanVisualStructuredValue(staticStructured[key]))
      .filter(Boolean),
  ].filter(Boolean);
  return composeCanonicalVideoDraft(parts, structured, videoAnalysis, {
    excludeReferenceIdentity: true,
    includePostProduction: false,
  });
}

export function isStructuredPortrait(structured) {
  return String(structured?.["图像类型"] || "").includes("人物");
}

export function shouldUseImageReference({
  isFinal = false,
  sourceAsset = null,
  isEditMode = false,
  structured = {},
  analysisFocus = "",
} = {}) {
  return Boolean(
    !isFinal
    && sourceAsset?.type === "image"
    && (
      isEditMode
      || analysisFocus === "replica"
      || Object.keys(structured || {}).length === 0
      || isStructuredPortrait(structured)
    )
  );
}

const IMAGE_STYLE_TRANSFER_KEYS = [
  "场景背景", "构图", "光线", "色调配色", "视角镜头", "视角构图",
  "景别", "风格", "氛围情绪", "后期质感",
];

const VIDEO_STYLE_TRANSFER_KEYS = [
  "场景背景", "广告目标", "风格", "视角构图", "镜头运动", "剪辑节奏", "时序分镜",
  "光线", "色调配色", "氛围情绪", "转场",
  "时长建议", "后期质感", "平台质感",
];

const GENERAL_STYLE_TRANSFER_EXTRA_KEYS = ["材质纹理"];
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
      "严格保留参考片中可迁移的具体动作、运镜、节奏和先后顺序，不得删除、替换或降速",
      safeMotion ? `具体可迁移动作：${safeMotion}` : "",
      "动作过程中保持同一 SKU 的包装结构、Logo、可见文字、颜色和材质纹理连续一致",
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
    const value = cleanVisualStructuredValue(structured[key]);
    if (value) out[key] = value;
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
    let value = cleanVisualStructuredValue(transferStructured[key]);
    if (!video && value) {
      value = truncateVisualClause(value, IMAGE_VISUAL_FIELD_LIMITS[key] || VISUAL_CLAUSE_LIMITS.image);
    }
    const identity = value.replace(/\s+/g, "").toLowerCase();
    if (identity && !seen.has(identity)) {
      seen.add(identity);
      parts.push(value);
    }
  }
  return compactVisualPrompt(parts, video ? "video" : "image") || fallback;
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
};

export function reverseVideoWorkspacePatch({
  analysis,
  current = {},
  startedRatio,
  startedDuration,
  maxDuration = MAX_VIDEO_DURATION_SECONDS,
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
