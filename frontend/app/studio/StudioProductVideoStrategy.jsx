"use client";

import { AlertTriangle, Check } from "lucide-react";

export default function StudioProductVideoStrategy({
  options = [],
  value = null,
  supported = true,
  onChange,
}) {
  return (
    <fieldset className="min-w-0 border-b border-line pb-3">
      <legend className="mb-2 flex w-full min-w-0 items-center justify-between gap-3 text-xs font-display font-semibold text-mist">
        <span>产品视频策略</span>
        {supported && <span className="text-[10px] font-normal text-fog">跟随当前模型能力</span>}
      </legend>

      {supported ? (
        <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
          {options.map((option) => {
            const selected = option.key === value;
            return (
              <button
                key={option.key}
                type="button"
                aria-pressed={selected}
                onClick={() => onChange?.(option.key)}
                className={`min-w-0 rounded-lg border p-2.5 text-left transition-colors ${
                  selected
                    ? "border-aqua/55 bg-aqua/10 text-snow"
                    : "border-line bg-white/[0.03] text-mist hover:border-white/20 hover:bg-white/[0.05]"
                }`}
              >
                <span className="flex min-w-0 items-center justify-between gap-2 text-xs font-display font-semibold">
                  <span className="min-w-0 break-words">{option.label}</span>
                  {selected && <Check className="shrink-0 text-aqua" size={14} aria-hidden="true" />}
                </span>
                <span className="mt-1 block break-words text-[11px] leading-relaxed text-fog">
                  {option.description}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="flex min-w-0 items-start gap-2 border border-warn/30 bg-warn/10 px-3 py-2.5 text-xs leading-relaxed text-warn" role="status">
          <AlertTriangle className="mt-0.5 shrink-0" size={14} aria-hidden="true" />
          <span className="min-w-0 break-words">当前视频模型未提供可用的产品视频策略，请切换模型后再生成。</span>
        </div>
      )}
    </fieldset>
  );
}
