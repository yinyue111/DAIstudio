"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Flag, FolderArchive, FolderInput, ScanSearch, Search, Tags, X } from "lucide-react";
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
const BATCH_DOWNLOAD_LIMIT = 100;

const REPORT_REASONS = [
  ["copyright", "涉嫌侵权或盗用他人素材"],
  ["sensitive", "包含敏感内容"],
  ["illegal", "违法违规信息"],
  ["privacy", "泄露个人隐私"],
  ["other", "其他问题"],
];

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

function parseTags(value) {
  const seen = new Set();
  const tags = [];
  for (const raw of String(value || "").split(/[,，]/)) {
    const tag = raw.trim().replace(/^#+/, "");
    const key = tag.toLocaleLowerCase();
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags;
}

export default function ProfilePage() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [profile, setProfile] = useState(null);
  const [filters, setFilters] = useState({
    origin: "all",
    type: "all",
    favorite: false,
    retention: "all",
    q: "",
    tag: "",
    folder: "all",
  });
  const [searchDraft, setSearchDraft] = useState("");
  const [tagDraft, setTagDraft] = useState("");
  const [folders, setFolders] = useState([]);
  const [moveFolderId, setMoveFolderId] = useState("");
  const [tagEditor, setTagEditor] = useState(null);
  const [similarity, setSimilarity] = useState(null);
  const [assets, setAssets] = useState(null);
  const [stats, setStats] = useState(null);
  const [total, setTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [msg, setMsg] = useState("");
  const [busyRefs, setBusyRefs] = useState(() => new Set());
  const [selectedRefs, setSelectedRefs] = useState(() => new Set());
  const [reportTarget, setReportTarget] = useState(null);
  const [reportReason, setReportReason] = useState("copyright");
  const [reportNote, setReportNote] = useState("");
  const [reportBusy, setReportBusy] = useState(false);
  const [batchDownloading, setBatchDownloading] = useState(false);
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
    loadFolders();
  }, []);

  useEffect(() => {
    loadAssets(true);
  }, [
    filters.origin,
    filters.type,
    filters.favorite,
    filters.retention,
    filters.q,
    filters.tag,
    filters.folder,
  ]);

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
        q: filters.q,
        tag: filters.tag,
        folder: filters.folder,
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

  async function loadFolders() {
    try {
      const rows = await api.assetFolders();
      setFolders(Array.isArray(rows) ? rows : []);
    } catch (error) {
      reportBackgroundError(error, "load asset folders");
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

  async function saveTags(ref) {
    const tags = parseTags(tagEditor?.value);
    if (tags.length > 20 || tags.some((tag) => tag.length > 32)) {
      notify.warn("最多保存 20 个标签，每个标签不超过 32 个字符");
      return;
    }
    await withBusy([ref], async () => {
      try {
        const metadata = await api.updateMeAssetTags(ref, tags);
        patchAssets([ref], { tags: metadata.tags || [] });
        setTagEditor(null);
        if (filters.tag && !tags.some((tag) => tag.toLocaleLowerCase() === filters.tag.toLocaleLowerCase())) {
          await loadAssets(true);
        }
        notify.success("素材标签已保存");
      } catch (error) {
        notify.error(error.message || "标签保存失败");
      }
    });
  }

  async function moveSelectedAssets() {
    const refs = [...selectedRefs];
    if (!refs.length || !moveFolderId) return;
    await withBusy(refs, async () => {
      try {
        if (moveFolderId === "unfiled") {
          const byFolder = new Map();
          for (const asset of selectedAssets) {
            if (!asset.folder_id) continue;
            const key = String(asset.folder_id);
            byFolder.set(key, [...(byFolder.get(key) || []), unifiedAssetKey(asset)]);
          }
          await Promise.all(
            [...byFolder.entries()].map(([folderId, assetRefs]) => (
              api.removeAssetsFromFolder(folderId, assetRefs)
            )),
          );
        } else {
          await api.moveAssetsToFolder(moveFolderId, refs);
        }
        setSelectedRefs(new Set());
        setMoveFolderId("");
        await Promise.all([loadAssets(true), loadFolders()]);
        notify.success(moveFolderId === "unfiled" ? "素材已移出文件夹" : "素材已移动到文件夹");
      } catch (error) {
        notify.error(error.message || "移动素材失败");
      }
    });
  }

  async function findSimilar(asset) {
    const ref = unifiedAssetKey(asset);
    await withBusy([ref], async () => {
      try {
        const result = await api.findMeSimilarAssets(ref, { maxDistance: 8 });
        const matches = (result.matches || []).map((item) => item.asset).filter(Boolean);
        setSimilarity({ query: asset, result, assets: [asset, ...matches] });
        if (!matches.length) notify.success("未发现重复或感知相似素材");
      } catch (error) {
        notify.error(error.message || "查重与相似检测失败");
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

  function openReport(asset) {
    if (!generatedAssetId(asset)) {
      notify.warn("仅平台生成的素材支持举报。");
      return;
    }
    setReportReason("copyright");
    setReportNote("");
    setReportTarget(asset);
  }

  function closeReport() {
    if (reportBusy) return;
    setReportTarget(null);
    setReportNote("");
  }

  async function submitReport() {
    const assetId = generatedAssetId(reportTarget);
    if (!assetId || reportBusy) return;
    const note = reportNote.trim();
    if (note.length > 500) {
      notify.warn("补充说明最多 500 字");
      return;
    }
    setReportBusy(true);
    try {
      const report = await api.reportAsset(assetId, { reason: reportReason, note: note || null });
      const createdAt = report?.created_at ? Date.parse(report.created_at) : NaN;
      const duplicated = Number.isFinite(createdAt) && Date.now() - createdAt > 15000;
      if (duplicated) {
        notify.warn("你已举报过该素材，平台正在核实中，请勿重复提交。");
      } else {
        notify.success("举报已受理，平台会尽快核实处理。");
      }
      setReportTarget(null);
      setReportNote("");
    } catch (error) {
      notify.error(error.message || "举报提交失败，请稍后再试");
    } finally {
      setReportBusy(false);
    }
  }

  async function batchDownloadSelected() {
    if (batchDownloading) return;
    const ids = selectedDownloadableIds;
    if (!ids.length) {
      notify.warn("所选素材中没有可打包的生成素材（需已解锁）。");
      return;
    }
    if (ids.length > BATCH_DOWNLOAD_LIMIT) {
      notify.warn(`单次最多打包下载 ${BATCH_DOWNLOAD_LIMIT} 个素材，请减少选择。`);
      return;
    }
    const skippedCount = selectedAssets.length - ids.length;
    setBatchDownloading(true);
    try {
      const filename = await api.batchDownloadAssets(ids);
      notify.success(skippedCount > 0
        ? `已开始下载 ${filename}，${skippedCount} 个非生成或未解锁素材未打包。`
        : `已开始下载 ${filename}`);
    } catch (error) {
      setMsg(error.message || "打包下载失败");
      notify.error(error.message || "打包下载失败，请稍后再试");
    } finally {
      setBatchDownloading(false);
    }
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
      setSearchDraft("");
      setTagDraft("");
      setFilters((current) => ({
        ...current,
        origin: "all",
        type,
        q: "",
        tag: "",
        folder: "all",
      }));
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
  const selectedDownloadableIds = useMemo(() => (
    selectedAssets
      .filter((asset) => asset.origin === "generated" && canDownloadAsset(asset))
      .map((asset) => generatedAssetId(asset))
      .filter(Boolean)
  ), [selectedAssets]);
  const compareAssets = (similarity?.assets || selectedAssets).slice(0, 4);
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
            <span className="badge bg-black/70 text-white">{asset.origin === "fetched" ? "抓取" : asset.origin === "uploaded" ? "上传" : "生成"}</span>
            <span className="badge bg-black/70 text-white">{asset.type === "video" ? "视频" : "图片"}</span>
          </div>
          <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap gap-1">
            {asset.retained && <span className="badge bg-aqua/90 text-black">长期保留</span>}
            {!asset.retained && asset.days_left != null && <span className="badge bg-black/70 text-white">{asset.days_left} 天后过期</span>}
          </div>
        </div>
        <div className="space-y-2 p-3">
          <div className="flex min-w-0 items-center justify-between gap-2 text-xs text-fog">
            <span className="truncate">{asset.origin === "fetched" ? "抓取素材" : asset.filename || (asset.origin === "uploaded" ? "上传素材" : `生成资产 ${generatedAssetId(asset) || ""}`)}</span>
            {asset.bytes ? <span className="shrink-0">{formatBytes(asset.bytes)}</span> : null}
          </div>
          {metadata && <p className="truncate text-[11px] text-fog">{metadata}</p>}
          {(asset.folder_name || (asset.tags || []).length > 0) && (
            <div className="flex min-h-5 flex-wrap gap-1" aria-label="素材归档信息">
              {asset.folder_name && <span className="badge border border-line bg-white/[0.04] text-fog">{asset.folder_name}</span>}
              {(asset.tags || []).map((tag) => <span key={tag} className="badge border border-aqua/25 bg-aqua/10 text-aqua">#{tag}</span>)}
            </div>
          )}
          {tagEditor?.ref === ref && (
            <form
              className="flex gap-1.5"
              onSubmit={(event) => {
                event.preventDefault();
                saveTags(ref);
              }}
            >
              <label className="sr-only" htmlFor={`asset-tags-${ref}`}>素材标签</label>
              <input
                id={`asset-tags-${ref}`}
                className="input h-8 min-w-0 flex-1 py-1 text-xs"
                value={tagEditor.value}
                onChange={(event) => setTagEditor({ ref, value: event.target.value })}
                maxLength={680}
                placeholder="商品, 主图"
                autoFocus
              />
              <button type="submit" className="btn-primary btn-sm" disabled={busy}>保存</button>
              <button type="button" className="icon-btn h-8 w-8" onClick={() => setTagEditor(null)} title="取消编辑" aria-label="取消编辑标签">
                <X size={13} aria-hidden="true" />
              </button>
            </form>
          )}
          <div className="flex items-center justify-between gap-2">
            <div className="flex gap-1">
              <button type="button" onClick={() => updateMetadata([ref], { favorite: !asset.favorite })} disabled={busy} className="btn-ghost btn-sm" aria-label={asset.favorite ? "取消收藏" : "收藏"} title={asset.favorite ? "取消收藏" : "收藏"}>{asset.favorite ? "★" : "☆"}</button>
              <button type="button" onClick={() => updateMetadata([ref], { retained: !asset.retained })} disabled={busy} className="btn-ghost btn-sm" title={asset.retained ? "取消长期保留" : "长期保留"}>{asset.retained ? "取消保留" : "保留"}</button>
              <button type="button" onClick={() => setTagEditor({ ref, value: (asset.tags || []).join(", ") })} disabled={busy} className="icon-btn h-8 w-8" title="编辑标签" aria-label="编辑素材标签"><Tags size={14} aria-hidden="true" /></button>
              <button type="button" onClick={() => findSimilar(asset)} disabled={busy} className="icon-btn h-8 w-8" title="查重与相似检测" aria-label="查重与相似检测"><ScanSearch size={14} aria-hidden="true" /></button>
              {asset.origin === "generated" && (
                <button type="button" onClick={() => openReport(asset)} disabled={busy} className="icon-btn h-8 w-8" title="举报素材" aria-label="举报素材"><Flag size={14} aria-hidden="true" /></button>
              )}
            </div>
            <div className="flex gap-1">
              {asset.type === "image" && <button type="button" onClick={() => createVariation(asset)} disabled={busy} className="btn-secondary btn-sm">变体</button>}
              <button type="button" onClick={() => download(asset)} disabled={busy || !canDownloadAsset(asset)} className="btn-primary btn-sm">{busy ? "处理中" : "下载"}</button>
              <button type="button" onClick={() => deleteRefs([ref])} disabled={busy} className="btn-ghost btn-sm text-bad" title="删除">删除</button>
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
          <FilterSelect label="来源" value={filters.origin} onChange={(origin) => setFilters((current) => ({ ...current, origin }))} options={[["all", "全部来源"], ["uploaded", "我的上传"], ["fetched", "素材抓取"], ["generated", "生成作品"]]} />
          <FilterSelect label="类型" value={filters.type} onChange={(type) => setFilters((current) => ({ ...current, type }))} options={[["all", "全部类型"], ["image", "图片"], ["video", "视频"]]} />
          <FilterSelect label="保留" value={filters.retention} onChange={(retention) => setFilters((current) => ({ ...current, retention }))} options={[["all", "全部策略"], ["retained", "长期保留"], ["expiring", "默认保留"]]} />
          <label className="chip cursor-pointer gap-2 px-3 py-2">
            <input type="checkbox" checked={filters.favorite} onChange={(event) => setFilters((current) => ({ ...current, favorite: event.target.checked }))} className="h-3.5 w-3.5 accent-brand" />
            仅收藏
          </label>
          <form
            className="flex min-w-full flex-1 flex-wrap items-center gap-2 pt-2 lg:min-w-0 lg:pt-0"
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              setFilters((current) => ({
                ...current,
                q: searchDraft.trim(),
                tag: tagDraft.trim().replace(/^#+/, ""),
              }));
            }}
          >
            <label className="sr-only" htmlFor="asset-library-search">搜索素材</label>
            <input
              id="asset-library-search"
              type="search"
              className="input h-9 min-w-48 flex-1 py-1.5 text-sm"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              maxLength={100}
              placeholder="名称、提示词、标签或文件夹"
            />
            <label className="sr-only" htmlFor="asset-library-tag">按标签筛选</label>
            <input
              id="asset-library-tag"
              className="input h-9 w-32 py-1.5 text-sm"
              value={tagDraft}
              onChange={(event) => setTagDraft(event.target.value)}
              maxLength={32}
              placeholder="精确标签"
            />
            <label className="sr-only" htmlFor="asset-library-folder">按文件夹筛选</label>
            <select
              id="asset-library-folder"
              className="input h-9 min-w-36 py-1.5 text-sm"
              value={filters.folder}
              onChange={(event) => setFilters((current) => ({ ...current, folder: event.target.value }))}
            >
              <option value="all">全部文件夹</option>
              <option value="unfiled">未归档</option>
              {folders.map((folder) => <option key={folder.id} value={String(folder.id)}>{folder.name}</option>)}
            </select>
            <button type="submit" className="icon-btn h-9 w-9" title="搜索素材" aria-label="搜索素材">
              <Search size={15} aria-hidden="true" />
            </button>
            {(filters.q || filters.tag || filters.folder !== "all") && (
              <button
                type="button"
                className="icon-btn h-9 w-9"
                title="清除搜索和归档筛选"
                aria-label="清除搜索和归档筛选"
                onClick={() => {
                  setSearchDraft("");
                  setTagDraft("");
                  setFilters((current) => ({ ...current, q: "", tag: "", folder: "all" }));
                }}
              >
                <X size={15} aria-hidden="true" />
              </button>
            )}
          </form>
          <span className="text-xs text-fog">收藏不等于长期保留。</span>
        </section>

        {selectedAssets.length > 0 && (
          <div className="sticky top-[73px] z-20 mb-5 flex flex-wrap items-center justify-between gap-2 border border-brand/30 bg-base2/95 px-3 py-2 shadow-pop backdrop-blur-xl">
            <span className="text-sm text-mist">已选择 {selectedAssets.length} 个资产</span>
            <div className="flex flex-wrap gap-2">
              <label className="sr-only" htmlFor="asset-batch-folder">目标文件夹</label>
              <select
                id="asset-batch-folder"
                className="input h-9 min-w-36 py-1.5 text-sm"
                value={moveFolderId}
                onChange={(event) => setMoveFolderId(event.target.value)}
              >
                <option value="">移动到文件夹</option>
                <option value="unfiled">移出文件夹</option>
                {folders.map((folder) => <option key={folder.id} value={String(folder.id)}>{folder.name}</option>)}
              </select>
              <button
                type="button"
                onClick={moveSelectedAssets}
                disabled={!moveFolderId || selectedAssets.some((asset) => busyRefs.has(unifiedAssetKey(asset)))}
                className="icon-btn h-9 w-9"
                title="移动所选素材"
                aria-label="移动所选素材"
              >
                <FolderInput size={15} aria-hidden="true" />
              </button>
              <button type="button" onClick={() => updateMetadata([...selectedRefs], { retained: true })} className="btn-secondary btn-sm">长期保留</button>
              <button type="button" onClick={() => updateMetadata([...selectedRefs], { favorite: true })} className="btn-secondary btn-sm">收藏</button>
              <button
                type="button"
                onClick={batchDownloadSelected}
                disabled={batchDownloading || !selectedDownloadableIds.length}
                className="btn-secondary btn-sm inline-flex items-center gap-1.5"
                title={selectedDownloadableIds.length
                  ? `将 ${selectedDownloadableIds.length} 个已解锁生成素材打包为 ZIP 下载`
                  : "所选素材中没有可打包的生成素材（需已解锁）"}
                aria-label="打包下载所选素材"
              >
                <FolderArchive size={14} aria-hidden="true" />
                {batchDownloading ? "打包中…" : `打包下载${selectedDownloadableIds.length ? ` ${selectedDownloadableIds.length}` : ""}`}
              </button>
              <button type="button" onClick={() => deleteRefs([...selectedRefs])} className="btn-ghost btn-sm text-bad">删除</button>
              <button type="button" onClick={() => setSelectedRefs(new Set())} className="btn-ghost btn-sm">取消选择</button>
            </div>
          </div>
        )}

        {msg && <div className="mb-5 border border-bad/30 bg-bad/10 px-4 py-3 text-sm text-bad">{msg}</div>}

        {similarity && (
          <div className={`mb-3 flex items-start justify-between gap-3 border px-3 py-2 text-sm ${similarity.result.status === "degraded" ? "border-warn/30 bg-warn/10 text-warn" : "border-aqua/30 bg-aqua/10 text-mist"}`} role="status">
            <div>
              <p className="font-medium text-snow">查重完成：发现 {similarity.result.matches?.length || 0} 个匹配素材</p>
              <p className="mt-0.5 text-xs">{similarity.result.message || "已完成 SHA-256 精确查重和图片感知相似检测。"}</p>
            </div>
            <button type="button" onClick={() => setSimilarity(null)} className="icon-btn h-8 w-8" title="关闭查重结果" aria-label="关闭查重结果">
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        )}

        <AssetComparePanel
          assets={compareAssets}
          onClose={() => {
            setSimilarity(null);
            setSelectedRefs(new Set());
          }}
        />

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
                <button type="button" onClick={() => setTagEditor({ ref, value: (asset.tags || []).join(", ") })} disabled={busyRefs.has(ref)} className="btn-secondary btn-sm">编辑标签</button>
                <button type="button" onClick={() => findSimilar(asset)} disabled={busyRefs.has(ref)} className="btn-secondary btn-sm">查重</button>
                {asset.type === "image" && <button type="button" onClick={() => createVariation(asset)} className="btn-secondary btn-sm">生成变体</button>}
                {asset.origin === "generated" && <button type="button" onClick={() => openReport(asset)} disabled={busyRefs.has(ref)} className="btn-ghost btn-sm">举报</button>}
                <button type="button" onClick={() => download(asset)} disabled={busyRefs.has(ref) || !canDownloadAsset(asset)} className="btn-primary btn-sm">下载</button>
                <button type="button" onClick={() => deleteRefs([ref])} disabled={busyRefs.has(ref)} className="btn-ghost btn-sm text-bad">删除</button>
              </>
            );
          }}
        </AssetPreviewDialog>
      )}

      {reportTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-label="举报素材">
          <div className="w-full max-w-md rounded-xl2 border border-line bg-base2 p-5 shadow-pop">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-base font-display font-semibold text-snow">举报素材</h2>
                <p className="mt-1 text-xs text-fog">举报后平台会人工核实，确认违规的素材将被下架。</p>
              </div>
              <button type="button" className="icon-btn h-8 w-8" onClick={closeReport} disabled={reportBusy} title="关闭" aria-label="关闭举报弹窗">
                <X size={14} aria-hidden="true" />
              </button>
            </div>
            <div className="mt-4 space-y-2" role="radiogroup" aria-label="举报理由">
              {REPORT_REASONS.map(([value, label]) => (
                <label
                  key={value}
                  className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm transition ${
                    reportReason === value ? "border-brand/60 bg-brand/10 text-snow" : "border-line text-mist hover:border-line2"
                  }`}
                >
                  <input
                    type="radio"
                    name="asset-report-reason"
                    value={value}
                    checked={reportReason === value}
                    onChange={() => setReportReason(value)}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  {label}
                </label>
              ))}
            </div>
            <label className="sr-only" htmlFor="asset-report-note">补充说明</label>
            <textarea
              id="asset-report-note"
              className="input mt-3 h-24 w-full resize-none py-2 text-sm"
              value={reportNote}
              onChange={(event) => setReportNote(event.target.value)}
              maxLength={500}
              placeholder="选填：补充具体情况，如侵权来源链接（最多 500 字）"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="btn-ghost btn-sm" onClick={closeReport} disabled={reportBusy}>取消</button>
              <button type="button" className="btn-primary btn-sm" onClick={submitReport} disabled={reportBusy}>
                {reportBusy ? "提交中…" : "提交举报"}
              </button>
            </div>
          </div>
        </div>
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
