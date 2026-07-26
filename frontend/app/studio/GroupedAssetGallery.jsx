"use client";

import { useMemo, useState } from "react";

function groupAssetsByTask(assets = []) {
  const groups = [];
  const byKey = new Map();
  for (const asset of assets || []) {
    const key = asset?.task_id != null ? `task-${asset.task_id}` : `asset-${asset?.id}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        key,
        taskId: asset?.task_id || null,
        category: asset?._cat || asset?.category || asset?.type || "image",
        createdAt: asset?.created_at || "",
        assets: [],
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.assets.push(asset);
  }
  return groups;
}

function batchTitle(group) {
  const count = group.assets.length;
  const firstType = group.assets[0]?.type;
  const type = group.category === "video" || firstType === "video"
    ? "视频"
    : group.category === "audio" || firstType === "audio" ? "音频" : "图片";
  return `本批次 · ${count} ${type === "图片" ? "张" : "条"}`;
}

export default function GroupedAssetGallery({
  assets = [],
  renderAsset,
  className = "grid grid-cols-2 items-start gap-3 sm:grid-cols-3 lg:grid-cols-4",
  groupClassName = "rounded-xl2 border border-line bg-white/[0.025] p-2.5 transition-colors duration-200 hover:border-line2",
  assetGridClassName = "grid grid-cols-1 gap-3",
}) {
  const groups = useMemo(() => groupAssetsByTask(assets), [assets]);
  const [expanded, setExpanded] = useState(() => new Set());

  if (!groups.length) return null;

  function toggle(key) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <div className={className}>
      {groups.map((group) => {
        const canCollapse = group.assets.length > 1;
        const isExpanded = expanded.has(group.key);
        const visibleAssets = canCollapse && !isExpanded ? group.assets.slice(0, 1) : group.assets;
        return (
          <section key={group.key} className={`${groupClassName} flex flex-col`}>
            <div className="mb-2 flex min-h-[2.75rem] items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-xs font-display font-semibold text-mist">{batchTitle(group)}</p>
                <p className="text-[11px] text-fog">
                  {canCollapse
                    ? (isExpanded ? "已展开全部素材" : "默认展示首张，展开查看整批")
                    : "单张素材"}
                </p>
              </div>
              {canCollapse ? (
                <button type="button" onClick={() => toggle(group.key)} className="btn-secondary btn-sm shrink-0">
                  {isExpanded ? "收起" : `展开 ${group.assets.length}`}
                </button>
              ) : (
                <span className="btn-secondary btn-sm pointer-events-none shrink-0 opacity-0">占位</span>
              )}
            </div>
            <div className="flex-1 overflow-hidden transition-all duration-300 ease-out">
              <div className={isExpanded ? `${assetGridClassName} items-start animate-fadeup` : "grid grid-cols-1 items-start gap-3"}>
                {visibleAssets.map((asset) => (
                  <div key={asset.id} className="h-full">
                    {renderAsset(asset)}
                  </div>
                ))}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}
