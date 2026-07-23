"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import {
  IMAGE_REVERSE_FOCUS_OPTIONS,
  REVERSE_OUTPUT_PURPOSE_OPTIONS,
  VIDEO_REVERSE_FOCUS_OPTIONS,
} from "./reverseConfig";
import {
  loadReverseAnalyzerHealth,
  summarizeImageAnalyzerHealth,
} from "./reverseAnalyzerHealth";

function capabilityClassName(status) {
  if (status === "available") return "border-good/35 text-good";
  if (["partial", "degraded"].includes(status)) return "border-warn/35 text-warn";
  return "border-line text-fog";
}

export default function StudioReverseIntentControls({
  category,
  config,
  disabled = false,
  onChange,
}) {
  const [analyzerStatus, setAnalyzerStatus] = useState(null);
  const [analyzerStatusLoading, setAnalyzerStatusLoading] = useState(category === "image");
  const [analyzerStatusError, setAnalyzerStatusError] = useState("");
  const options = category === "video"
    ? VIDEO_REVERSE_FOCUS_OPTIONS
    : IMAGE_REVERSE_FOCUS_OPTIONS;
  const imageHealth = useMemo(
    () => summarizeImageAnalyzerHealth(analyzerStatus),
    [analyzerStatus],
  );

  useEffect(() => {
    if (category !== "image") return undefined;
    let cancelled = false;
    setAnalyzerStatusLoading(true);
    loadReverseAnalyzerHealth(() => api.reverseAnalyzerStatus())
      .then((result) => {
        if (cancelled) return;
        setAnalyzerStatus(result);
        setAnalyzerStatusError("");
      })
      .catch(() => {
        if (cancelled) return;
        setAnalyzerStatus(null);
        setAnalyzerStatusError("图片分析能力读取失败，提交后以后端实际分析结果为准。");
      })
      .finally(() => {
        if (!cancelled) setAnalyzerStatusLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [category]);

  function patch(next) {
    onChange?.({ ...config, ...next });
  }

  return (
    <section className="rounded-xl border border-line bg-black/15 p-2" aria-labelledby="reverse-intent-title">
      <div className="flex items-center justify-between gap-2">
        <h4 id="reverse-intent-title" className="text-xs font-display font-medium text-mist">
          反推目标
        </h4>
        <span className="text-[11px] text-fog">决定重点分析什么</span>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-1.5" role="radiogroup" aria-label="反推目标">
        {options.map((option) => {
          const active = config.analysis_focus === option.key;
          return (
            <button
              key={option.key}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => patch({ analysis_focus: option.key })}
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

      {category === "image" && (
        <div className="mt-2 border-t border-line pt-2" aria-live="polite">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-display font-medium text-mist">图片证据能力</span>
            <span className="text-[10px] text-fog">按当前服务环境</span>
          </div>
          {analyzerStatusLoading ? (
            <p className="mt-1 text-[10px] text-fog">正在读取图片分析能力…</p>
          ) : analyzerStatusError ? (
            <p className="mt-1 text-[10px] leading-relaxed text-warn" role="alert">
              {analyzerStatusError}
            </p>
          ) : (
            <>
              <p className="mt-1 text-[10px] leading-relaxed text-fog">{imageHealth.summary}</p>
              <div className="mt-1 flex flex-wrap gap-1" aria-label="图片分析器能力状态">
                {imageHealth.capabilities.map((capability) => (
                  <span
                    key={capability.key}
                    className={`border px-1.5 py-0.5 text-[10px] ${capabilityClassName(capability.status)}`}
                    title={capability.reason || undefined}
                    aria-label={`${capability.label}：${capability.statusLabel}${
                      capability.reason ? `，${capability.reason}` : ""
                    }`}
                  >
                    {capability.label} · {capability.statusLabel}
                  </span>
                ))}
              </div>
              {imageHealth.issues && (
                <p className="mt-1 text-[10px] leading-relaxed text-warn">{imageHealth.issues}</p>
              )}
            </>
          )}
        </div>
      )}

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
