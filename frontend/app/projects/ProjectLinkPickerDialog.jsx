"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, ListTodo } from "lucide-react";
import { api } from "../../lib/api";
import {
  normalizeUnifiedTaskPage,
  taskKindLabel,
  taskStatusClass,
  taskStatusLabel,
} from "../../lib/unifiedTasks";

const TASK_KIND_FILTERS = [
  ["all", "全部"],
  ["generation", "生成"],
  ["reverse", "反推"],
  ["parse", "解析"],
  ["workflow", "工作流"],
];

const EMPTY_LIST = [];

// 项目页内选择已有任务/配方加入项目的弹窗（mode: "tasks" | "recipes"）。
export default function ProjectLinkPickerDialog({
  open,
  mode,
  excludedKeys = EMPTY_LIST,
  busy = false,
  onClose,
  onConfirm,
}) {
  const dialogRef = useRef(null);
  const [kind, setKind] = useState("all");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState(() => new Map());

  useEffect(() => {
    if (!open) return;
    setKind("all");
    setSelection(new Map());
    setError("");
  }, [open, mode]);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setLoading(true);
    setError("");
    const load = mode === "tasks"
      ? api.taskCenter({ kind, status: "all", category: "all", limit: 50 })
        .then((payload) => normalizeUnifiedTaskPage(payload).items.map((task) => ({
          key: task.key,
          kind: task.kind,
          id: task.id,
          title: task.title,
          summary: task.summary,
          status: task.status,
          status_group: task.status_group,
        })))
      : api.creationRecipes({ limit: 100 })
        .then((rows) => (Array.isArray(rows) ? rows : []).map((recipe) => ({
          key: `recipe:${recipe.id}`,
          id: recipe.id,
          title: recipe.title,
          summary: recipe.category === "video" ? "视频配方" : "图片配方",
          version: recipe.current_version,
        })));
    load.then((rows) => {
      if (!cancelled) setItems(rows);
    }).catch((loadError) => {
      if (!cancelled) setError(loadError.message || (mode === "tasks" ? "任务加载失败" : "配方加载失败"));
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [open, mode, kind]);

  useEffect(() => {
    if (!open) return undefined;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose?.();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus?.();
    };
  }, [open, onClose]);

  const excludedKeySet = useMemo(
    () => new Set((excludedKeys || []).map((value) => String(value || "").trim()).filter(Boolean)),
    [excludedKeys],
  );
  const visibleItems = useMemo(
    () => items.filter((item) => !excludedKeySet.has(item.key)),
    [items, excludedKeySet],
  );

  if (!open) return null;

  const title = mode === "tasks" ? "选择要加入项目的任务" : "选择要加入项目的配方";
  const HeaderIcon = mode === "tasks" ? ListTodo : BookOpen;

  function toggle(item) {
    setSelection((current) => {
      const next = new Map(current);
      if (next.has(item.key)) next.delete(item.key);
      else next.set(item.key, item);
      return next;
    });
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/75 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <section
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-link-picker-title"
        className="flex max-h-[86vh] w-full max-w-2xl flex-col overflow-hidden rounded-t-xl3 border border-line bg-base2 shadow-pop outline-none sm:rounded-xl3"
      >
        <header className="flex min-h-16 items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <h2 id="project-link-picker-title" className="flex items-center gap-2 truncate text-base font-display font-semibold text-snow">
              <HeaderIcon size={16} className="text-aqua" aria-hidden="true" /> {title}
            </h2>
            <p className="mt-0.5 text-xs text-fog">已选择 {selection.size} 项</p>
          </div>
          <button type="button" onClick={onClose} className="btn-ghost btn-sm" aria-label="关闭选择器">关闭</button>
        </header>

        {mode === "tasks" && (
          <div className="flex flex-wrap items-center gap-1 border-b border-line px-4 py-3 sm:px-5" aria-label="任务类型">
            {TASK_KIND_FILTERS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setKind(value)}
                className={kind === value ? "chip chip-active" : "chip"}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-2 sm:px-5">
          {error && (
            <p className="border-y border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad" role="alert">{error}</p>
          )}
          {loading ? (
            <div className="skeleton my-3 h-32" aria-label="加载中" />
          ) : !visibleItems.length ? (
            <p className="py-10 text-center text-sm text-fog" role="status">
              {mode === "tasks" ? "没有可加入的任务" : "没有可加入的配方"}
            </p>
          ) : (
            <ul className="divide-y divide-line" aria-label={mode === "tasks" ? "可加入任务" : "可加入配方"}>
              {visibleItems.map((item) => (
                <li key={item.key}>
                  <label className="flex min-w-0 cursor-pointer items-center gap-3 py-2.5">
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0 accent-current"
                      checked={selection.has(item.key)}
                      onChange={() => toggle(item)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 items-center gap-2">
                        <span className="truncate text-sm text-snow">{item.title || `#${item.id}`}</span>
                        {mode === "tasks" ? (
                          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${taskStatusClass(item.status_group)}`}>
                            {taskStatusLabel(item.status, item.status_group)}
                          </span>
                        ) : (
                          item.version && <span className="shrink-0 text-[11px] text-fog">v{item.version}</span>
                        )}
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-fog">
                        {mode === "tasks" ? `${taskKindLabel(item.kind)} #${item.id}` : item.summary}
                        {mode === "tasks" && item.summary ? ` · ${item.summary}` : ""}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-line px-4 py-3 sm:px-5">
          <button type="button" className="btn-ghost btn-sm" onClick={onClose}>取消</button>
          <button
            type="button"
            className="btn-primary btn-sm"
            disabled={!selection.size || busy}
            onClick={() => onConfirm?.([...selection.values()])}
          >
            加入项目（{selection.size}）
          </button>
        </footer>
      </section>
    </div>
  );
}
