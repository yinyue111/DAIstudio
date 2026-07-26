"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  Film,
  Folder,
  FolderInput,
  ImageOff,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { safeAssetMediaSrc } from "../../components/AssetMedia";

// 计算某个文件夹的所有后代 id，用于"移动到"下拉里排除自身与子孙（防环，后端也有 409 校验）。
function descendantIds(folders, rootId) {
  const childrenByParent = new Map();
  for (const item of folders) {
    const parentId = item.parent_id || 0;
    if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
    childrenByParent.get(parentId).push(item.id);
  }
  const result = new Set();
  const queue = [rootId];
  while (queue.length) {
    const current = queue.shift();
    for (const childId of childrenByParent.get(current) || []) {
      if (!result.has(childId)) {
        result.add(childId);
        queue.push(childId);
      }
    }
  }
  return result;
}

function folderItemLabel(item) {
  if (item.filename) return item.filename;
  if (item.available === false) return "素材已失效";
  return item.type === "video" ? "视频素材" : "图片素材";
}

function FolderItemThumb({ item }) {
  const [failed, setFailed] = useState(false);
  const src = item.type === "video" ? "" : safeAssetMediaSrc(item.thumb || item.preview_url || item.url || "");
  if (item.available === false || failed || !src) {
    const Icon = item.type === "video" ? Film : ImageOff;
    return (
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-line bg-black/30 text-fog">
        <Icon size={13} aria-hidden="true" />
      </span>
    );
  }
  return (
    <span className="h-8 w-8 shrink-0 overflow-hidden rounded border border-line bg-black/30">
      <img src={src} alt="" loading="lazy" className="h-full w-full object-cover" onError={() => setFailed(true)} />
    </span>
  );
}

