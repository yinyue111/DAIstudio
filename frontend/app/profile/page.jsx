"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, clearToken, downloadBlob, loginPath } from "../../lib/api";
import { formatLocalDateTime } from "../../lib/datetime";
import { redirectOnAuthError, reportBackgroundError } from "../../lib/errorHandling";
import { saveStudioUserDraft } from "../../lib/studioSession";
import {
  normalizeUnifiedAsset,
  normalizeUnifiedAssetPage,
  unifiedAssetKey,
} from "../../lib/unifiedAssets";
import Nav from "../../components/Nav";
import { useToast } from "../../components/ToastProvider";
import AssetMedia, {
  assetPreviewSrc,
  assetUnavailableText,
  canDownloadAsset,
} from "../../components/AssetMedia";
import AssetPreviewDialog from "../../components/AssetPreviewDialog";
import AssetComparePanel from "../../components/AssetComparePanel";
import { assetVariationSourceUrl } from "../studio/assetActions";
import { STUDIO_VARIATION_DRAFT_KEY } from "../studio/constants";

const PAGE_SIZE = 36;

function generatedAssetId(asset) {
  if (asset?.origin !== "generated") return null;
  const match = /^g\.(\d+)$/.exec(unifiedAssetKey(asset));
  return match ? Number(match[1]) : (Number(asset?.id) || null);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function formatDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)} 秒`;
  const roundedSeconds = Math.round(seconds);
  const minutes = Math.floor(roundedSeconds / 60);
  const remainder = roundedSeconds % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function assetMeta(asset) {
  const width = Number(asset?.width || 0);
  const height = Number(asset?.height || 0);
  const dimensions = width > 0 && height > 0 ? `${width}×${height}` : "";
  const duration = asset?.type === "video" ? formatDuration(asset.duration) : "";
  const createdAt = asset?.created_at ? formatLocalDateTime(asset.created_at) : "";
  return [dimensions, duration, createdAt].filter((value) => value && value !== "-").join(" · ");
}

function downloadFilename(asset) {
  if (asset?.filename) return asset.filename;
  const id = generatedAssetId(asset) || "media";
  return asset?.type === "video" ? `asset-${id}.mp4` : `asset-${id}.png`;
}

export default function ProfilePage() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [profile, setProfile] = useState(null);
  const [filters, setFilters] = useState({ origin: "all", type: "all", favorite: false, retention: "all" });
  const [assets, setAssets] = useState(null);
  const [stats, setStats] = useState(null);
  const [total, setTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const [busyRefs, setBusyRefs] = useState(() => new Set());
  const [selectedRefs, setSelectedRefs] = useState(() => new Set());
  const [uploading, setUploading] = useState("");
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState("");
  const requestRef = useRef(0);
  const assetsRef = useRef([]);
  const loadingRef = useRef(false);
  const imageInputRef = useRef(null);
  const videoInputRef = useRef(null);

  useEffect(() => {
    api.me().then(setMe).catch((error) => redirectOnAuthError(error, router, setMsg, "profile session probe"));
    api.profile().then(setProfile).catch((error) => reportBackgroundError(error, "load profile summary"));
  }, []);

  useEffect(() => {
    loadAssets(true);
  }, [filters.origin, filters.type, filters.favorite, filters.retention]);

  useEffect(() => {
    if (!assets) return;
    const visible = new Set(assets.map(unifiedAssetKey));
    setSelectedRefs((current) => new Set([...current].filter((ref) => visible.has(ref))));
  }, [assets]);

  async function loadAssets(reset = false) {
    if (loadingRef.current && !reset) return;
    loadingRef.current = true;
    setLoading(true);
    setMsg("");
    const requestId = ++requestRef.current;
    const current = reset ? [] : (assetsRef.current || []);
    if (reset && !current.length) setAssets(null);
    try {
      const payload = await api.meAssets({
        origin: filters.origin,
        type: filters.type,
        favorite: filters.favorite ? true : "",
        retention: filters.retention,
        limit: PAGE_SIZE,
        cursor: reset ? "" : nextCursor,
        offset: 0,
      });
      if (requestId !== requestRef.current) return;
      const page = normalizeUnifiedAssetPage(payload);
      const seen = new Set(current.map(unifiedAssetKey));
      const merged = reset ? [] : [...current];
      for (const asset of page.items) {
        const ref = unifiedAssetKey(asset);
        if (!seen.has(ref)) {
          seen.add(ref);
          merged.push(asset);
        }
      }
      assetsRef.current = merged;
      setAssets(merged);
      setStats(page.stats);
      setTotal(page.total);
      setNextCursor(page.nextCursor || null);
    } catch (error) {
      if (requestId === requestRef.current) {
        setMsg(error.message || "资产加载失败");
        notify.error(error.message || "资产加载失败，已保留当前列表");
      }
    } finally {
      if (requestId === requestRef.current) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  }

  function patchAssets(refs, patch) {
    const targets = new Set(refs);
    const apply = (list) => (list || []).map((asset) => (
      targets.has(unifiedAssetKey(asset)) ? normalizeUnifiedAsset({ ...asset, ...patch }) : asset
    ));
    setAssets((current) => {
      const next = apply(current);
      assetsRef.current = next;
      return next;
    });
    setLightbox((current) => (
      current && targets.has(unifiedAssetKey(current))
        ? normalizeUnifiedAsset({ ...current, ...patch })
        : current
    ));
  }

  async function withBusy(refs, action) {
    const keys = [...new Set(refs)].filter(Boolean);
    if (!keys.length || keys.some((ref) => busyRefs.has(ref))) return;
    setBusyRefs((current) => new Set([...current, ...keys]));
    try {
      await action();
    } finally {
      setBusyRefs((current) => {
        const next = new Set(current);
        keys.forEach((ref) => next.delete(ref));
        return next;
      });
    }
  }

  async function updateMetadata(refs, patch) {
    await withBusy(refs, async () => {
      try {
        const targetRefs = new Set(refs);
        const visibleTargets = (assetsRef.current || []).filter((asset) => (
          targetRefs.has(unifiedAssetKey(asset))
        ));
        const retainedDelta = Object.hasOwn(patch, "retained")
          ? visibleTargets.reduce((delta, asset) => (
            Boolean(asset.retained) === Boolean(patch.retained)
              ? delta
              : delta + (patch.retained ? 1 : -1)
          ), 0)
          : 0;
        const favoriteDelta = Object.hasOwn(patch, "favorite")
          ? visibleTargets.reduce((delta, asset) => (
            Boolean(asset.favorite) === Boolean(patch.favorite)
              ? delta
              : delta + (patch.favorite ? 1 : -1)
          ), 0)
          : 0;
        await api.updateMeAssetMetadata({ asset_refs: refs, ...patch });
        patchAssets(refs, patch);
        if (retainedDelta || favoriteDelta) {
          setStats((current) => current ? {
            ...current,
            retained: Math.max(0, Number(current.retained || 0) + retainedDelta),
            favorites: Math.max(0, Number(current.favorites || 0) + favoriteDelta),
          } : current);
        }
        if (filters.favorite && patch.favorite === false) await loadAssets(true);
        if (filters.retention !== "all" && Object.hasOwn(patch, "retained")) await loadAssets(true);
        notify.success(Object.hasOwn(patch, "favorite")
          ? (patch.favorite ? "已收藏。" : "已取消收藏。")
          : (patch.retained ? "已设为长期保留。" : "已恢复默认保留策略。"));
      } catch (error) {
        setMsg(error.message || "资产更新失败");
        notify.error(error.message || "资产更新失败");
      }
    });
  }

  async function deleteRefs(refs) {
    if (!refs.length || !window.confirm(`确认删除选中的 ${refs.length} 个资产？删除后不可恢复。`)) return;
    await withBusy(refs, async () => {
      try {
        const response = await api.batchDeleteMeAssets(refs);
        const deletedRefs = new Set(response?.asset_refs || response?.deleted || refs);
        const next = (assetsRef.current || []).filter((asset) => !deletedRefs.has(unifiedAssetKey(asset)));
        assetsRef.current = next;
        setAssets(next);
        setSelectedRefs((current) => new Set([...current].filter((ref) => !deletedRefs.has(ref))));
        setLightbox((current) => current && deletedRefs.has(unifiedAssetKey(current)) ? null : current);
        notify.success(`已删除 ${deletedRefs.size} 个资产。`);
        await loadAssets(true);
      } catch (error) {
        setMsg(error.message || "删除失败");
        notify.error(error.message || "删除失败；正在被草稿或任务使用的资产不能删除");
      }
    });
  }

  async function download(asset) {
    const ref = unifiedAssetKey(asset);
    if (!canDownloadAsset(asset)) {
      notify.warn(asset.origin === "generated" ? "请先解锁后再下载。" : "当前资产不可下载。");
      return;
    }
    await withBusy([ref], async () => {
      try {
        const path = asset.download_url || `/api/me/assets/download?asset_ref=${encodeURIComponent(ref)}`;
        const filename = await downloadBlob(path, downloadFilename(asset));
        notify.success(`已开始下载 ${filename}`);
      } catch (error) {
        setMsg(error.message || "下载失败");
        notify.error(error.message || "下载失败");
      }
    });
  }

  function createVariation(asset) {
    const sourceUrl = assetVariationSourceUrl(asset);
    if (!sourceUrl) {
      notify.warn("当前图片暂不可作为变体来源。");
      return;
    }
    const saved = saveStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, me?.id, {
      asset: { ...asset, type: "image", url: sourceUrl, thumb: asset.preview_url || asset.thumb || sourceUrl },
      prompt: "基于这张图生成同主体、同构图、同光线和同广告质感的近似变体；保留主体结构、产品文字、Logo、比例和核心视觉，只做轻微差异化。",
    });
    if (!saved) {
      notify.error("创建变体草稿失败");
      return;
    }
    router.push("/");
  }

  async function uploadFile(file, type) {
    if (!file || uploading) return;
    setUploading(type);
    setMsg("");
    try {
      if (type === "image") await api.uploadImage(file);
      else await api.uploadVideo(file);
      notify.success(`${type === "image" ? "图片" : "视频"}已加入我的资产。`);
      setFilters((current) => ({ ...current, origin: "all", type }));
      await loadAssets(true);
    } catch (error) {
      setMsg(error.message || "上传失败");
      notify.error(error.message || "上传失败");
    } finally {
      setUploading("");
      if (imageInputRef.current) imageInputRef.current.value = "";
      if (videoInputRef.current) videoInputRef.current.value = "";
    }
  }

  async function changePassword() {
    setPwMsg("");
    if (newPw.length < 6) return setPwMsg("新密码至少 6 位");
    try {
      await api.changePassword(oldPw, newPw);
      clearToken();
      router.push(loginPath());
    } catch (error) {
      setPwMsg(error.message);
    }
  }

  function toggleSelected(ref) {
    setSelectedRefs((current) => {
      const next = new Set(current);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  }

  const user = profile?.user || me;
  const compareSelection = selectedRefs;
  const selectedAssets = useMemo(
    () => (assets || []).filter((asset) => compareSelection.has(unifiedAssetKey(asset))),
    [assets, compareSelection],
  );
  const compareAssets = selectedAssets.slice(0, 4);
  const hasMore = Boolean(nextCursor);

  function renderAsset(asset) {
    const ref = unifiedAssetKey(asset);
    const selected = selectedRefs.has(ref);
    const busy = busyRefs.has(ref);
    const preview = assetPreviewSrc(asset);
    const metadata = assetMeta(asset);
    return (
      <article key={ref} className={`group overflow-hidden rounded-xl2 border bg-base2 ${selected ? "border-brand ring-1 ring-brand/30" : "border-line"}`}>
        <div className="relative aspect-[4/5] overflow-hidden bg-black/20">
          <button type="button" onClick={() => setLightbox(asset)} className="absolute inset-0 w-full cursor-zoom-in" aria-label={`预览${asset.type === "video" ? "视频" : "图片"}`}>
            {preview ? (
              <AssetMedia asset={asset} className="h-full w-full object-contain transition duration-200 group-hover:scale-[1.03]" fallbackClassName="flex h-full items-center justify-center text-xs text-fog" />
            ) : (
              <span className="flex h-full items-center justify-center px-4 text-xs text-fog">{assetUnavailableText(asset)}</span>
            )}
          </button>
          <label className="absolute right-2 top-2 z-10 flex h-7 w-7 cursor-pointer items-center justify-center rounded-full border border-white/25 bg-black/65">
            <input type="checkbox" checked={selected} onChange={() => toggleSelected(ref)} className="h-3.5 w-3.5 accent-brand" aria-label="选择资产" />
          </label>
          <div className="pointer-events-none absolute left-2 top-2 flex flex-wrap gap-1">
            <span className="badge bg-black/70 text-white">{asset.origin === "uploaded" ? "上传" : "生成"}</span>
            <span className="badge bg-black/70 text-white">{asset.type === "video" ? "视频" : "图片"}</span>
          </div>
          <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap gap-1">
            {asset.retained && <span className="badge bg-aqua/90 text-black">长期保留</span>}
            {!asset.retained && asset.days_left != null && <span className="badge bg-black/70 text-white">{asset.days_left} 天后过期</span>}
          </div>
        </div>
        <div className="space-y-2 p-3">
          <div className="flex min-w-0 items-center justify-between gap-2 text-xs text-fog">
            <span className="truncate">{asset.filename || (asset.origin === "uploaded" ? "上传素材" : `生成资产 ${generatedAssetId(asset) || ""}`)}</span>
            {asset.bytes ? <span className="shrink-0">{formatBytes(asset.bytes)}</span> : null}
          </div>
          {metadata && <p className="truncate text-[11px] text-fog">{metadata}</p>}
          <div className="flex items-center justify-between gap-2">
            <div className="flex gap-1">
              <button type="button" onClick={() => updateMetadata([ref], { favorite: !asset.favorite })} disabled={busy} className="btn-ghost btn-sm" aria-label={asset.favorite ? "取消收藏" : "收藏"} title={asset.favorite ? "取消收藏" : "收藏"}>{asset.favorite ? "★" : "☆"}</button>
              <button type="button" onClick={() => updateMetadata([ref], { retained: !asset.retained })} disabled={busy} className="btn-ghost btn-sm" title={asset.retained ? "取消长期保留" : "长期保留"}>{asset.retained ? "取消保留" : "保留"}</button>
              <button type="button" onClick={() => deleteRefs([ref])} disabled={busy} className="btn-ghost btn-sm text-bad" title="删除">删除</button>
            </div>
            <div className="flex gap-1">
              {asset.type === "image" && <button type="button" onClick={() => createVariation(asset)} disabled={busy} className="btn-secondary btn-sm">变体</button>}
              <button type="button" onClick={() => download(asset)} disabled={busy || !canDownloadAsset(asset)} className="btn-primary btn-sm">{busy ? "处理中" : "下载"}</button>
            </div>
          </div>
        </div>
      </article>
    );
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} active="profile" />
      <main className="mx-auto max-w-7xl px-4 pb-24 pt-8 sm:px-6">
        <section className="mb-7 border-b border-line pb-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h1 className="text-2xl font-display font-bold text-snow">我的资产</h1>
              <p className="mt-1 text-sm text-fog">统一管理生成作品和上传素材，也可在创作时直接复用。</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <input ref={imageInputRef} type="file" accept="image/*" className="hidden" onChange={(event) => uploadFile(event.target.files?.[0], "image")} />
              <input ref={videoInputRef} type="file" accept="video/*" className="hidden" onChange={(event) => uploadFile(event.target.files?.[0], "video")} />
              <button type="button" onClick={() => imageInputRef.current?.click()} disabled={Boolean(uploading)} className="btn-secondary btn-sm">{uploading === "image" ? "上传中…" : "上传图片"}</button>
              <button type="button" onClick={() => videoInputRef.current?.click()} disabled={Boolean(uploading)} className="btn-primary btn-sm">{uploading === "video" ? "上传中…" : "上传视频"}</button>
              <button type="button" onClick={() => setPwOpen((open) => !open)} className="btn-ghost btn-sm">账户设置</button>
            </div>
          </div>

          {pwOpen && (
            <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
              <span className="mr-2 text-sm text-mist">{user?.nickname || user?.phone || "当前账户"}</span>
              <input type="password" className="input w-40" placeholder="原密码" value={oldPw} onChange={(event) => setOldPw(event.target.value)} />
              <input type="password" className="input w-40" placeholder="新密码(≥6 位)" value={newPw} onChange={(event) => setNewPw(event.target.value)} />
              <button type="button" onClick={changePassword} className="btn-primary btn-sm">确认修改</button>
              {pwMsg && <span className="text-sm text-bad">{pwMsg}</span>}
            </div>
          )}

          <div className="mt-5 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="全部" value={total} />
            <Stat label="生成" value={stats?.generated ?? "—"} />
            <Stat label="上传" value={stats?.uploaded ?? "—"} />
            <Stat label="图片" value={stats?.images ?? "—"} />
            <Stat label="视频" value={stats?.videos ?? "—"} />
            <Stat label="长期保留" value={stats?.retained ?? "—"} />
          </div>
        </section>

        <section className="mb-5 flex flex-wrap items-center gap-2 border-b border-line pb-4" aria-label="资产筛选">
          <FilterSelect label="来源" value={filters.origin} onChange={(origin) => setFilters((current) => ({ ...current, origin }))} options={[["all", "全部来源"], ["uploaded", "我的上传"], ["generated", "生成作品"]]} />
          <FilterSelect label="类型" value={filters.type} onChange={(type) => setFilters((current) => ({ ...current, type }))} options={[["all", "全部类型"], ["image", "图片"], ["video", "视频"]]} />
          <FilterSelect label="保留" value={filters.retention} onChange={(retention) => setFilters((current) => ({ ...current, retention }))} options={[["all", "全部策略"], ["retained", "长期保留"], ["expiring", "默认保留"]]} />
          <label className="chip cursor-pointer gap-2 px-3 py-2">
            <input type="checkbox" checked={filters.favorite} onChange={(event) => setFilters((current) => ({ ...current, favorite: event.target.checked }))} className="h-3.5 w-3.5 accent-brand" />
            仅收藏
          </label>
          <span className="ml-auto text-xs text-fog">默认资产按平台保留策略过期；收藏不等于长期保留。</span>
        </section>

        {selectedAssets.length > 0 && (
          <div className="sticky top-[73px] z-20 mb-5 flex flex-wrap items-center justify-between gap-2 border border-brand/30 bg-base2/95 px-3 py-2 shadow-pop backdrop-blur-xl">
            <span className="text-sm text-mist">已选择 {selectedAssets.length} 个资产</span>
            <div className="flex flex-wrap gap-2">
              <button type="button" onClick={() => updateMetadata([...selectedRefs], { retained: true })} className="btn-secondary btn-sm">长期保留</button>
              <button type="button" onClick={() => updateMetadata([...selectedRefs], { favorite: true })} className="btn-secondary btn-sm">收藏</button>
              <button type="button" onClick={() => deleteRefs([...selectedRefs])} className="btn-ghost btn-sm text-bad">删除</button>
              <button type="button" onClick={() => setSelectedRefs(new Set())} className="btn-ghost btn-sm">取消选择</button>
            </div>
          </div>
        )}

        {msg && <div className="mb-5 border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad">{msg}</div>}

        <AssetComparePanel assets={compareAssets} onClose={() => setSelectedRefs(new Set())} />

        {assets === null ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {Array.from({ length: 8 }).map((_, index) => <div key={index} className="skeleton aspect-[4/5]" />)}
          </div>
        ) : assets.length === 0 ? (
          <div className="flex min-h-72 flex-col items-center justify-center gap-3 border-y border-line text-center">
            <p className="text-sm text-mist">当前筛选下还没有资产。</p>
            <button type="button" onClick={() => imageInputRef.current?.click()} className="btn-primary btn-sm">上传第一张素材</button>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 items-start gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">{assets.map(renderAsset)}</div>
            {hasMore && (
              <div className="mt-7 text-center">
                <button type="button" onClick={() => loadAssets(false)} disabled={loading} className="btn-secondary">{loading ? "加载中…" : "加载更多"}</button>
              </div>
            )}
          </>
        )}
      </main>

      {lightbox && (
        <AssetPreviewDialog asset={lightbox} onClose={() => setLightbox(null)} onError={(error) => setMsg(error?.message || "预览加载失败")} meta={lightbox.retained ? " · 长期保留" : (lightbox.days_left != null ? ` · ${lightbox.days_left} 天后过期` : "")}>
          {({ asset }) => {
            const ref = unifiedAssetKey(asset);
            return (
              <>
                <button type="button" onClick={() => updateMetadata([ref], { favorite: !asset.favorite })} disabled={busyRefs.has(ref)} className="btn-ghost btn-sm">{asset.favorite ? "★ 已收藏" : "☆ 收藏"}</button>
                <button type="button" onClick={() => updateMetadata([ref], { retained: !asset.retained })} disabled={busyRefs.has(ref)} className="btn-secondary btn-sm">{asset.retained ? "取消长期保留" : "长期保留"}</button>
                {asset.type === "image" && <button type="button" onClick={() => createVariation(asset)} className="btn-secondary btn-sm">生成变体</button>}
                <button type="button" onClick={() => download(asset)} disabled={busyRefs.has(ref) || !canDownloadAsset(asset)} className="btn-primary btn-sm">下载</button>
                <button type="button" onClick={() => deleteRefs([ref])} disabled={busyRefs.has(ref)} className="btn-ghost btn-sm text-bad">删除</button>
              </>
            );
          }}
        </AssetPreviewDialog>
      )}
    </div>
  );
}

function FilterSelect({ label, value, onChange, options }) {
  return (
    <label className="flex items-center gap-2 text-xs text-fog">
      <span>{label}</span>
      <select className="input min-w-28 px-3 py-2 text-xs" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, optionLabel]) => <option key={optionValue} value={optionValue}>{optionLabel}</option>)}
      </select>
    </label>
  );
}

function Stat({ label, value }) {
  return (
    <div className="border-l border-line px-3 py-1 first:border-l-0">
      <div className="font-display text-xl font-bold text-snow">{value}</div>
      <div className="mt-0.5 text-xs text-fog">{label}</div>
    </div>
  );
}
