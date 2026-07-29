export type ReverseCategory = "image" | "video";
export type ReverseAnalysisPrecision = "fast" | "standard" | "fine" | "ultra";

export interface ReverseConfigOption {
  key: string;
  label: string;
  description: string;
}

export interface ReverseSourceRange {
  start_seconds: number;
  end_seconds: number;
}

export interface ReverseConfig {
  analysis_focus: string;
  analysis_precision: ReverseAnalysisPrecision;
  output_purpose: string;
  custom_instruction: string;
  source_range: ReverseSourceRange | null;
  source_ranges: ReverseSourceRange[];
  custom_keyframes: number[];
  include_audio: boolean;
}

export interface ReverseConfigValidationError {
  field: keyof ReverseConfig;
  message: string;
}

export const MAX_REVERSE_CUSTOM_INSTRUCTION_LENGTH = 500;
// 关键帧上限以后端为准：backend/app/schemas/reverse.py 的 custom_keyframes
// Field(max_length=36)。此前前端为 24，用户设 25-36 个会被静默截断后提交。
// 超限时 validateReverseConfig 会给出错误提示，而不是静默改数。
export const MAX_REVERSE_CUSTOM_KEYFRAMES = 36;
export const MAX_REVERSE_SOURCE_RANGES = 8;
export const MAX_REVERSE_SELECTED_DURATION_SECONDS = 300;

export const IMAGE_REVERSE_FOCUS_OPTIONS: readonly ReverseConfigOption[] = [
  { key: "comprehensive", label: "全面拆解", description: "分析主体、场景、构图、光线、色彩、材质和文字。" },
  { key: "replica", label: "同款复刻", description: "提取可直接用于复刻整体画面的生成要素。" },
  { key: "style", label: "风格提取", description: "只迁移风格、色彩和质感，不复刻原主体。" },
  { key: "product_ad", label: "商品拆解", description: "识别商品结构、包装、Logo、材质和不可改项。" },
  { key: "portrait", label: "人像摄影", description: "分析人物、妆发、服装、姿态、机位和布光。" },
  { key: "poster_layout", label: "海报版式", description: "提取文案、字体风格、信息层级和版式区域。" },
  { key: "composition_lighting", label: "构图光影", description: "聚焦景别、视角、空间关系、光位和色调。" },
];

export const VIDEO_REVERSE_FOCUS_OPTIONS: readonly ReverseConfigOption[] = [
  { key: "comprehensive", label: "全面拆解", description: "分析画面、动作、运镜、剪辑、文字和当前环境可提供的音频证据。" },
  { key: "camera_motion", label: "镜头运动", description: "聚焦机位、景别、推拉摇移、跟随和环绕。" },
  { key: "subject_action", label: "人物动作", description: "提取主体动作、方向、速度、幅度和时序。" },
  { key: "product_ad", label: "商品广告", description: "分析商品展示动作、卖点镜头和广告节奏。" },
  { key: "storyboard", label: "分镜脚本", description: "按镜头输出时间、画面、动作、运镜和转场。" },
  { key: "editing_rhythm", label: "剪辑节奏", description: "聚焦镜头切换间隔、转场和信息密度；音乐节拍只引用已验证证据。" },
  { key: "audio_script", label: "对白文案", description: "仅根据已完成的 ASR、说话人与音频证据整理对白、旁白和节奏。" },
];

export const REVERSE_OUTPUT_PURPOSE_OPTIONS: Readonly<Record<ReverseCategory, readonly ReverseConfigOption[]>> = {
  image: [
    { key: "generation", label: "直接生成", description: "输出可直接用于图片模型的提示词。" },
    { key: "style_transfer", label: "风格迁移", description: "输出不复刻原主体的风格迁移指令。" },
    { key: "edit", label: "图片编辑", description: "输出保留原图并执行局部修改的编辑指令。" },
    { key: "analysis_report", label: "分析报告", description: "保留观察事实和结构化拆解，不直接生成。" },
  ],
  video: [
    { key: "generation", label: "直接生成", description: "输出可直接用于视频模型的提示词。" },
    { key: "style_transfer", label: "风格迁移", description: "输出迁移动作、镜头和风格的生成指令。" },
    { key: "storyboard", label: "分镜脚本", description: "输出可拆分提交的多镜头分镜脚本。" },
    { key: "analysis_report", label: "分析报告", description: "保留镜头、音频和证据拆解，不直接生成。" },
  ],
};

