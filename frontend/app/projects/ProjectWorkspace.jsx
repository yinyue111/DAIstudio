"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Archive,
  BookOpen,
  BookPlus,
  Cloud,
  CloudAlert,
  Coins,
  Download,
  FilePenLine,
  FolderInput,
  Images,
  ListPlus,
  ListTodo,
  LoaderCircle,
  PackageOpen,
  Save,
  ScanSearch,
  Tags,
  Trash2,
  Undo2,
} from "lucide-react";
import AssetMedia from "../../components/AssetMedia";
import { formatLocalDateTime } from "../../lib/datetime";
import ProjectRecipes from "./ProjectRecipes";
import ProjectTasks from "./ProjectTasks";

const ROLE_LABELS = {
  source: "原始素材",
  reference: "参考素材",
  product: "商品",
  product_detail: "商品细节",
  subject: "人物/主体",
  character: "角色",
  style: "风格",
  first_frame: "首帧",
  last_frame: "尾帧",
  mask: "蒙版",
  fallback: "备用图",
  output: "成品",
};

const TABS = [
  ["assets", "素材", Images],
  ["tasks", "任务", ListTodo],
  ["recipes", "配方", BookOpen],
  ["draft", "草稿", FilePenLine],
];

function EmptyState({ children }) {
  return <div className="border-y border-line py-12 text-center text-sm text-mist" role="status">{children}</div>;
}

