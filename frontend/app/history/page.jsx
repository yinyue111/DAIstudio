"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob } from "../../lib/api";
import { formatLocalDateTime } from "../../lib/datetime";
import { redirectOnAuthError, reportBackgroundError, showError } from "../../lib/errorHandling";
import { saveStudioUserDraft } from "../../lib/studioSession";
import Nav from "../../components/Nav";
import { useToast } from "../../components/ToastProvider";
import AssetMedia, {
  assetPreviewSrc,
  assetUnavailableText,
  canDownloadAsset,
  isAssetTakenDown,
} from "../../components/AssetMedia";
import AssetPreviewDialog from "../../components/AssetPreviewDialog";
import AssetComparePanel from "../../components/AssetComparePanel";
import { assetVariationSourceUrl, confirmAssetUnlock } from "../studio/assetActions";
import { STUDIO_VARIATION_DRAFT_KEY } from "../studio/constants";
import { statusStyle, statusZh } from "../studio/helpers";

function srcOf(a) {
  return assetPreviewSrc(a);
}

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "needs_review", "canceled"]);
const TASK_POLL_INTERVAL_MS = 3000;
const TASK_POLL_MAX_BACKOFF_MS = 30000;

function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

function runningTaskHint(task) {
  if (task.status === "queued") return "任务已提交，正在等待 Worker 接手。";
  if (task.status !== "running") return "暂无结果素材";
  const n = Number(task.requested_count || 0);
  const sizeMatch = /^(\d+)x(\d+)$/.exec(String(task.params?.size || ""));
  const width = sizeMatch ? Number(sizeMatch[1]) : 0;
  const height = sizeMatch ? Number(sizeMatch[2]) : 0;
  const isHighRes = Math.max(width, height) > 2560 || width * height >= 3840 * 2160 * 0.9;
  if (task.category === "image") {
    return isHighRes && n > 1
      ? `正在等待图像网关响应，${n} 张高分辨率图可能需要数分钟；如果网关超时会自动失败并退回积分。`
      : "正在等待图像网关响应，完成后会自动出现在历史记录。";
  }
  return "正在等待视频网关响应，完成后会自动出现在历史记录。";
}

