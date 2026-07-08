"use client";

import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { reportBackgroundError } from "../lib/errorHandling";

export const STUDIO_DRAFT_PROMPT_KEY = "studio:draftPrompt";

const PROMPT_LIBRARY_URL = "/prompt-library/prompt-data.json";
const PROMPT_LIBRARY_PREVIEW_URL = "/prompt-library/prompt-preview.json";
const PROMPT_LIBRARY_LICENSE_URL = "/prompt-library/license.json";
const CATEGORY_ALL = { id: "all", label: "全部", count: 0 };

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch (error) {
    return "";
  }
}

function safeImageUrl(value) {
  const src = String(value || "").trim();
  if (!src) return "";
  if (src.startsWith("/") && !src.startsWith("//")) return src;
  return safeExternalUrl(src);
}

export default function PromptLibraryBrowser({
  variant = "panel",
  title = "提示词库",
  description = "从本地库选择参考，套用后可继续改主体、品牌、尺寸和限制。",
  primaryLabel = "套用",
  secondaryLabel = "追加",
  onPrimary,
  onSecondary,
  onClose,
}) {
  const [library, setLibrary] = useState(null);
  const [license, setLicense] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [activeCategory, setActiveCategory] = useState("all");
  const [previewItem, setPreviewItem] = useState(null);
  const isPage = variant === "page";
  const [visible, setVisible] = useState(isPage ? 24 : 8);
  const pageSize = isPage ? 24 : 8;

  useEffect(() => {
    let alive = true;
    fetch(isPage ? PROMPT_LIBRARY_URL : PROMPT_LIBRARY_PREVIEW_URL)
      .then((res) => {
        if (!res.ok) throw new Error(`提示词库读取失败(${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (alive) setLibrary(data);
      })
      .catch((error) => {
        if (alive) setLoadError(error.message || "提示词库读取失败");
      });
    return () => { alive = false; };
  }, [isPage]);

  useEffect(() => {
    let alive = true;
    fetch(PROMPT_LIBRARY_LICENSE_URL)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (alive) setLicense(data);
      })
      .catch((e) => {
        reportBackgroundError(e, "load prompt library license");
      });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    setVisible(pageSize);
  }, [activeCategory, pageSize, query]);

  const categories = useMemo(() => {
    if (!library) return [CATEGORY_ALL];
    return [{ ...CATEGORY_ALL, count: library.stats?.total || library.items?.length || 0 }, ...library.categories];
  }, [library]);

  const filteredItems = useMemo(() => {
    if (!library?.items) return [];
    const q = deferredQuery.trim().toLowerCase();
    const scored = [];
    for (const item of library.items) {
      if (activeCategory !== "all" && item.category !== activeCategory) continue;
      const titleText = item.title.toLowerCase();
      const promptText = item.prompt.toLowerCase();
      const tagText = (item.tags || []).join(" ").toLowerCase();
      const authorText = (item.author || "").toLowerCase();
      let score = 1;
      if (q) {
        score = 0;
        if (titleText.includes(q)) score += 60;
        if (tagText.includes(q)) score += 36;
        if (authorText.includes(q)) score += 22;
        if (promptText.includes(q)) score += 16;
        for (const token of q.split(/\s+/).filter(Boolean)) {
          if (titleText.includes(token)) score += 12;
          if (tagText.includes(token)) score += 10;
          if (promptText.includes(token)) score += 4;
        }
      }
      if (!q || score > 0) scored.push({ item, score });
    }
    scored.sort((a, b) => b.score - a.score || b.item.imageCount - a.item.imageCount || a.item.caseId - b.item.caseId);
    return scored.map(({ item }) => item);
  }, [activeCategory, deferredQuery, library]);

  const total = library?.stats?.total || library?.items?.length || 0;
  const visibleItems = filteredItems.slice(0, visible);
  const licenseSourceUrl = safeExternalUrl(license?.sourceUrl);

  return (
    <div className={isPage ? "panel p-4 sm:p-5" : "mt-2.5 rounded-xl3 border border-line bg-base2/40 p-3 animate-fadeup"}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <h2 className={isPage ? "text-xl font-bold text-snow" : "text-sm font-semibold text-snow"}>{title}</h2>
            <span className="badge bg-white/[0.08] text-fog">{total || "读取中"} 条</span>
          </div>
          <p className="mt-0.5 text-xs text-fog">
            {description}
            {!isPage && library?.preview?.sourceTotal ? ` · 面板预览 ${library.items.length} / ${library.preview.sourceTotal}` : ""}
          </p>
          {licenseSourceUrl && (
            <p className="mt-1 text-[11px] text-fog">
              来源：
              <a
                className="text-brand hover:text-iris-400"
                href={licenseSourceUrl}
                target="_blank"
                rel="noreferrer"
              >
                {license.source}
              </a>
              <span> · 第三方参考库，公开商用前需完成授权和内容风险审核</span>
            </p>
          )}
        </div>
        {onClose && <button onClick={onClose} className="btn-ghost btn-sm">收起</button>}
      </div>

      <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto]">
        <input
          className="input px-3 py-2 text-xs"
          type="search"
          placeholder="搜索：人像、电商、国潮、UI、poster、cinematic..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          onClick={() => { setQuery(""); setActiveCategory("all"); }}
          className="btn-secondary btn-sm"
        >
          清空
        </button>
      </div>

      <div className="mb-3 flex gap-1.5 overflow-x-auto pb-1">
        {categories.map((category) => (
          <button
            key={category.id}
            onClick={() => setActiveCategory(category.id)}
            className={`chip shrink-0 ${activeCategory === category.id ? "chip-active" : ""}`}
            title={category.description || category.label}
          >
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ backgroundColor: category.accent || "rgba(255,255,255,0.45)" }}
            />
            {category.label}
            {category.count ? <span className="text-[10px] opacity-70">{category.count}</span> : null}
          </button>
        ))}
      </div>

      {loadError ? (
        <div className="rounded-xl border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{loadError}</div>
      ) : !library ? (
        <div className={isPage ? "grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3" : "grid grid-cols-2 gap-2 sm:grid-cols-4"}>
          {Array.from({ length: isPage ? 9 : 4 }).map((_, i) => (
            <div key={i} className="skeleton aspect-[4/5]" />
          ))}
        </div>
      ) : filteredItems.length === 0 ? (
        <div className="rounded-xl border border-line bg-black/10 px-3 py-8 text-center text-xs text-fog">没有匹配的提示词</div>
      ) : (
        <>
          <div className={isPage ? "grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3" : "grid max-h-[560px] grid-cols-1 gap-2 overflow-y-auto pr-1 sm:grid-cols-2"}>
            {visibleItems.map((item) => (
              <PromptLibraryCard
                key={item.id}
                item={item}
                categories={library.categories}
                primaryLabel={primaryLabel}
                secondaryLabel={secondaryLabel}
                onPrimary={onPrimary}
                onSecondary={onSecondary}
                onPreviewImage={setPreviewItem}
              />
            ))}
          </div>
          {isPage && visible < filteredItems.length && (
            <div className="mt-5 text-center">
              <button onClick={() => setVisible((v) => v + pageSize)} className="btn-secondary">
                加载更多（已显示 {visibleItems.length} / {filteredItems.length}）
              </button>
            </div>
          )}
        </>
      )}
      {previewItem && (
        <PromptImageDialog
          item={previewItem}
          category={categories.find((c) => c.id === previewItem.category)}
          onClose={() => setPreviewItem(null)}
        />
      )}
    </div>
  );
}

function PromptLibraryCard({ item, categories, primaryLabel, secondaryLabel, onPrimary, onSecondary, onPreviewImage }) {
  const category = categories.find((c) => c.id === item.category) || { label: item.category, accent: "#7b61ff" };
  const excerpt = compactPrompt(item.prompt);
  const sourceUrl = safeExternalUrl(item.sourceUrl);
  const previewUrl = safeImageUrl(item.previewUrl);
  return (
    <article className="group overflow-hidden rounded-xl2 border border-line bg-black/15 transition hover:border-line2 hover:bg-white/5">
      <div className="grid grid-cols-[112px_1fr] gap-2 p-2">
        <div className="relative aspect-[4/5] overflow-hidden rounded-lg bg-black/20">
          {previewUrl ? (
            <button
              type="button"
              onClick={() => onPreviewImage?.(item)}
              className="group/preview h-full w-full cursor-zoom-in text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
              aria-label={`查看大图：${item.title}`}
            >
              <img loading="lazy" src={previewUrl} alt="" className="h-full w-full object-cover transition duration-300 group-hover:scale-[1.04]" />
              <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/75 via-black/25 to-transparent px-2 pb-2 pt-7 text-[10px] text-white/90 opacity-0 transition group-hover/preview:opacity-100 group-focus-visible/preview:opacity-100">
                点击查看大图
              </span>
            </button>
          ) : (
            <div className="flex h-full w-full items-center justify-center px-2 text-center text-[11px] text-fog">无预览</div>
          )}
          <span className="badge absolute left-1.5 top-1.5 bg-black/60 text-[10px] text-white">Case {item.caseId}</span>
        </div>
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-1.5">
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: category.accent || "#7b61ff" }}
            />
            <span className="truncate text-[11px] text-fog">{category.label}</span>
          </div>
          <h3 className="line-clamp-2 text-[13px] font-semibold leading-snug text-snow">{item.title}</h3>
          <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-fog">{excerpt}</p>
          <div className="mt-2 flex flex-wrap gap-1">
            {(item.tags || []).slice(0, 3).map((tag) => (
              <span key={tag} className="rounded-full bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-fog">{tag}</span>
            ))}
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-line px-2 py-2">
        {sourceUrl ? (
          <a
            className="truncate text-[10px] text-fog hover:text-brand"
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
          >
            {item.author || "未知作者"} · {item.promptLength} 字
          </a>
        ) : (
          <span className="truncate text-[10px] text-fog">{item.author || "未知作者"} · {item.promptLength} 字</span>
        )}
        <div className="flex gap-1.5">
          {onSecondary && <button onClick={() => onSecondary(item)} className="btn-secondary btn-sm px-2 py-1 text-[11px]">{secondaryLabel}</button>}
          {onPrimary && <button onClick={() => onPrimary(item)} className="btn-primary btn-sm px-2 py-1 text-[11px]">{primaryLabel}</button>}
        </div>
      </div>
    </article>
  );
}

function PromptImageDialog({ item, category, onClose }) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const previewUrl = safeImageUrl(item.previewUrl);
  const sourceUrl = safeExternalUrl(item.sourceUrl);

  useEffect(() => {
    previousFocusRef.current = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      const previous = previousFocusRef.current;
      if (previous && typeof previous.focus === "function") previous.focus();
    };
  }, [onClose]);

  if (!previewUrl || typeof document === "undefined") return null;

  return createPortal((
    <div
      ref={dialogRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={`提示词预览：${item.title}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-3 backdrop-blur-sm outline-none sm:p-5"
      onClick={onClose}
    >
      <div className="panel max-h-[92vh] w-full max-w-5xl overflow-hidden p-3 sm:p-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex max-h-[76vh] items-center justify-center overflow-hidden rounded-xl2 bg-black/30">
          <img
            src={previewUrl}
            alt={item.title}
            className="max-h-[76vh] w-auto max-w-full object-contain"
          />
        </div>
        <div className="mt-3 flex flex-col gap-2 text-sm sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: category?.accent || "#7b61ff" }}
              />
              <span className="truncate font-semibold text-snow">{item.title}</span>
            </div>
            <p className="mt-0.5 text-xs text-fog">
              {category?.label || item.category} · Case {item.caseId}
              {sourceUrl ? (
                <>
                  <span> · </span>
                  <a className="text-brand hover:text-iris-400" href={sourceUrl} target="_blank" rel="noreferrer">
                    查看来源
                  </a>
                </>
              ) : null}
            </p>
          </div>
          <button type="button" onClick={onClose} className="btn-secondary btn-sm shrink-0">关闭</button>
        </div>
      </div>
    </div>
  ), document.body);
}

function compactPrompt(value) {
  return String(value || "")
    .replace(/[{}"[\]]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 180);
}
