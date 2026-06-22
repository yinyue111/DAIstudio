"use client";

import { useState } from "react";

export function Card({ children, className = "" }) {
  return <div className={`card p-5 animate-fadeup ${className}`}>{children}</div>;
}

export function Th({ children }) {
  return <th className="py-2 pr-3 text-xs font-display font-medium text-fog">{children}</th>;
}

export function PayInput({ label, value, onChange, placeholder = "", disabled = false }) {
  return (
    <label className="grid gap-1 text-xs text-fog">
      <span>{label}</span>
      <input className="input w-full" value={value || ""} placeholder={placeholder}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export function PaySecret({ label, configured, onChange, disabled = false }) {
  const [value, setValue] = useState("");
  function update(next) {
    if (disabled) return;
    setValue(next);
    onChange(next);
  }
  return (
    <label className="grid gap-1 text-xs text-fog">
      <span className="flex items-center justify-between gap-2">
        <span>{label}{configured ? <b className="ml-2 font-normal text-ok">已配置</b> : null}</span>
        {configured && !disabled ? (
          <button type="button" className="text-warn hover:text-snow"
            onClick={() => {
              if (window.confirm(`确认清空${label}？保存渠道后生效。`)) update("__clear__");
            }}>
            清空
          </button>
        ) : null}
      </span>
      <textarea className="input min-h-20 w-full resize-y py-2" placeholder={disabled ? "由环境变量配置" : "留空表示不修改"}
        value={value}
        disabled={disabled}
        onChange={(e) => update(e.target.value)} />
    </label>
  );
}
