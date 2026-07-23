"use client";

import { BookOpen, LoaderCircle, RotateCcw, X } from "lucide-react";

export default function ProjectRecipes({ recipes = [], busy, onRemove, onRestore }) {
  if (!recipes.length) {
    return <div className="border-y border-line py-12 text-center text-sm text-mist" role="status">暂无创作配方</div>;
  }
  return (
    <ul className="divide-y divide-line border-y border-line" aria-label="项目配方">
      {recipes.map((recipe) => (
        <li key={recipe.recipe_id} className="flex min-w-0 items-center gap-3 py-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line bg-white/[0.03] text-good">
            <BookOpen size={15} aria-hidden="true" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-sm font-medium text-snow">{recipe.title || `配方 #${recipe.recipe_id}`}</span>
              {recipe.current_version && <span className="text-[11px] text-fog">v{recipe.current_version}</span>}
            </div>
            <p className="mt-1 truncate text-xs text-fog">
              {recipe.category === "video" ? "视频配方" : "图片配方"}
              {recipe.schema_version ? ` · ${recipe.schema_version}` : ""}
            </p>
          </div>
          <button type="button" className="btn-secondary btn-sm shrink-0" onClick={() => onRestore(recipe)} disabled={busy}>
            {busy ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <RotateCcw size={14} aria-hidden="true" />}
            恢复到创作
          </button>
          <button type="button" className="icon-btn h-8 w-8" onClick={() => onRemove(recipe.recipe_id)} disabled={busy} title="移出项目" aria-label="移出项目">
            <X size={14} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  );
}