export default function HistoryPage() {
  const router = useRouter();
  const notify = useToast();
  const PAGE = 30;
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [tasks, setTasks] = useState(null);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const trackersRef = useRef(new Map());
  const loadingRef = useRef(false);
  const loadSeqRef = useRef(0);
  const tasksRef = useRef([]);
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());
  const busyAssetIdsRef = useRef(new Set());
  const [compareSelection, setCompareSelection] = useState(() => new Set());

  useEffect(() => {
    api.me().then(setMe).catch((e) => redirectOnAuthError(e, router, setMsg, "history session probe"));
    api.config().then(setCfg).catch((e) => showError(setMsg, e, "加载下载配置失败"));
    load(true);
    return () => {
      trackersRef.current.forEach((stop) => stop());
      trackersRef.current.clear();
    };
  }, []);

  function syncTaskTracking(list) {
    const nextIds = new Set((list || []).filter((t) => !isTerminalStatus(t.status)).map((t) => t.id));
    trackersRef.current.forEach((stop, id) => {
      if (!nextIds.has(id)) {
        stop();
        trackersRef.current.delete(id);
      }
    });
    nextIds.forEach((id) => {
      if (!trackersRef.current.has(id)) trackersRef.current.set(id, trackTask(id));
    });
  }

  async function load(reset) {
    if (loadingRef.current) return;  // guard against double-click duplicate pages
    loadingRef.current = true;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    const currentTasks = reset ? [] : (tasksRef.current || []);
    const off = reset ? 0 : currentTasks.length;
    try {
      const list = await api.tasks(PAGE, off);
      if (seq !== loadSeqRef.current) return;
      const seen = new Set(currentTasks.map((t) => t.id));
      const nextTasks = [...currentTasks];
      for (const item of list) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          nextTasks.push(item);
        }
      }
      tasksRef.current = nextTasks;
      setTasks(nextTasks);
      syncTaskTracking(nextTasks);
      setHasMore(list.length === PAGE);
    } catch (e) {
      if (seq === loadSeqRef.current) {
        setMsg(e.message);
        notify.error(e.message || "历史记录加载失败");
      }
    } finally {
      if (seq === loadSeqRef.current) loadingRef.current = false;
      setLoading(false);
    }
  }

  async function retry(taskId) {
    try {
      await api.retryTask(taskId);
      load(true);
      api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after retry"));
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "任务重试失败");
    }
  }

  async function cancelTask(task) {
    const running = task?.status === "running";
    const text = running
      ? "确认提交取消请求？运行中的任务会在安全阶段停止；如果外部网关已接收，可能无法中途取消。"
      : "确认取消这个排队任务？已冻结积分会退回。";
    if (!window.confirm(text)) return;
    setMsg("");
    try {
      const next = await api.cancelTask(task.id);
      upsertTask(next);
      api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after cancel"));
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "取消任务失败");
    }
  }

  function upsertTask(next) {
    setTasks((current) => {
      const list = current || [];
      const exists = list.some((t) => t.id === next.id);
      const updated = exists ? list.map((t) => (t.id === next.id ? next : t)) : [next, ...list];
      tasksRef.current = updated;
      return updated;
    });
  }

  function trackTask(taskId) {
    let stopped = false;
    let failures = 0;
    let failureReported = false;
    let timer = null;
    const tick = async () => {
      if (stopped) return;
      let nextDelay = TASK_POLL_INTERVAL_MS;
      try {
        const task = await api.task(taskId);
        if (stopped) return;
        failures = 0;
        failureReported = false;
        upsertTask(task);
        if (isTerminalStatus(task.status)) {
          const stop = trackersRef.current.get(task.id);
          if (stop) {
            stop();
            trackersRef.current.delete(task.id);
          }
          api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after tracked task"));
          return;
        }
      } catch (e) {
        if (stopped) return;
        failures += 1;
        nextDelay = Math.min(
          TASK_POLL_MAX_BACKOFF_MS,
          TASK_POLL_INTERVAL_MS * (2 ** Math.min(failures, 4)),
        );
        if (failures >= 5 && !failureReported) {
          failureReported = true;
          setMsg(`任务状态暂时无法同步，正在自动重试: ${e.message}`);
        }
      }
      if (!stopped) timer = setTimeout(tick, nextDelay);
    };
    tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }

  async function withAssetBusy(assetId, fn) {
    if (busyAssetIdsRef.current.has(assetId)) return;
    busyAssetIdsRef.current.add(assetId);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      await fn();
    } finally {
      busyAssetIdsRef.current.delete(assetId);
      setBusyAssetIds(new Set(busyAssetIdsRef.current));
    }
  }

  async function unlock(asset) {
    if (isAssetTakenDown(asset)) {
      setMsg("素材已下架，不能继续解锁。");
      return;
    }
    if (!confirmAssetUnlock(asset, me, cfg)) return;
    await withAssetBusy(asset.id, async () => {
      try {
        const updated = await api.unlock(asset.id);
        if (lightbox && lightbox.id === asset.id) setLightbox(updated);
        load(true);
        api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after unlock"));
      } catch (e) {
        setMsg(e.message);
        notify.error(e.message || "解锁失败");
      }
    });
  }

  async function download(asset) {
    if (isAssetTakenDown(asset)) {
      setMsg("素材已下架，不能继续下载。");
      return;
    }
    if (!canDownloadAsset(asset)) {
      setMsg("请先解锁后再下载。");
      return;
    }
    await withAssetBusy(asset.id, async () => {
      try {
        const filename = await downloadBlob(
          `/api/assets/${asset.id}/download`,
          asset.type === "video" ? `asset-${asset.id}.mp4` : undefined,
        );
        setMsg(`已开始下载 ${filename}`);
        notify.success(`已开始下载 ${filename}`);
      } catch (e) {
        setMsg(e.message);
        notify.error(e.message || "下载失败");
      }
    });
  }

  function createVariation(asset) {
    const sourceUrl = assetVariationSourceUrl(asset);
    if (!sourceUrl) {
      setMsg("当前图片暂不可作为变体来源，请确认预览可用后再试。");
      return;
    }
    try {
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
    } catch (e) {
      setMsg(e.message || "创建变体草稿失败");
      notify.error(e.message || "创建变体草稿失败");
    }
  }

  function toggleCompareAsset(asset) {
    setCompareSelection((prev) => {
      const next = new Set(prev);
      if (next.has(asset.id)) {
        next.delete(asset.id);
        return next;
      }
      if (next.size >= 4) {
        notify.warn("最多同时对比 4 个作品。");
        return prev;
      }
      next.add(asset.id);
      return next;
    });
  }

  const compareAssets = useMemo(() => {
    const byId = new Map();
    for (const task of tasks || []) {
      for (const asset of task.assets || []) byId.set(asset.id, asset);
    }
    return [...compareSelection].map((id) => byId.get(id)).filter(Boolean);
  }, [compareSelection, tasks]);

  return (
    <div className="min-h-screen">
      <Nav me={me} active="history" />
      <main className="mx-auto max-w-3xl px-4 pb-24 pt-10 sm:px-6">
        <div className="mb-6 flex items-end justify-between animate-fadeup">
          <div>
            <h1 className="text-3xl font-extrabold leading-tight">
              历史<span className="text-grad">记录</span>
            </h1>
            <p className="mt-1.5 text-sm text-mist">查看过往生成任务与结果素材。</p>
          </div>
          <button onClick={() => load(true)} className="btn-secondary btn-sm">刷新</button>
        </div>

        {msg && (
          <div className="mb-4 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">
            {msg}
          </div>
        )}

        <AssetComparePanel assets={compareAssets} onClose={() => setCompareSelection(new Set())} />

        {tasks === null ? (
          <div className="card p-8 text-center text-sm text-mist">加载中…</div>
        ) : tasks.length === 0 ? (
          <div className="card flex flex-col items-center justify-center gap-3 p-14 text-center">
            <span className="text-3xl">🗂️</span>
            <p className="text-sm text-mist">还没有生成记录。</p>
            <a href="/" className="btn-primary btn-sm">去工作台生成</a>
          </div>
        ) : (
          <div className="space-y-4">
            {tasks.map((t) => {
              const promptRecords = taskPromptRecords(t);
              return (
              <div key={t.id} className="card p-4 animate-fadeup">
                <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
	                  <span className="font-display font-semibold text-snow">{taskSummaryLabel(t)}</span>
	                  <span className="chip">{t.category === "video" ? "视频" : "图片"}</span>
	                  <span className={`badge ${statusStyle(t.status)}`}>{statusZh(t.status)}</span>
                  <span className="ml-auto text-xs text-fog">
                    {formatLocalDateTime(t.created_at)} · 结算 {t.cost_settled}
                  </span>
                </div>
                <div className="mb-3 flex min-w-0 flex-wrap items-start gap-x-4 gap-y-2 border-b border-line/70 pb-3 text-xs">
                  <p className="min-w-0 text-fog">
                    <span className="mr-2 text-mist">模型</span>
                    <span className="break-all text-snow">{taskModelLabel(t)}</span>
                  </p>
                  {promptRecords.map((record) => (
                    <details key={record.key} className="min-w-0 flex-1 basis-full sm:basis-auto">
                      <summary className="w-fit cursor-pointer select-none text-mist transition hover:text-snow">
                        查看{record.label}
                      </summary>
                      <p className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-black/20 px-3 py-2 leading-5 text-fog">
                        {record.text}
                      </p>
                    </details>
                  ))}
                </div>
                {t.partial && (
                  <div className="mb-3 rounded-lg bg-warn/10 px-3 py-2 text-xs text-warn">
                    {t.status === "needs_review" ? (
                      <p>批量生成已保存 {t.saved_count || t.assets?.length || 0}/{t.requested_count || "?"} 张成功结果，剩余部分状态未知，积分暂不结算或退回，待系统确认。</p>
                    ) : (
                      <p>批量生成完成 {t.saved_count || t.assets?.length || 0}/{t.requested_count || "?"} 张，失败部分已按实际成功张数结算。</p>
                    )}
                    {t.partial_errors?.length > 0 && (
                      <p className="mt-1 text-warn/80">未完成原因：{t.partial_errors.join("；")}</p>
                    )}
                  </div>
                )}

                {t.assets?.length > 0 ? (
                  <div>
                    <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                      {t.assets.map((a) => (
                        <HistoryAssetButton
                          key={a.id}
                          asset={a}
                          onOpen={() => setLightbox(a)}
                          onVariation={() => createVariation(a)}
                          onCompare={() => toggleCompareAsset(a)}
                          compareSelected={compareSelection.has(a.id)}
                        />
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col gap-2 rounded-lg border border-line bg-white/[0.03] px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                    <p className="text-xs text-fog">
                      {t.status === "failed"
                        ? t.error || "生成失败"
                        : t.status === "needs_review"
                          ? t.error || "提交状态未知，等待确认"
                          : runningTaskHint(t)}
                    </p>
                    {!isTerminalStatus(t.status) && (
                      <div className="flex gap-2">
                        <button onClick={() => load(true)} className="btn-secondary btn-sm">刷新状态</button>
                        {["queued", "running"].includes(t.status) && (
                          <button onClick={() => cancelTask(t)} className="btn-ghost btn-sm text-bad">
                            {t.status === "running" ? "提交取消请求" : "取消任务"}
                          </button>
                        )}
                      </div>
                    )}
                    {t.status === "failed" && (
                      <button onClick={() => retry(t.id)} className="btn-secondary btn-sm">重试</button>
                    )}
                  </div>
                )}
              </div>
              );
            })}
            {hasMore && (
              <div className="text-center">
                <button onClick={() => load(false)} disabled={loading} className="btn-secondary">
                  {loading ? "加载中…" : "加载更多"}
                </button>
              </div>
            )}
          </div>
        )}
      </main>

      {lightbox && (
        <AssetPreviewDialog
          asset={lightbox}
          onClose={() => setLightbox(null)}
          onError={(e) => setMsg(e?.message || "预览加载失败")}
          meta={lightbox.width ? ` · ${lightbox.width}×${lightbox.height}` : ""}
        >
          {({ asset, takenDown, canDownload }) => (
            <>
              {!takenDown && !asset.unlocked && (
                <button
                  type="button"
                  onClick={() => unlock(asset)}
                  disabled={busyAssetIds.has(asset.id)}
                  className="btn-primary btn-sm"
                >
                  {busyAssetIds.has(asset.id) ? "解锁中…" : "解锁"}
                </button>
              )}
              {!takenDown && asset.unlocked && (
                <button
                  type="button"
                  onClick={() => download(asset)}
                  disabled={busyAssetIds.has(asset.id) || !canDownload}
                  className={canDownload ? "btn-primary btn-sm" : "btn-secondary btn-sm cursor-not-allowed opacity-70"}
                >
                  下载
                </button>
              )}
              {!takenDown && asset.type === "image" && (
                <button type="button" onClick={() => createVariation(asset)} className="btn-secondary btn-sm">
                  生成变体
                </button>
              )}
            </>
          )}
        </AssetPreviewDialog>
      )}
    </div>
  );
}

function HistoryAssetButton({ asset, onOpen, onVariation, onCompare, compareSelected }) {
  const src = srcOf(asset);
  const takenDown = isAssetTakenDown(asset);
  return (
    <div
      className="group relative overflow-hidden rounded-xl2 border border-line bg-base2 transition hover:border-line2"
      style={mediaAspectStyle(asset)}
    >
      <button
        onClick={onOpen}
        aria-label={`预览${asset.type === "video" ? "视频" : "图片"}素材 #${asset.id}`}
        className="block h-full w-full"
      >
        {!src ? (
          <div className="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
            {assetUnavailableText(asset)}
          </div>
        ) : (
          <AssetMedia
            asset={asset}
            className="h-full w-full cursor-zoom-in object-contain transition group-hover:scale-105"
            fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
          />
        )}
      </button>
      {takenDown && (
        <span className="badge absolute left-1 top-1 bg-bad/80 text-white">已下架</span>
      )}
      {!takenDown && asset.type === "image" && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onVariation();
          }}
          className="absolute bottom-1.5 right-1.5 rounded-full border border-white/20 bg-black/65 px-2 py-1 text-[11px] font-medium text-white opacity-100 transition hover:bg-black/80 focus-visible:opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
        >
          变体
        </button>
      )}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onCompare();
        }}
        className={`absolute left-1.5 bottom-1.5 rounded-full border px-2 py-1 text-[11px] font-medium transition ${
          compareSelected
            ? "border-brand bg-brand text-white"
            : "border-white/20 bg-black/65 text-white hover:bg-black/80"
        }`}
      >
        {compareSelected ? "已选" : "对比"}
      </button>
    </div>
  );
}

