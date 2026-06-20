"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, getToken } from "../../lib/api";
import Nav from "../../components/Nav";
import AssetMedia, { assetPreviewSrc } from "../../components/AssetMedia";

function srcOf(a) {
  return assetPreviewSrc(a);
}

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "needs_review"]);

function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

function unlockConfirm(asset, me, cfg) {
  const type = asset.type === "video" ? "视频" : "图片";
  const balance = Number(me?.balance_credits ?? 0);
  const cost = Number(asset.unlock_cost ?? cfg?.models?.[asset.type]?.unlock_cost ?? 0);
  return window.confirm(`解锁${type}高清将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`);
}

export default function HistoryPage() {
  const router = useRouter();
  const PAGE = 30;
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [tasks, setTasks] = useState(null);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const [finalizingId, setFinalizingId] = useState(null);
  const trackersRef = useRef(new Map());

  useEffect(() => {
    if (!getToken()) return router.push("/login");
    api.me().then(setMe).catch(() => router.push("/login"));
    api.config().then(setCfg).catch(() => {});
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
    if (loading) return;  // guard against double-click duplicate pages
    setLoading(true);
    const off = reset ? 0 : (tasks?.length || 0);  // derive offset from list, not stale state
    try {
      const list = await api.tasks(PAGE, off);
      const nextTasks = reset ? list : [...(tasks || []), ...list];
      setTasks(nextTasks);
      syncTaskTracking(nextTasks);
      setHasMore(list.length === PAGE);
    } catch (e) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function retry(taskId) {
    try {
      await api.retryTask(taskId);
      load(true);
      api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    }
  }

  function upsertTask(next) {
    setTasks((current) => {
      const list = current || [];
      const exists = list.some((t) => t.id === next.id);
      if (exists) return list.map((t) => (t.id === next.id ? next : t));
      return [next, ...list];
    });
  }

  function trackTask(taskId) {
    let stopped = false;
    let failures = 0;
    const tick = async () => {
      try {
        const task = await api.task(taskId);
        failures = 0;
        upsertTask(task);
        if (isTerminalStatus(task.status)) {
          const stop = trackersRef.current.get(task.id);
          if (stop) {
            stop();
            trackersRef.current.delete(task.id);
          }
          api.me().then(setMe).catch(() => {});
          return;
        }
      } catch (e) {
        failures += 1;
        if (failures >= 5) {
          setMsg(`连续获取任务状态失败: ${e.message}`);
          stopped = true;
          trackersRef.current.delete(taskId);
          return;
        }
      }
      if (!stopped) setTimeout(tick, 3000);
    };
    tick();
    return () => { stopped = true; };
  }

  async function renderFinal(taskId) {
    if (finalizingId) return;
    const cost = Number(cfg?.models?.video?.final_cost ?? cfg?.models?.video?.cost_credits ?? 0);
    const balance = Number(me?.balance_credits ?? 0);
    if (!window.confirm(`渲染完整视频将冻结 ${cost} 积分，当前余额 ${balance}，确认继续？`)) return;
    setMsg("");
    setFinalizingId(taskId);
    try {
      const task = await api.generate({
        category: "video",
        stage: "final",
        parent_task_id: taskId,
        params: {},
      });
      upsertTask(task);
      if (!isTerminalStatus(task.status) && !trackersRef.current.has(task.id)) {
        trackersRef.current.set(task.id, trackTask(task.id));
      }
      load(true);
      api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    } finally {
      setFinalizingId(null);
    }
  }

  async function unlock(asset) {
    if (!unlockConfirm(asset, me, cfg)) return;
    try {
      const updated = await api.unlock(asset.id);
      if (lightbox && lightbox.id === asset.id) setLightbox(updated);
      load(true);
      api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function download(asset) {
    try {
      await downloadBlob(`/api/assets/${asset.id}/download`, `asset-${asset.id}`);
    } catch (e) {
      setMsg(e.message);
    }
  }

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
              const finalStatus = t.final_status;
              const finalAssetCount = Number(t.final_asset_count || 0);
              const hasActiveOrSucceededFinal = ["queued", "running", "needs_review"].includes(finalStatus)
                || (finalStatus === "succeeded" && finalAssetCount > 0);
              const hasFailedFinal = finalStatus === "failed";
              return (
              <div key={t.id} className="card p-4 animate-fadeup">
                <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-display font-semibold text-snow">{taskSummaryLabel(t)}</span>
                  <span className="chip">{t.category === "video" ? "视频" : "图片"}</span>
                  {t.stage === "preview" && t.category === "video" && (
                    <span className="chip">预览</span>
                  )}
                  <span className={`badge ${statusStyle(t.status)}`}>{statusZh(t.status)}</span>
                  <span className="ml-auto text-xs text-fog">
                    {(t.created_at || "").replace("T", " ").slice(0, 16)} · 结算 {t.cost_settled}
                  </span>
                </div>
                {t.partial && (
                  <p className="mb-3 rounded-lg bg-warn/10 px-3 py-2 text-xs text-warn">
                    批量生成完成 {t.saved_count || t.assets?.length || 0}/{t.requested_count || "?"} 张，失败部分已退回积分。
                  </p>
                )}

                {t.assets?.length > 0 ? (
                  <div>
                    <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                      {t.assets.map((a) => (
                        <HistoryAssetButton key={a.id} asset={a} onOpen={() => setLightbox(a)} />
                      ))}
                    </div>
                    {t.category === "video" && t.stage === "preview" && t.status === "succeeded" && hasActiveOrSucceededFinal && (
                      <p className="mt-3 rounded-lg border border-line bg-white/5 px-3 py-2 text-center text-xs text-fog">
                        已提交完整渲染
                      </p>
                    )}
                    {t.category === "video" && t.stage === "preview" && t.status === "succeeded" && hasFailedFinal && !hasActiveOrSucceededFinal && (
                      <p className="mt-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-center text-xs text-warn">
                        完整渲染失败，可重新提交
                      </p>
                    )}
                    {t.category === "video" && t.stage === "preview" && t.status === "succeeded" && !hasActiveOrSucceededFinal && (
                      <button
                        onClick={() => renderFinal(t.id)}
                        disabled={finalizingId === t.id}
                        className="btn-primary btn-sm mt-3 w-full"
                      >
                        {finalizingId === t.id
                          ? "提交中…"
                          : `方向满意 → 渲染完整视频(${Number(cfg?.models?.video?.final_cost ?? cfg?.models?.video?.cost_credits ?? 0)}积分)`}
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs text-fog">
                      {t.status === "failed"
                        ? t.error || "生成失败"
                        : t.status === "needs_review"
                          ? t.error || "提交状态未知，等待管理员对账"
                          : "暂无结果素材"}
                    </p>
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
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          onClick={() => setLightbox(null)}
        >
          <div
            className="panel max-h-[92vh] max-w-3xl overflow-auto p-3"
            onClick={(e) => e.stopPropagation()}
          >
            {!srcOf(lightbox) ? (
              <div className="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog">
                {lightbox.unlocked ? "预览暂不可用，请稍后重试。" : "预览暂不可用，请先解锁后再下载高清。"}
              </div>
            ) : (
              <AssetMedia
                asset={lightbox}
                interactive
                controls
                autoPlay
                muted={false}
                className="mx-auto max-h-[76vh] w-auto rounded-xl2"
                fallbackClassName="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog"
                onError={(e) => setMsg(e?.message || "预览加载失败")}
              />
            )}
            <div className="mt-3 flex items-center justify-between gap-2 text-sm">
              <span className="text-fog">
                {lightbox.unlocked ? "预览 · 已解锁，可下载高清" : "预览 · 带水印"}
                {lightbox.width ? ` · ${lightbox.width}×${lightbox.height}` : ""}
              </span>
              <div className="flex gap-2">
                {!lightbox.unlocked && (
                  <button onClick={() => unlock(lightbox)} className="btn-primary btn-sm">解锁高清</button>
                )}
                {lightbox.unlocked && (
                  <button onClick={() => download(lightbox)} className="btn-primary btn-sm">下载</button>
                )}
                <button onClick={() => setLightbox(null)} className="btn-secondary btn-sm">关闭</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function HistoryAssetButton({ asset, onOpen }) {
  const src = srcOf(asset);
  return (
    <button
      onClick={onOpen}
      className="group relative overflow-hidden rounded-xl2 border border-line bg-base2 transition hover:border-line2"
      style={mediaAspectStyle(asset)}
    >
      {!src ? (
        <div className="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
          预览暂不可用
        </div>
      ) : (
        <AssetMedia
          asset={asset}
          className="h-full w-full cursor-zoom-in object-contain transition group-hover:scale-105"
          fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
        />
      )}
      {asset.unlocked && (
        <span className="badge absolute right-1 top-1 bg-brand text-white">HD</span>
      )}
    </button>
  );
}

function statusZh(s) {
  return { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败", needs_review: "待人工对账" }[s] || s;
}
function statusStyle(s) {
  return {
    queued: "bg-white/10 text-mist",
    running: "bg-aqua/15 text-aqua",
    succeeded: "bg-ok/15 text-ok",
    failed: "bg-bad/15 text-bad",
    needs_review: "bg-warn/15 text-warn",
  }[s] || "bg-white/10 text-mist";
}

function taskSummaryLabel(t) {
  if (t.category === "image") {
    const count = Number(t.saved_count ?? t.assets?.length ?? 0);
    const requested = Number(t.requested_count ?? count);
    if (t.partial) return `${count}/${requested} 张图片`;
    return `${count || requested || 1} 张图片`;
  }
  return t.stage === "final" ? "完整视频" : "视频预览";
}

function mediaAspectStyle(a) {
  const width = Number(a?.width);
  const height = Number(a?.height);
  if (!width || !height) return { aspectRatio: "1 / 1" };
  return { aspectRatio: `${width} / ${height}` };
}
