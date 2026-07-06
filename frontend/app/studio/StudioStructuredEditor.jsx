"use client";

export default function StudioStructuredEditor({
  structured,
  open,
  onToggleOpen,
  onRecompose,
  onClear,
  onChange,
}) {
  const keys = Object.keys(structured || {});
  if (keys.length === 0) return null;

  return (
    <div className="mt-2.5 rounded-xl3 border border-line bg-base2/40 animate-fadeup">
      <div className="flex items-center justify-between px-3 py-2">
        <button onClick={onToggleOpen} className="flex items-center gap-2 text-xs font-display font-medium text-mist hover:text-snow">
          <span className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
          反推维度 · {keys.length} 项（可逐项微调）
        </button>
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={onRecompose} className="chip" title="用当前维度重新拼接提示词">↻ 重组提示词</button>
          <button type="button" onClick={onClear} className="chip">清除风格参考</button>
        </div>
      </div>
      {open && (
        <div className="grid grid-cols-1 gap-2 px-3 pb-3 sm:grid-cols-2">
          {keys.map((key) => (
            <div key={key}>
              <label className="label mb-1 normal-case">{key}</label>
              <input
                className="input px-2.5 py-1.5 text-xs"
                value={structured[key] || ""}
                onChange={(event) => onChange(key, event.target.value)}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
