"use client";

export const MODEL_SELECTION_USES = ["vision", "image", "video", "prompt"];
export const MODEL_SELECTION_STORAGE_KEY = "studio_model_selections_v1";

const USE_LABELS = {
  vision: "反推模型",
  image: "图片模型",
  video: "视频模型",
  prompt: "优化模型",
};

function cleanId(value) {
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function cleanCapabilities(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  return value && typeof value === "object" ? { ...value } : null;
}

function normalizeOption(raw, use) {
  if (!raw || typeof raw !== "object") return null;
  const id = cleanId(raw.id ?? raw.model_config_id ?? raw.config_id);
  if (!id || raw.enabled === false) return null;
  const displayName = String(
    raw.display_name || raw.name || raw.label || raw.model_name || raw.model_id || `模型 ${id}`,
  ).trim();
  const providerLabel = String(raw.provider_label || raw.provider_name || raw.provider || "").trim();
  const directPriceHint = String(raw.price_hint || raw.cost_hint || "").trim();
  const cost = Number(raw.cost_credits);
  return {
    id,
    use,
    display_name: displayName || `模型 ${id}`,
    provider_label: providerLabel,
    price_hint: directPriceHint || (Number.isFinite(cost) ? `${cost} 积分` : ""),
    is_default: Boolean(raw.is_default ?? raw.default),
    sort_order: Number(raw.sort_order ?? 0) || 0,
    capabilities: cleanCapabilities(raw.capabilities),
    model_id: String(raw.model_id || "").trim(),
    provider: String(raw.provider || "").trim(),
    cost_credits: Number.isFinite(cost) ? cost : 0,
    unlock_cost: Number(raw.unlock_cost) || 0,
    preview_cost: raw.preview_cost == null ? null : (Number(raw.preview_cost) || 0),
    final_cost: raw.final_cost == null ? null : (Number(raw.final_cost) || 0),
  };
}

export function normalizeModelOptions(config) {
  const rawOptions = config?.model_options;
  const grouped = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [use, []]));
  if (Array.isArray(rawOptions)) {
    for (const raw of rawOptions) {
      const use = String(raw?.use || "");
      if (!MODEL_SELECTION_USES.includes(use)) continue;
      const option = normalizeOption(raw, use);
      if (option) grouped[use].push(option);
    }
  } else if (rawOptions && typeof rawOptions === "object") {
    for (const use of MODEL_SELECTION_USES) {
      const rows = Array.isArray(rawOptions[use]) ? rawOptions[use] : [];
      grouped[use] = rows.map((raw) => normalizeOption(raw, use)).filter(Boolean);
    }
  }
  for (const use of MODEL_SELECTION_USES) {
    grouped[use].sort((left, right) => (
      Number(right.is_default) - Number(left.is_default)
      || left.sort_order - right.sort_order
      || left.id - right.id
    ));
  }
  return grouped;
}

function capabilityValue(capabilities, aliases) {
  if (Array.isArray(capabilities)) {
    return aliases.some((alias) => capabilities.includes(alias));
  }
  if (!capabilities || typeof capabilities !== "object") return null;
  const declared = aliases.filter((alias) => Object.prototype.hasOwnProperty.call(capabilities, alias));
  if (!declared.length) return null;
  return declared.some((alias) => capabilities[alias] !== false);
}

export function modelOptionSupports(option, requirements = []) {
  if (!option || !requirements.length) return true;
  const capabilities = option.capabilities;
  if (!capabilities) return true;
  return requirements.every((aliases) => capabilityValue(capabilities, aliases) !== false);
}

export function strictMultiReferenceLimit(option) {
  const capabilities = option?.capabilities;
  if (!capabilities || typeof capabilities !== "object" || Array.isArray(capabilities)) return 0;
  if (capabilities.multi_reference !== true) return 0;
  const raw = capabilities.max_reference_images;
  if (typeof raw !== "number" || !Number.isInteger(raw) || raw < 2) return 0;
  return raw;
}

export function validateMultiReferenceSelection(option, requiredCount, detailCount = 0) {
  const details = Number(detailCount || 0);
  if (details <= 0) return { ok: true, limit: strictMultiReferenceLimit(option), message: "" };
  const limit = strictMultiReferenceLimit(option);
  if (!limit) {
    return {
      ok: false,
      limit: 0,
      message: "当前模型未明确支持多参考图，不能添加产品细节图。",
    };
  }
  const required = Number(requiredCount || 0);
  if (!Number.isInteger(required) || required > limit) {
    return {
      ok: false,
      limit,
      message: `当前模型最多支持 ${limit} 张参考图，本次实际需要 ${required} 张。`,
    };
  }
  return { ok: true, limit, message: "" };
}

