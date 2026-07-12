"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, clearToken, downloadBlob, loginPath } from "../../lib/api";
import { redirectOnAuthError, reportBackgroundError, showError } from "../../lib/errorHandling";
import { localDateRangeToProfileAssetParams } from "../../lib/datetime";
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
import GroupedAssetGallery from "../studio/GroupedAssetGallery";
import { assetVariationSourceUrl, confirmAssetUnlock } from "../studio/assetActions";
import { STUDIO_VARIATION_DRAFT_KEY } from "../studio/constants";

const PAGE = 30;
function srcOf(a) {
  return assetPreviewSrc(a);
}

export default function ProfilePage() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [data, setData] = useState(null);
  const [filter, setFilter] = useState("all");
  const [assetFilters, setAssetFilters] = useState({
    created_from: "",
    created_to: "",
    model_use: "",
    size: "",
  });
  const [assets, setAssets] = useState(null);
  const [loading, setLoading] = useState(false);
  const reqRef = useRef(0);
  const loadingRef = useRef(false);
  const assetsRef = useRef([]);
  const [hasMore, setHasMore] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());
  const busyAssetIdsRef = useRef(new Set());
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [batchDownloading, setBatchDownloading] = useState(false);

  // change password
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState("");

  useEffect(() => {
    api.me().then(setMe).catch((e) => redirectOnAuthError(e, router, setMsg, "profile session probe"));
    api.config().then(setCfg).catch((e) => showError(setMsg, e, "加载素材配置失败"));
    api.profile().then(setData).catch((e) => setMsg(e.message));
  }, []);

  useEffect(() => {
    loadAssets(true);
  }, [filter, assetFilters.created_from, assetFilters.created_to, assetFilters.model_use, assetFilters.size]);

  useEffect(() => {
    if (!assets) return;
    const visibleIds = new Set(assets.map((asset) => asset.id));
    setSelectedIds((prev) => {
      const next = new Set([...prev].filter((id) => visibleIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [assets]);

  function paramsFor(f) {
    const base = {
      ...assetFilters,
      ...localDateRangeToProfileAssetParams(assetFilters.created_from, assetFilters.created_to),
    };
    if (f === "fav") return { ...base, favorite: true };
    if (f === "image" || f === "video") return { ...base, type: f };
    return base;
  }

  async function loadAssets(reset) {
    if (loadingRef.current && !reset) return;  // block double load-more; a filter reset may interrupt
    loadingRef.current = true;
    setLoading(true);
    setMsg("");
    if (reset) {
      if (!assetsRef.current.length) setAssets(null);
      setHasMore(false);
      setSelectedIds(new Set());
    }
    const myReq = ++reqRef.current;  // newest request wins; stale pages are dropped
    const currentAssets = reset ? [] : (assetsRef.current || []);
    const off = reset ? 0 : currentAssets.length;
    try {
      const list = await api.profileAssets({ ...paramsFor(filter), limit: PAGE, offset: off });
      if (myReq !== reqRef.current) return;  // superseded (filter switch / refresh)
      const seen = new Set(currentAssets.map((asset) => asset.id));
      const merged = reset ? [] : [...currentAssets];
      for (const item of list) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          merged.push(item);
        }
      }
      assetsRef.current = merged;
      setAssets(merged);
      setHasMore(list.length === PAGE);
    } catch (e) {
      if (myReq === reqRef.current) {
        setMsg(e.message);
        notify.error(e.message || "作品加载失败，已保留当前列表");
      }
    } finally {
      if (myReq === reqRef.current) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  }

  function patchAsset(updated) {
    const removedFromCurrentView = filter === "fav" && updated.favorite === false;
    setAssets((prev) => {
      const list = prev || [];
      if (removedFromCurrentView) {
        const next = list.filter((x) => x.id !== updated.id);
        assetsRef.current = next;
        return next;
      }
      const next = list.map((x) => (x.id === updated.id ? updated : x));
      assetsRef.current = next;
      return next;
    });
    if (removedFromCurrentView) {
      setSelectedIds((prev) => {
        if (!prev.has(updated.id)) return prev;
        const next = new Set(prev);
        next.delete(updated.id);
        return next;
      });
    }
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
    if (isAssetTakenDown(asset)) {
      setMsg("素材已下架，不能继续解锁。");
      notify.warn("素材已下架，不能继续解锁。");
      return;
    }
    if (!confirmAssetUnlock(asset, me, cfg)) return;
    await withAssetBusy(asset.id, async () => {
      try {
        patchAsset(await api.unlock(asset.id));
        notify.success("素材已解锁。");
        api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after unlock"));
        api.profile().then(setData).catch((e) => reportBackgroundError(e, "refresh profile after unlock"));
      } catch (e) {
        setMsg(e.message);
        notify.error(e.message || "解锁失败");
      }
    });
  }

  async function toggleFav(asset) {
    if (isAssetTakenDown(asset)) {
      setMsg("素材已下架，不能继续收藏。");
      notify.warn("素材已下架，不能继续收藏。");
      return;
    }
    await withAssetBusy(asset.id, async () => {
      try {
        const updated = await api.favoriteAsset(asset.id);
        patchAsset(updated);
        notify.success(updated.favorite ? "已收藏。" : "已取消收藏。");
      } catch (e) {
        setMsg(e.message);
        notify.error(e.message || "收藏操作失败");
      }
    });
  }

  async function del(asset) {
    const label = asset.type === "video" ? "视频" : "图片";
    if (!window.confirm(`确认删除这条${label}素材？删除后不可恢复。`)) return;
    await withAssetBusy(asset.id, async () => {
      try {
        await api.deleteAsset(asset.id);
        setAssets((prev) => {
          const next = (prev || []).filter((x) => x.id !== asset.id);
          assetsRef.current = next;
          return next;
        });
        setSelectedIds((prev) => {
          if (!prev.has(asset.id)) return prev;
          const next = new Set(prev);
          next.delete(asset.id);
          return next;
        });
        if (lightbox && lightbox.id === asset.id) setLightbox(null);
        notify.success("素材已删除。");
        api.profile().then(setData).catch((e) => reportBackgroundError(e, "refresh profile after delete"));
      } catch (e) {
        setMsg(e.message);
        notify.error(e.message || "删除失败");
      }
    });
  }

  async function download(asset) {
    if (isAssetTakenDown(asset)) {
      setMsg("素材已下架，不能继续下载。");
      notify.warn("素材已下架，不能继续下载。");
      return;
    }
    if (!canDownloadAsset(asset)) {
      setMsg("请先解锁后再下载。");
      notify.warn("请先解锁后再下载。");
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
      notify.warn("当前图片暂不可作为变体来源，请确认预览可用后再试。");
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

  function toggleSelected(assetId) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  }

  async function batchDelete() {
    const visibleIds = new Set((assetsRef.current || []).map((asset) => asset.id));
    const ids = [...selectedIds].filter((id) => visibleIds.has(id));
    if (!ids.length) return;
    if (!window.confirm(`确认删除选中的 ${ids.length} 个素材？删除后不可恢复。`)) return;
    setMsg("");
    try {
      const res = await api.batchDeleteAssets(ids);
      const deleted = new Set(res.deleted || []);
      setAssets((prev) => {
        const next = (prev || []).filter((x) => !deleted.has(x.id));
        assetsRef.current = next;
        return next;
      });
      setSelectedIds(new Set());
      api.profile().then(setData).catch((e) => reportBackgroundError(e, "refresh profile after batch action"));
      const failed = res.failed?.length || 0;
      const text = failed ? `已删除 ${deleted.size} 个，${failed} 个删除失败。` : `已删除 ${deleted.size} 个素材。`;
      setMsg(text);
      notify.success(text);
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "批量删除失败");
    }
  }

  async function batchDownload() {
    const selectedAssets = (assetsRef.current || []).filter((asset) => selectedIds.has(asset.id));
    const downloadable = selectedAssets.filter(canDownloadAsset);
    const skipped = selectedAssets.length - downloadable.length;
    const ids = downloadable.map((asset) => asset.id);
    if (!selectedAssets.length || batchDownloading) return;
    if (!ids.length) {
      setMsg("选中的素材都未解锁或已下架，无法批量下载。");
      notify.warn("选中的素材都未解锁或已下架，无法批量下载。");
      return;
    }
    setMsg("");
    setBatchDownloading(true);
    try {
      const filename = await api.batchDownloadAssets(ids);
      const text = skipped ? `已开始下载 ${filename}，已跳过 ${skipped} 个未解锁或已下架素材。` : `已开始下载 ${filename}`;
      setMsg(text);
      notify.success(text);
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "批量下载失败");
    } finally {
      setBatchDownloading(false);
    }
  }

  async function changePassword() {
    setPwMsg("");
    if (newPw.length < 6) {
      notify.warn("新密码至少 6 位");
      return setPwMsg("新密码至少 6 位");
    }
    try {
      await api.changePassword(oldPw, newPw);
      clearToken();
      router.push(loginPath());
    } catch (e) { setPwMsg(e.message); }
  }

  const u = data?.user || me;
  const initial = (u?.nickname || u?.phone || "?").slice(-2);
  const visibleSelectedCount = (assets || []).filter((asset) => selectedIds.has(asset.id)).length;
  const compareSelection = selectedIds;
  const compareAssets = useMemo(
    () => (assets || []).filter((asset) => compareSelection.has(asset.id)).slice(0, 4),
    [assets, compareSelection],
  );

  function renderAssetCard(a) {
    const src = srcOf(a);
    const busy = busyAssetIds.has(a.id);
    const takenDown = isAssetTakenDown(a);
    return (
      <div className={`group relative flex h-full flex-col overflow-hidden rounded-xl2 border bg-base2 ${selectedIds.has(a.id) ? "border-brand" : "border-line"}`}>
        <label className="absolute right-2 top-2 z-10 flex h-7 w-7 cursor-pointer items-center justify-center rounded-full border border-white/20 bg-black/55">
          <input
            type="checkbox"
            checked={selectedIds.has(a.id)}
            onChange={() => toggleSelected(a.id)}
            className="h-3.5 w-3.5 accent-brand"
            aria-label={`选择素材 #${a.id}`}
          />
        </label>
        <button
          onClick={() => setLightbox(a)}
          aria-label={`预览${a.type === "video" ? "视频" : "图片"}素材 #${a.id}`}
          className="relative block aspect-[4/5] w-full cursor-zoom-in overflow-hidden bg-black/20"
        >
          {!src ? (
            <div className="absolute inset-0 flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
              {assetUnavailableText(a)}
            </div>
          ) : (
            <AssetMedia
              asset={a}
              className="absolute inset-0 h-full w-full object-contain transition duration-300 group-hover:scale-[1.04]"
              fallbackClassName="absolute inset-0 flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
            />
          )}
          <span className="badge absolute left-2 top-2 bg-black/60 text-white">
            {a.type === "video" ? "视频" : "图片"}
          </span>
          {takenDown && <span className="badge absolute right-2 bottom-2 bg-bad/80 text-white">已下架</span>}
          {a.days_left != null && (
            <span className="badge absolute bottom-2 left-2 bg-black/60 text-fog">{a.days_left} 天后过期</span>
          )}
        </button>
        <div className="mt-auto flex items-center justify-between gap-1 p-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => toggleFav(a)}
              disabled={busy || takenDown}
              title={takenDown ? "素材已下架" : "收藏"}
              aria-label={`${a.favorite ? "取消收藏" : "收藏"}素材 #${a.id}`}
              className={`text-base leading-none transition disabled:opacity-50 ${a.favorite ? "text-rose" : "text-fog hover:text-snow"}`}>
              {a.favorite ? "★" : "☆"}
            </button>
            <button
              onClick={() => del(a)}
              disabled={busy}
              title="删除"
              aria-label={`删除${a.type === "video" ? "视频" : "图片"}素材 #${a.id}`}
              className="text-fog transition hover:text-bad disabled:opacity-50">🗑</button>
          </div>
          {takenDown ? (
            <span className="btn-secondary btn-sm cursor-not-allowed opacity-60">已下架</span>
          ) : (
            <div className="flex gap-1.5">
              {a.type === "image" && (
                <button onClick={() => createVariation(a)} disabled={busy} className="btn-secondary btn-sm">
                  变体
                </button>
              )}
              {a.unlocked ? (
                <button
                  onClick={() => download(a)}
                  disabled={busy || !canDownloadAsset(a)}
                  className={canDownloadAsset(a) ? "btn-primary btn-sm" : "btn-secondary btn-sm cursor-not-allowed opacity-70"}
                >
                  {busy ? "处理中…" : "下载"}
                </button>
              ) : (
                <button onClick={() => unlock(a)} disabled={busy} className="btn-secondary btn-sm">解锁</button>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

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

        <div className="mb-5 grid gap-2 rounded-xl2 border border-line bg-white/[0.03] p-3 sm:grid-cols-[1fr_1fr_auto_auto_auto]">
          <input
            type="date"
            className="input px-3 py-2 text-xs"
            value={assetFilters.created_from}
            onChange={(e) => setAssetFilters((v) => ({ ...v, created_from: e.target.value }))}
            aria-label="开始日期"
          />
          <input
            type="date"
            className="input px-3 py-2 text-xs"
            value={assetFilters.created_to}
            onChange={(e) => setAssetFilters((v) => ({ ...v, created_to: e.target.value }))}
            aria-label="结束日期"
          />
          <select
            className="input px-3 py-2 text-xs"
            value={assetFilters.model_use}
            onChange={(e) => setAssetFilters((v) => ({ ...v, model_use: e.target.value }))}
            aria-label="模型类型"
          >
            <option value="">全部模型</option>
            <option value="image">图片模型</option>
            <option value="video">视频模型</option>
          </select>
          <select
            className="input px-3 py-2 text-xs"
            value={assetFilters.size}
            onChange={(e) => setAssetFilters((v) => ({ ...v, size: e.target.value }))}
            aria-label="画面比例"
          >
            <option value="">全部比例</option>
            <option value="portrait">竖图</option>
            <option value="landscape">横图</option>
            <option value="square">方图</option>
          </select>
          <button
            onClick={() => setAssetFilters({ created_from: "", created_to: "", model_use: "", size: "" })}
            className="btn-secondary btn-sm"
          >
            重置筛选
          </button>
        </div>

        {visibleSelectedCount > 0 && (
          <div className="mb-5 flex flex-wrap items-center justify-between gap-2 rounded-xl2 border border-brand/30 bg-brand/10 px-3 py-2">
            <span className="text-sm text-mist">已选择 {visibleSelectedCount} 个素材</span>
            <div className="flex flex-wrap gap-2">
              <button onClick={batchDownload} disabled={batchDownloading} className="btn-secondary btn-sm">
                {batchDownloading ? "打包中..." : "批量下载"}
              </button>
              <button onClick={batchDelete} className="btn-ghost btn-sm text-bad">批量删除</button>
              <button onClick={() => setSelectedIds(new Set())} className="btn-ghost btn-sm">取消选择</button>
            </div>
          </div>
        )}

        {msg && (
          <div className="mb-5 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">{msg}</div>
        )}

        <AssetComparePanel assets={compareAssets} onClose={() => setSelectedIds(new Set())} />

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
            <GroupedAssetGallery
              assets={assets}
              renderAsset={renderAssetCard}
              className="grid grid-cols-1 items-start gap-4 sm:grid-cols-2"
              groupClassName="rounded-xl2 border border-line bg-white/[0.025] p-2.5"
              assetGridClassName="grid grid-cols-1 gap-3"
            />
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
        <AssetPreviewDialog
          asset={lightbox}
          onClose={() => setLightbox(null)}
          onError={(e) => setMsg(e?.message || "预览加载失败")}
          meta={lightbox.days_left != null ? ` · ${lightbox.days_left} 天后过期` : ""}
        >
          {({ asset, takenDown, canDownload }) => (
            <>
              {!takenDown && (
                <button
                  type="button"
                  onClick={() => toggleFav(asset)}
                  disabled={busyAssetIds.has(asset.id)}
                  aria-pressed={Boolean(asset.favorite)}
                  className="btn-ghost btn-sm"
                >
                  {asset.favorite ? "★ 已收藏" : "☆ 收藏"}
                </button>
              )}
              {!takenDown && !asset.unlocked && (
                <button type="button" onClick={() => unlock(asset)} disabled={busyAssetIds.has(asset.id)} className="btn-primary btn-sm">
                  解锁
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

function Stat({ label, value }) {
  return (
    <div className="rounded-xl2 border border-line bg-white/5 px-3 py-3 text-center transition hover:border-line2">
      <div className="font-display text-xl font-bold text-snow">{value}</div>
      <div className="mt-0.5 text-xs text-fog">{label}</div>
    </div>
  );
}
