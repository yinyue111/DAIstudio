"use client";

import { Eye, History, RefreshCw, RotateCw, Save } from "lucide-react";
import AssetMedia from "../../components/AssetMedia";

const STATUS_LABELS = {
  queued: "排队中",
  running: "分析中",
  needs_confirmation: "待确认",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
};

function operationAsset(operation) {
  const context = operation?.request_context || {};
  const sources = Array.isArray(context.sources) ? context.sources : [];
  const primary = sources.find((source) => source.role === "primary") || sources[0];
  const snapshot = operation?.workspace_snapshot_v3 || operation?.workspace_snapshot_v2 || {};
  const selected = snapshot.selected || {};
  const type = primary?.source_type || operation?.source_type || selected.type || "image";
  return {
    type,
    url: selected.url || primary?.asset_url || context.asset_url || "",
    thumb: selected.thumb || context.fallback_image || "",
    preview_url: selected.preview_url || selected.thumb || context.fallback_image || "",
    available: selected.available,
    expired: selected.expired,
  };
}

export default function StudioRecentReversePanel({
  operations = [],
  loading = false,
  error = "",
  busyOperationId = null,
  onRefresh,
  onOpen,
  onRetry,
  onSaveRecipe,
  recipesEnabled = true,
}) {
  return (
    <section className="mt-3 border-t border-line pt-3" aria-labelledby="recent-reverse-title">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <History size={15} className="text-fog" aria-hidden="true" />
          <h3 id="recent-reverse-title" className="text-xs font-display font-medium text-mist">最近反推</h3>
        </div>
        <button type="button" className="icon-btn h-10 w-10" aria-label="刷新最近反推" onClick={onRefresh} disabled={loading}>
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} aria-hidden="true" />
        </button>
      </div>

      {error && <p className="mt-2 text-xs text-bad" role="alert">{error}</p>}
      {loading && operations.length === 0 && (
        <div className="mt-2 space-y-2" aria-busy="true" aria-label="正在加载最近反推">
          {[0, 1, 2].map((key) => <div key={key} className="h-14 animate-pulse rounded-lg bg-white/[0.04]" />)}
        </div>
      )}
      {!loading && !error && operations.length === 0 && (
        <p className="mt-2 rounded-lg border border-dashed border-line px-3 py-5 text-center text-xs text-fog" role="status">
          完成反推后会在这里保留可恢复记录。
        </p>
      )}

      {operations.length > 0 && (
        <ol className="mt-2 divide-y divide-line" aria-label="最近反推任务">
          {operations.map((operation) => {
            const asset = operationAsset(operation);
            const busy = String(busyOperationId || "") === String(operation.id);
            const expired = Boolean(operation.expired || operation.error_code === "RESULT_EXPIRED");
            return (
              <li key={operation.id} className="flex min-w-0 items-center gap-2 py-2">
                <div className="h-12 w-12 flex-none overflow-hidden rounded-lg border border-line bg-black/25">
                  <AssetMedia asset={asset} className="h-full w-full object-cover" fallbackClassName="h-full w-full" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-display font-medium text-mist">
                    {operation.target === "video" ? "视频" : "图片"} · {operation.analysis_focus || "comprehensive"}
                  </p>
                  <p className="mt-0.5 truncate text-[10px] text-fog">
                    {expired ? "结果已过期" : (STATUS_LABELS[operation.status] || operation.status)}
                    {operation.model_name ? ` · ${operation.model_name}` : ""}
                    {operation.cost_settled ? ` · ${operation.cost_settled} 积分` : ""}
                  </p>
                </div>
                <div className="flex flex-none items-center gap-1">
                  <button type="button" className="icon-btn h-10 w-10" aria-label="查看反推结果" disabled={expired || operation.status !== "succeeded"} onClick={() => onOpen?.(operation)}>
                    <Eye size={15} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-btn h-10 w-10" aria-label="再次反推" disabled={busy || expired || !["succeeded", "failed", "canceled"].includes(operation.status)} onClick={() => onRetry?.(operation)}>
                    <RotateCw size={15} className={busy ? "animate-spin" : ""} aria-hidden="true" />
                  </button>
                  {recipesEnabled && (
                    <button type="button" className="icon-btn h-10 w-10" aria-label="保存为创作配方" disabled={busy || expired || operation.status !== "succeeded"} onClick={() => onSaveRecipe?.(operation)}>
                      <Save size={15} aria-hidden="true" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
