"use client";

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

export default function StudioMessageBar({ message, kind }) {
  if (!message) return null;
  const resolvedKind = KIND_STYLES[kind] ? kind : inferKind(message);

  return (
    <div className={`mt-3 rounded-xl border px-4 py-2.5 text-sm shadow-pop max-lg:fixed max-lg:inset-x-3 max-lg:bottom-[76px] max-lg:z-30 max-lg:mt-0 ${KIND_STYLES[resolvedKind]}`}>
      {message}
    </div>
  );
}