function taskSummaryLabel(t) {
  if (t.category === "image") {
    const count = Number(t.saved_count ?? t.assets?.length ?? 0);
    const requested = Number(t.requested_count ?? count);
    if (t.partial) return `${count}/${requested} 张图片`;
    return `${count || requested || 1} 张图片`;
  }
  return "视频";
}

function taskModelLabel(task) {
  const model = String(task?.model_id || "").trim();
  const provider = String(task?.model_provider || "").trim();
  if (model && provider && provider.toLowerCase() !== model.toLowerCase()) return `${model} · ${provider}`;
  return model || provider || "未记录（历史任务）";
}

function taskPromptLabel(task) {
  if (task?.prompt_text_source === "generation") return "最终生成提示词";
  if (task?.prompt_text_source === "request") return "原始请求提示词";
  return "提示词记录";
}

function taskPromptRecords(task) {
  const requestPrompt = String(task?.request_prompt_text || "").trim();
  const generationPrompt = String(task?.generation_prompt_text || "").trim();
  const records = [];
  if (requestPrompt) {
    records.push({ key: "request", label: "原始请求提示词", text: requestPrompt });
  }
  if (generationPrompt) {
    records.push({ key: "generation", label: "最终生成提示词", text: generationPrompt });
  }
  if (records.length) return records;
  return [{
    key: "legacy",
    label: taskPromptLabel(task),
    text: task?.prompt_text || "该历史任务未保存可展示的提示词。",
  }];
}

function mediaAspectStyle(a) {
  const width = Number(a?.width);
  const height = Number(a?.height);
  if (!width || !height) return { aspectRatio: "1 / 1" };
  return { aspectRatio: `${width} / ${height}` };
}
