"use client";

import { Compass, Library, RefreshCw, RotateCcw, Search } from "lucide-react";
import CreationRecipeCard from "./CreationRecipeCard";

const FILTERS = [
  ["all", "全部"],
  ["image", "图片"],
  ["video", "视频"],
];

export default function CreationRecipeBrowser({
  recipes = [],
  loading = false,
  error = "",
  scope = "mine",
  filter = "all",
  query = "",
  onScopeChange,
  onFilterChange,
  onQueryChange,
  onSearch,
  onRefresh,
  onRestore,
  onFavorite,
  onDelete,
  onRename,
  onToggleVisibility,
  onClone,
  onLoadVersions,
  onActivate,
}) {
  const mine = scope === "mine";
  const visibleFilters = mine ? [...FILTERS, ["favorite", "收藏"]] : FILTERS;

  return (
    <section className="panel mb-6 p-4 sm:p-5" aria-labelledby="creation-recipes-title">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="creation-recipes-title" className="text-xl font-bold text-snow">创作配方</h2>
          <p className="mt-1 max-w-2xl text-xs text-fog">
            {mine
              ? "管理完整创作工作区，可恢复历史版本、回滚、公开或派生副本。"
              : "发现社区公开的图片与视频配方，派生后会作为私有副本加入你的配方。"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1" role="group" aria-label="创作配方视图">
            <button
              type="button"
              className={`chip min-h-10 px-3 ${mine ? "chip-active" : ""}`}
              aria-pressed={mine}
              onClick={() => onScopeChange?.("mine")}
            >
              <Library size={14} aria-hidden="true" /> 我的配方
            </button>
            <button
              type="button"
              className={`chip min-h-10 px-3 ${!mine ? "chip-active" : ""}`}
              aria-pressed={!mine}
              onClick={() => onScopeChange?.("discover")}
            >
              <Compass size={14} aria-hidden="true" /> 公开发现
            </button>
          </div>
          <button type="button" className="icon-btn h-10 w-10" aria-label="刷新创作配方" onClick={onRefresh} disabled={loading}>
            <RefreshCw size={16} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 border-y border-line py-3">
        <div className="flex flex-wrap gap-1" role="group" aria-label="筛选创作配方">
          {visibleFilters.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`chip min-h-10 px-3 ${filter === value ? "chip-active" : ""}`}
              aria-pressed={filter === value}
              onClick={() => onFilterChange?.(value)}
            >
              {label}
            </button>
          ))}
        </div>
        {!mine && (
          <div className="flex min-w-0 flex-1 items-center justify-end gap-2 sm:max-w-sm" role="search">
            <label htmlFor="public-recipe-search" className="sr-only">搜索公开创作配方</label>
            <input
              id="public-recipe-search"
              type="search"
              className="input min-w-0 flex-1 px-3 py-2 text-xs"
              placeholder="搜索公开配方"
              value={query}
              onChange={(event) => onQueryChange?.(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") onSearch?.(); }}
            />
            <button type="button" className="icon-btn h-10 w-10" aria-label="搜索公开配方" onClick={onSearch} disabled={loading}>
              <Search size={16} aria-hidden="true" />
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-xl border border-bad/30 bg-bad/10 px-3 py-3 text-sm text-bad" role="alert">
          <p>{error}</p>
          <button type="button" className="btn-secondary btn-sm mt-2" onClick={onRefresh}>重新加载</button>
        </div>
      )}

      {loading && recipes.length === 0 && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-busy="true" aria-label="正在加载创作配方">
          {[0, 1, 2].map((key) => <div key={key} className="h-80 animate-pulse rounded-xl2 bg-white/[0.04]" />)}
        </div>
      )}

      {!loading && !error && recipes.length === 0 && (
        <div className="rounded-xl border border-dashed border-line px-4 py-10 text-center" role="status">
          <RotateCcw size={22} className="mx-auto text-fog" aria-hidden="true" />
          <p className="mt-2 text-sm text-mist">
            {mine ? "当前筛选下还没有创作配方" : "暂时没有匹配的公开配方"}
          </p>
        </div>
      )}

      {recipes.length > 0 && (
        <div className="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {recipes.map((recipe) => (
            <CreationRecipeCard
              key={`${scope}-${recipe.id}`}
              recipe={recipe}
              scope={scope}
              onRestore={onRestore}
              onFavorite={onFavorite}
              onDelete={onDelete}
              onRename={onRename}
              onToggleVisibility={onToggleVisibility}
              onClone={onClone}
              onLoadVersions={onLoadVersions}
              onActivate={onActivate}
            />
          ))}
        </div>
      )}
    </section>
  );
}
