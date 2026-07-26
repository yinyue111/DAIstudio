"use client";

import { useEffect, useState } from "react";
import {
  Check,
  ChevronDown,
  Copy,
  Eye,
  EyeOff,
  Flame,
  GitBranch,
  ImageIcon,
  Pencil,
  Play,
  Star,
  Trash2,
  Video,
  X,
} from "lucide-react";
import AssetMedia from "../../components/AssetMedia";
import { api } from "../../lib/api";
import { formatLocalDateTime } from "../../lib/datetime";
import CreationRecipeGovernance from "./CreationRecipeGovernance";

function recipeAsset(recipe) {
  const url = String(recipe?.cover_asset_url || "");
  if (!url) return null;
  return {
    type: recipe.category === "video" ? "video" : "image",
    url,
    thumb: url,
    preview_url: url,
  };
}

function previewText(recipe) {
  return String(
    recipe?.version?.payload?.prompt
    || recipe?.version?.payload?.reverse_result?.final_text
    || "",
  );
}

// 配方热度：后端 _serialize 聚合的 usage 统计（apply/clone/generation_* 埋点总数）。
function usageSummary(recipe) {
  const usage = recipe?.usage;
  if (!usage || typeof usage.total !== "number") return null;
  const derived = Number(usage.by_event?.clone || 0);
  return {
    total: Number(usage.total || 0),
    uniqueUsers: Number(usage.unique_users || 0),
    derived,
  };
}

function visibilityLabel(recipe) {
  if (recipe.visibility !== "public") return "私有";
  if (recipe.moderation_status === "approved" && recipe.approved_version === recipe.current_version) return "已公开";
  if (recipe.moderation_status === "pending") return "审核中";
  if (recipe.moderation_status === "rejected") return "未通过";
  return "待提交";
}

