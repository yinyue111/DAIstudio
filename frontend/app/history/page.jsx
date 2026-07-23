"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { ListChecks, RefreshCw, Wifi, WifiOff } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import Nav from "../../components/Nav";
import TaskDetailDrawer from "../../components/TaskDetailDrawer";
import { UnifiedTaskFilters, UnifiedTaskList } from "../../components/GlobalTaskCenter";
import { useToast } from "../../components/ToastProvider";
import useUnifiedTaskCenter from "../../hooks/useUnifiedTaskCenter";
import { api } from "../../lib/api";
import { redirectOnAuthError } from "../../lib/errorHandling";
import { saveStudioUserDraft } from "../../lib/studioSession";
import { STUDIO_VARIATION_DRAFT_KEY } from "../studio/constants";
import { assetVariationSourceUrl } from "../studio/assetActions";

const COUNT_FILTERS = [
  ["all", "全部"],
  ["active", "处理中"],
  ["needs_attention", "待确认"],
  ["succeeded", "已完成"],
  ["failed", "失败"],
  ["canceled", "已取消"],
];

function HistoryPageContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [sessionError, setSessionError] = useState("");
  const center = useUnifiedTaskCenter({ enabled: Boolean(me), limit: 30, live: true });
  const selectedTaskKey = searchParams.get("task") || "";
  const selectedTask = center.items.find((task) => task.key === selectedTaskKey) || null;

  const loadMe = useCallback(async () => {
    try {
      setSessionError("");
      setMe(await api.me());
    } catch (error) {
      if (!redirectOnAuthError(error, router, setSessionError, "history session probe")) {
        const message = error?.message || "任务记录加载失败";
        setSessionError(message);
        notify.error(message);
      }
    }
  }, [notify, router]);

  useEffect(() => { loadMe(); }, [loadMe]);

  const createVariation = useCallback(async (task) => {
    if (task.kind !== "generation" || task.category !== "image") return false;
    try {
      const detail = await api.task(task.id);
      const asset = (detail.assets || []).find((item) => item.type === "image");
      const sourceUrl = assetVariationSourceUrl(asset);
      if (!sourceUrl) throw new Error("当前图片暂不可作为变体来源");
      const saved = saveStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, me?.id, {
        asset: {
          ...asset,
          type: "image",
          url: sourceUrl,
          thumb: asset.preview_url || asset.thumb || sourceUrl,
        },
        prompt: "基于这张图生成同主体、同构图、同光线和同广告质感的近似变体；保留主体结构、产品文字、Logo、比例和核心视觉，只做轻微差异化。",
      });
      if (!saved) throw new Error("无法保存当前用户的变体草稿");
      router.push("/");
      return true;
    } catch (error) {
      notify.error(error?.message || "创建变体草稿失败");
      return true;
    }
  }, [me?.id, notify, router]);

  const countRows = useMemo(() => COUNT_FILTERS.map(([status, label]) => ({
    status,
    label,
    count: Number(center.counts[status] || 0),
  })), [center.counts]);

  const closeTaskDetail = useCallback(() => {
    const next = new URLSearchParams(searchParams.toString());
    next.delete("task");
    router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  }, [pathname, router, searchParams]);

  return (
    <div className="min-h-screen">
      <Nav me={me} active="history" />
      <main className="mx-auto max-w-5xl px-4 pb-24 pt-7 sm:px-6">
        <header className="flex flex-col gap-4 border-b border-line pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex items-center gap-2 text-aqua"><ListChecks size={17} aria-hidden="true" /><span className="text-xs font-medium">统一任务中心</span></div>
            <h1 className="mt-1 text-2xl font-display font-semibold text-snow">任务记录</h1>
            <p className="mt-1 text-sm text-mist">生成、反推与链接解析任务统一展示。</p>
          </div>
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1 text-xs text-fog" title={center.transport === "events" ? "实时事件已连接" : "正在通过轮询同步"}>
              {center.transport === "events" ? <Wifi size={14} aria-hidden="true" /> : <WifiOff size={14} aria-hidden="true" />}
              {center.transport === "events" ? "实时同步" : "自动同步"}
            </span>
            <button type="button" className="btn-secondary btn-sm" onClick={() => center.refresh()} disabled={center.loading}>
              <RefreshCw className={center.loading ? "animate-spin" : ""} size={14} aria-hidden="true" /> 刷新
            </button>
          </div>
        </header>

        <section aria-label="任务筛选" className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <UnifiedTaskFilters center={center} />
          <span className="text-xs text-fog">共 {center.total} 个任务</span>
        </section>

        <section aria-label="任务状态" className="mt-4 flex gap-2 overflow-x-auto pb-1 no-scrollbar">
          {countRows.map(({ status, label, count }) => (
            <button
              type="button"
              key={status}
              onClick={() => center.setFilters({ status })}
              className={`chip shrink-0 ${center.filters.status === status ? "chip-active" : ""}`}
              aria-pressed={center.filters.status === status}
            >
              {label} <span className="tabular-nums">{count}</span>
            </button>
          ))}
        </section>

        {sessionError && <p role="alert" className="mt-4 rounded-xl border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad">{sessionError}</p>}
        {center.error && center.items.length > 0 && <p role="status" className="mt-4 rounded-xl border border-warn/30 bg-warn/10 px-4 py-3 text-sm text-warn">{center.error}</p>}

        <section className="mt-5" aria-live="polite">
          <UnifiedTaskList center={center} variant="page" onCreateSimilar={createVariation} />
        </section>
      </main>
      <TaskDetailDrawer taskKey={selectedTaskKey} summary={selectedTask} onClose={closeTaskDetail} />
    </div>
  );
}

export default function HistoryPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-base" aria-label="任务记录加载中" />}>
      <HistoryPageContent />
    </Suspense>
  );
}
