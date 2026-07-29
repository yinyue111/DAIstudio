"use client";

import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { MAX_REVERSE_SOURCE_RANGES } from "./reverseConfig";

function finiteNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export default function StudioVideoAnalysisControls({
  config,
  presets = [],
  disabled = false,
  timelineEnabled = true,
  maxDuration = null,
  selectedCost = 0,
  onChange,
}) {
  const [keyframeInput, setKeyframeInput] = useState("");
  const [keyframeError, setKeyframeError] = useState("");
  const ranges = Array.isArray(config.source_ranges) && config.source_ranges.length
    ? config.source_ranges
    : config.source_range ? [config.source_range] : [];
  const keyframes = Array.isArray(config.custom_keyframes) ? config.custom_keyframes : [];

  function patch(next) {
    onChange?.({ ...config, ...next });
  }

  function setRanges(next) {
    patch({
      source_ranges: next,
      source_range: next.length === 1 ? next[0] : null,
    });
  }

  function updateRange(index, rangePatch) {
    setRanges(ranges.map((range, rangeIndex) => (
      rangeIndex === index ? { ...range, ...rangePatch } : range
    )));
  }

  function addRange() {
    if (ranges.length >= MAX_REVERSE_SOURCE_RANGES) return;
    const previousEnd = finiteNumber(ranges.at(-1)?.end_seconds, 0);
    const duration = finiteNumber(maxDuration, 0);
    const start = duration > 0 ? Math.min(previousEnd, Math.max(0, duration - 0.1)) : previousEnd;
    const end = duration > 0 ? Math.min(duration, start + 15) : start + 15;
    if (end <= start) return;
    setRanges([...ranges, { start_seconds: start, end_seconds: end }]);
  }

  function addKeyframe() {
    const value = finiteNumber(keyframeInput, -1);
    if (value < 0) return;
    const duration = finiteNumber(maxDuration, 0);
    if (duration > 0 && value > duration) {
      setKeyframeError("关键帧时间必须位于素材时长范围内。");
      return;
    }
    if (ranges.length > 0 && !ranges.some(
      (range) => finiteNumber(range.start_seconds) <= value && value <= finiteNumber(range.end_seconds),
    )) {
      setKeyframeError("关键帧必须位于某个已选分析片段内。");
      return;
    }
    const next = [...new Set([...keyframes, Math.round(value * 1000) / 1000])]
      .sort((left, right) => left - right);
    patch({ custom_keyframes: next });
    setKeyframeInput("");
    setKeyframeError("");
  }

  return (
    <section className="rounded-xl border border-line bg-black/15 p-2" aria-labelledby="video-analysis-title">
      <div className="flex items-center justify-between gap-2">
        <h4 id="video-analysis-title" className="text-xs font-display font-medium text-mist">
          视频分析设置
        </h4>
        {selectedCost > 0 && <span className="text-[11px] text-fog">预计冻结 {selectedCost} 积分</span>}
      </div>

      <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-4" role="radiogroup" aria-label="分析精度">
        {presets.map((preset) => {
          const active = config.analysis_precision === preset.key;
          return (
            <button
              key={preset.key}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => patch({ analysis_precision: preset.key })}
              className={`min-h-11 rounded-lg border px-2 py-1.5 text-left transition disabled:cursor-not-allowed disabled:opacity-45 ${
                active
                  ? "border-iris bg-iris/25 text-snow"
                  : "border-line bg-white/[0.04] text-fog hover:border-line2 hover:text-mist"
              }`}
            >
              <span className="block text-xs font-display font-semibold">{preset.label}</span>
              <span className="mt-0.5 block text-[10px] leading-snug">
                {preset.frame_range || preset.short_range || preset.description || preset.long_range || ""}
              </span>
            </button>
          );
        })}
      </div>

      <label className="mt-2 flex min-h-11 items-center justify-between gap-3 rounded-lg border border-line bg-white/[0.035] px-2.5 py-2">
          <span>
          <span className="block text-xs font-display font-medium text-mist">选择分析片段</span>
          <span className="block text-[10px] text-fog">可选多个互不重叠的时间范围</span>
        </span>
        <input
          type="checkbox"
          className="h-4 w-4 accent-brand"
          checked={ranges.length > 0}
          disabled={disabled || !timelineEnabled}
          onChange={(event) => setRanges(event.target.checked ? [{
            start_seconds: 0,
            end_seconds: Math.max(1, Math.min(15, finiteNumber(maxDuration, 15))),
          }] : [])}
        />
      </label>

      {ranges.length > 0 && timelineEnabled && (
        <div className="mt-2 space-y-2">
          {ranges.map((range, index) => (
            <div key={index} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_2.75rem] gap-2 rounded-lg border border-line bg-white/[0.025] p-2">
              <label>
                <span className="label mb-1">片段 {index + 1} 开始</span>
                <input
                  type="number"
                  min="0"
                  max={maxDuration || undefined}
                  step="0.1"
                  className="input min-h-11 px-2.5 py-2 text-xs"
                  value={range.start_seconds}
                  disabled={disabled}
                  onChange={(event) => updateRange(index, { start_seconds: finiteNumber(event.target.value) })}
                />
              </label>
              <label>
                <span className="label mb-1">结束（秒）</span>
                <input
                  type="number"
                  min="0.1"
                  max={maxDuration || undefined}
                  step="0.1"
                  className="input min-h-11 px-2.5 py-2 text-xs"
                  value={range.end_seconds}
                  disabled={disabled}
                  onChange={(event) => updateRange(index, { end_seconds: finiteNumber(event.target.value, 1) })}
                />
              </label>
              <button
                type="button"
                className="icon-btn mt-5 h-11 w-11 text-fog hover:text-bad"
                disabled={disabled}
                aria-label={`删除片段 ${index + 1}`}
                onClick={() => setRanges(ranges.filter((_, rangeIndex) => rangeIndex !== index))}
              >
                <Trash2 size={15} aria-hidden="true" />
              </button>
            </div>
          ))}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <button
              type="button"
              className="btn-secondary btn-sm min-h-10"
              disabled={disabled || ranges.length >= MAX_REVERSE_SOURCE_RANGES}
              onClick={addRange}
            >
              <Plus size={14} aria-hidden="true" /> 添加片段
            </button>
            <span className="text-[10px] text-fog">
              共 {ranges.length}/{MAX_REVERSE_SOURCE_RANGES} 段 · 累计 {ranges.reduce((total, item) => (
                total + Math.max(0, finiteNumber(item.end_seconds) - finiteNumber(item.start_seconds))
              ), 0).toFixed(1)}s
            </span>
          </div>
        </div>
      )}

      <div className="mt-2">
        <label className="label mb-1" htmlFor="reverse-keyframe-input">指定关键帧（秒）</label>
        <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
          <input
            id="reverse-keyframe-input"
            type="number"
            min="0"
            max={maxDuration || undefined}
            step="0.1"
            className="input min-h-11 px-2.5 py-2 text-xs"
            value={keyframeInput}
            disabled={disabled || !timelineEnabled}
            placeholder="例如 3.5"
            onChange={(event) => setKeyframeInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addKeyframe();
              }
            }}
          />
          <button type="button" className="btn-secondary btn-sm min-h-11 px-3" disabled={disabled || !keyframeInput} onClick={addKeyframe}>
            添加
          </button>
        </div>
        {keyframeError && <p className="mt-1 text-[11px] text-bad" role="alert">{keyframeError}</p>}
        {keyframes.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="已指定关键帧">
            {keyframes.map((timestamp) => (
              <button
                key={timestamp}
                type="button"
                className="chip min-h-9 px-2 text-xs"
                disabled={disabled}
                aria-label={`移除 ${timestamp} 秒关键帧`}
                onClick={() => patch({ custom_keyframes: keyframes.filter((value) => value !== timestamp) })}
              >
                {timestamp}s ×
              </button>
            ))}
          </div>
        )}
      </div>

    </section>
  );
}
