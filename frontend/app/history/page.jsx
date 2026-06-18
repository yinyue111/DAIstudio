"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, getToken } from "../../lib/api";
import Nav from "../../components/Nav";

function srcOf(a) {
  return a.preview_url || (a.unlocked ? a.hd_url || "" : "");
}

function unlockConfirm(asset, me, cfg) {
  const type = asset.type === "video" ? "视频" : "图片";
  const balance = Number(me?.balance_credits ?? 0);
  const cost = Number(cfg?.models?.[asset.type]?.unlock_cost || 0);
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

  useEffect(() => {
    if (!getToken()) return router.push("/login");
    api.me().then(setMe).catch(() => router.push("/login"));
    api.config().then(setCfg).catch(() => {});
    load(true);
  }, []);

  async function load(reset) {
    if (loading) return;  // guard against double-click duplicate pages
    setLoading(true);
    const off = reset ? 0 : (tasks?.length || 0);  // derive offset from list, not stale state
    try {
      const list = await api.tasks(PAGE, off);
      setTasks(reset ? list : [...(tasks || []), ...list]);
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

  async function renderFinal(taskId) {
    if (finalizingId) return;
    setMsg("");
    setFinalizingId(taskId);
    try {
      await api.generate({
        category: "video",
        stage: "final",
        parent_task_id: taskId,
        params: {},
      });
      await load(true);
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
            {tasks.map((t) => (
              <div key={t.id} className="card p-4 animate-fadeup">
                <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-display font-semibold text-snow">#{t.id}</span>
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
                    {t.category === "video" && t.stage === "preview" && t.status === "succeeded" && (
                      <button
                        onClick={() => renderFinal(t.id)}
                        disabled={finalizingId === t.id}
                        className="btn-primary btn-sm mt-3 w-full"
                      >
                        {finalizingId === t.id ? "提交中…" : "方向满意 → 渲染完整视频"}
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs text-fog">
                      {t.status === "failed" ? t.error || "生成失败" : "暂无结果素材"}
                    </p>
                    {t.status === "failed" && (
                      <button onClick={() => retry(t.id)} className="btn-secondary btn-sm">重试</button>
                    )}
                  </div>
                )}
              </div>
            ))}
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
            ) : lightbox.type === "video" ? (
              <video
                src={srcOf(lightbox)}
                controls
                autoPlay
                className="mx-auto max-h-[76vh] w-auto rounded-xl2"
              />
            ) : (
              <img
                src={srcOf(lightbox)}
                alt=""
                className="mx-auto max-h-[76vh] w-auto rounded-xl2"
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
      ) : asset.type === "video" ? (
        <video
          src={src}
          muted
          preload="metadata"
          playsInline
          className="h-full w-full cursor-zoom-in object-contain bg-black/20"
        />
      ) : (
        <img
          src={src}
          alt=""
          loading="lazy"
          className="h-full w-full cursor-zoom-in object-contain transition group-hover:scale-105"
        />
      )}
      {asset.unlocked && (
        <span className="badge absolute right-1 top-1 bg-brand text-white">HD</span>
      )}
    </button>
  );
}

function statusZh(s) {
  return { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "失败" }[s] || s;
}
function statusStyle(s) {
  return {
    queued: "bg-white/10 text-mist",
    running: "bg-aqua/15 text-aqua",
    succeeded: "bg-ok/15 text-ok",
    failed: "bg-bad/15 text-bad",
  }[s] || "bg-white/10 text-mist";
}

function mediaAspectStyle(a) {
  const width = Number(a?.width);
  const height = Number(a?.height);
  if (!width || !height) return { aspectRatio: "1 / 1" };
  return { aspectRatio: `${width} / ${height}` };
}