function parseTags(value) {
  return Array.from(new Set(
    String(value || "")
      .split(/[,，]/)
      .map((item) => item.trim().replace(/^#+/, ""))
      .filter(Boolean),
  )).slice(0, 20);
}

function LinkedAsset({
  link,
  fallbackAsset,
  busy,
  similarity,
  onRemove,
  onSaveTags,
  onFindSimilar,
}) {
  const asset = link.asset || fallbackAsset;
  const [tagText, setTagText] = useState((link.metadata?.tags || []).join(", "));
  useEffect(() => {
    setTagText((link.metadata?.tags || []).join(", "));
  }, [link.asset_ref, link.metadata?.updated_at]);
  const exactMatches = similarity?.matches?.filter((item) => item.match_type === "exact").length || 0;
  const perceptualMatches = similarity?.matches?.filter((item) => item.match_type === "similar").length || 0;
  return (
    <li className="group relative overflow-hidden rounded-lg border border-line bg-black/20">
      <div className="aspect-square">
        {asset ? (
          <AssetMedia asset={asset} className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-xs text-fog">素材已失效</div>
        )}
      </div>
      <div className="flex min-w-0 items-center gap-2 border-t border-line px-2 py-2">
        <span className="min-w-0 flex-1 truncate text-[11px] text-mist">{ROLE_LABELS[link.role] || link.role}</span>
        {(link.duplicate_count > 0 || link.similar_count > 0) && (
          <span className="shrink-0 text-[10px] text-warn">
            {link.duplicate_count > 0 ? `重复 ${link.duplicate_count}` : ""}
            {link.duplicate_count > 0 && link.similar_count > 0 ? " · " : ""}
            {link.similar_count > 0 ? `相似 ${link.similar_count}` : ""}
          </span>
        )}
        <button
          type="button"
          className="icon-btn h-7 w-7"
          onClick={() => onRemove(link.asset_ref)}
          disabled={busy}
          aria-label="从项目移除素材"
          title="从项目移除"
        >
          <Trash2 size={13} aria-hidden="true" />
        </button>
      </div>
      <div className="space-y-2 border-t border-line px-2 py-2">
        <form
          className="flex min-w-0 items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            onSaveTags(link.asset_ref, parseTags(tagText));
          }}
        >
          <Tags size={13} className="shrink-0 text-fog" aria-hidden="true" />
          <label className="sr-only" htmlFor={`project-asset-tags-${link.id}`}>素材标签</label>
          <input
            id={`project-asset-tags-${link.id}`}
            className="input h-8 min-w-0 flex-1 px-2 py-1 text-xs"
            value={tagText}
            onChange={(event) => setTagText(event.target.value)}
            maxLength={400}
            placeholder="标签，用逗号分隔"
          />
          <button type="submit" className="icon-btn h-8 w-8" disabled={busy} title="保存标签" aria-label="保存素材标签">
            <Save size={13} aria-hidden="true" />
          </button>
        </form>
        <button
          type="button"
          className="btn-ghost btn-sm w-full justify-center"
          disabled={busy}
          onClick={() => onFindSimilar(link.asset_ref)}
        >
          <ScanSearch size={13} aria-hidden="true" /> 查重与相似检测
        </button>
        {link.metadata?.analysis_status === "degraded" && !similarity && (
          <p className="flex items-start gap-1 text-[10px] leading-4 text-warn">
            <AlertTriangle size={11} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{link.metadata.analysis_error || "素材无法完整分析"}</span>
          </p>
        )}
        {similarity && (
          <div className="text-[10px] leading-4" role="status">
            <p className={similarity.status === "degraded" ? "text-warn" : "text-mist"}>
              精确重复 {exactMatches} · 感知相似 {perceptualMatches}
            </p>
            {similarity.message && <p className="mt-0.5 text-fog">{similarity.message}</p>}
            {!similarity.message && exactMatches + perceptualMatches === 0 && (
              <p className="mt-0.5 text-fog">未发现重复或相似素材</p>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function ProjectDraft({ value, status, updatedAt, onChange }) {
  const DraftIcon = status === "saving" ? LoaderCircle : status === "error" ? CloudAlert : Cloud;
  const statusText = status === "saving" ? "正在保存" : status === "error" ? "保存失败" : "已保存到云端";
  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3 text-xs text-fog">
        <span className="flex items-center gap-1.5">
          <DraftIcon size={14} className={status === "saving" ? "animate-spin" : ""} aria-hidden="true" />
          {statusText}
        </span>
        {updatedAt && <span>{formatLocalDateTime(updatedAt)}</span>}
      </div>
      <label className="sr-only" htmlFor="project-cloud-draft">项目云端草稿</label>
      <textarea
        id="project-cloud-draft"
        className="input min-h-72 w-full resize-y py-3 text-sm leading-6"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        maxLength={80000}
        placeholder="记录脚本、创意方向、交付要求..."
      />
    </div>
  );
}

export default function ProjectWorkspace({
  project,
  assetMap,
  loading,
  busy,
  assetRole,
  draftText,
  draftStatus,
  onDraftChange,
  onAssetRoleChange,
  onAddAssets,
  onAddTasks,
  onAddRecipes,
  onRemoveAsset,
  onRemoveTask,
  onRemoveRecipe,
  onRestoreRecipe,
  onSaveAssetTags,
  onFindSimilar,
  similarityByRef,
  onAutoArchiveChange,
  onExport,
  onArchive,
  onDelete,
}) {
  const [activeTab, setActiveTab] = useState("assets");
  useEffect(() => setActiveTab("assets"), [project?.id]);
  if (loading) return <div className="skeleton min-h-[420px]" aria-label="项目详情加载中" />;
  if (!project) {
    return (
      <div className="flex min-h-[420px] flex-col items-center justify-center border-y border-line text-center" role="status">
        <PackageOpen size={30} className="text-fog" aria-hidden="true" />
        <p className="mt-3 text-sm text-mist">选择一个项目</p>
      </div>
    );
  }
  return (
    <section aria-labelledby="project-workspace-title">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line pb-4">
        <div className="min-w-0">
          <h2 id="project-workspace-title" className="truncate text-lg font-display font-semibold text-snow">{project.title}</h2>
          <p className="mt-1 max-w-2xl text-sm text-fog">{project.description || `${project.asset_count} 个素材 · ${project.recipe_count} 个配方 · ${project.task_count} 个任务`}</p>
        </div>
        <div className="flex items-center gap-1">
          <button type="button" className="btn-ghost btn-sm" onClick={onExport} disabled={busy}>
            <Download size={15} aria-hidden="true" /> 导出 ZIP
          </button>
          <button type="button" className="icon-btn" onClick={onArchive} disabled={busy} title={project.status === "archived" ? "恢复项目" : "归档项目"} aria-label={project.status === "archived" ? "恢复项目" : "归档项目"}>
            {project.status === "archived" ? <Undo2 size={16} aria-hidden="true" /> : <Archive size={16} aria-hidden="true" />}
          </button>
          <button type="button" className="icon-btn text-bad" onClick={onDelete} disabled={busy} title="删除项目" aria-label="删除项目">
            <Trash2 size={16} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line py-3 text-xs text-fog">
        <span className="flex items-center gap-1.5">
          <Coins size={14} className="text-aqua" aria-hidden="true" />
          已结算 <b className="tabular-nums text-snow">{project.cost_settled || 0}</b> 积分
        </span>
        <span>累计冻结 <b className="tabular-nums text-mist">{project.cost_frozen || 0}</b> 积分</span>
        <label className="flex items-center gap-2" htmlFor="project-auto-archive">
          自动归档
          <select
            id="project-auto-archive"
            className="input h-8 w-28 py-1 text-xs"
            value={project.auto_archive_after_days ?? ""}
            onChange={(event) => onAutoArchiveChange(event.target.value ? Number(event.target.value) : null)}
            disabled={busy}
          >
            <option value="">不自动归档</option>
            <option value="7">7 天未更新</option>
            <option value="30">30 天未更新</option>
            <option value="90">90 天未更新</option>
            <option value="180">180 天未更新</option>
            <option value="365">365 天未更新</option>
          </select>
        </label>
      </div>

      <div className="my-4 grid grid-cols-4 border-y border-line" role="tablist" aria-label="项目内容">
        {TABS.map(([key, label, Icon]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={activeTab === key}
            onClick={() => setActiveTab(key)}
            className={`flex h-10 items-center justify-center gap-1.5 text-xs transition ${activeTab === key ? "bg-white/[0.07] text-snow" : "text-fog hover:bg-white/[0.04] hover:text-mist"}`}
          >
            <Icon size={14} aria-hidden="true" /> {label}
          </button>
        ))}
      </div>

      {activeTab === "assets" && (
        <div role="tabpanel">
          <div className="flex flex-wrap items-center gap-2 pb-4">
            <label className="text-xs text-fog" htmlFor="project-asset-role">素材角色</label>
            <select id="project-asset-role" className="input h-9 w-36 py-1 text-sm" value={assetRole} onChange={(event) => onAssetRoleChange(event.target.value)}>
              {Object.entries(ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <button type="button" className="btn-primary btn-sm" onClick={onAddAssets} disabled={busy}>
              <FolderInput size={15} aria-hidden="true" /> 添加素材
            </button>
          </div>
          {!project.assets.length ? (
            <EmptyState>项目中还没有素材</EmptyState>
          ) : (
            <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4" aria-label="项目素材">
              {project.assets.map((link) => (
                <LinkedAsset
                  key={link.id || link.asset_ref}
                  link={link}
                  fallbackAsset={assetMap.get(link.asset_ref)}
                  busy={busy}
                  similarity={similarityByRef[link.asset_ref]}
                  onRemove={onRemoveAsset}
                  onSaveTags={onSaveAssetTags}
                  onFindSimilar={onFindSimilar}
                />
              ))}
            </ul>
          )}
        </div>
      )}
      {activeTab === "tasks" && (
        <div role="tabpanel">
          <div className="flex items-center justify-end pb-4">
            <button type="button" className="btn-primary btn-sm" onClick={onAddTasks} disabled={busy}>
              <ListPlus size={15} aria-hidden="true" /> 添加任务
            </button>
          </div>
          <ProjectTasks tasks={project.tasks} busy={busy} onRemove={onRemoveTask} />
        </div>
      )}
      {activeTab === "recipes" && (
        <div role="tabpanel">
          <div className="flex items-center justify-end pb-4">
            <button type="button" className="btn-primary btn-sm" onClick={onAddRecipes} disabled={busy}>
              <BookPlus size={15} aria-hidden="true" /> 添加配方
            </button>
          </div>
          <ProjectRecipes recipes={project.recipes} busy={busy} onRemove={onRemoveRecipe} onRestore={onRestoreRecipe} />
        </div>
      )}
      {activeTab === "draft" && (
        <div role="tabpanel">
          <ProjectDraft value={draftText} status={draftStatus} updatedAt={project.draft_updated_at} onChange={onDraftChange} />
        </div>
      )}
    </section>
  );
}
