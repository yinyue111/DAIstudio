"use client";

import {
  IMAGE_REVERSE_FOCUS_OPTIONS,
  REVERSE_OUTPUT_PURPOSE_OPTIONS,
  VIDEO_REVERSE_FOCUS_OPTIONS,
} from "./reverseConfig";

export default function StudioReverseIntentControls({
  category,
  config,
  disabled = false,
  onChange,
}) {
  const options = category === "video"
    ? VIDEO_REVERSE_FOCUS_OPTIONS
    : IMAGE_REVERSE_FOCUS_OPTIONS;

  function patch(next) {
    onChange?.({ ...config, ...next });
  }

  function selectFocus(focus) {
    patch({
      analysis_focus: focus,
      ...(category === "video" && focus === "audio_script" ? { include_audio: true } : {}),
    });
  }

  return (
    <section className="rounded-xl border border-line bg-black/15 p-2" aria-labelledby="reverse-intent-title">
      <div className="flex items-center justify-between gap-2">
        <h4 id="reverse-intent-title" className="text-xs font-display font-medium text-mist">
          {category === "video" ? "反推设置" : "反推目标"}
        </h4>
        {category === "image" && <span className="text-[11px] text-fog">决定重点分析什么</span>}
      </div>

      <div className="mt-2">
        <div className="grid grid-cols-2 gap-1.5" role="radiogroup" aria-label="反推目标">
          {options.map((option) => {
            const active = config.analysis_focus === option.key;
            return (
              <button
                key={option.key}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={disabled}
                onClick={() => selectFocus(option.key)}
                className={`min-h-11 rounded-lg border px-2 py-1.5 text-left transition disabled:cursor-not-allowed disabled:opacity-45 ${
                  active
                    ? "border-aqua/55 bg-aqua/15 text-snow"
                    : "border-line bg-white/[0.035] text-fog hover:border-line2 hover:text-mist"
                }`}
              >
                <span className="block text-xs font-display font-semibold">{option.label}</span>
                <span className="mt-0.5 block text-[10px] leading-snug">{option.description}</span>
              </button>
            );
          })}
        </div>

        <label className="mt-2 block">
          <span className="label mb-1">输出用途</span>
          <select
            className="input min-h-11 w-full px-2.5 py-2 text-xs"
            value={config.output_purpose || "generation"}
            disabled={disabled}
            onChange={(event) => patch({ output_purpose: event.target.value })}
          >
            {REVERSE_OUTPUT_PURPOSE_OPTIONS[category].map((option) => (
              <option key={option.key} value={option.key}>{option.label}</option>
            ))}
          </select>
        </label>

        {category === "video" && (
          <label className="mt-2 flex min-h-12 cursor-pointer items-center gap-2.5 rounded-lg border border-line bg-white/[0.025] px-2.5 py-2">
            <input
              type="checkbox"
              className="h-4 w-4 accent-aqua"
              checked={Boolean(config.include_audio)}
              disabled={disabled}
              onChange={(event) => patch({ include_audio: event.target.checked })}
            />
            <span className="min-w-0">
              <span className="block text-xs font-display font-medium text-mist">分析音频</span>
              <span className="mt-0.5 block text-[10px] leading-snug text-fog">
                提取旁白、音乐、节拍和音效；转写能力以当前服务为准
              </span>
            </span>
          </label>
        )}
      </div>

      <label className="mt-2 block">
        <span className="label mb-1">补充要求</span>
        <textarea
          className="textarea min-h-20 resize-y border-line bg-base2/60 px-2.5 py-2 text-xs"
          value={config.custom_instruction || ""}
          maxLength={500}
          disabled={disabled}
          placeholder={category === "video"
            ? "例如：重点描述手部动作、镜头节奏和商品露出方式"
            : "例如：只提取构图和光线，不复刻原图人物"}
          onChange={(event) => patch({ custom_instruction: event.target.value })}
        />
        <span className="mt-1 block text-right text-[10px] text-fog">
          {String(config.custom_instruction || "").length}/500
        </span>
      </label>
    </section>
  );
}