export function modelRequirements({ use, creationMode, selected, productAsset, subjectMode }) {
  if (use === "prompt") return [["prompt_optimization", "optimize_prompt", "text"]];
  if (use === "vision") {
    if (productAsset && subjectMode === "portrait") return [["portrait_profile"]];
    if (productAsset && subjectMode === "product") return [["product_profile"]];
    if (selected?.type === "video") return [["video_analysis", "video_input", "video"]];
    return [["image_analysis", "image_input", "vision"]];
  }
  if (use === "image") {
    const requirements = creationMode === "image_edit"
      ? [["image_to_image", "image_edit", "edit"]]
      : [["image_generation", "text_to_image", "image"]];
    if (creationMode !== "image_edit" && selected) {
      requirements.push(["reference_image", "image_reference", "image_input"]);
    } else if (selected && productAsset) {
      requirements.push(["multi_reference", "reference_image", "image_reference"]);
    }
    return requirements;
  }
  if (use === "video") {
    const requirements = [];
    if (creationMode === "video_edit" || productAsset || selected?.type === "image") {
      requirements.push(["image_to_video", "reference_image", "image_reference"]);
    } else if (selected?.type === "video") {
      requirements.push(["video_to_video", "reference_video", "video_reference"]);
    } else {
      requirements.push(["video_generation", "text_to_video", "video"]);
    }
    return requirements;
  }
  return [];
}

export function filterModelOptions(options, context) {
  const requirements = modelRequirements(context);
  return (Array.isArray(options) ? options : []).filter((option) => (
    modelOptionSupports(option, requirements)
  ));
}

export function resolveModelConfigId(options, preferredId) {
  const rows = Array.isArray(options) ? options : [];
  const preferred = cleanId(preferredId);
  if (preferred && rows.some((option) => option.id === preferred)) return preferred;
  return rows.find((option) => option.is_default)?.id || rows[0]?.id || null;
}

export function readModelSelections(storage, ownerId) {
  if (!storage || !ownerId) return {};
  try {
    const parsed = JSON.parse(storage.getItem(`${MODEL_SELECTION_STORAGE_KEY}:${ownerId}`) || "{}");
    return Object.fromEntries(MODEL_SELECTION_USES.map((use) => [use, cleanId(parsed?.[use])]));
  } catch (_error) {
    return {};
  }
}

export function writeModelSelections(storage, ownerId, selections) {
  if (!storage || !ownerId) return;
  const clean = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [use, cleanId(selections?.[use])]));
  storage.setItem(`${MODEL_SELECTION_STORAGE_KEY}:${ownerId}`, JSON.stringify(clean));
}

export function modelOptionName(option) {
  return String(option?.display_name || option?.model_id || "模型").trim();
}

export function modelOptionCostLabel(option, use) {
  const hint = String(option?.price_hint || "").trim();
  if (!hint) return "";
  const qualifier = {
    image: "单张",
    video: "基础",
    vision: "单次",
    prompt: "单次",
  }[use] || "参考";
  return `${qualifier} ${hint}`;
}

export default function StudioModelSelector({
  use,
  options = [],
  value = null,
  onChange,
  compact = false,
  disabled = false,
}) {
  const label = USE_LABELS[use] || "模型";
  const selectedId = resolveModelConfigId(options, value);
  const selectedOption = options.find((item) => item.id === selectedId) || options[0] || null;
  const selectedName = selectedOption ? modelOptionName(selectedOption) : "";
  const costLabel = selectedOption ? modelOptionCostLabel(selectedOption, use) : "";
  return (
    <div className={`flex min-w-0 flex-wrap items-center justify-end gap-1.5 text-[11px] text-fog ${compact ? "max-w-full" : ""}`}>
      <label className="flex min-w-0 items-center gap-1.5">
        <span className="shrink-0 font-display font-medium text-mist">{label}</span>
        <select
          aria-label={label}
          value={selectedId == null ? "" : String(selectedId)}
          onChange={(event) => onChange?.(cleanId(event.target.value))}
          disabled={disabled || options.length === 0}
          title={options.length ? `${label}：${selectedName}` : `${label}暂无可用选项`}
          className="h-8 min-w-0 max-w-[220px] rounded-lg border border-line2 bg-base2 px-2 text-xs text-snow outline-none transition hover:border-iris/60 focus:border-aqua disabled:cursor-not-allowed disabled:opacity-50"
        >
          {options.length === 0 ? (
            <option value="">暂无可用模型</option>
          ) : options.map((option) => (
            <option key={option.id} value={String(option.id)}>
              {modelOptionName(option)}
            </option>
          ))}
        </select>
      </label>
      {costLabel && (
        <span
          className="shrink-0 whitespace-nowrap rounded-md border border-aqua/20 bg-aqua/10 px-2 py-1 font-display text-[10px] font-medium text-aqua"
          aria-label={`${label}消耗 ${costLabel}`}
          title="模型计费参考；本次准确总额以提交按钮旁的预计消耗为准"
        >
          {costLabel}
        </span>
      )}
    </div>
  );
}
