"use client";

import { useEffect } from "react";

const KIND_STYLES = {
  bad: "border-bad/30 bg-bad/10 text-bad",
  warn: "border-warn/35 bg-warn/10 text-warn",
  ok: "border-ok/35 bg-ok/10 text-ok",
  info: "border-line2 bg-surface2/90 text-mist",
};

function inferKind(message) {
  const text = String(message || "");
  if (/失败|错误|不能|请先|过期|不足|超时|无法|不可|中断|拒绝|下架/.test(text)) return "bad";
  if (/审核中|等待|处理中|连接中|正在/.test(text)) return "warn";
  if (/已|完成|成功/.test(text)) return "ok";
  return "info";
}

export default function StudioMessageBar({ message, kind, onDismiss }) {
  const resolvedKind = KIND_STYLES[kind] ? kind : inferKind(message);

  useEffect(() => {
    if (!message || resolvedKind !== "ok" || typeof onDismiss !== "function") return undefined;
    const timer = window.setTimeout(() => onDismiss(""), 4200);
    return () => window.clearTimeout(timer);
  }, [message, onDismiss, resolvedKind]);

  if (!message) return null;

  return (
    <div
      role="status"
      className={`mt-3 flex items-start gap-3 rounded-xl border px-4 py-2.5 text-sm shadow-pop max-lg:fixed max-lg:inset-x-3 max-lg:bottom-[76px] max-lg:z-30 max-lg:mt-0 ${KIND_STYLES[resolvedKind]}`}
    >
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {typeof onDismiss === "function" && (
        <button
          type="button"
          onClick={() => onDismiss("")}
          className="shrink-0 rounded-full px-1.5 text-lg leading-none text-current opacity-70 hover:bg-white/10 hover:opacity-100"
          aria-label="关闭消息"
          title="关闭消息"
        >
          ×
        </button>
      )}
    </div>
  );
}