export default function AssetFolderPanel({
  folders,
  activeFolder,
  name,
  onNameChange,
  busy,
  onCreate,
  onSelect,
  onAddAssets,
  onRemoveAsset,
  onRename,
  onMoveParent,
  onReorder,
  onDelete,
}) {
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");

  useEffect(() => {
    setRenaming(false);
    setRenameDraft(activeFolder?.name || "");
  }, [activeFolder?.id]);

  const moveTargets = useMemo(() => {
    if (!activeFolder) return [];
    const blocked = descendantIds(folders, activeFolder.id);
    return folders.filter((item) => item.id !== activeFolder.id && !blocked.has(item.id));
  }, [folders, activeFolder]);

  const siblings = useMemo(() => {
    if (!activeFolder) return [];
    return folders.filter((item) => (item.parent_id || null) === (activeFolder.parent_id || null));
  }, [folders, activeFolder]);
  const siblingIndex = siblings.findIndex((item) => item.id === activeFolder?.id);

  function submitRename(event) {
    event.preventDefault();
    const trimmed = renameDraft.trim();
    if (!trimmed || trimmed === activeFolder?.name) {
      setRenaming(false);
      return;
    }
    onRename(trimmed);
    setRenaming(false);
  }

  return (
    <aside aria-labelledby="asset-folders-title">
      <div className="flex items-center justify-between gap-2">
        <h2 id="asset-folders-title" className="text-sm font-display font-semibold text-snow">素材文件夹</h2>
        {activeFolder && (
          <button type="button" className="icon-btn h-8 w-8 text-bad" onClick={onDelete} disabled={busy} title="删除文件夹" aria-label="删除文件夹">
            <Trash2 size={14} aria-hidden="true" />
          </button>
        )}
      </div>
      <form className="mt-3 flex gap-2" onSubmit={(event) => { event.preventDefault(); onCreate(); }}>
        <label className="sr-only" htmlFor="new-folder-name">新文件夹名称</label>
        <input id="new-folder-name" className="input h-9 min-w-0 flex-1 py-1 text-sm" value={name} onChange={(event) => onNameChange(event.target.value)} maxLength={128} placeholder="新文件夹" />
        <button type="submit" className="icon-btn h-9 w-9" disabled={!name.trim() || busy} title="创建文件夹" aria-label="创建文件夹">
          <Plus size={15} aria-hidden="true" />
        </button>
      </form>

      {!folders.length ? (
        <p className="mt-5 border-y border-line py-6 text-center text-xs text-fog">暂无文件夹</p>
      ) : (
        <ul className="mt-3 divide-y divide-line border-y border-line">
          {folders.map((folder) => (
            <li key={folder.id}>
              <button type="button" onClick={() => onSelect(folder.id)} className={`flex w-full items-center gap-2 px-2 py-2.5 text-left ${activeFolder?.id === folder.id ? "bg-white/[0.07]" : "hover:bg-white/[0.04]"}`}>
                <Folder size={14} className={activeFolder?.id === folder.id ? "text-aqua" : "text-fog"} aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-sm text-mist">
                  {folder.parent_id ? <span className="text-fog">└ </span> : null}
                  {folder.name}
                </span>
                <span className="text-[11px] text-fog">{folder.item_count}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {activeFolder && (
        <section className="mt-4" aria-labelledby="folder-detail-title">
          <div className="flex min-w-0 items-center justify-between gap-2">
            {renaming ? (
              <form className="flex min-w-0 flex-1 items-center gap-1" onSubmit={submitRename}>
                <label className="sr-only" htmlFor="rename-folder-name">文件夹名称</label>
                <input
                  id="rename-folder-name"
                  className="input h-8 min-w-0 flex-1 py-1 text-xs"
                  value={renameDraft}
                  onChange={(event) => setRenameDraft(event.target.value)}
                  maxLength={128}
                  autoFocus
                />
                <button type="submit" className="icon-btn h-7 w-7" disabled={!renameDraft.trim() || busy} title="保存名称" aria-label="保存文件夹名称">
                  <Check size={13} aria-hidden="true" />
                </button>
                <button type="button" className="icon-btn h-7 w-7" onClick={() => { setRenaming(false); setRenameDraft(activeFolder.name); }} title="取消重命名" aria-label="取消重命名">
                  <X size={13} aria-hidden="true" />
                </button>
              </form>
            ) : (
              <>
                <h3 id="folder-detail-title" className="truncate text-xs font-display font-medium text-mist">{activeFolder.name}</h3>
                <div className="flex shrink-0 items-center gap-1">
                  <button type="button" className="icon-btn h-7 w-7" onClick={() => { setRenaming(true); setRenameDraft(activeFolder.name); }} disabled={busy} title="重命名文件夹" aria-label="重命名文件夹">
                    <Pencil size={13} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-btn h-7 w-7" onClick={() => onReorder("up")} disabled={busy || siblingIndex <= 0} title="上移" aria-label="文件夹上移">
                    <ArrowUp size={13} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-btn h-7 w-7" onClick={() => onReorder("down")} disabled={busy || siblingIndex < 0 || siblingIndex >= siblings.length - 1} title="下移" aria-label="文件夹下移">
                    <ArrowDown size={13} aria-hidden="true" />
                  </button>
                </div>
              </>
            )}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <label className="sr-only" htmlFor="move-folder-parent">移动文件夹到</label>
            <select
              id="move-folder-parent"
              className="input h-8 min-w-0 flex-1 py-1 text-xs"
              value={activeFolder.parent_id || ""}
              onChange={(event) => onMoveParent(event.target.value ? Number(event.target.value) : null)}
              disabled={busy}
            >
              <option value="">根目录</option>
              {moveTargets.map((target) => (
                <option key={target.id} value={target.id}>{target.name}</option>
              ))}
            </select>
            <button type="button" className="btn-secondary btn-sm shrink-0" onClick={onAddAssets} disabled={busy}>
              <FolderInput size={14} aria-hidden="true" /> 移入素材
            </button>
          </div>
          <ul className="mt-2 space-y-1" aria-label="文件夹素材">
            {activeFolder.items.map((item) => (
              <li key={item.asset_ref} className="flex min-w-0 items-center gap-2 border-b border-line py-2 text-xs">
                <FolderItemThumb item={item} />
                <span className="min-w-0 flex-1 truncate text-fog" title={item.asset_ref}>{folderItemLabel(item)}</span>
                <button type="button" className="icon-btn h-7 w-7" onClick={() => onRemoveAsset(item.asset_ref)} disabled={busy} aria-label="移出文件夹" title="移出文件夹">
                  <X size={13} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}
