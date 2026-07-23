"use client";

import {
  ArrowDown,
  ArrowUp,
  Import,
  Lock,
  Merge,
  Play,
  RefreshCw,
  Save,
  Scissors,
  Trash2,
  Unlock,
  WandSparkles,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

const SHOT_FIELDS = [
  ["visual", "画面"],
  ["action", "动作"],
  ["camera", "镜头"],
  ["transition", "转场"],
  ["audio_cue", "声音"],
];

function numberValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function midpoint(shot) {
  const start = numberValue(shot.start_seconds);
  const end = numberValue(shot.end_seconds, start);
  return Number(((start + end) / 2).toFixed(2));
}

export default function StudioReverseStoryboardShot({
  shot,
  nextShot = null,
  index,
  total,
  selected = false,
  busy = false,
  targetModelName = "当前视频模型",
  onSelect,
  onUpdate,
  onDelete,
  onSplit,
  onMerge,
  onMoveBoundary,
  onMove,
  onToggleLock,
  onCompile,
  onApply,
  onReanalyze,
  onGenerate,
  generationDisabledReason = "",
  actionStatus = "",
}) {
  const selectedRef = useRef(null);
  const [splitAt, setSplitAt] = useState(() => midpoint(shot));
  const [boundaryAt, setBoundaryAt] = useState(() => numberValue(shot.end_seconds));
  const locked = Boolean(shot.locked);
  const compiledPrompt = String(shot.compiled_prompt || "").trim();
  const compilation = shot.compilation && typeof shot.compilation === "object" ? shot.compilation : {};

  useEffect(() => {
    if (selected) selectedRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [selected]);
  useEffect(() => setSplitAt(midpoint(shot)), [shot.start_seconds, shot.end_seconds]);
  useEffect(() => setBoundaryAt(numberValue(shot.end_seconds)), [shot.end_seconds]);
  const canMoveBoundary = Boolean(
    nextShot
    && !locked
    && !nextShot.locked
    && numberValue(nextShot.source_segment_index, 1) === numberValue(shot.source_segment_index, 1),
  );

  return (
    <li
      ref={selected ? selectedRef : null}
      aria-current={selected ? "true" : undefined}
      className={`border-b pb-4 last:border-0 ${selected ? "border-aqua bg-aqua/[0.05] px-2" : "border-line"}`}
      onFocusCapture={onSelect}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button type="button" className="text-left" onClick={onSelect}>
          <span className="text-xs font-display font-semibold text-snow">镜头 {index + 1}</span>
          <span className="ml-2 text-[10px] text-fog">
            {shot.source_segment_index ? `片段 ${shot.source_segment_index} · ` : ""}
            {numberValue(shot.start_seconds).toFixed(2)}-{numberValue(shot.end_seconds).toFixed(2)}s
          </span>
        </button>
        <div className="flex flex-wrap items-center gap-1" role="toolbar" aria-label={`镜头 ${index + 1} 操作`}>
          <button type="button" className="icon-btn h-9 w-9" disabled={busy || locked || index === 0} aria-label="上移镜头" title="在当前来源片段内上移" onClick={() => onMove?.(-1)}>
            <ArrowUp size={14} aria-hidden="true" />
          </button>
          <button type="button" className="icon-btn h-9 w-9" disabled={busy || locked || index === total - 1} aria-label="下移镜头" title="在当前来源片段内下移" onClick={() => onMove?.(1)}>
            <ArrowDown size={14} aria-hidden="true" />
          </button>
          <button type="button" className="icon-btn h-9 w-9" disabled={busy || locked || index === total - 1} aria-label="与下一镜头合并" title="与下一镜头合并" onClick={onMerge}>
            <Merge size={14} aria-hidden="true" />
          </button>
          <button type="button" className={`icon-btn h-9 w-9 ${locked ? "border-warn/50 bg-warn/10 text-warn" : ""}`} disabled={busy} aria-label={locked ? "解锁镜头" : "锁定镜头"} aria-pressed={locked} title={locked ? "解锁后允许修改" : "锁定内容和顺序"} onClick={onToggleLock}>
            {locked ? <Lock size={14} aria-hidden="true" /> : <Unlock size={14} aria-hidden="true" />}
          </button>
          <button type="button" className="icon-btn h-9 w-9 text-fog hover:text-bad" disabled={busy || locked} aria-label={`删除镜头 ${index + 1}`} title="删除镜头" onClick={onDelete}>
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      </div>

      <fieldset disabled={busy || locked} className="mt-2 disabled:opacity-65">
        <legend className="sr-only">镜头 {index + 1} 内容</legend>
        <div className="grid grid-cols-2 gap-2">
          <label>
            <span className="label mb-1">开始（秒）</span>
            <input type="number" min="0" step="0.1" className="input min-h-11 px-2.5 py-2 text-xs" value={shot.start_seconds ?? 0} onChange={(event) => onUpdate?.({ start_seconds: numberValue(event.target.value) })} />
          </label>
          <label>
            <span className="label mb-1">结束（秒）</span>
            <input type="number" min="0" step="0.1" className="input min-h-11 px-2.5 py-2 text-xs" value={shot.end_seconds ?? 0} onChange={(event) => onUpdate?.({ end_seconds: numberValue(event.target.value) })} />
          </label>
        </div>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {SHOT_FIELDS.map(([field, label]) => (
            <label key={field} className={field === "visual" ? "sm:col-span-2" : ""}>
              <span className="label mb-1">{label}</span>
              <textarea className="textarea min-h-16 resize-y border-line bg-base2/60 px-2.5 py-2 text-xs" value={String(shot[field] || "")} onChange={(event) => onUpdate?.({ [field]: event.target.value })} />
            </label>
          ))}
        </div>
      </fieldset>

      {nextShot && (
        <div className="mt-2 grid grid-cols-[1fr_auto] items-end gap-2 border-t border-line pt-2">
          <label className="min-w-0">
            <span className="label mb-1">与下一镜头边界 · {boundaryAt.toFixed(2)}s</span>
            <input
              type="range"
              min={numberValue(shot.start_seconds) + 0.05}
              max={numberValue(nextShot.end_seconds) - 0.05}
              step="0.05"
              className="h-10 w-full accent-aqua"
              value={boundaryAt}
              disabled={busy || !canMoveBoundary}
              onChange={(event) => setBoundaryAt(numberValue(event.target.value))}
            />
          </label>
          <button type="button" className="icon-btn h-10 w-10" disabled={busy || !canMoveBoundary || boundaryAt === numberValue(shot.end_seconds)} title="保存相邻镜头边界" aria-label="保存相邻镜头边界" onClick={() => onMoveBoundary?.(boundaryAt)}>
            <Save size={14} aria-hidden="true" />
          </button>
        </div>
      )}

      <div className="mt-2 flex flex-col gap-2 border-t border-line pt-2 sm:flex-row sm:items-end">
        <label className="min-w-0 flex-1">
          <span className="label mb-1">拆分时间</span>
          <input type="number" min={numberValue(shot.start_seconds)} max={numberValue(shot.end_seconds)} step="0.1" className="input min-h-10 px-2.5 py-1.5 text-xs" value={splitAt} disabled={busy || locked} onChange={(event) => setSplitAt(numberValue(event.target.value))} />
        </label>
        <button type="button" className="btn-secondary btn-sm min-h-10 justify-center" disabled={busy || locked} onClick={() => onSplit?.(splitAt)}>
          <Scissors size={14} aria-hidden="true" /> 拆分
        </button>
        <button type="button" className="btn-secondary btn-sm min-h-10 justify-center" disabled={busy || locked} title={`使用提示词模型编译到 ${targetModelName}`} onClick={onCompile}>
          <WandSparkles size={14} aria-hidden="true" /> {compiledPrompt ? "重新编译" : "编译镜头"}
        </button>
        <button type="button" className="icon-btn h-10 w-10 shrink-0" disabled={busy || locked || !shot.shot_id} title={shot.shot_id ? "仅重新分析当前镜头" : "旧结果缺少稳定 shot_id"} aria-label="重新分析当前镜头" onClick={onReanalyze}>
          <RefreshCw size={14} aria-hidden="true" />
        </button>
        <button type="button" className="icon-btn h-10 w-10 shrink-0" disabled={busy || !shot.shot_id || Boolean(generationDisabledReason)} title={generationDisabledReason || (shot.shot_id ? "生成当前镜头" : "旧结果缺少稳定 shot_id")} aria-label="生成当前镜头" onClick={onGenerate}>
          <Play size={14} aria-hidden="true" />
        </button>
        <button type="button" className="btn-primary btn-sm min-h-10 justify-center" disabled={busy} title="应用到视频工作区后可直接发起生成" onClick={onApply}>
          <Import size={14} aria-hidden="true" /> 应用镜头
        </button>
      </div>

      {actionStatus && <p className="mt-1 text-[10px] text-aqua" role="status">{actionStatus}</p>}

      <p className="mt-2 text-[10px] text-fog">
        证据帧：{Array.isArray(shot.evidence_frame_indices) && shot.evidence_frame_indices.length ? shot.evidence_frame_indices.join("、") : "无"}
        {Number.isFinite(Number(shot.confidence)) ? ` · 置信度 ${Math.round(Number(shot.confidence) * 100)}%` : ""}
        {locked ? " · 已锁定" : ""}
      </p>
      {compiledPrompt && (
        <div className="mt-2 border-l-2 border-aqua/55 bg-aqua/[0.06] px-3 py-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-fog">
            <span>模型编译稿 · {compilation.target_model_name || targetModelName}</span>
            {compilation.charged_credits ? <span>{compilation.charged_credits} 积分</span> : null}
          </div>
          <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-snow">{compiledPrompt}</p>
          {Array.isArray(compilation.warnings) && compilation.warnings.length > 0 && (
            <p className="mt-1 text-[10px] text-warn">{compilation.warnings.join("；")}</p>
          )}
        </div>
      )}
    </li>
  );
}
