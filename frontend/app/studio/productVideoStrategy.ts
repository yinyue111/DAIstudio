export const PRODUCT_VIDEO_STRATEGIES = [
  {
    key: "prompt_driven",
    label: "提示词驱动",
    description: "完整执行提示词中的产品动作、运镜和节奏。",
  },
  {
    key: "reference_sequence",
    label: "参考分镜",
    description: "沿用反推结果的镜头顺序、动作和转场。",
  },
  {
    key: "stable_showcase",
    label: "稳定陈列",
    description: "产品正面稳定入镜，变化集中在光影与背景。",
  },
  {
    key: "slow_push",
    label: "慢速推近",
    description: "低速推近或轻微拉远，突出包装和材质细节。",
  },
  {
    key: "handheld_display",
    label: "手持展示",
    description: "以自然手部交互展示产品，不遮挡关键结构。",
  },
  {
    key: "background_motion",
    label: "背景动效",
    description: "产品保持稳定，让光影、道具或布景产生动态。",
  },
  {
    key: "soft_splash",
    label: "轻水花",
    description: "水花或颗粒围绕产品运动，不覆盖文字与轮廓。",
  },
  {
    key: "single_clip_action",
    label: "单段动作",
    description: "用一个连续镜头完成核心产品动作并以细节收束。",
  },
] as const;

export type ProductVideoStrategy = typeof PRODUCT_VIDEO_STRATEGIES[number]["key"];
export type ProductVideoStrategyOption = typeof PRODUCT_VIDEO_STRATEGIES[number];

const STRATEGY_BY_KEY = new Map<ProductVideoStrategy, ProductVideoStrategyOption>(
  PRODUCT_VIDEO_STRATEGIES.map((strategy) => [strategy.key, strategy] as const),
);

export function normalizeProductVideoStrategyKey(value: unknown): ProductVideoStrategy | null {
  const key = String(value ?? "").trim().toLowerCase() as ProductVideoStrategy;
  return STRATEGY_BY_KEY.has(key) ? key : null;
}

export type ProductVideoStrategyCapabilities = {
  declared: boolean;
  legacyFallback: boolean;
  supported: boolean;
  strategies: ProductVideoStrategy[];
  options: ProductVideoStrategyOption[];
};

export function normalizeProductVideoStrategyCapabilities(
  capabilities: unknown,
): ProductVideoStrategyCapabilities {
  const isCapabilityRecord = Boolean(
    capabilities
    && typeof capabilities === "object"
    && !Array.isArray(capabilities),
  );
  const declared = isCapabilityRecord && Object.prototype.hasOwnProperty.call(
    capabilities,
    "product_video_templates",
  );
  const raw = declared
    ? (capabilities as Record<string, unknown>).product_video_templates
    : ["prompt_driven"];
  const strategies = Array.isArray(raw)
    ? [...new Set(raw.map(normalizeProductVideoStrategyKey).filter(Boolean))] as ProductVideoStrategy[]
    : [];
  const options = strategies
    .map((key) => STRATEGY_BY_KEY.get(key))
    .filter(Boolean) as ProductVideoStrategyOption[];

  return {
    declared,
    legacyFallback: !declared,
    supported: options.length > 0,
    strategies,
    options,
  };
}

export type ProductVideoStrategySelection = ProductVideoStrategyCapabilities & {
  requestedValue: ProductVideoStrategy | null;
  value: ProductVideoStrategy | null;
  effectiveValue: ProductVideoStrategy | null;
  fellBack: boolean;
};

export function resolveProductVideoStrategySelection(
  capabilities: unknown,
  requestedValue: unknown,
): ProductVideoStrategySelection {
  const normalized = normalizeProductVideoStrategyCapabilities(capabilities);
  const requested = normalizeProductVideoStrategyKey(requestedValue);
  const value = requested && normalized.strategies.includes(requested)
    ? requested
    : normalized.strategies[0] || null;

  return {
    ...normalized,
    requestedValue: requested,
    value,
    effectiveValue: value,
    fellBack: Boolean(value && value !== requested),
  };
}
