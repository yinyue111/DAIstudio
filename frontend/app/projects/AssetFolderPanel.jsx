"use client";

import { Folder, FolderInput, Plus, Trash2, X } from "lucide-react";

export default function AssetFolderPanel({ folders, activeFolder, name, onNameChange, busy, onCreate, onSelect, onAddAssets, onRemoveAsset, onDelete }) {
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
                <span className="min-w-0 flex-1 truncate text-sm text-mist">{folder.name}</span>
                <span className="text-[11px] text-fog">{folder.item_count}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {activeFolder && (
        <section className="mt-4" aria-labelledby="folder-detail-title">
          <div className="flex items-center justify-between gap-2">
            <h3 id="folder-detail-title" className="truncate text-xs font-display font-medium text-mist">{activeFolder.name}</h3>
            <button type="button" className="btn-secondary btn-sm" onClick={onAddAssets} disabled={busy}>
              <FolderInput size={14} aria-hidden="true" /> 移入素材
            </button>
          </div>
          <ul className="mt-2 space-y-1" aria-label="文件夹素材">
            {activeFolder.items.map((item) => (
              <li key={item.asset_ref} className="flex min-w-0 items-center gap-2 border-b border-line py-2 text-xs">
                <span className="min-w-0 flex-1 truncate text-fog">{item.asset_ref}</span>
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
