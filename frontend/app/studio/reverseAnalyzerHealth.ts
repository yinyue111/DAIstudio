import type {
  AnalyzerHealthStatus,
  ReverseAnalyzerStatus,
} from "../../lib/api";

type AnalyzerCapabilityDefinition<Key extends string = string> = {
  key: Key;
  label: string;
  unsupportedLabel: "未配置" | "不可用";
  falseFlag?: string;
  falseFlagReason?: string;
  modelFallback?: boolean;
};

export const IMAGE_ANALYZER_CAPABILITIES = [
  { key: "ocr", label: "OCR 文字识别", unsupportedLabel: "不可用" },
  {
    key: "region_proposal",
    label: "区域提议（非语义）",
    unsupportedLabel: "不可用",
    falseFlag: "semantic_detection",
    falseFlagReason: "仅提供非语义区域提议，不代表商品或人物检测。",
  },
  { key: "detector", label: "语义检测", unsupportedLabel: "未配置" },
  { key: "segmenter", label: "精细分割", unsupportedLabel: "未配置" },
] as const satisfies readonly AnalyzerCapabilityDefinition[];

export const VIDEO_ANALYZER_CAPABILITIES = [
  { key: "subject_tracking", label: "主体追踪", unsupportedLabel: "未配置", modelFallback: true },
  { key: "pose", label: "姿态", unsupportedLabel: "未配置", modelFallback: true },
  { key: "action", label: "动作", unsupportedLabel: "未配置", modelFallback: true },
  { key: "transition", label: "转场", unsupportedLabel: "未配置", modelFallback: true },
  { key: "camera_motion", label: "运镜", unsupportedLabel: "不可用" },
] as const satisfies readonly AnalyzerCapabilityDefinition[];

export const AUDIO_ANALYZER_CAPABILITIES = [
  { key: "asr", label: "ASR", unsupportedLabel: "未配置" },
  { key: "speaker", label: "说话人", unsupportedLabel: "未配置" },
  {
    key: "music",
    label: "音乐倾向",
    unsupportedLabel: "不可用",
    falseFlag: "semantic_classification",
    falseFlagReason: "基于本地音频信号特征，不是语义音乐分类。",
  },
  { key: "beat", label: "BPM/节拍", unsupportedLabel: "不可用" },
  {
    key: "sfx",
    label: "瞬态音效",
    unsupportedLabel: "不可用",
    falseFlag: "semantic_classification",
    falseFlagReason: "仅检测瞬态声学事件，不识别具体音效类别。",
  },
] as const satisfies readonly AnalyzerCapabilityDefinition[];

export type ImageAnalyzerCapabilityKey = (typeof IMAGE_ANALYZER_CAPABILITIES)[number]["key"];
export type VideoAnalyzerCapabilityKey = (typeof VIDEO_ANALYZER_CAPABILITIES)[number]["key"];
export type AudioAnalyzerCapabilityKey = (typeof AUDIO_ANALYZER_CAPABILITIES)[number]["key"];
export type AnalyzerCapabilityUiStatus = AnalyzerHealthStatus | "unknown";
export type AudioAnalyzerUiStatus = AnalyzerCapabilityUiStatus;

export type AnalyzerCapabilityView<Key extends string = string> = {
  key: Key;
  label: string;
  status: AnalyzerCapabilityUiStatus;
  statusLabel: string;
  reason: string | null;
  configured: boolean | null;
  fallbackLabel: string | null;
};

export type AnalyzerCapabilitySummary<Key extends string = string> = {
  explicitlyUnavailable: boolean;
  capabilities: AnalyzerCapabilityView<Key>[];
  summary: string;
  issues: string | null;
};

export type ImageAnalyzerCapabilityView = AnalyzerCapabilityView<ImageAnalyzerCapabilityKey>;
export type VideoAnalyzerCapabilityView = AnalyzerCapabilityView<VideoAnalyzerCapabilityKey>;
export type AudioAnalyzerCapabilityView = AnalyzerCapabilityView<AudioAnalyzerCapabilityKey>;
export type ImageAnalyzerSummary = AnalyzerCapabilitySummary<ImageAnalyzerCapabilityKey>;
export type VideoAnalyzerSummary = AnalyzerCapabilitySummary<VideoAnalyzerCapabilityKey>;
export type AudioAnalyzerSummary = AnalyzerCapabilitySummary<AudioAnalyzerCapabilityKey>;

const STATUS_LABELS: Record<Exclude<AnalyzerCapabilityUiStatus, "unsupported">, string> = {
  available: "可用",
  partial: "部分可用",
  degraded: "降级",
  unknown: "待确认",
};
const ANALYZER_HEALTH_CACHE_TTL_MS = 15_000;

let cachedAnalyzerHealth: { value: ReverseAnalyzerStatus; expiresAt: number } | null = null;
let analyzerHealthRequest: Promise<ReverseAnalyzerStatus> | null = null;

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function normalizedStatus(value: unknown): AnalyzerCapabilityUiStatus {
  const status = String(value || "").trim().toLowerCase();
  if (status === "analyzed") return "available";
  return ["available", "partial", "degraded", "unsupported"].includes(status)
    ? status as AnalyzerHealthStatus
    : "unknown";
}

function capabilityStatusLabel(
  status: AnalyzerCapabilityUiStatus,
  configured: boolean | null,
  unsupportedLabel: "未配置" | "不可用",
) {
  if (status !== "unsupported") return STATUS_LABELS[status];
  if (configured === false) return "未配置";
  if (configured === true) return "不可用";
  return unsupportedLabel;
}

