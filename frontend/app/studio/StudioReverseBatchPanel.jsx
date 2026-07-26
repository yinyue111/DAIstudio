"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Ban,
  CheckSquare2,
  Eye,
  FolderPlus,
  LibraryBig,
  RefreshCw,
  RotateCcw,
  Settings2,
  Square,
} from "lucide-react";
import { ReferenceAssetPreview } from "./StudioMedia";
import { reversePrecisionOptions } from "./reverseConfig";
import { assetReferenceUrl, unifiedAssetKey } from "../../lib/unifiedAssets";
import {
  normalizeReverseBatchItemOverride,
  parseReverseBatchKeyframes,
  parseReverseBatchRanges,
  reverseBatchConfirmationState,
  reverseBatchOverrideCapability,
  reverseBatchOverrideCount,
  reverseBatchSucceededOperations,
} from "../../lib/reverseBatches";

const STATUS_LABELS = {
  queued: "等待中",
  running: "分析中",
  needs_confirmation: "需要你确认",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  canceled: "已取消",
};

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "等待中";
}

// 确认截止倒计时：待确认状态有 15 分钟 TTL，超时后端自动取消并退款，
// 所以剩余时间必须实时可见，而不是显示成静态的"等待中"。
function useConfirmationCountdown(expiresAt) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return undefined;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [expiresAt]);
  if (!expiresAt) return null;
  const deadline = Date.parse(expiresAt);
  if (!Number.isFinite(deadline)) return null;
  const remaining = Math.max(0, Math.floor((deadline - now) / 1000));
  const minutes = String(Math.floor(remaining / 60)).padStart(2, "0");
  const seconds = String(remaining % 60).padStart(2, "0");
  return { expired: remaining <= 0, text: `${minutes}:${seconds}` };
}

function assetKey(asset) {
  return unifiedAssetKey(asset) || assetReferenceUrl(asset);
}

function formatRanges(ranges) {
  return (ranges || []).map((item) => `${item.start_seconds}-${item.end_seconds}`).join(", ");
}

function formatKeyframes(values) {
  return (values || []).join(", ");
}