export default function CreationRecipeCard({
  recipe,
  scope = "mine",
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
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [resolvedRecipe, setResolvedRecipe] = useState(recipe);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(recipe.title);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionsLoaded, setVersionsLoaded] = useState(false);
  const [versions, setVersions] = useState(recipe.version ? [recipe.version] : []);
  const [selectedVersion, setSelectedVersion] = useState(recipe.current_version);

  useEffect(() => {
    setResolvedRecipe(recipe);
    setTitle(recipe.title);
    setSelectedVersion(recipe.current_version);
    setVersions((rows) => rows.map((row) => (
      row.version === recipe.current_version && recipe.version ? recipe.version : row
    )));
  }, [recipe]);

  async function run(action, callback) {
    if (busy) return null;
    setBusy(action);
    setError("");
    try {
      return await callback();
    } catch (cause) {
      setError(cause?.message || "操作失败，请稍后重试");
      return null;
    } finally {
      setBusy("");
    }
  }

  async function toggleVersions() {
    const next = !versionsOpen;
    setVersionsOpen(next);
    if (!next || versionsLoaded || !mine) return;
    const rows = await run("versions", () => onLoadVersions?.(recipe));
    if (Array.isArray(rows)) {
      setVersions(rows);
      setVersionsLoaded(true);
    }
  }

  async function submitRename() {
    const normalized = title.trim();
    if (!normalized) {
      setError("配方名称不能为空");
      return;
    }
    const updated = await run("rename", () => onRename?.(recipe, normalized));
    if (updated) {
      setResolvedRecipe(updated);
      setEditing(false);
    }
  }

  async function restoreRecipe(version) {
    const eventId = `recipe-apply-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    await api.recordCreationRecipeUsage(recipe.id, {
      event_type: "apply",
      version: Number(version?.version) || recipe.current_version,
      client_event_id: eventId,
      context: { entry: "prompt_library" },
    });
    return onRestore?.(recipe, version);
  }

  const asset = recipeAsset(recipe);
  const usage = usageSummary(recipe);
  const selected = versions.find((row) => row.version === selectedVersion)
    || (selectedVersion === recipe.current_version ? recipe.version : null);

  return (
    <article className="overflow-hidden rounded-xl2 border border-line bg-black/15 transition hover:border-line2">
      <div className="relative aspect-video border-b border-line bg-base2/70">
        {asset ? (
          <AssetMedia asset={asset} className="h-full w-full object-cover" fallbackClassName="h-full w-full" />
        ) : (
          <div className="flex h-full items-center justify-center text-fog">
            {recipe.category === "video"
              ? <Video size={28} aria-hidden="true" />
              : <ImageIcon size={28} aria-hidden="true" />}
          </div>
        )}
        <div className="absolute left-2 top-2 flex flex-wrap gap-1.5">
          <span className="badge border border-line bg-black/70 text-white">
            {recipe.category === "video" ? "视频" : "图片"}
          </span>
          <span className="badge border border-line bg-black/70 text-white">
            {visibilityLabel(resolvedRecipe)}
          </span>
        </div>
        {mine && (
          <button
            type="button"
            className={`icon-btn absolute right-2 top-2 h-10 w-10 bg-black/70 ${recipe.favorite ? "text-rose" : "text-white"}`}
            aria-label={recipe.favorite ? "取消收藏创作配方" : "收藏创作配方"}
            aria-pressed={Boolean(recipe.favorite)}
            onClick={() => run("favorite", () => onFavorite?.(recipe))}
            disabled={Boolean(busy)}
          >
            <Star size={16} fill={recipe.favorite ? "currentColor" : "none"} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="p-3">
        {editing ? (
          <div className="flex items-center gap-1.5">
            <label htmlFor={`recipe-title-${recipe.id}`} className="sr-only">创作配方名称</label>
            <input
              id={`recipe-title-${recipe.id}`}
              className="input min-w-0 flex-1 px-2.5 py-2 text-sm"
              value={title}
              maxLength={128}
              autoFocus
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submitRename();
                if (event.key === "Escape") {
                  setTitle(recipe.title);
                  setEditing(false);
                }
              }}
            />
            <button type="button" className="icon-btn h-10 w-10 text-ok" aria-label="保存名称" onClick={submitRename} disabled={Boolean(busy)}>
              <Check size={16} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="icon-btn h-10 w-10"
              aria-label="取消重命名"
              onClick={() => {
                setTitle(recipe.title);
                setEditing(false);
              }}
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <div className="flex min-w-0 items-center justify-between gap-2">
            <h3 className="min-w-0 truncate text-sm font-semibold text-snow">{recipe.title}</h3>
            {mine && (
              <button type="button" className="icon-btn h-9 w-9 flex-none" aria-label={`重命名 ${recipe.title}`} onClick={() => setEditing(true)}>
                <Pencil size={14} aria-hidden="true" />
              </button>
            )}
          </div>
        )}

        <p className="mt-2 line-clamp-3 min-h-[3.75rem] text-xs leading-relaxed text-fog">
          {previewText(recipe) || "该配方保存了完整工作区，可直接恢复到创作页。"}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-fog">
          <span>v{recipe.current_version}</span>
          {recipe.updated_at && <span>{formatLocalDateTime(recipe.updated_at)}</span>}
          {usage && (
            <span
              className={`inline-flex items-center gap-1 ${usage.total > 0 ? "text-warn" : ""}`}
              title={`累计使用 ${usage.total} 次 · ${usage.uniqueUsers} 人使用过${usage.derived ? ` · 被派生 ${usage.derived} 次` : ""}`}
            >
              <Flame size={11} aria-hidden="true" />
              热度 {usage.total}
            </span>
          )}
          {recipe.version?.payload?.derived_from_recipe_id && (
            <span className="inline-flex items-center gap-1">
              <GitBranch size={11} aria-hidden="true" />
              派生自 #{recipe.version.payload.derived_from_recipe_id}
            </span>
          )}
        </div>

        {error && <p className="mt-2 text-xs text-bad" role="alert">{error}</p>}

        {mine ? (
          <>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <button type="button" className="btn-primary btn-sm min-h-10" onClick={() => run("restore", () => restoreRecipe(recipe.version))} disabled={Boolean(busy)}>
                <Play size={14} aria-hidden="true" /> 恢复创作
              </button>
              <button type="button" className="btn-secondary btn-sm min-h-10" onClick={toggleVersions} aria-expanded={versionsOpen} disabled={Boolean(busy)}>
                <ChevronDown size={14} className={versionsOpen ? "rotate-180" : ""} aria-hidden="true" />
                版本管理
              </button>
            </div>

            {versionsOpen && (
              <div className="mt-2 rounded-xl border border-line bg-black/15 p-2">
                <label htmlFor={`recipe-version-${recipe.id}`} className="text-[11px] font-medium text-mist">选择配方版本</label>
                <select
                  id={`recipe-version-${recipe.id}`}
                  className="input mt-1 w-full px-2.5 py-2 text-xs"
                  value={selectedVersion}
                  onChange={(event) => setSelectedVersion(Number(event.target.value))}
                  disabled={busy === "versions" || versions.length === 0}
                >
                  {versions.map((version) => (
                    <option key={version.id} value={version.version}>
                      v{version.version}{version.version === recipe.current_version ? " · 当前" : ""}
                      {version.created_at ? ` · ${formatLocalDateTime(version.created_at)}` : ""}
                    </option>
                  ))}
                </select>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <button type="button" className="btn-secondary btn-sm min-h-10" onClick={() => run("restore-version", () => restoreRecipe(selected))} disabled={Boolean(busy) || !selected}>
                    <Play size={14} aria-hidden="true" /> 恢复所选
                  </button>
                  <button
                    type="button"
                    className="btn-secondary btn-sm min-h-10"
                    onClick={() => run("activate", async () => {
                      const updated = await onActivate?.(recipe, selectedVersion);
                      if (updated) {
                        setResolvedRecipe(updated);
                        setSelectedVersion(updated.current_version);
                      }
                      return updated;
                    })}
                    disabled={Boolean(busy) || !selected || selectedVersion === recipe.current_version}
                  >
                    <Check size={14} aria-hidden="true" /> 设为当前
                  </button>
                </div>
              </div>
            )}

            <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-line pt-2">
              <button type="button" className="btn-ghost btn-sm min-h-10" onClick={() => run("visibility", async () => {
                const updated = await onToggleVisibility?.(resolvedRecipe);
                if (updated) setResolvedRecipe(updated);
                return updated;
              })} disabled={Boolean(busy)}>
                {resolvedRecipe.visibility === "public"
                  ? <EyeOff size={14} aria-hidden="true" />
                  : <Eye size={14} aria-hidden="true" />}
                {resolvedRecipe.visibility === "public" ? "转为私有" : "设为公开候选"}
              </button>
              <button type="button" className="btn-ghost btn-sm min-h-10" onClick={() => run("clone", () => onClone?.(recipe, selectedVersion))} disabled={Boolean(busy)}>
                <Copy size={14} aria-hidden="true" /> 复制派生
              </button>
              <button type="button" className="icon-btn ml-auto h-10 w-10 text-bad" aria-label={`删除 ${recipe.title}`} onClick={() => run("delete", () => onDelete?.(recipe))} disabled={Boolean(busy)}>
                <Trash2 size={15} aria-hidden="true" />
              </button>
            </div>
            <CreationRecipeGovernance
              recipe={resolvedRecipe}
              version={selectedVersion}
              onRecipeChange={setResolvedRecipe}
            />
          </>
        ) : (
          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <button type="button" className="btn-primary btn-sm min-h-10" onClick={() => run("restore", () => restoreRecipe(recipe.version))} disabled={Boolean(busy)}>
              <Play size={14} aria-hidden="true" /> 直接使用
            </button>
            <button type="button" className="btn-secondary btn-sm min-h-10" onClick={() => run("clone", () => onClone?.(recipe, recipe.current_version))} disabled={Boolean(busy)}>
              <Copy size={14} aria-hidden="true" /> 派生到我的配方
            </button>
          </div>
        )}
      </div>
    </article>
  );
}