// 兜底精度档位：仅在尚未拿到 /api/config 下发的 reverse.video_presets 时使用。
// 必须与后端 backend/app/schemas/reverse.py 的 ReverseAnalysisPrecision Literal 完全一致，
// 权威档位以服务端下发为准（见 registerReversePrecisionOptions）。
export const REVERSE_PRECISION_OPTIONS: readonly ReverseConfigOption[] = [
  { key: "fast", label: "快速", description: "较少证据采样，适合快速获得方向。" },
  { key: "standard", label: "标准", description: "平衡证据覆盖、耗时和费用。" },
  { key: "fine", label: "精细", description: "提高证据覆盖，适合复杂画面和多镜头视频。" },
  { key: "ultra", label: "超精细", description: "高密度镜头与动作分析，适合复杂多镜头素材。" },
];

// /api/config 下发的精度档位（reverse.video_presets）。拿到后以服务端为准，
// 避免前端白名单与后端枚举漂移；拿不到时回退 REVERSE_PRECISION_OPTIONS。
let dynamicPrecisionOptions: ReverseConfigOption[] | null = null;

export function registerReversePrecisionOptions(presets: unknown): void {
  if (!Array.isArray(presets)) return;
  const options = presets
    .map((item) => {
      const raw = item && typeof item === "object" && !Array.isArray(item)
        ? item as Record<string, unknown>
        : {};
      const key = String(raw.key ?? "").trim();
      return key
        ? {
            key,
            label: String(raw.label ?? key),
            description: String(raw.description ?? raw.frame_range ?? ""),
          }
        : null;
    })
    .filter((item): item is ReverseConfigOption => Boolean(item));
  if (options.length > 0) dynamicPrecisionOptions = options;
}

export function reversePrecisionOptions(): readonly ReverseConfigOption[] {
  return dynamicPrecisionOptions ?? REVERSE_PRECISION_OPTIONS;
}

export function isReverseAnalysisPrecision(value: unknown): value is ReverseAnalysisPrecision {
  return reversePrecisionOptions().some((option) => option.key === value);
}

export const DEFAULT_REVERSE_CONFIG: Readonly<ReverseConfig> = Object.freeze({
  analysis_focus: "comprehensive",
  analysis_precision: "standard",
  output_purpose: "generation",
  custom_instruction: "",
  source_range: null,
  source_ranges: [],
  custom_keyframes: [],
  include_audio: true,
});

export function reverseConfigForSourceChange(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const retained = { ...(value as Record<string, unknown>) };
  delete retained.source_range;
  delete retained.source_ranges;
  delete retained.custom_keyframes;
  delete retained.keyframes;
  return {
    ...retained,
    source_range: null,
    source_ranges: [],
    custom_keyframes: [],
  };
}

function optionKeys(options: readonly ReverseConfigOption[]) {
  return new Set(options.map((option) => option.key));
}

