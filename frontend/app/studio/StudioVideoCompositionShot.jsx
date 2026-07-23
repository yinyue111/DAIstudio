"use client";

import { FolderOpen, Link2Off, Video } from "lucide-react";
import AssetMedia from "../../components/AssetMedia";

const TRANSITIONS = [
  ["cut", "硬切"],
  ["fade", "淡入淡出"],
  ["crossfade", "交叉溶解"],
  ["wipeleft", "向左擦除"],
  ["slideright", "向右滑动"],
];

function finite(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function assetLabel(asset, assetRef) {
  return asset?.filename || asset?.name || assetRef || "未绑定素材";
}

export default function StudioVideoCompositionShot({
  draft,
  storyboardShot = {},
  index,
  busy = false,
  onChange,
  onPick,
  onClear,
}) {
  const asset = draft.asset || null;
  const bound = Boolean(draft.asset_ref);
  return (
    <li className="border-b border-line py-3 last:border-0">
      <div className="flex min-w-0 items-start gap-3">
        <div className="flex h-16 w-24 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-line bg-black/25">
          {asset ? (
            <AssetMedia
              asset={{ ...asset, type: "video" }}
              className="h-full w-full object-cover"
              fallbackClassName="flex h-full w-full items-center justify-center text-fog"
            />
          ) : <Video size={20} className="text-fog" aria-hidden="true" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <p className="text-xs font-display font-semibold text-snow">镜头 {index + 1}</p>
            <span className="badge border border-line bg-white/[0.04] text-fog">
              {finite(storyboardShot.end_seconds) - finite(storyboardShot.start_seconds) > 0
                ? `${(finite(storyboardShot.end_seconds) - finite(storyboardShot.start_seconds)).toFixed(1)}s`
                : "时长待定"}
            </span>
            <span className="min-w-0 truncate text-[11px] text-fog" title={draft.shot_id}>{draft.shot_id}</span>
            {draft.generation_task_id && (
              <span className="badge border border-aqua/25 bg-aqua/10 text-aqua">
                生成任务 #{draft.generation_task_id}
              </span>
            )}
          </div>
          <p className={`mt-1 truncate text-xs ${bound ? "text-mist" : draft.generation_task_id ? "text-aqua" : "text-warn"}`} title={assetLabel(asset, draft.asset_ref)}>
            {!bound && draft.generation_task_id
              ? "正在生成，完成后自动绑定"
              : assetLabel(asset, draft.asset_ref)}
          </p>
          <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-fog">
            {String(storyboardShot.visual || storyboardShot.action || "当前镜头暂无画面描述")}
          </p>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            className="icon-btn h-9 w-9"
            onClick={onPick}
            disabled={busy}
            aria-label={bound ? `替换镜头 ${index + 1} 素材` : `绑定镜头 ${index + 1} 素材`}
            title={bound ? "替换视频素材" : "绑定视频素材"}
          >
            <FolderOpen size={15} aria-hidden="true" />
          </button>
          {bound && (
            <button
              type="button"
              className="icon-btn h-9 w-9 text-fog hover:text-bad"
              onClick={onClear}
              disabled={busy}
              aria-label={`解除镜头 ${index + 1} 素材绑定`}
              title="解除素材绑定"
            >
              <Link2Off size={15} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <label>
          <span className="label mb-1">素材起点</span>
          <input
            type="number"
            min="0"
            max="1800"
            step="0.1"
            className="input min-h-10 px-2 py-1.5 text-xs"
            value={draft.source_start_seconds ?? 0}
            disabled={busy}
            onChange={(event) => onChange?.({ source_start_seconds: finite(event.target.value) })}
          />
        </label>
        <label>
          <span className="label mb-1">素材终点</span>
          <input
            type="number"
            min="0.1"
            max="1800"
            step="0.1"
            className="input min-h-10 px-2 py-1.5 text-xs"
            value={draft.source_end_seconds ?? ""}
            placeholder="自动"
            disabled={busy}
            onChange={(event) => onChange?.({
              source_end_seconds: event.target.value === "" ? null : finite(event.target.value),
            })}
          />
        </label>
        <label>
          <span className="label mb-1">成片时长</span>
          <input
            type="number"
            min="0.1"
            max="1800"
            step="0.1"
            className="input min-h-10 px-2 py-1.5 text-xs"
            value={draft.duration_seconds ?? ""}
            placeholder="自动"
            disabled={busy}
            onChange={(event) => onChange?.({
              duration_seconds: event.target.value === "" ? null : finite(event.target.value),
            })}
          />
        </label>
        <label>
          <span className="label mb-1">转场</span>
          <select
            className="select min-h-10 px-2 py-1.5 text-xs"
            value={draft.transition?.type || "cut"}
            disabled={busy}
            onChange={(event) => onChange?.({
              transition: {
                type: event.target.value,
                duration_seconds: event.target.value === "cut"
                  ? 0
                  : Math.max(0.05, finite(draft.transition?.duration_seconds, 0.4)),
              },
            })}
          >
            {TRANSITIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label>
          <span className="label mb-1">转场时长</span>
          <input
            type="number"
            min="0.05"
            max="3"
            step="0.05"
            className="input min-h-10 px-2 py-1.5 text-xs"
            value={draft.transition?.duration_seconds ?? 0}
            disabled={busy || draft.transition?.type === "cut"}
            onChange={(event) => onChange?.({
              transition: { ...draft.transition, duration_seconds: finite(event.target.value, 0.4) },
            })}
          />
        </label>
      </div>
    </li>
  );
}
