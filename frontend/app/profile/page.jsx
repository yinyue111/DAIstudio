"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, clearToken, downloadBlob, getToken } from "../../lib/api";
import Nav from "../../components/Nav";
import AssetMedia, { assetPreviewSrc } from "../../components/AssetMedia";

const PAGE = 30;
function srcOf(a) {
  return assetPreviewSrc(a);
}

function unlockConfirm(asset, me, cfg) {
  const type = asset.type === "video" ? "视频" : "图片";
  const balance = Number(me?.balance_credits ?? 0);
  const cost = Number(asset.unlock_cost ?? cfg?.models?.[asset.type]?.unlock_cost ?? 0);
  return window.confirm(`解锁${type}高清将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`);
}

export default function ProfilePage() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");
  const [assets, setAssets] = useState(null);
  const [loading, setLoading] = useState(false);
  const reqRef = useRef(0);
  const [hasMore, setHasMore] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());
  const busyAssetIdsRef = useRef(new Set());

  // change password
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState("");

  useEffect(() => {
    if (!getToken()) return router.push("/login");
    api.me().then(setMe).catch(() => router.push("/login"));
    api.config().then(setCfg).catch(() => {});
    api.profile().then(setData).catch((e) => setMsg(e.message));
  }, []);

  useEffect(() => {
    loadAssets(true);
  }, [filter]);

  function paramsFor(f) {
    if (f === "fav") return { favorite: true };
    if (f === "image" || f === "video") return { type: f };
    return {};
  }

  async function loadAssets(reset) {
    if (loading && !reset) return;  // block double load-more; a filter reset may interrupt
    setLoading(true);
    setMsg("");
    if (reset) {
      setAssets(null);
      setHasMore(false);
    }
    const myReq = ++reqRef.current;  // newest request wins; stale pages are dropped
    const off = reset ? 0 : (assets?.length || 0);
    try {
      const list = await api.profileAssets({ ...paramsFor(filter), limit: PAGE, offset: off });
      if (myReq !== reqRef.current) return;  // superseded (filter switch / refresh)
      setAssets(reset ? list : [...(assets || []), ...list]);
      setHasMore(list.length === PAGE);
    } catch (e) {
      if (myReq === reqRef.current) setMsg(e.message);
    } finally {
      if (myReq === reqRef.current) setLoading(false);
    }
  }

  function patchAsset(updated) {
    setAssets((prev) => {
      const list = prev || [];
      if (filter === "fav" && updated.favorite === false) {
        return list.filter((x) => x.id !== updated.id);
      }
      return list.map((x) => (x.id === updated.id ? updated : x));
    });
    if (lightbox && lightbox.id === updated.id) setLightbox(updated);
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
    if (!unlockConfirm(asset, me, cfg)) return;
    await withAssetBusy(asset.id, async () => {
      try {
        patchAsset(await api.unlock(asset.id));
        api.me().then(setMe).catch(() => {});
        api.profile().then(setData).catch(() => {});
      } catch (e) { setMsg(e.message); }
    });
  }

  async function toggleFav(asset) {
    await withAssetBusy(asset.id, async () => {
      try { patchAsset(await api.favoriteAsset(asset.id)); }
      catch (e) { setMsg(e.message); }
    });
  }

  async function del(asset) {
    const label = asset.type === "video" ? "视频" : "图片";
    if (!window.confirm(`确认删除这条${label}素材？删除后不可恢复。`)) return;
    await withAssetBusy(asset.id, async () => {
      try {
        await api.deleteAsset(asset.id);
        setAssets((prev) => (prev || []).filter((x) => x.id !== asset.id));
        if (lightbox && lightbox.id === asset.id) setLightbox(null);
        api.profile().then(setData).catch(() => {});
      } catch (e) { setMsg(e.message); }
    });
  }

  async function download(asset) {
    try {
      await downloadBlob(`/api/assets/${asset.id}/download`, `asset-${asset.id}`);
    } catch (e) { setMsg(e.message); }
  }

  async function changePassword() {
    setPwMsg("");
    if (newPw.length < 6) return setPwMsg("新密码至少 6 位");
    try {
      await api.changePassword(oldPw, newPw);
      clearToken();
      router.push("/login");
    } catch (e) { setPwMsg(e.message); }
  }

  const u = data?.user || me;
  const initial = (u?.nickname || u?.phone || "?").slice(-2);

  return (
    <div className="min-h-screen">
      <Nav me={me} active="profile" />
      <main className="mx-auto max-w-5xl px-4 pb-24 pt-10 sm:px-6">
        {/* header */}
        <div className="card mb-6 p-6 animate-fadeup sm:p-7">
          <div className="flex flex-wrap items-center gap-4">
            <span className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full bg-brand text-xl font-display font-semibold text-white shadow-glow-sm">
              {initial}
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h1 className="truncate text-2xl font-bold">{u?.nickname || u?.phone || "我的主页"}</h1>
                {u?.is_admin && <span className="badge border border-iris/30 bg-iris/15 text-iris-400">admin</span>}
              </div>
              <p className="mt-1 text-sm text-mist">{u?.phone}{u?.department ? ` · ${u.department}` : ""}</p>
            </div>
            <div className="ml-auto flex flex-col items-end gap-2">
              <div className="rounded-xl2 border border-iris/30 bg-iris/10 px-4 py-2 text-right">
                <div className="font-display text-2xl font-bold text-grad">{u?.balance_credits ?? "—"}</div>
                <div className="text-xs text-fog">可用额度{u?.frozen_credits ? ` · 冻结 ${u.frozen_credits}` : ""}</div>
              </div>
              <div className="flex gap-2">
                <a href="/recharge" className="btn-primary btn-sm">充值积分</a>
                <button onClick={() => setPwOpen(!pwOpen)} className="btn-ghost btn-sm">修改密码</button>
              </div>
            </div>
          </div>

          {pwOpen && (
            <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-5 animate-fadeup">
              <input type="password" className="input w-44" placeholder="原密码"
                value={oldPw} onChange={(e) => setOldPw(e.target.value)} />
              <input type="password" className="input w-44" placeholder="新密码(≥6 位)"
                value={newPw} onChange={(e) => setNewPw(e.target.value)} />
              <button onClick={changePassword} className="btn-primary btn-sm">确认修改</button>
              <span className="text-xs text-fog">修改后需重新登录</span>
              {pwMsg && <span className="text-sm text-bad">{pwMsg}</span>}
            </div>
          )}

          {data && (
            <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="图片" value={data.stats.images} />
              <Stat label="视频" value={data.stats.videos} />
              <Stat label="已解锁" value={data.stats.unlocked} />
              <Stat label="任务" value={data.stats.tasks} />
            </div>
          )}
          <p className="mt-5 text-xs text-fog">
            生成的素材默认保留 <b className="text-mist">{data?.retention_days ?? 30}</b> 天，过期后自动清理，请及时下载需要的素材。
          </p>
        </div>

        {/* filter */}
        <div className="mb-5 inline-flex gap-1 rounded-full border border-line bg-white/5 p-1">
          {[["all", "全部"], ["image", "图片"], ["video", "视频"], ["fav", "收藏"]].map(([k, label]) => (
            <button key={k} onClick={() => setFilter(k)}
              className={filter === k ? "chip chip-active" : "chip"}>
              {label}
            </button>
          ))}
        </div>

        {msg && (
          <div className="mb-5 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">{msg}</div>
        )}

        {assets === null ? (
          <div className="masonry">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="skeleton" style={{ height: 160 + (i % 4) * 60 }} />
            ))}
          </div>
        ) : assets.length === 0 ? (
          <div className="card flex flex-col items-center justify-center gap-3 p-16 text-center">
            <span className="text-3xl">🪄</span>
            <p className="text-sm text-mist">{filter === "fav" ? "还没有收藏的素材。" : "还没有生成的素材。"}</p>
            <a href="/" className="btn-primary btn-sm mt-1">去工作台生成</a>
          </div>
        ) : (
          <>
            <div className="masonry">
              {assets.map((a) => {
                const src = srcOf(a);
                const busy = busyAssetIds.has(a.id);
                return (
                  <div key={a.id} className="group overflow-hidden rounded-xl2 border border-line bg-base2">
                    <button onClick={() => setLightbox(a)} className="relative block w-full cursor-zoom-in bg-black/20" style={mediaAspectStyle(a)}>
                      {!src ? (
                        <div className="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
                          预览暂不可用
                        </div>
                      ) : (
                        <AssetMedia
                          asset={a}
                          className="h-full w-full object-contain transition duration-300 group-hover:scale-[1.04]"
                          fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
                        />
                      )}
                      <span className="badge absolute left-2 top-2 bg-black/60 text-white">
                        {a.type === "video" ? "视频" : "图片"}
                      </span>
                      {a.unlocked && <span className="badge absolute right-2 top-2 bg-brand text-white">HD</span>}
                      {a.days_left != null && (
                        <span className="badge absolute bottom-2 left-2 bg-black/60 text-fog">{a.days_left} 天后过期</span>
                      )}
                    </button>
                    <div className="flex items-center justify-between gap-1 p-2">
                      <div className="flex items-center gap-2">
                        <button onClick={() => toggleFav(a)} disabled={busy} title="收藏"
                          className={`text-base leading-none transition disabled:opacity-50 ${a.favorite ? "text-rose" : "text-fog hover:text-snow"}`}>
                          {a.favorite ? "★" : "☆"}
                        </button>
                        <button onClick={() => del(a)} disabled={busy} title="删除"
                          className="text-fog transition hover:text-bad disabled:opacity-50">🗑</button>
                      </div>
                      {a.unlocked ? (
                        <button onClick={() => download(a)} disabled={busy} className="btn-primary btn-sm">下载</button>
                      ) : (
                        <button onClick={() => unlock(a)} disabled={busy} className="btn-secondary btn-sm">解锁</button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
            {hasMore && (
              <div className="mt-6 text-center">
                <button onClick={() => loadAssets(false)} disabled={loading} className="btn-secondary">
                  {loading ? "加载中…" : "加载更多"}
                </button>
              </div>
            )}
          </>
        )}
      </main>

      {lightbox && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" onClick={() => setLightbox(null)}>
          <div className="panel max-h-[92vh] max-w-3xl overflow-auto p-3" onClick={(e) => e.stopPropagation()}>
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
                {lightbox.days_left != null ? ` · ${lightbox.days_left} 天后过期` : ""}
              </span>
              <div className="flex gap-2">
                <button onClick={() => toggleFav(lightbox)} disabled={busyAssetIds.has(lightbox.id)} className="btn-ghost btn-sm">
                  {lightbox.favorite ? "★ 已收藏" : "☆ 收藏"}
                </button>
                {!lightbox.unlocked && <button onClick={() => unlock(lightbox)} disabled={busyAssetIds.has(lightbox.id)} className="btn-primary btn-sm">解锁高清</button>}
                {lightbox.unlocked && <button onClick={() => download(lightbox)} disabled={busyAssetIds.has(lightbox.id)} className="btn-primary btn-sm">下载</button>}
                <button onClick={() => setLightbox(null)} className="btn-secondary btn-sm">关闭</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl2 border border-line bg-white/5 px-3 py-3 text-center transition hover:border-line2">
      <div className="font-display text-xl font-bold text-snow">{value}</div>
      <div className="mt-0.5 text-xs text-fog">{label}</div>
    </div>
  );
}

function mediaAspectStyle(a) {
  const width = Number(a?.width);
  const height = Number(a?.height);
  if (!width || !height) return { aspectRatio: "1 / 1" };
  return { aspectRatio: `${width} / ${height}` };
}