function BatchItemOverrideEditor({ asset, value, inherited, capability, disabled, onChange }) {
  const override = normalizeReverseBatchItemOverride(value);
  const count = reverseBatchOverrideCount(override);
  // 精度档位跟随 /api/config 下发的服务端枚举，避免下拉值与后端契约漂移。
  const precisionOptions = reversePrecisionOptions();
  const inheritedPrecision = inherited.analysis_precision || "standard";
  const inheritedPrecisionLabel = precisionOptions.find((option) => option.key === inheritedPrecision)?.label
    || inheritedPrecision;
  const effectiveTarget = override.target || inherited.target || asset.type;
  const video = asset.type === "video" && effectiveTarget === "video";
  const supports = (field) => capability.fields.includes(field);
  function patch(next) {
    onChange?.(normalizeReverseBatchItemOverride({ ...override, ...next }));
  }
  return (
    <div className="border-b border-line py-2 last:border-0">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="min-w-0 truncate text-[11px] text-mist">{asset.name || asset.title || assetReferenceUrl(asset)}</span>
        <span className={`shrink-0 text-[10px] ${count ? "text-aqua" : "text-fog"}`}>{count ? `已覆盖 ${count} 项` : "继承批次"}</span>
      </div>
      <div className="mt-1.5 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <label className="text-[10px] text-fog">
          <span className="mb-1 block">目标</span>
          <select className="select min-h-10 px-2 text-xs" disabled={disabled || !supports("target")} value={override.target || ""} onChange={(event) => patch({ target: event.target.value || undefined })}>
            <option value="">继承 {inherited.target || asset.type}</option>
            <option value="image" disabled={asset.type === "video"}>图片</option>
            <option value="video">视频</option>
          </select>
        </label>
        <label className="text-[10px] text-fog">
          <span className="mb-1 block">精度</span>
          <select className="select min-h-10 px-2 text-xs" disabled={disabled || !supports("analysis_precision")} value={override.analysis_precision || ""} onChange={(event) => patch({ analysis_precision: event.target.value || undefined })}>
            <option value="">继承 {inheritedPrecisionLabel}</option>
            {precisionOptions.map((option) => (
              <option key={option.key} value={option.key}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="text-[10px] text-fog">
          <span className="mb-1 block">分析片段</span>
          <input key={formatRanges(override.source_ranges)} className="input min-h-10 px-2 text-xs" disabled={disabled || !video || !supports("source_ranges")} defaultValue={formatRanges(override.source_ranges)} placeholder="0-5, 8-12" onBlur={(event) => patch({ source_ranges: parseReverseBatchRanges(event.target.value) })} />
        </label>
        <label className="text-[10px] text-fog">
          <span className="mb-1 block">关键帧</span>
          <input key={formatKeyframes(override.custom_keyframes)} className="input min-h-10 px-2 text-xs" disabled={disabled || !video || !supports("custom_keyframes")} defaultValue={formatKeyframes(override.custom_keyframes)} placeholder="1.5, 4, 9" onBlur={(event) => patch({ custom_keyframes: parseReverseBatchKeyframes(event.target.value) })} />
        </label>
      </div>
      {video && supports("include_audio") && (
        <label className="mt-1.5 flex min-h-9 items-center justify-between text-[10px] text-fog">
          <span>音频策略：{override.include_audio == null ? `继承（${inherited.include_audio ? "ASR" : "关闭"}）` : override.include_audio ? "覆盖为 ASR" : "覆盖为关闭"}</span>
          <select className="select min-h-9 w-32 px-2 text-xs" disabled={disabled} value={override.include_audio == null ? "inherit" : override.include_audio ? "on" : "off"} onChange={(event) => patch({ include_audio: event.target.value === "inherit" ? undefined : event.target.value === "on" })}>
            <option value="inherit">继承</option>
            {capability.audio_policies.includes("analyze") && <option value="on">启用 ASR</option>}
            {capability.audio_policies.includes("exclude") && <option value="off">关闭</option>}
          </select>
        </label>
      )}
    </div>
  );
}

function BatchSource({ asset, onRemove, disabled }) {
  return (
    <div className="relative aspect-square overflow-hidden rounded-lg border border-iris/40 bg-black/25">
      <ReferenceAssetPreview asset={asset} compact />
      <button
        type="button"
        onClick={() => onRemove?.(asset)}
        disabled={disabled}
        className="absolute right-1 top-1 flex h-7 w-7 items-center justify-center rounded-full border border-white/20 bg-black/75 text-white disabled:opacity-40"
        aria-label="移出批量反推"
        title="移出批量反推"
      >
        <Ban size={13} aria-hidden="true" />
      </button>
    </div>
  );
}

function BatchItem({ item, busy, onOpen, onRetry }) {
  const operation = item.operation;
  const status = operation?.status || "queued";
  const preview = item.source?.fallback_image || item.source?.asset_url || "";
  const prompt = String(operation?.result?.final_text || operation?.result?.prompt || "").trim();
  return (
    <li className="grid min-w-0 grid-cols-[52px_minmax(0,1fr)_auto] items-center gap-2 border-b border-line py-2 last:border-b-0">
      <div className="h-[52px] w-[52px] overflow-hidden rounded-lg border border-line bg-black/25">
        <ReferenceAssetPreview asset={{ type: item.source?.source_type || "image", url: preview, thumb: preview }} compact />
      </div>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2 text-xs">
          <span className="font-display font-medium text-mist">素材 {item.index + 1}</span>
          <span className={status === "failed" ? "text-bad" : status === "succeeded" ? "text-aqua" : status === "needs_confirmation" ? "font-medium text-warn" : "text-fog"}>
            {statusLabel(status)}
          </span>
          {operation && <span className="text-fog">{Math.round(Number(operation.progress || 0))}%</span>}
        </div>
        <p className="mt-1 truncate text-[11px] text-fog">
          {prompt || operation?.error || item.source?.asset_url || "等待服务端创建任务"}
        </p>
      </div>
      <div className="flex items-center gap-1">
        {operation?.status === "needs_confirmation" && (
          <button type="button" className="btn-primary btn-sm shrink-0" onClick={() => onOpen?.(operation)} disabled={busy} title="查看降级原因并确认" aria-label="去确认">
            去确认
          </button>
        )}
        {operation?.status === "succeeded" && (
          <button type="button" className="icon-btn h-9 w-9" onClick={() => onOpen?.(operation)} title="查看并应用" aria-label="查看并应用">
            <Eye size={15} aria-hidden="true" />
          </button>
        )}
        {["failed", "canceled"].includes(operation?.status) && (
          <button type="button" className="icon-btn h-9 w-9" onClick={() => onRetry?.(operation)} disabled={busy} title="重试该项" aria-label="重试该项">
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        )}
      </div>
    </li>
  );
}

export default function StudioReverseBatchPanel({
  assets = [],
  batch = null,
  recentBatches = [],
  busyAction = "",
  reverseEnabled = true,
  sharedConfig = {},
  batchCapabilities = null,
  onOpenAssetPicker,
  onRemoveAsset,
  onStart,
  onCancel,
  onOpenBatch,
  onOpenItem,
  onRetryItem,
  onSaveAll,
}) {
  const [compareOpen, setCompareOpen] = useState(false);
  const [overridesOpen, setOverridesOpen] = useState(false);
  const [itemOverrides, setItemOverrides] = useState({});
  const capability = useMemo(() => reverseBatchOverrideCapability({
    capabilities: batchCapabilities || batch?.capabilities || {},
  }), [batchCapabilities, batch?.capabilities]);
  const overrideCount = Object.values(itemOverrides).filter((item) => reverseBatchOverrideCount(item) > 0).length;
  useEffect(() => {
    const keys = new Set(assets.map(assetKey));
    setItemOverrides((current) => Object.fromEntries(Object.entries(current).filter(([key]) => keys.has(key))));
  }, [assets]);
  const successful = reverseBatchSucceededOperations(batch);
  const creating = busyAction === "create";
  const totalCost = Number(batch?.cost_settled || batch?.cost_frozen || 0);
  const confirmation = reverseBatchConfirmationState(batch);
  const countdown = useConfirmationCountdown(confirmation.expires_at);
  return (
    <section className="mt-3 border-t border-line pt-3" aria-labelledby="reverse-batch-title">
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 id="reverse-batch-title" className="text-xs font-display font-semibold text-snow">批量反推</h4>
          <p className="mt-0.5 text-[11px] text-fog">共享当前目标和精度，每个素材独立分析、计费和保存。</p>
        </div>
        <button type="button" className="btn-secondary btn-sm shrink-0" onClick={onOpenAssetPicker} disabled={creating || batch?.active}>
          <LibraryBig size={14} aria-hidden="true" /> 从资产选择
        </button>
      </div>

      {assets.length > 0 && (
        <div className="mt-2 grid grid-cols-5 gap-1.5">
          {assets.map((asset) => (
            <BatchSource key={unifiedAssetKey(asset) || assetReferenceUrl(asset)} asset={asset} onRemove={onRemoveAsset} disabled={creating || batch?.active} />
          ))}
        </div>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[11px] text-fog">已选 {assets.length}/20</span>
        <button type="button" className="btn-secondary btn-sm" disabled={!assets.length} onClick={() => setOverridesOpen((value) => !value)} title="逐项分析设置">
          <Settings2 size={14} aria-hidden="true" /> 逐项设置{overrideCount ? ` ${overrideCount}` : ""}
        </button>
        <button
          type="button"
          className="btn-primary btn-sm"
          disabled={!reverseEnabled || assets.length < 2 || creating || batch?.active}
          onClick={() => onStart?.(itemOverrides, capability)}
        >
          {creating ? <RefreshCw size={14} className="animate-spin" aria-hidden="true" /> : <CheckSquare2 size={14} aria-hidden="true" />}
          {creating ? "创建中" : "开始批量反推"}
        </button>
      </div>

      {overridesOpen && assets.length > 0 && (
        <div className="mt-2 border-y border-line px-1 py-2" aria-label="批量反推逐项覆盖">
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <p className={`text-[10px] ${capability.supported ? "text-fog" : "text-warn"}`}>
              {capability.supported ? "空值继承批次设置；仅提交明确覆盖项。" : capability.reason}
            </p>
            <div className="flex gap-1">
              <button type="button" className="btn-secondary btn-sm" disabled={!capability.supported || !assets.length} onClick={() => {
                const template = itemOverrides[assetKey(assets[0])] || {};
                setItemOverrides(Object.fromEntries(assets.map((asset) => [assetKey(asset), template])));
              }}>应用首项到全部</button>
              <button type="button" className="icon-btn h-9 w-9" disabled={!overrideCount} title="重置全部覆盖" aria-label="重置全部覆盖" onClick={() => setItemOverrides({})}><RotateCcw size={14} aria-hidden="true" /></button>
            </div>
          </div>
          <div className="mt-1">
            {assets.map((asset) => {
              const key = assetKey(asset);
              return <BatchItemOverrideEditor key={key} asset={asset} value={itemOverrides[key]} inherited={sharedConfig} capability={capability} disabled={!capability.supported || creating || batch?.active} onChange={(value) => setItemOverrides((current) => ({ ...current, [key]: value }))} />;
            })}
          </div>
        </div>
      )}

      {batch && (
        <div className="mt-3 rounded-xl2 border border-line bg-black/15 p-2.5">
          <div className="flex min-w-0 items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-xs font-display font-medium text-mist">{batch.name}</p>
              <p className="mt-0.5 text-[11px] text-fog">
                {statusLabel(batch.status)} · {batch.counts.succeeded}/{batch.counts.total} 完成
                {totalCost > 0 ? ` · ${totalCost} 积分` : ""}
              </p>
            </div>
            {batch.active && (
              <button type="button" className="btn-secondary btn-sm shrink-0" onClick={onCancel} disabled={busyAction === "cancel"}>
                <Square size={12} aria-hidden="true" /> 取消批次
              </button>
            )}
          </div>
          {confirmation.count > 0 && (
            <div className="mt-2 rounded-lg border border-warn/60 bg-warn/10 p-2 text-[11px] text-warn" role="alert">
              <p className="font-medium">
                {confirmation.count} 项需要你确认后才能继续
                {countdown ? (countdown.expired ? "（确认已超时，正在取消并退款）" : `（剩余 ${countdown.text}）`) : ""}
              </p>
              <p className="mt-0.5">视频帧提取失败，请点击"去确认"选择是否降级为封面分析；超时未确认将自动取消并全额退回积分。</p>
            </div>
          )}
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-black/30">
            <div
              className="h-full bg-aqua transition-[width]"
              style={{ width: `${batch.counts.total ? Math.max(2, (batch.counts.succeeded + batch.counts.failed + batch.counts.canceled) / batch.counts.total * 100) : 2}%` }}
            />
          </div>
          <ol className="mt-2">
            {batch.items.map((item) => (
              <BatchItem key={item.id || item.operation_id || item.index} item={item} busy={Boolean(busyAction)} onOpen={onOpenItem} onRetry={onRetryItem} />
            ))}
          </ol>
          {successful.length > 0 && (
            <div className="mt-2 flex flex-wrap justify-end gap-2 border-t border-line pt-2">
              <button type="button" className="btn-secondary btn-sm" onClick={() => setCompareOpen((value) => !value)}>
                <Eye size={14} aria-hidden="true" /> 结果对比
              </button>
              <button type="button" className="btn-primary btn-sm" onClick={() => onSaveAll?.(successful)} disabled={Boolean(busyAction)}>
                <FolderPlus size={14} aria-hidden="true" /> 批量保存配方
              </button>
            </div>
          )}
          {compareOpen && successful.length > 0 && (
            <div className="mt-2 grid gap-2 sm:grid-cols-2" aria-label="批量反推结果对比">
              {successful.map((operation, index) => (
                <button key={operation.id} type="button" onClick={() => onOpenItem?.(operation)} className="min-w-0 rounded-lg border border-line bg-white/[0.04] p-2 text-left hover:border-line2">
                  <span className="text-[11px] text-fog">结果 {index + 1}</span>
                  <p className="mt-1 line-clamp-4 text-xs leading-relaxed text-mist">{operation.result?.final_text || operation.result?.prompt || "查看结构化结果"}</p>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {recentBatches.length > 1 && (
        <div className="mt-2 flex min-w-0 gap-1.5 overflow-x-auto pb-1" aria-label="最近批量反推">
          {recentBatches.slice(0, 6).map((item) => (
            <button key={item.id} type="button" className={`chip shrink-0 ${batch?.id === item.id ? "chip-active" : ""}`} onClick={() => onOpenBatch?.(item.id)}>
              #{item.id} · {statusLabel(item.status)}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