function finiteNumber(value: unknown) {
  if (value === "" || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function rawRange(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  return {
    start: finiteNumber(raw.start_seconds ?? raw.start),
    end: finiteNumber(raw.end_seconds ?? raw.end),
  };
}

function normalizeRange(value: unknown, duration?: number | null): ReverseSourceRange | null {
  const parsed = rawRange(value);
  if (!parsed || parsed.start === null || parsed.end === null || parsed.end <= parsed.start) return null;
  const maxDuration = finiteNumber(duration);
  const start = Math.max(0, parsed.start);
  const end = maxDuration !== null ? Math.min(maxDuration, parsed.end) : parsed.end;
  if (end <= start) return null;
  return { start_seconds: start, end_seconds: end };
}

function requestedRanges(raw: Record<string, unknown>) {
  if (Array.isArray(raw.source_ranges) && raw.source_ranges.length > 0) return raw.source_ranges;
  return raw.source_range ? [raw.source_range] : [];
}

function normalizeRanges(value: unknown, duration?: number | null) {
  if (!Array.isArray(value)) return [];
  const normalized = value
    .map((item) => normalizeRange(item, duration))
    .filter((item): item is ReverseSourceRange => Boolean(item))
    .sort((left, right) => left.start_seconds - right.start_seconds || left.end_seconds - right.end_seconds)
    .slice(0, MAX_REVERSE_SOURCE_RANGES);
  return normalized.reduce<ReverseSourceRange[]>((merged, item) => {
    const previous = merged.at(-1);
    if (previous && item.start_seconds <= previous.end_seconds) {
      previous.end_seconds = Math.max(previous.end_seconds, item.end_seconds);
    } else {
      merged.push({ ...item });
    }
    return merged;
  }, []);
}

function normalizeKeyframes(value: unknown, duration?: number | null) {
  if (!Array.isArray(value)) return [];
  const maxDuration = finiteNumber(duration);
  return [...new Set(
    value
      .map(finiteNumber)
      .filter((item): item is number => item !== null && item >= 0)
      .filter((item) => maxDuration === null || item <= maxDuration),
  )].sort((left, right) => left - right).slice(0, MAX_REVERSE_CUSTOM_KEYFRAMES);
}

export function normalizeReverseConfig(
  value: unknown,
  {
    category = "image",
    selectedType,
    duration,
  }: { category?: ReverseCategory; selectedType?: ReverseCategory | null; duration?: number | null } = {},
): ReverseConfig {
  const raw = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const focusOptions = category === "video" ? VIDEO_REVERSE_FOCUS_OPTIONS : IMAGE_REVERSE_FOCUS_OPTIONS;
  const purposeOptions = REVERSE_OUTPUT_PURPOSE_OPTIONS[category];
  const requestedFocus = String(raw.analysis_focus ?? raw.focus ?? "").trim();
  const requestedPrecision = String(
    raw.analysis_precision ?? raw.precision ?? DEFAULT_REVERSE_CONFIG.analysis_precision,
  ).trim();
  const requestedPurpose = String(raw.output_purpose ?? raw.purpose ?? "").trim();
  const defaultPurpose = "generation";
  const videoSource = category === "video" && selectedType !== "image";
  const sourceRanges = videoSource ? normalizeRanges(requestedRanges(raw), duration) : [];
  return {
    analysis_focus: optionKeys(focusOptions).has(requestedFocus)
      ? requestedFocus
      : focusOptions[0].key,
    analysis_precision: (isReverseAnalysisPrecision(requestedPrecision)
      ? requestedPrecision
      : DEFAULT_REVERSE_CONFIG.analysis_precision) as ReverseAnalysisPrecision,
    output_purpose: optionKeys(purposeOptions).has(requestedPurpose)
      ? requestedPurpose
      : defaultPurpose,
    custom_instruction: String(raw.custom_instruction ?? "").trim().slice(0, MAX_REVERSE_CUSTOM_INSTRUCTION_LENGTH),
    source_range: sourceRanges.length === 1 ? sourceRanges[0] : null,
    source_ranges: sourceRanges,
    custom_keyframes: videoSource ? normalizeKeyframes(raw.custom_keyframes ?? raw.keyframes, duration) : [],
    include_audio: videoSource && (
      raw.include_audio === undefined ? true : Boolean(raw.include_audio)
    ),
  };
}

export function validateReverseConfig(
  value: unknown,
  {
    category = "image",
    selectedType,
    duration,
  }: { category?: ReverseCategory; selectedType?: ReverseCategory | null; duration?: number | null } = {},
) {
  const raw = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const errors: ReverseConfigValidationError[] = [];
  const focusOptions = category === "video" ? VIDEO_REVERSE_FOCUS_OPTIONS : IMAGE_REVERSE_FOCUS_OPTIONS;
  const focus = String(raw.analysis_focus ?? raw.focus ?? DEFAULT_REVERSE_CONFIG.analysis_focus).trim();
  if (!optionKeys(focusOptions).has(focus)) {
    errors.push({ field: "analysis_focus", message: "请选择当前媒体类型支持的分析目标。" });
  }
  const precision = String(raw.analysis_precision ?? raw.precision ?? DEFAULT_REVERSE_CONFIG.analysis_precision).trim();
  if (!isReverseAnalysisPrecision(precision)) {
    errors.push({ field: "analysis_precision", message: "分析精度无效。" });
  }
  const purpose = String(raw.output_purpose ?? raw.purpose ?? "").trim();
  if (purpose && !optionKeys(REVERSE_OUTPUT_PURPOSE_OPTIONS[category]).has(purpose)) {
    errors.push({ field: "output_purpose", message: "请选择当前媒体类型支持的输出用途。" });
  }
  const instruction = String(raw.custom_instruction ?? "").trim();
  if (instruction.length > MAX_REVERSE_CUSTOM_INSTRUCTION_LENGTH) {
    errors.push({ field: "custom_instruction", message: `补充要求不能超过 ${MAX_REVERSE_CUSTOM_INSTRUCTION_LENGTH} 字。` });
  }
  const rawRanges = requestedRanges(raw);
  const maxDuration = finiteNumber(duration);
  if (rawRanges.length > MAX_REVERSE_SOURCE_RANGES) {
    errors.push({ field: "source_ranges", message: `最多选择 ${MAX_REVERSE_SOURCE_RANGES} 个分析片段。` });
  }
  const validatedRanges: ReverseSourceRange[] = [];
  for (const rawItem of rawRanges) {
    const range = rawRange(rawItem);
    if (!range) {
      errors.push({ field: "source_ranges", message: "分析片段格式无效。" });
      continue;
    }
    if (range.start === null || range.end === null) {
      errors.push({ field: "source_ranges", message: "时间范围必须同时填写开始和结束时间。" });
    } else if (range.start < 0 || range.end <= range.start) {
      errors.push({ field: "source_ranges", message: "结束时间必须大于开始时间，且开始时间不能小于 0。" });
    } else if (maxDuration !== null && range.end > maxDuration) {
      errors.push({ field: "source_ranges", message: "时间范围不能超过素材时长。" });
    } else {
      validatedRanges.push({ start_seconds: range.start, end_seconds: range.end });
    }
  }
  const mergedRanges = normalizeRanges(validatedRanges, duration);
  const selectedDuration = mergedRanges.reduce(
    (total, item) => total + item.end_seconds - item.start_seconds,
    0,
  );
  if (selectedDuration > MAX_REVERSE_SELECTED_DURATION_SECONDS) {
    errors.push({
      field: "source_ranges",
      message: `片段累计分析时长不能超过 ${MAX_REVERSE_SELECTED_DURATION_SECONDS} 秒。`,
    });
  }
  const keyframes = Array.isArray(raw.custom_keyframes ?? raw.keyframes)
    ? raw.custom_keyframes ?? raw.keyframes as unknown[]
    : [];
  if (Array.isArray(keyframes)) {
    if (keyframes.length > MAX_REVERSE_CUSTOM_KEYFRAMES) {
      errors.push({ field: "custom_keyframes", message: `最多选择 ${MAX_REVERSE_CUSTOM_KEYFRAMES} 个关键帧。` });
    } else if (keyframes.some((item) => {
      const time = finiteNumber(item);
      return time === null || time < 0 || (maxDuration !== null && time > maxDuration);
    })) {
      errors.push({ field: "custom_keyframes", message: "关键帧时间必须位于素材时长范围内。" });
    } else if (mergedRanges.length > 0 && keyframes.some((item) => {
      const time = finiteNumber(item);
      return time !== null && !mergedRanges.some(
        (range) => range.start_seconds <= time && time <= range.end_seconds,
      );
    })) {
      errors.push({ field: "custom_keyframes", message: "关键帧必须位于某个已选分析片段内。" });
    }
  }
  return {
    valid: errors.length === 0,
    errors,
    value: normalizeReverseConfig(raw, { category, selectedType, duration }),
  };
}
