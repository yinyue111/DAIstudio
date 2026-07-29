"use client";

import {
  emptyStructuredListItem,
  parseStructuredNumberInput,
  removeStructuredListItem,
  reviveStructuredValue,
  setStructuredValueAtPath,
  structuredEnumOptions,
  structuredFieldKind,
  structuredFieldMetadata,
  structuredFieldValue,
  structuredPathKey,
  structuredValueAtPath,
} from "./structuredEditor";

function controlIdForPath(key, path) {
  return `structured-dim-${structuredPathKey([key, ...path])}`;
}

function StructuredValueControl({ dimensionKey, root, path, onEdit, onRemoveItem, onAddItem }) {
  const rawValue = structuredValueAtPath(root, path);
  const metadata = structuredFieldMetadata({}, path.length ? path : [dimensionKey], rawValue);
  const kind = structuredFieldKind(rawValue, metadata);
  const value = structuredFieldValue(rawValue);
  const controlId = controlIdForPath(dimensionKey, path);

  if (kind === "enum") {
    const options = structuredEnumOptions(metadata);
    const current = String(value ?? "");
    const known = options.some((option) => String(option.value ?? "") === current);
    return (
      <select
        id={controlId}
        className="input select px-2.5 py-1.5 text-xs"
        value={current}
        onChange={(event) => {
          const picked = options.find((option) => String(option.value ?? "") === event.target.value);
          onEdit(path, picked ? picked.value : event.target.value);
        }}
      >
        {!known && <option value={current}>{current || "（未设置）"}</option>}
        {options.map((option) => (
          <option key={String(option.value ?? option.label)} value={String(option.value ?? "")}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }

  if (kind === "boolean") {
    return (
      <label className="flex min-h-8 items-center gap-2 text-xs text-mist" htmlFor={controlId}>
        <input
          id={controlId}
          type="checkbox"
          className="h-4 w-4 accent-current"
          checked={Boolean(value)}
          onChange={(event) => onEdit(path, event.target.checked)}
        />
        <span>{value ? "是" : "否"}</span>
      </label>
    );
  }

  if (kind === "number") {
    return (
      <input
        id={controlId}
        type="number"
        className="input px-2.5 py-1.5 text-xs"
        value={typeof value === "number" ? value : ""}
        onChange={(event) => onEdit(path, parseStructuredNumberInput(event.target.value, value))}
      />
    );
  }

  if (kind === "list") {
    const items = Array.isArray(value) ? value : [];
    return (
      <div className="space-y-1.5 rounded-xl border border-line/70 bg-white/[0.02] p-2">
        {items.length === 0 && <p className="text-[11px] text-fog">列表为空，可添加一项。</p>}
        {items.map((_, index) => (
          <div key={index} className="flex items-start gap-1.5">
            <span className="mt-2 shrink-0 text-[10px] text-fog">{index + 1}.</span>
            <div className="min-w-0 flex-1">
              <StructuredValueControl
                dimensionKey={dimensionKey}
                root={root}
                path={[...path, index]}
                onEdit={onEdit}
                onRemoveItem={onRemoveItem}
                onAddItem={onAddItem}
              />
            </div>
            <button
              type="button"
              className="chip shrink-0 px-2 py-0.5"
              aria-label={`删除第 ${index + 1} 项`}
              title="删除该项"
              onClick={() => onRemoveItem(path, index)}
            >
              ✕
            </button>
          </div>
        ))}
        <button type="button" className="chip" onClick={() => onAddItem(path, metadata, items)}>
          ＋ 添加一项
        </button>
      </div>
    );
  }

  if (kind === "object") {
    const entries = Object.entries(value && typeof value === "object" ? value : {});
    if (!entries.length) return <p className="text-[11px] text-fog">（空对象）</p>;
    return (
      <div className="space-y-1.5 rounded-xl border border-line/70 bg-white/[0.02] p-2">
        {entries.map(([entryKey]) => (
          <div key={entryKey}>
            <label className="label mb-0.5 text-[10px] normal-case" htmlFor={controlIdForPath(dimensionKey, [...path, entryKey])}>
              {entryKey}
            </label>
            <StructuredValueControl
              dimensionKey={dimensionKey}
              root={root}
              path={[...path, entryKey]}
              onEdit={onEdit}
              onRemoveItem={onRemoveItem}
              onAddItem={onAddItem}
            />
          </div>
        ))}
      </div>
    );
  }

  const text = typeof value === "string" ? value : value == null ? "" : String(value);
  const multiline = Boolean(metadata.multiline) || text.includes("\n") || text.length > 60;
  if (multiline) {
    return (
      <textarea
        id={controlId}
        className="textarea min-h-16 resize-y px-2.5 py-1.5 text-xs"
        value={text}
        onChange={(event) => onEdit(path, event.target.value)}
      />
    );
  }
  return (
    <input
      id={controlId}
      className="input px-2.5 py-1.5 text-xs"
      value={text}
      onChange={(event) => onEdit(path, event.target.value)}
    />
  );
}

export default function StudioStructuredEditor({
  structured,
  open,
  onToggleOpen,
  onRecompose,
  onUndo,
  onClear,
  onChange,
  structuredDirty = false,
  promptDirty = false,
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
        <div className="px-3 pb-3">
          {structuredDirty && promptDirty && (
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-warn/35 bg-warn/10 px-3 py-2" role="alert">
              <p className="text-xs text-warn">提示词已手工修改。生成前请选择应用结构修改或撤销结构修改。</p>
              <div className="flex gap-1.5">
                <button type="button" onClick={onUndo} className="btn-secondary btn-sm px-2.5 py-1 text-xs">
                  撤销结构修改
                </button>
                <button type="button" onClick={onRecompose} className="btn-primary btn-sm px-2.5 py-1 text-xs">
                  应用结构修改
                </button>
              </div>
            </div>
          )}
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {keys.map((key) => {
              // 历史退化的字符串（被 JSON.stringify 过的对象/数组）在这里容错还原，
              // 编辑写回时即恢复为真实结构。
              const root = reviveStructuredValue(structured[key]);
              const kind = structuredFieldKind(root, structuredFieldMetadata({}, [key], root));
              const commit = (nextRoot) => onChange(key, nextRoot);
              return (
                <div key={key} className={["list", "object"].includes(kind) ? "sm:col-span-2" : ""}>
                  <label className="label mb-1 normal-case" htmlFor={controlIdForPath(key, [])}>{key}</label>
                  <StructuredValueControl
                    dimensionKey={key}
                    root={root}
                    path={[]}
                    onEdit={(path, nextValue) => commit(setStructuredValueAtPath(root, path, nextValue))}
                    onRemoveItem={(path, index) => commit(removeStructuredListItem(root, path, index))}
                    onAddItem={(path, metadata, items) => commit(setStructuredValueAtPath(
                      root,
                      [...path, items.length],
                      emptyStructuredListItem(items, metadata),
                    ))}
                  />
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