function capabilityView<Key extends string>(
  section: Record<string, unknown>,
  definition: AnalyzerCapabilityDefinition<Key>,
): AnalyzerCapabilityView<Key> {
  const feature = objectValue(section[definition.key]);
  const status = normalizedStatus(feature?.status);
  const configured = typeof feature?.configured === "boolean" ? feature.configured : null;
  const explicitReason = String(feature?.degraded_reason || "").trim() || null;
  const truthfulReason = definition.falseFlag && feature?.[definition.falseFlag] === false
    ? definition.falseFlagReason || null
    : null;
  return {
    key: definition.key,
    label: definition.label,
    status,
    statusLabel: capabilityStatusLabel(status, configured, definition.unsupportedLabel),
    reason: explicitReason || truthfulReason,
    configured,
    fallbackLabel: definition.modelFallback && status !== "available"
      ? "主模型跨帧推断"
      : null,
  };
}

function capabilitySummary<Key extends string>(
  payload: ReverseAnalyzerStatus | Record<string, unknown> | null | undefined,
  sectionKey: "image" | "video" | "audio",
  definitions: readonly AnalyzerCapabilityDefinition<Key>[],
  unavailableSummary: string,
): AnalyzerCapabilitySummary<Key> {
  const root = objectValue(payload);
  const section = objectValue(root?.[sectionKey]);
  const capabilities = definitions.map((definition) => capabilityView(section || {}, definition));
  const explicitlyUnavailable = Boolean(section)
    && capabilities.every(({ status }) => status === "unsupported");
  if (!section) {
    return {
      explicitlyUnavailable: false,
      capabilities,
      summary: "能力状态未返回，提交后以后端实际分析结果为准。",
      issues: null,
    };
  }
  const issues = capabilities
    .filter(({ reason, status }) => Boolean(reason) && status !== "available")
    .map(({ label, reason }) => `${label}：${reason}`)
    .join("；") || null;
  if (explicitlyUnavailable) {
    return { explicitlyUnavailable, capabilities, summary: unavailableSummary, issues };
  }

  const available = capabilities.filter(({ status }) => status === "available").map(({ label }) => label);
  const limited = capabilities.filter(({ status }) => ["partial", "degraded"].includes(status)).map(({ label }) => label);
  const unconfigured = capabilities
    .filter(({ status, statusLabel }) => status === "unsupported" && statusLabel === "未配置")
    .map(({ label }) => label);
  const unavailable = capabilities
    .filter(({ status, statusLabel }) => status === "unsupported" && statusLabel === "不可用")
    .map(({ label }) => label);
  const unknown = capabilities.filter(({ status }) => status === "unknown").map(({ label }) => label);
  const modelFallback = capabilities
    .filter(({ fallbackLabel }) => Boolean(fallbackLabel))
    .map(({ label }) => label);
  const parts = [
    available.length ? `可用：${available.join("、")}` : "",
    limited.length ? `降级：${limited.join("、")}` : "",
    unconfigured.length ? `未配置：${unconfigured.join("、")}` : "",
    unavailable.length ? `不可用：${unavailable.join("、")}` : "",
    unknown.length ? `待确认：${unknown.join("、")}` : "",
    modelFallback.length ? `主模型跨帧推断：${modelFallback.join("、")}` : "",
  ].filter(Boolean);
  return {
    explicitlyUnavailable,
    capabilities,
    summary: `${parts.join("；")}。`,
    issues,
  };
}

export function summarizeImageAnalyzerHealth(
  payload: ReverseAnalyzerStatus | Record<string, unknown> | null | undefined,
): ImageAnalyzerSummary {
  return capabilitySummary(
    payload,
    "image",
    IMAGE_ANALYZER_CAPABILITIES,
    "当前图片独立分析能力不可用，本次仍以主反推结果为准。",
  );
}

export function summarizeVideoAnalyzerHealth(
  payload: ReverseAnalyzerStatus | Record<string, unknown> | null | undefined,
): VideoAnalyzerSummary {
  return capabilitySummary(
    payload,
    "video",
    VIDEO_ANALYZER_CAPABILITIES,
    "独立时序分析器未配置；主体追踪、姿态、动作和转场由主视觉模型基于多帧提供跨帧推断，运镜仍以后端光流证据为准。",
  );
}

export function summarizeAudioAnalyzerHealth(
  payload: ReverseAnalyzerStatus | Record<string, unknown> | null | undefined,
): AudioAnalyzerSummary {
  return capabilitySummary(
    payload,
    "audio",
    AUDIO_ANALYZER_CAPABILITIES,
    "当前音频分析能力不可用，本次反推不会提取音频证据。",
  );
}

export function loadReverseAnalyzerHealth(
  fetcher: () => Promise<ReverseAnalyzerStatus>,
): Promise<ReverseAnalyzerStatus> {
  const now = Date.now();
  if (cachedAnalyzerHealth && cachedAnalyzerHealth.expiresAt > now) {
    return Promise.resolve(cachedAnalyzerHealth.value);
  }
  if (analyzerHealthRequest) return analyzerHealthRequest;
  analyzerHealthRequest = Promise.resolve()
    .then(fetcher)
    .then((value) => {
      cachedAnalyzerHealth = {
        value,
        expiresAt: Date.now() + ANALYZER_HEALTH_CACHE_TTL_MS,
      };
      return value;
    })
    .finally(() => {
      analyzerHealthRequest = null;
    });
  return analyzerHealthRequest;
}

export function invalidateReverseAnalyzerHealthCache() {
  cachedAnalyzerHealth = null;
  analyzerHealthRequest = null;
}
