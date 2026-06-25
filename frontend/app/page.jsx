"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, loginPath, wsUrl } from "../lib/api";
import Nav from "../components/Nav";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import {
  EXAMPLES,
  IMAGE_QUALITY_PRESETS,
  PARSE_POLL_INTERVAL_MS,
  PARSE_POLL_TIMEOUT_MS,
  RATIOS,
  VIDEO_DURATION_PRESETS,
  VIDEO_QUALITIES,
  VIDEO_RATIO_KEYS,
} from "./studio/constants";
import StudioReferencePanel from "./studio/StudioReferencePanel";
import StudioResults from "./studio/StudioResults";
import { generationSubmitDisabled, shouldBlockNewGeneration } from "./studio/taskConcurrency";
import {
  assetDims,
  assetSignature,
  boundedImageCount,
  boundedVideoDuration,
  buildSourceAssetMeta,
  composePromptFromStructured,
  composeStyleTransferPrompt,
  formatDuration,
  imageSizeFor,
  isRequestTimeoutError,
  isTerminalTaskStatus,
  nearestRatio,
  qualityKeyForSize,
  ratioKeyForSize,
  videoRatioOptions,
} from "./studio/helpers";

const CREATION_MODES = [
  { key: "image", label: "文生图", icon: "✦" },
  { key: "image_edit", label: "图片编辑", icon: "◐" },
  { key: "video", label: "文生视频", icon: "▶" },
  { key: "video_edit", label: "视频编辑", icon: "▣" },
];

const EDIT_STYLE_KEYS = {
  image: ["场景背景", "广告目标", "风格", "构图", "景别", "视角镜头", "视角构图", "光线", "色调配色", "材质纹理", "氛围情绪", "后期质感", "标签"],
  video: ["场景背景", "广告目标", "风格", "视角构图", "镜头运动", "剪辑节奏", "时序分镜", "字幕卖点", "光线", "色调配色", "材质纹理", "氛围情绪", "转场", "时长建议", "后期质感", "标签"],
};

const EDIT_PROMPT_CHIPS = [
  "保留品牌标识",
  "包装文字逐字保留",
  "强化产品卖点",
  "产品边缘自然融入场景",
  "干净商业广告质感",
  "小红书种草氛围",
];

const EDIT_PROTECTION_NEGATIVE = [
  "logo变形",
  "文字乱码",
  "标签不可读",
  "标签文字不可读",
  "包装被篡改",
  "比例失真",
  "错误品牌文字",
  "品牌名错误",
  "商标错误",
  "包装文字变化",
  "包装文字错字",
  "包装文字缺失",
  "文字被翻译",
  "文字新增",
  "文字删减",
  "标签位置改变",
  "产品变形",
];

const PRODUCT_EDIT_NEGATIVE = [
  "logo扭曲",
  "logo丢失",
  "Logo重绘",
  "Logo字体变化",
  "包装文字乱码",
  "包装文字被改写",
  "产品正面文字被重排",
  "顶部文字被改写",
  "品牌名被替换",
  "图案变形",
  "包装纹理丢失",
  "产品外形改变",
  "产品颜色漂移",
  "标签缺失",
  "贴纸变形",
  "材质错误",
  "主体被替换",
  "多余商品",
  "低清产品",
  "边缘糊化",
];

function creationModeLabel(mode) {
  return CREATION_MODES.find((item) => item.key === mode)?.label || "生成";
}

function createWorkspaceState() {
  return {
    prompt: "",
    negative: "",
    imageEditProductMode: false,
    ratio: "1:1",
    imageQuality: "1k",
    n: 4,
    seed: "",
    vDuration: 5,
    vResolution: "720p",
    videoAnalysisPreset: "standard",
    url: "",
    parsing: false,
    uploading: false,
    reversing: false,
    assets: [],
    selected: null,
    productAsset: null,
    structured: {},
    structuredSource: "",
    negativeTouched: false,
    promptDirty: false,
  };
}

function createModeWorkspaces() {
  return Object.fromEntries(CREATION_MODES.map(({ key }) => [key, createWorkspaceState()]));
}

export default function Home() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);

  // creation state
  const [creationMode, setCreationMode] = useState("image"); // image | video | image_edit | video_edit
  const [workspaces, setWorkspaces] = useState(createModeWorkspaces);
  const [showNegative, setShowNegative] = useState(false);
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);

  // reference (paste link -> reverse) state
  const [refOpen, setRefOpen] = useState(false);
  const [structOpen, setStructOpen] = useState(true);

  // run state
  const [task, setTask] = useState(null);
  const [runningSnapshot, setRunningSnapshot] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState("");
  const [trackingLost, setTrackingLost] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [finalTaskId, setFinalTaskId] = useState(null);
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());

  // gallery
  const [works, setWorks] = useState(null);

  const pollRef = useRef(null);
  const wsRef = useRef(null);
  const backgroundTrackersRef = useRef(new Map());
  const activeIdRef = useRef(null); // id of the task currently being tracked
  const selectedByModeRef = useRef({});
  const refVersionRef = useRef({});
  const parseRequestRef = useRef({});
  const uploadRequestRef = useRef({});
  const productUploadRequestRef = useRef({});
  const reverseRequestRef = useRef({});
  const pendingGenerateRequestRef = useRef(null);
  const busyAssetIdsRef = useRef(new Set());
  const imageUploadInputRef = useRef(null);
  const productUploadInputRef = useRef(null);
  const videoUploadInputRef = useRef(null);
  const objectUrlsByModeRef = useRef({});
  const productObjectUrlsRef = useRef({});
  const creationModeRef = useRef("image");
  const PENDING_GENERATE_STORAGE_KEY = "studio_pending_generate_request_v1";
  const workspace = workspaces[creationMode] || createWorkspaceState();
  const {
    prompt,
    negative,
    imageEditProductMode,
    ratio,
    imageQuality,
    n,
    seed,
    vDuration,
    vResolution,
    videoAnalysisPreset,
    url,
    parsing,
    uploading,
    reversing,
    assets,
    selected,
    productAsset,
    structured,
    structuredSource,
    negativeTouched,
    promptDirty,
  } = workspace;
  const category = creationMode === "video" || creationMode === "video_edit" ? "video" : "image";
  const isEditMode = creationMode === "image_edit" || creationMode === "video_edit";
  const isImageEditMode = creationMode === "image_edit";
  const productGenerationMode = isImageEditMode && imageEditProductMode;

  function updateWorkspaceField(field, valueOrUpdater, mode = creationMode) {
    setWorkspaces((prev) => {
      const current = prev[mode] || createWorkspaceState();
      const nextValue = typeof valueOrUpdater === "function"
        ? valueOrUpdater(current[field])
        : valueOrUpdater;
      return {
        ...prev,
        [mode]: {
          ...current,
          [field]: nextValue,
        },
      };
    });
  }

  function setWorkspacePatch(patchOrUpdater, mode = creationMode) {
    setWorkspaces((prev) => {
      const current = prev[mode] || createWorkspaceState();
      const patch = typeof patchOrUpdater === "function" ? patchOrUpdater(current) : patchOrUpdater;
      return {
        ...prev,
        [mode]: {
          ...current,
          ...patch,
        },
      };
    });
  }

  const setPrompt = (value) => updateWorkspaceField("prompt", value);
  const setNegative = (value) => updateWorkspaceField("negative", value);
  const setImageEditProductMode = (value) => updateWorkspaceField("imageEditProductMode", value);
  const setRatio = (value) => updateWorkspaceField("ratio", value);
  const setImageQuality = (value) => updateWorkspaceField("imageQuality", value);
  const setN = (value) => updateWorkspaceField("n", value);
  const setSeed = (value) => updateWorkspaceField("seed", value);
  const setVDuration = (value) => updateWorkspaceField("vDuration", value);
  const setVResolution = (value) => updateWorkspaceField("vResolution", value);
  const setVideoAnalysisPreset = (value) => updateWorkspaceField("videoAnalysisPreset", value);
  const setUrl = (value) => updateWorkspaceField("url", value);
  const setStructured = (value) => updateWorkspaceField("structured", value);
  const setNegativeTouched = (value) => updateWorkspaceField("negativeTouched", value);
  const setPromptDirty = (value) => updateWorkspaceField("promptDirty", value);

  useEffect(() => {
    api.me().then(setMe).catch(() => router.push(loginPath()));
    api.config().then((c) => {
      setCfg(c);
      // honour admin defaults so the values we submit match the backend config
      const d = c.defaults || {};
      const patch = {};
      if (d.image_n) patch.n = Number(d.image_n);
      if (d.image_size) {
        const rk = ratioKeyForSize(d.image_size);
        if (rk) patch.ratio = rk;
        patch.imageQuality = qualityKeyForSize(d.image_size);
      }
      if (Object.keys(patch).length) {
        setWorkspaces((prev) => Object.fromEntries(
          Object.entries(prev).map(([mode, current]) => [
            mode,
            mode === "image" || mode === "image_edit" ? { ...current, ...patch } : current,
          ]),
        ));
      }
    }).catch(() => {});
    loadWorks({ restoreActive: true });
    try {
      const draft = window.localStorage.getItem(STUDIO_DRAFT_PROMPT_KEY);
      if (draft) {
        setWorkspacePatch({ prompt: draft, promptDirty: true }, "image");
        window.localStorage.removeItem(STUDIO_DRAFT_PROMPT_KEY);
      }
    } catch (e) {}
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (wsRef.current) try { wsRef.current.close(); } catch (e) {}
      backgroundTrackersRef.current.forEach((stop) => stop());
      backgroundTrackersRef.current.clear();
      revokeUploadedObjectUrls();
      revokeProductObjectUrls();
    };
  }, []);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((r) => r.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

  useEffect(() => {
    creationModeRef.current = creationMode;
    selectedByModeRef.current[creationMode] = selected;
  }, [creationMode, selected]);

  function refreshMe() { api.me().then(setMe).catch(() => {}); }

  function formatBytes(bytes) {
    const n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n >= 1024 * 1024 * 1024) return `${(n / 1024 / 1024 / 1024).toFixed(1)}GB`;
    if (n >= 1024 * 1024) return `${Math.ceil(n / 1024 / 1024)}MB`;
    if (n >= 1024) return `${Math.ceil(n / 1024)}KB`;
    return `${n}B`;
  }

  function uploadLimitExceeded(file, kind) {
    const limit = Number(kind === "video" ? cfg?.max_upload_video_bytes : cfg?.max_upload_image_bytes);
    if (!limit || !file?.size || file.size <= limit) return false;
    setMsg(`${kind === "video" ? "视频" : "图片"}文件过大，当前 ${formatBytes(file.size)}，上限 ${formatBytes(limit)}。请压缩后再上传。`);
    return true;
  }

  function modelEnabled(kind) {
    if (!cfg) return true;
    const modelUse = kind === "video" || kind === "video_edit" ? "video" : "image";
    return cfg.models?.[modelUse]?.enabled !== false;
  }

  function switchCreationMode(kind) {
    if (!modelEnabled(kind)) {
      setMsg(`${kind === "video" || kind === "video_edit" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
      return;
    }
    setMsg("");
    setCreationMode(kind);
  }

  function restoreActiveTaskFromList(list) {
    if (task && !isTerminalTaskStatus(task.status)) return;
    const active = (list || []).find((item) => (
      item
      && !isTerminalTaskStatus(item.status)
      && item.category === "video"
    ));
    if (!active) return;
    setCreationMode("video");
    setTask(active);
    setFinalTaskId(active.stage === "final" ? active.id : null);
    setTrackingLost(false);
    const dims = assetDims(active.assets?.[0]);
    const activeRatio = dims
      ? ratioOptions.find((r) => r.key === nearestRatio(dims.width, dims.height, ratioOptions))
      : null;
    setRunningSnapshot({
      category: active.category,
      n: 1,
      ratio: activeRatio || ratioOptions[0],
    });
    startTracking(active.id);
  }

  async function loadWorks({ restoreActive = false } = {}) {
    try {
      const list = await api.tasks(20, 0);
      const flat = [];
      for (const t of list) {
        for (const a of t.assets || []) flat.push({ ...a, _cat: t.category });
      }
      setWorks(flat);
      if (restoreActive) restoreActiveTaskFromList(list);
    } catch (e) { setWorks([]); }
  }

  // ---- reference: parse + reverse ----
  function rememberUploadedObjectUrl(mode, url) {
    if (!objectUrlsByModeRef.current[mode]) objectUrlsByModeRef.current[mode] = new Set();
    objectUrlsByModeRef.current[mode].add(url);
  }

  function revokeUploadedObjectUrls(mode = null) {
    if (mode) {
      objectUrlsByModeRef.current[mode]?.forEach((url) => URL.revokeObjectURL(url));
      objectUrlsByModeRef.current[mode]?.clear();
      return;
    }
    Object.values(objectUrlsByModeRef.current).forEach((urls) => {
      urls?.forEach((url) => URL.revokeObjectURL(url));
      urls?.clear();
    });
    objectUrlsByModeRef.current = {};
  }

  function revokeProductObjectUrl(mode = creationMode) {
    const url = productObjectUrlsRef.current[mode];
    if (url) {
      URL.revokeObjectURL(url);
      delete productObjectUrlsRef.current[mode];
    }
  }

  function revokeProductObjectUrls() {
    Object.values(productObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
    productObjectUrlsRef.current = {};
  }

  function bumpRefVersion(mode = creationMode) {
    const next = Number(refVersionRef.current[mode] || 0) + 1;
    refVersionRef.current[mode] = next;
    return next;
  }

  function isRefVersionCurrent(mode, version) {
    return Number(refVersionRef.current[mode] || 0) === version;
  }

  function isModeVisible(mode) {
    return creationModeRef.current === mode;
  }

  function bumpRequest(ref, mode = creationMode) {
    const next = Number(ref.current[mode] || 0) + 1;
    ref.current[mode] = next;
    return next;
  }

  function isRequestCurrent(ref, mode, id) {
    return Number(ref.current[mode] || 0) === id;
  }

  function selectAssetForMode(asset, mode = creationMode) {
    const targetMode = asset.type === "video" && mode === "image" ? "video" : mode;
    const previousSignature = assetSignature(selectedByModeRef.current[targetMode]);
    const nextSignature = assetSignature(asset);
    selectedByModeRef.current[targetMode] = asset;
    setWorkspacePatch((current) => ({
      selected: asset,
      ...(previousSignature !== nextSignature
        ? {
            structured: {},
            structuredSource: "",
            ...(current.negativeTouched ? {} : { negative: "" }),
          }
        : {}),
    }), targetMode);
    if (previousSignature !== nextSignature) {
      bumpRequest(reverseRequestRef, targetMode);
      setWorkspacePatch({ reversing: false }, targetMode);
    }
    const dims = assetDims(asset);
    if (targetMode !== mode) setCreationMode(targetMode);
    if (asset.type === "video" && dims) {
      updateWorkspaceField("ratio", nearestRatio(dims.width, dims.height, videoRatioOptions()), targetMode);
    } else if (dims) {
      updateWorkspaceField("ratio", nearestRatio(dims.width, dims.height), targetMode);
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitForParseResult(initial, refVersion, mode) {
    let current = initial;
    const startedAt = Date.now();
    while (current?.status === "queued" || current?.status === "running") {
      if (!isRefVersionCurrent(mode, refVersion)) return null;
      if (Date.now() - startedAt > PARSE_POLL_TIMEOUT_MS) {
        throw new Error("抓取仍在处理中，请稍后重试");
      }
      await sleep(PARSE_POLL_INTERVAL_MS);
      if (!isRefVersionCurrent(mode, refVersion)) return null;
      current = await api.parseStatus(current.id);
    }
    return current;
  }

  async function doParse() {
    if (!url.trim() || parsing) return;
    const mode = creationMode;
    const targetUrl = url.trim();
    const reqId = bumpRequest(parseRequestRef, mode);
    const refVersion = bumpRefVersion(mode);
    bumpRequest(reverseRequestRef, mode);
    revokeUploadedObjectUrls(mode);
    selectedByModeRef.current[mode] = null;
    setMsg("");
    setWorkspacePatch((current) => ({
      assets: [],
      selected: null,
      structured: {},
      structuredSource: "",
      ...(current.negativeTouched ? {} : { negative: "" }),
    }), mode);
    setWorkspacePatch({ parsing: true }, mode);
    setRefOpen(true);
    try {
      const first = await api.parse(targetUrl);
      const r = await waitForParseResult(first, refVersion, mode);
      if (!isRefVersionCurrent(mode, refVersion)) return;
      if (!r) return;
      if (r.status === "failed") throw new Error(r.error || "抓取失败，请稍后重试或更换链接");
      if (r.status !== "done") throw new Error("抓取状态异常，请稍后重试");
      setWorkspacePatch({ assets: r.assets || [] }, mode);
      if (!r.assets?.length && isModeVisible(mode)) setMsg("未在该页面发现可用素材");
    } catch (e) {
      if (isRefVersionCurrent(mode, refVersion) && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(parseRequestRef, mode, reqId)) setWorkspacePatch({ parsing: false }, mode);
    }
  }

  async function doUploadImage(file) {
    if (!file || uploading) return;
    const mode = creationMode;
    if (!file.type?.startsWith("image/")) {
      setMsg("请选择图片文件");
      if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
      return;
    }
    if (uploadLimitExceeded(file, "image")) {
      if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, mode);
    const refVersion = bumpRefVersion(mode);
    bumpRequest(reverseRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true }, mode);
    try {
      const uploaded = await api.uploadImage(file);
      if (!isRefVersionCurrent(mode, refVersion)) return;
      const previewUrl = URL.createObjectURL(file);
      rememberUploadedObjectUrl(mode, previewUrl);
      const displayAsset = {
        ...uploaded,
        display_url: previewUrl,
        display_thumb: previewUrl,
      };
      setWorkspacePatch((current) => ({
        assets: [displayAsset, ...current.assets],
      }), mode);
      selectAssetForMode(displayAsset, mode);
      setRefOpen(true);
    } catch (e) {
      if (isRefVersionCurrent(mode, refVersion) && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false }, mode);
        if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
      }
    }
  }

  async function doUploadProductImage(file) {
    if (!file || uploading) return;
    const mode = creationMode;
    if (!file.type?.startsWith("image/")) {
      setMsg("请选择产品图片文件");
      if (productUploadInputRef.current) productUploadInputRef.current.value = "";
      return;
    }
    if (uploadLimitExceeded(file, "image")) {
      if (productUploadInputRef.current) productUploadInputRef.current.value = "";
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, mode);
    const productReqId = bumpRequest(productUploadRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true }, mode);
    try {
      const uploaded = await api.uploadImage(file);
      if (!isRequestCurrent(productUploadRequestRef, mode, productReqId)) return;
      const previewUrl = URL.createObjectURL(file);
      if (!isRequestCurrent(productUploadRequestRef, mode, productReqId)) {
        URL.revokeObjectURL(previewUrl);
        return;
      }
      revokeProductObjectUrl(mode);
      productObjectUrlsRef.current[mode] = previewUrl;
      setWorkspacePatch({
        productAsset: {
          ...uploaded,
          display_url: previewUrl,
          display_thumb: previewUrl,
        },
      }, mode);
      setRefOpen(true);
    } catch (e) {
      if (isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false }, mode);
        if (productUploadInputRef.current) productUploadInputRef.current.value = "";
      }
    }
  }

  async function doUploadVideo(file) {
    if (!file || uploading) return;
    const mode = creationMode;
    const targetMode = !isEditMode && category !== "video" ? "video" : mode;
    if (!file.type?.startsWith("video/")) {
      setMsg("请选择视频文件");
      if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
      return;
    }
    if (uploadLimitExceeded(file, "video")) {
      if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, mode);
    const refVersion = bumpRefVersion(targetMode);
    bumpRequest(reverseRequestRef, targetMode);
    setMsg("");
    setWorkspacePatch({ uploading: true }, mode);
    try {
      const uploaded = await api.uploadVideo(file);
      if (!isRefVersionCurrent(targetMode, refVersion)) return;
      const previewUrl = URL.createObjectURL(file);
      rememberUploadedObjectUrl(targetMode, previewUrl);
      const displayAsset = {
        ...uploaded,
        display_url: previewUrl,
        display_thumb: uploaded.thumb || previewUrl,
      };
      setWorkspacePatch((current) => ({
        assets: [displayAsset, ...current.assets],
      }), targetMode);
      selectAssetForMode(displayAsset, targetMode);
      if (targetMode !== mode) setCreationMode(targetMode);
      setRefOpen(true);
    } catch (e) {
      if (isRefVersionCurrent(targetMode, refVersion) && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false }, mode);
        if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
      }
    }
  }

  function pickAsset(a) {
    selectAssetForMode(a, creationMode);
  }

  function clearProductAsset() {
    bumpRequest(productUploadRequestRef, creationMode);
    revokeProductObjectUrl(creationMode);
    setWorkspacePatch({ productAsset: null });
    if (productUploadInputRef.current) productUploadInputRef.current.value = "";
  }

  async function doReverse() {
    if (!selected) return;
    const mode = creationMode;
    const targetCategory = category;
    const targetVideoPreset = videoAnalysisPreset;
    const targetNegativeTouched = negativeTouched;
    const target = selected;
    const targetSignature = assetSignature(target);
    const reqId = bumpRequest(reverseRequestRef, mode);
    const isCurrent = () => (
      isRequestCurrent(reverseRequestRef, mode, reqId)
      && assetSignature(selectedByModeRef.current[mode]) === targetSignature
    );
    setWorkspacePatch({ reversing: true }, mode);
    setMsg("");
    try {
      const isVideo = target.type === "video";
      const refUrl = isVideo ? target.url || target.thumb : target.url;
      const reverseTarget = isVideo ? "video" : targetCategory;
      const r = await api.reverse(
        refUrl,
        reverseTarget,
        isVideo ? target.thumb : null,
        target.type,
        isVideo ? targetVideoPreset : null,
      );
      if (!isCurrent()) return;
      const s = r.structured || {};
      setWorkspacePatch({
        structured: s,
        structuredSource: targetSignature,
        prompt: composePromptFromStructured(s, r.final_text || ""),
        promptDirty: false,
        ...(s["负向"] && !targetNegativeTouched ? { negative: s["负向"] } : {}),
      }, mode);
      setStructOpen(true);
      if (s["负向"] && !targetNegativeTouched && isModeVisible(mode)) setShowNegative(true);
      if (typeof r.charged_credits === "number") {
        const suffix = r.reference_count > 1 ? `（${r.reference_count} 帧）` : "";
        if (isModeVisible(mode)) setMsg(`反推完成，已扣 ${r.charged_credits} 积分${suffix}`);
      }
      refreshMe();
    } catch (e) {
      if (isCurrent() && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(reverseRequestRef, mode, reqId)) setWorkspacePatch({ reversing: false }, mode);
    }
  }

  function clearRef() {
    const mode = creationMode;
    bumpRefVersion(mode);
    bumpRequest(parseRequestRef, mode);
    bumpRequest(uploadRequestRef, mode);
    bumpRequest(reverseRequestRef, mode);
    revokeUploadedObjectUrls(mode);
    if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
    if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
    selectedByModeRef.current[mode] = null;
    setWorkspacePatch({
      selected: null,
      assets: [],
      url: "",
      parsing: false,
      uploading: false,
      reversing: false,
      structured: {},
      structuredSource: "",
      ...(negativeTouched ? {} : { negative: "" }),
    }, mode);
  }

  // rebuild the prompt text from the (possibly edited) reverse dimensions
  function recompose() {
    setPrompt(
      isEditMode
        ? composeStyleTransferPrompt(structured, prompt, { video: category === "video" })
        : composePromptFromStructured(structured, prompt)
    );
    setPromptDirty(false);
  }

  function productEditPrompt(baseText, { video = false, hasStyleReference = false, generalEdit = false } = {}) {
    const base = String(baseText || "").trim() || "生成同风格商业素材";
    const styleScope = hasStyleReference
      ? "风格参考只用于迁移场景、构图、镜头语言、光线、色调、材质、广告质感和氛围；不要迁移风格参考里的主体、人物、商品、品牌、Logo、包装、文字水印或促销文案。"
      : "";
    if (generalEdit) {
      return `以用户上传的图片作为唯一编辑源，严格按照用户提示词执行局部或整体编辑；保留未被要求修改的主体、构图、Logo、文字、颜色、比例和关键细节，不要无故替换主体或品牌。${styleScope}编辑要求：${base}`;
    }
    const guard = video
      ? "以用户上传的产品主体图作为视频首帧和唯一产品身份参考，必须完整保留产品主体、Logo、包装、颜色、形状、材质、比例、文字标识和品牌身份；产品表面像素视为锁定图层，不替换、不重绘、不改款、不改品牌；"
      : "以用户上传的产品图作为唯一产品身份和编辑源，必须完整保留同一 SKU 的主体、Logo、包装结构、品牌色、形状轮廓、材质纹理、比例、标签版式、文字标识和可识别细节；产品表面像素视为锁定图层，不替换、不重绘、不改款、不改品牌；";
    const fidelity = "包装上的品牌名、Logo、中文、英文、韩文、数字、装饰图案、标签位置和排版必须逐字逐形保持原图，不得翻译、改写、补写、删减、重排、风格化、模糊或替换；产品主体必须清晰锐利，边缘自然融入新场景；只允许迁移或生成背景、台面、道具、光线、构图、广告氛围和画面质感，不得把参考素材里的商品或品牌覆盖到产品上。若风格迁移和产品保真冲突，优先保证产品主体与包装文字完全不变。";
    return `${guard}${styleScope}${fidelity}生成广告级商业素材。迁移要求：${base}`;
  }

  function mergedNegativePrompt(value, { productMode = false } = {}) {
    const parts = String(value || "")
      .split(/[,，、\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (!isEditMode) return parts.join("，");
    const seen = new Set(parts.map((item) => item.toLowerCase()));
    for (const item of [
      ...EDIT_PROTECTION_NEGATIVE,
      ...(productMode ? PRODUCT_EDIT_NEGATIVE : []),
    ]) {
      if (!seen.has(item.toLowerCase())) parts.push(item);
    }
    return parts.join("，");
  }

  function applyLibraryPrompt(text, mode = "replace") {
    const next = String(text || "").trim();
    if (!next) return;
    setPrompt((current) => {
      if (mode !== "append" || !current.trim()) return next;
      return `${current.trim()}\n\n${next}`;
    });
    setPromptDirty(true);
  }

  // ---- generate ----
  function generateClientRequestId(stage, signature) {
    const existing = pendingGenerateRequestRef.current;
    if (existing?.stage === stage && existing?.signature === signature && existing?.id) return existing.id;
    try {
      const stored = JSON.parse(window.sessionStorage.getItem(PENDING_GENERATE_STORAGE_KEY) || "null");
      const ageMs = Date.now() - Number(stored?.createdAt || 0);
      if (
        stored?.stage === stage
        && stored?.signature === signature
        && stored?.id
        && ageMs >= 0
        && ageMs <= 2 * 60 * 60 * 1000
      ) {
        pendingGenerateRequestRef.current = stored;
        return stored.id;
      }
    } catch (e) {}
    const random = (
      typeof window !== "undefined" && window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    );
    const id = `studio-${stage}-${random}`;
    const record = { stage, signature, id, createdAt: Date.now() };
    pendingGenerateRequestRef.current = record;
    try {
      window.sessionStorage.setItem(PENDING_GENERATE_STORAGE_KEY, JSON.stringify(record));
    } catch (e) {}
    return id;
  }

  function clearPendingGenerateRequest(id = null) {
    if (!id || pendingGenerateRequestRef.current?.id === id) {
      pendingGenerateRequestRef.current = null;
    }
    try {
      const stored = JSON.parse(window.sessionStorage.getItem(PENDING_GENERATE_STORAGE_KEY) || "null");
      if (!id || stored?.id === id) window.sessionStorage.removeItem(PENDING_GENERATE_STORAGE_KEY);
    } catch (e) {
      try { window.sessionStorage.removeItem(PENDING_GENERATE_STORAGE_KEY); } catch (_e) {}
    }
  }

  async function submit(stage = "preview") {
    if (submitting) return;  // reentrancy guard: protects every caller incl. double-clicks + Ctrl+Enter
    if (shouldBlockNewGeneration(task, category)) {
      setMsg(category === "image" ? "" : "当前视频任务仍在生成中，请等待完成后再发起新的视频生成。");
      return;
    }
    const isFinal = stage === "final" && task;
    if (!isFinal) {
      if (isEditMode && !productAsset) {
        setMsg(isImageEditMode ? "请先上传要编辑的图片" : "请先上传产品主体图片");
        return;
      }
      if (isEditMode && !prompt.trim() && Object.keys(structured || {}).length === 0) {
        setMsg(isImageEditMode ? "请输入图片编辑要求" : "请先反推风格参考或输入希望迁移的风格提示词");
        return;
      }
      if (!isEditMode && !prompt.trim() && !selected) { setMsg("请输入提示词，或从参考反推"); return; }
    }
    setSubmitting(true); setMsg("");
    let requestId = null;
    try {
      // a final render belongs to its preview task — use ITS category, not the
      // current tab (the user may have switched tabs after the preview).
      const effCategory = isFinal ? task.category : category;
      if (!modelEnabled(effCategory)) {
        setMsg(`${effCategory === "video" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
        return;
      }
      const ratioPool = effCategory === "video" ? videoRatioOptions() : RATIOS;
      const rt = ratioPool.find((r) => r.key === ratio) || ratioPool[0];
      const imageSize = imageSizeFor(rt, imageQuality, cfg?.image_size_max_dim || 4096);
      const sourceAsset = isFinal ? null : (isEditMode ? productAsset : selected);
      const dims = sourceAsset ? assetDims(sourceAsset) : null;
      const refImage = sourceAsset ? (sourceAsset.type === "video" ? sourceAsset.thumb : sourceAsset.url) : null;
      const styleReferenceUrl = isEditMode && selected ? (selected.type === "video" ? selected.thumb : selected.url) : null;
      const editNegative = mergedNegativePrompt(negative, { productMode: productGenerationMode || creationMode === "video_edit" });
      const styleSignature = assetSignature(selected);
      const effectiveStructured = (
        structuredSource && structuredSource !== styleSignature
          ? {}
          : structured
      );
      const promptText = prompt.trim();
      const structuredText = isEditMode
        ? composeStyleTransferPrompt(effectiveStructured, "", { video: effCategory === "video" })
        : composePromptFromStructured(effectiveStructured);
      const baseFinalText = (
        promptDirty
          ? [structuredText, promptText].filter(Boolean).join("；")
          : (structuredText || promptText)
      ) || "生成同风格的新素材";
      const finalText = isEditMode
        ? productEditPrompt(baseFinalText, {
          video: effCategory === "video",
          hasStyleReference: Boolean(selected),
            generalEdit: creationMode === "image_edit" && !productGenerationMode,
          })
        : baseFinalText;
      // image reference chosen but not reversed -> send an instruction so the
      // worker actually feeds the reference image to the edit endpoint (图+指令→图),
      // instead of a plain text-to-image that ignores it.
      const useRefImage = !isFinal && sourceAsset && sourceAsset.type === "image" && (isEditMode || Object.keys(effectiveStructured).length === 0);
      const useRefVideo = !isFinal && sourceAsset && sourceAsset.type === "video" && Object.keys(effectiveStructured).length === 0;
      const sourceAssetMeta = sourceAsset ? {
        ...buildSourceAssetMeta(sourceAsset),
        mode: creationMode,
        product_generation_mode: productGenerationMode || creationMode === "video_edit",
        style_reference: selected ? buildSourceAssetMeta(selected) : null,
        product_subject: productAsset ? buildSourceAssetMeta(productAsset) : null,
      } : null;
      const payload = {
        source_asset_url: sourceAsset ? sourceAsset.url : null,
        source_type: sourceAsset ? sourceAsset.type : "image",
        source_asset_meta: sourceAssetMeta,
        category: effCategory,
        stage,
        parent_task_id: isFinal ? task.id : null,
        prompt: {
          ...(Object.keys(effectiveStructured).length ? effectiveStructured : {}),
          final_text: finalText,
          ...(useRefImage ? { instruction: finalText || prompt.trim() || "参考所选图生成同款风格的新素材" } : {}),
          ...(useRefVideo ? { instruction: prompt.trim() || "参考所选视频的主体、动作和镜头节奏生成同款视频" } : {}),
        },
        params:
          effCategory === "image"
            ? {
                n: boundedImageCount(n, cfg?.image_n_max || 8), size: imageSize,
                ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
                ...(seed !== "" ? { seed: Number(seed) } : {}),
                ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
                ...(editNegative ? { negative_prompt: editNegative } : {}),
              }
            : {
                duration: boundedVideoDuration(vDuration, cfg?.video_duration_max_seconds || 900),
                resolution: vResolution,
                target_resolution: vResolution,
                ratio: rt.key,
                ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
                ...(refImage ? { reference_image_url: refImage } : {}),
                ...(styleReferenceUrl ? { style_reference_image: styleReferenceUrl } : {}),
                ...(editNegative ? { negative_prompt: editNegative } : {}),
              },
      };
      payload.client_request_id = generateClientRequestId(stage, JSON.stringify(payload));
      requestId = payload.client_request_id;
      const t = await api.generate(payload);
      clearPendingGenerateRequest(requestId);
      const previousTask = task;
      if (
        previousTask
        && previousTask.id !== t.id
        && previousTask.category === "image"
        && !isTerminalTaskStatus(previousTask.status)
        && !backgroundTrackersRef.current.has(previousTask.id)
      ) {
        backgroundTrackersRef.current.set(previousTask.id, startBackgroundTracking(previousTask.id));
      }
      setTask(t);
      if (stage === "preview") setFinalTaskId(null);
      if (isFinal) setFinalTaskId(t.id);
      setTrackingLost(false);
      setRunningSnapshot({
        category: effCategory,
        n: effCategory === "image" ? Number(payload.params?.n || n || 1) : 1,
        ratio: rt,
      });
      refreshMe(); startTracking(t.id);
    } catch (e) {
      if (isRequestTimeoutError(e)) {
        setMsg(`${e.message}。任务可能已提交，重新点击会复用同一次请求，避免重复扣费。`);
      } else {
        clearPendingGenerateRequest(requestId);
        setMsg(e.message);
      }
    } finally { setSubmitting(false); }
  }

  async function startTracking(id) {
    // a new task takes over: stop any prior poll loop + socket and mark active id
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (wsRef.current) { try { wsRef.current.close(); } catch (e) {} }
    activeIdRef.current = id;
    setTrackingLost(false);
    let done = false;
    try {
      const { ticket } = await api.taskWsTicket(id);
      if (activeIdRef.current !== id) return;
      if (!ticket) {
        startPolling(id);
        return;
      }
      const ws = new WebSocket(wsUrl(`/ws/tasks/${id}?ticket=${encodeURIComponent(ticket)}`));
      wsRef.current = ws;
      ws.onmessage = (ev) => {
        let d = null;
        try {
          d = JSON.parse(ev.data);
        } catch (e) {
          if (!done && wsRef.current === ws) startPolling(id);
          try { ws.close(); } catch (_e) {}
          return;
        }
        const terminal = isTerminalTaskStatus(d.status);
        setTask((p) => (
          p && p.id === id
            ? { ...p, status: d.status, progress: terminal ? 100 : d.percent, error: d.error || p.error }
            : p
        ));
        if (terminal) {
          setTrackingLost(false);
          done = true;
          api.task(id).then((t) => { if (activeIdRef.current === id) setTask(t); }).catch(() => {});
          refreshMe(); loadWorks();
          try { ws.close(); } catch (e) {}
        }
      };
      // Any drop BEFORE a terminal status -> fall back to polling, but only if
      // this is still the active socket (a newer task may have replaced it).
      ws.onerror = () => { if (!done && wsRef.current === ws) startPolling(id); };
      ws.onclose = () => { if (!done && wsRef.current === ws) startPolling(id); };
    } catch (e) { startPolling(id); }
  }

  function startBackgroundTracking(id) {
    let stopped = false;
    let timer = null;
    let failures = 0;
    const stop = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    const schedule = () => {
      if (!stopped) timer = setTimeout(tick, 3000);
    };
    const tick = async () => {
      try {
        const t = await api.task(id);
        if (stopped) return;
        failures = 0;
        if (isTerminalTaskStatus(t.status)) {
          stop();
          backgroundTrackersRef.current.delete(id);
          refreshMe();
          loadWorks();
          return;
        }
      } catch (e) {
        failures += 1;
        if (failures >= 5) {
          stop();
          backgroundTrackersRef.current.delete(id);
          return;
        }
      }
      schedule();
    };
    schedule();
    return stop;
  }

  function startPolling(id) {
    if (pollRef.current) clearInterval(pollRef.current);
    let fails = 0;
    const interval = setInterval(async () => {
      // a newer task took over -> stop this stale loop, don't clobber its state
      if (activeIdRef.current !== id) {
        clearInterval(interval);
        if (pollRef.current === interval) pollRef.current = null;
        return;
      }
      try {
        const t = await api.task(id);
        fails = 0;
        if (activeIdRef.current !== id) return;
        if (isTerminalTaskStatus(t.status)) {
          setTask({ ...t, progress: 100 });
          setTrackingLost(false);
          clearInterval(interval);
          if (pollRef.current === interval) pollRef.current = null;
          refreshMe(); loadWorks();
        } else {
          setTask(t);
          setTrackingLost(false);
        }
      } catch (e) {
        // tolerate transient errors; only give up after several in a row
        if (++fails >= 5) {
          clearInterval(interval);
          if (pollRef.current === interval) pollRef.current = null;
          setTrackingLost(true);
          setMsg("无法获取任务进度,请稍后刷新页面查看结果。");
        }
      }
    }, 1200);
    pollRef.current = interval;
  }

  async function unlock(asset) {
    if (busyAssetIdsRef.current.has(asset.id)) return;
    const cost = Number(asset.unlock_cost ?? cfg?.models?.[asset.type]?.unlock_cost ?? 0);
    const balance = Number(me?.balance_credits ?? 0);
    if (!window.confirm(`解锁${asset.type === "video" ? "视频" : "图片"}高清将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`)) return;
    busyAssetIdsRef.current.add(asset.id);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      const updated = await api.unlock(asset.id);
      if (task) api.task(task.id).then(setTask).catch(() => {});
      if (lightbox && lightbox.id === asset.id) setLightbox(updated);
      refreshMe(); loadWorks();
    } catch (e) {
      setMsg(e.message);
    } finally {
      busyAssetIdsRef.current.delete(asset.id);
      setBusyAssetIds(new Set(busyAssetIdsRef.current));
    }
  }

  async function download(asset) {
    if (busyAssetIdsRef.current.has(asset.id)) return;
    busyAssetIdsRef.current.add(asset.id);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      await downloadBlob(
        `/api/assets/${asset.id}/download`,
        asset.type === "video" ? `asset-${asset.id}.mp4` : undefined,
      );
    } catch (e) {
      setMsg(e.message);
    } finally {
      busyAssetIdsRef.current.delete(asset.id);
      setBusyAssetIds(new Set(busyAssetIdsRef.current));
    }
  }

  async function report(asset) {
    const reason = window.prompt("举报原因: copyright / sensitive / illegal / privacy / other", "copyright");
    if (!reason) return;
    const normalized = reason.trim();
    if (!["copyright", "sensitive", "illegal", "privacy", "other"].includes(normalized)) {
      setMsg("举报原因只支持 copyright / sensitive / illegal / privacy / other");
      return;
    }
    const note = window.prompt("补充说明（可选）", "") || "";
    try {
      await api.reportAsset(asset.id, { reason: normalized, note });
      setMsg("举报已提交，管理员会在后台处理。");
    } catch (e) { setMsg(e.message); }
  }

  async function refreshActiveTask() {
    if (!task?.id) return;
    setMsg("");
    try {
      const t = await api.task(task.id);
      setTask(t);
      setTrackingLost(false);
      if (!isTerminalTaskStatus(t.status)) startTracking(t.id);
      else { refreshMe(); loadWorks(); }
    } catch (e) {
      setMsg(e.message);
    }
  }

  const activeNonTerminalTask = Boolean(task && !isTerminalTaskStatus(task.status));
  const blockingGeneration = shouldBlockNewGeneration(task, category);
  const running = blockingGeneration;
  const showRunningProgress = activeNonTerminalTask && !trackingLost;
  const unitCost = cfg?.models?.[category]?.cost_credits || 0;
  const videoPreviewCost = cfg?.models?.video?.preview_cost ?? Math.max(1, Math.floor((cfg?.models?.video?.cost_credits || 0) / 10));
  const videoFinalCost = cfg?.models?.video?.final_cost ?? cfg?.models?.video?.cost_credits ?? 0;
  const reverseCost = cfg?.models?.vision?.cost_credits || 0;
  const reverseImageCost = cfg?.reverse?.image_cost ?? reverseCost;
  const reverseVideoPresets = Array.isArray(cfg?.reverse?.video_presets) ? cfg.reverse.video_presets : [];
  const reverseVideoPreset = reverseVideoPresets.find((p) => p.key === videoAnalysisPreset) || reverseVideoPresets[0] || null;
  const reverseVideoFrameCount = Number(reverseVideoPreset?.max_frames || cfg?.reverse?.video_frame_count || 1);
  const reverseVideoMaxCost = reverseVideoPreset?.max_cost ?? cfg?.reverse?.video_max_cost ?? (reverseCost * reverseVideoFrameCount);
  const selectedReverseCost = selected?.type === "video" ? reverseVideoMaxCost : reverseImageCost;
  const selectedReverseCostLabel = selected?.type === "video" && reverseVideoFrameCount > 1
    ? `${selectedReverseCost}积分(最多${reverseVideoFrameCount}帧)`
    : `${selectedReverseCost}积分`;
  const reverseEnabled = cfg?.features?.reverse_prompt_enabled !== false; // admin switch
  const ratioOptions = category === "video" ? videoRatioOptions() : RATIOS;
  const rt = ratioOptions.find((r) => r.key === ratio) || ratioOptions[0];
  const maxImageN = Number(cfg?.image_n_max || 8);
  const imageCount = boundedImageCount(n, maxImageN);
  const maxVideoDuration = Number(cfg?.video_duration_max_seconds || 900);
  const videoDuration = boundedVideoDuration(vDuration, maxVideoDuration);
  const currentImageSize = imageSizeFor(rt, imageQuality, cfg?.image_size_max_dim || 4096);
  const estCost = category === "image" ? unitCost * imageCount : videoPreviewCost;
  const currentModelEnabled = modelEnabled(creationMode);
  const currentGatewayMock = cfg?.gateways?.[category]?.mock_mode ?? cfg?.mock_mode;
  const gatewayStatus = cfg === null
    ? "检测生成网关中"
    : !currentModelEnabled
      ? `${creationModeLabel(creationMode)}模型未启用`
      : currentGatewayMock
        ? `${creationModeLabel(creationMode)}演示模式 · 占位素材`
        : `${creationModeLabel(creationMode)}网关已连接`;

  const promptPlaceholder = isEditMode
    ? (isImageEditMode
        ? (productGenerationMode
            ? "描述产品图要生成成什么商业素材：电商主图、小红书种草、广告场景、背景光线、卖点氛围… 产品身份会强保护。⌘/Ctrl + Enter 生成"
            : "描述要如何编辑这张图：替换背景、增加文案、调整光线、改变风格、保留主体细节… ⌘/Ctrl + Enter 生成")
        : `${creationModeLabel(creationMode)}：先在右侧选择风格参考并反推，再上传产品主体图；这里可补充必须保留或强化的卖点…`)
    : category === "video"
      ? "描述你想要的视频：主体 / 动作 / 镜头运动 / 光线 / 节奏… ⌘/Ctrl + Enter 生成"
      : "描述你想要的画面：主体 / 风格 / 光线 / 色调 / 构图… ⌘/Ctrl + Enter 生成";
  const editStyleKeys = (EDIT_STYLE_KEYS[category] || EDIT_STYLE_KEYS.image)
    .filter((key) => String(structured?.[key] || "").trim());
  const editReadySteps = isImageEditMode
    ? [
        { label: productGenerationMode ? "产品图" : "编辑源", ready: Boolean(productAsset), readyText: "已上传", pendingText: "待上传" },
        { label: productGenerationMode ? "生产要求" : "编辑要求", ready: Boolean(prompt.trim() || editStyleKeys.length), readyText: "已填写", pendingText: "待填写" },
        { label: "风格参考", ready: Boolean(selected), readyText: "已选择", pendingText: "可选" },
      ]
    : [
        { label: "风格参考", ready: Boolean(selected), readyText: "已选择", pendingText: "待选择" },
        { label: "产品主体", ready: Boolean(productAsset), readyText: "已上传", pendingText: "待上传" },
        { label: "风格提示", ready: Boolean(prompt.trim() || editStyleKeys.length), readyText: "已就绪", pendingText: "待补充" },
      ];
  const submitLabel = submitting || running
    ? "生成中…"
    : creationMode === "video"
      ? "生成预览 ▶"
        : creationMode === "video_edit"
          ? "编辑视频 ▶"
        : creationMode === "image_edit"
          ? (productGenerationMode ? "产品生成 ✦" : "编辑生成 ✦")
          : "立即生成 ✦";

  function renderSubmitBar(variant = "desktop") {
    const isMobile = variant === "mobile";
    return (
      <div
        className={isMobile
          ? "fixed inset-x-0 bottom-0 z-30 flex min-w-0 items-center justify-between gap-3 border-t border-line bg-base/90 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-pop backdrop-blur-xl lg:hidden"
          : "mt-3 hidden min-w-0 items-center justify-between gap-3 px-1 lg:flex"
        }
      >
        <p className="min-w-0 text-xs leading-snug text-fog">
          {estCost ? (
            <>预计消耗 <b className="text-mist">{category === "video" ? `预览 ${videoPreviewCost} · 完整 ${videoFinalCost} · ${formatDuration(videoDuration)} · ${vResolution}` : estCost}</b> 积分{category === "image" ? " · 可连续提交" : ""}</>
          ) : "提交后冻结预估积分"}
        </p>
        <button
          onClick={() => submit("preview")}
          disabled={generationSubmitDisabled({ submitting, currentTask: task, nextCategory: category, currentModelEnabled })}
          className="btn-primary btn-lg min-w-28 shrink-0 px-4 sm:min-w-32 sm:px-6"
        >
          {(submitting || running) && (
            <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-white/35 border-t-white" aria-hidden />
          )}
          {submitLabel}
        </button>
      </div>
    );
  }

  function renderGenerationControls() {
    const controlCardClass = "min-w-0 rounded-xl border border-line bg-white/[0.03] px-3 py-2";
    const controlLabelClass = "block shrink-0 text-xs leading-none text-fog";
    const scrollPillRowClass = "no-scrollbar flex min-w-0 gap-1 overflow-x-auto pb-1";
    return (
      <>
        <div className="grid min-w-0 gap-3 px-1 py-1">
          <div className="min-w-0">
            <span className="text-xs text-fog">比例</span>
            <div className="no-scrollbar mt-1 flex gap-1 overflow-x-auto pb-1">
              {ratioOptions.map((r) => (
                <button
                  key={r.key}
                  onClick={() => setRatio(r.key)}
                  title={r.hint}
                  className={`group flex shrink-0 flex-col items-center gap-1 rounded-lg border px-2 py-1.5 transition ${
                    ratio === r.key ? "border-iris bg-iris/15" : "border-line hover:border-line2"
                  }`}
                >
                  <span
                    className={`block rounded-[3px] ${ratio === r.key ? "bg-iris-400" : "bg-fog"}`}
                    style={{ width: 18 * (r.w >= r.h ? 1 : r.w / r.h), height: 18 * (r.h >= r.w ? 1 : r.h / r.w) }}
                  />
                  <span className={`text-[10px] ${ratio === r.key ? "text-snow" : "text-fog"}`}>{r.label}</span>
                </button>
              ))}
            </div>
          </div>

          {category === "image" ? (
            <>
              <div className="grid min-w-0 gap-3 sm:grid-cols-2">
                <div className={controlCardClass}>
                  <div className="flex min-w-0 items-center justify-between gap-3">
                    <span className={controlLabelClass}>质量</span>
                    <span className="min-w-0 truncate text-right text-xs text-fog">{currentImageSize}</span>
                  </div>
                  <div className={`${scrollPillRowClass} mt-2`}>
                    {IMAGE_QUALITY_PRESETS.map((q) => (
                      <button
                        key={q.key}
                        onClick={() => setImageQuality(q.key)}
                        title={q.hint}
                        className={`chip shrink-0 ${imageQuality === q.key ? "chip-active" : ""}`}
                      >
                        {q.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className={controlCardClass}>
                  <div className="flex min-w-0 items-center justify-between gap-3">
                    <span className={controlLabelClass}>数量</span>
                    <span className="text-xs text-fog">最多 {maxImageN} 张</span>
                  </div>
                  <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_4.5rem] items-center gap-2">
                    <div className={scrollPillRowClass}>
                      {[1, 2, 4, 8].filter((v) => v <= maxImageN).map((v) => (
                        <button
                          key={v}
                          onClick={() => setN(v)}
                          className={`chip shrink-0 ${imageCount === v ? "chip-active" : ""}`}
                        >
                          {v}
                        </button>
                      ))}
                    </div>
                    <input
                      className="input h-[38px] min-w-0 px-2 py-1 text-center text-xs"
                      type="number"
                      min="1"
                      max={maxImageN}
                      value={n}
                      onChange={(e) => setN(e.target.value)}
                      onBlur={() => setN(boundedImageCount(n, maxImageN))}
                      title={`最多 ${maxImageN} 张`}
                    />
                  </div>
                </div>
              </div>
            </>
          ) : (
            <>
              <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
                <div className={controlCardClass}>
                  <div className="flex min-w-0 items-center justify-between gap-3">
                    <span className={controlLabelClass}>时长</span>
                    <span className="text-xs text-fog">最长 {formatDuration(maxVideoDuration)}</span>
                  </div>
                  <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_5.5rem] items-center gap-2">
                    <div className={scrollPillRowClass}>
                      {VIDEO_DURATION_PRESETS.filter((p) => p.seconds <= maxVideoDuration).map((p) => (
                        <button
                          key={p.seconds}
                          onClick={() => setVDuration(p.seconds)}
                          title={p.hint}
                          className={`chip shrink-0 ${videoDuration === p.seconds ? "chip-active" : ""}`}
                        >
                          {p.label}
                        </button>
                      ))}
                    </div>
                    <input
                      className="input h-[38px] min-w-0 px-2 py-1 text-center text-xs"
                      type="number"
                      min="1"
                      max={maxVideoDuration}
                      value={vDuration}
                      onChange={(e) => setVDuration(e.target.value)}
                      onBlur={() => setVDuration(boundedVideoDuration(vDuration, maxVideoDuration))}
                      title={`最长 ${formatDuration(maxVideoDuration)}`}
                    />
                  </div>
                </div>
                <div className={controlCardClass}>
                  <span className={controlLabelClass}>质量</span>
                  <div className={`${scrollPillRowClass} mt-2`}>
                    {VIDEO_QUALITIES.map((q) => (
                      <button
                        key={q.key}
                        onClick={() => setVResolution(q.key)}
                        title={q.hint}
                        className={`chip shrink-0 ${vResolution === q.key ? "chip-active" : ""}`}
                      >
                        {q.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            </>
          )}

          <div className="grid min-w-0 gap-2 sm:grid-cols-[auto_minmax(12rem,20rem)]">
            <button onClick={() => setShowNegative((s) => !s)} className={`chip justify-center ${showNegative ? "chip-active" : ""}`}>
              负向词
            </button>
            {category === "image" && (
              <label className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-full border border-line bg-white/[0.03] px-3 py-1.5 text-xs text-fog">
                <span className="shrink-0">Seed</span>
                <input className="input h-[34px] min-w-0 rounded-full px-3 py-1 text-xs" placeholder="随机" value={seed}
                  onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))} />
              </label>
            )}
          </div>
        </div>

        {showNegative && (
          <input className="input mt-2" placeholder="不想出现的元素：文字, 水印, 多余手指, 畸变…"
            value={negative} onChange={(e) => { setNegative(e.target.value); setNegativeTouched(true); }} />
        )}

        {renderSubmitBar("desktop")}
      </>
    );
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} active="studio" />

      <main className="mx-auto max-w-7xl overflow-x-hidden px-3 pb-28 pt-7 sm:px-6 sm:pb-24 sm:pt-10">
        {/* hero */}
        <section className="mx-auto mb-6 max-w-3xl text-center animate-fadeup sm:mb-8">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-line bg-white/5 px-3 py-1 text-xs text-mist">
            <span className="h-1.5 w-1.5 rounded-full bg-aqua animate-glowpulse" />
            {gatewayStatus}
          </div>
          <h1 className="text-3xl font-extrabold leading-tight sm:text-5xl">
            一句话，<span className="text-grad">生成你的画面</span>
          </h1>
          <p className="mt-3 text-[15px] text-mist">
            输入提示词即刻生成，或用风格参考 + 产品主体做同款广告素材。
          </p>
        </section>

        {/* creation console */}
        <section className="mx-auto max-w-5xl min-w-0 lg:animate-fadeup">
          <div className="panel min-w-0 p-2.5">
            {/* creation mode tabs */}
            <div className="mb-2.5 grid grid-cols-2 gap-1 rounded-2xl border border-line bg-base2/50 p-1 text-sm sm:grid-cols-4">
              {CREATION_MODES.map(({ key, label, icon }) => {
                const k = key;
                const disabled = !modelEnabled(k);
                return (
                <button
                  key={k}
                  onClick={() => switchCreationMode(k)}
                  disabled={disabled}
                  title={disabled ? "模型未启用，请联系管理员配置" : ""}
                  className={`rounded-xl px-3 py-2 font-display font-medium transition-all ${
                    disabled
                      ? "cursor-not-allowed text-fog opacity-45"
                      : creationMode === k ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:text-snow"
                  }`}
                >
                  <span className="mr-1.5">{icon}</span>{label}
                </button>
                );
              })}
            </div>
            {!currentModelEnabled && (
              <p className="mb-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                当前{creationModeLabel(creationMode)}模型未启用，请管理员在后台模型配置中启用后再生成。
              </p>
            )}

            {/* prompt + reference */}
            <div className="grid min-w-0 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-w-0 rounded-xl3 border border-line bg-base2/40 p-3 lg:self-start">
                {isEditMode ? (
                  <div className="grid gap-3">
                    <div className="rounded-2xl border border-line2 bg-gradient-to-br from-iris/15 via-white/[0.055] to-aqua/10 p-4">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <p className="text-xs font-display text-fog">编辑 Brief</p>
                          <h2 className="mt-1 text-xl font-display font-semibold text-snow">
                            {isImageEditMode
                              ? (productGenerationMode ? "上传产品图生成商业素材" : "上传图片后按提示词编辑")
                              : `${creationModeLabel(creationMode)}素材重构`}
                          </h2>
                        </div>
                        <span className="badge bg-brand-soft text-snow">
                          {category === "video" ? "视频编辑源" : (productGenerationMode ? "产品高保真" : "图片编辑源")}
                        </span>
                      </div>
                      {isImageEditMode && (
                        <div className="mt-4 grid grid-cols-2 gap-2 rounded-xl border border-line bg-base/35 p-1">
                          {[
                            { key: "general", label: "普通编辑", desc: "按提示修图" },
                            { key: "product", label: "产品生产", desc: "强保护Logo和包装" },
                          ].map((item) => {
                            const active = item.key === (productGenerationMode ? "product" : "general");
                            return (
                              <button
                                key={item.key}
                                type="button"
                                onClick={() => setImageEditProductMode(item.key === "product")}
                                className={`rounded-lg px-3 py-2 text-left transition ${
                                  active
                                    ? "border border-aqua/50 bg-aqua/15 text-snow shadow-glow-sm"
                                    : "border border-transparent text-fog hover:bg-white/[0.04] hover:text-mist"
                                }`}
                              >
                                <span className="block text-sm font-display font-semibold">{item.label}</span>
                                <span className="mt-0.5 block text-[11px] leading-snug">{item.desc}</span>
                              </button>
                            );
                          })}
                        </div>
                      )}
                      <div className="mt-4 grid gap-2 sm:grid-cols-3">
                        {editReadySteps.map((step) => (
                          <div
                            key={step.label}
                            className={`rounded-xl border px-3 py-2 ${
                              step.ready
                                ? "border-aqua/35 bg-aqua/10"
                                : "border-line bg-base/40"
                            }`}
                          >
                            <p className="text-[11px] text-fog">{step.label}</p>
                            <p className={`mt-1 text-sm font-display font-medium ${step.ready ? "text-aqua" : "text-mist"}`}>
                              {step.ready ? step.readyText : step.pendingText}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-2xl border border-line bg-base/35 p-3">
                      <div className="mb-2 flex items-center justify-between gap-3">
                        <label className="label m-0">补充要求 / 卖点 / 必须保留</label>
                        <span className="text-[11px] text-fog">⌘/Ctrl + Enter</span>
                      </div>
                      <textarea
                        className="textarea h-28 resize-none border-line bg-base2/60 text-[14px]"
                        placeholder={promptPlaceholder}
                        value={prompt}
                        onChange={(e) => { setPrompt(e.target.value); setPromptDirty(true); }}
                        onKeyDown={(e) => {
                          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit("preview"); }
                        }}
                      />
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {(isImageEditMode
                          ? (productGenerationMode
                              ? ["包装文字逐字保留", "保留包装和Logo", "生成电商主图", "小红书产品种草图", "替换广告场景", "产品边缘自然融入"]
                              : ["替换为干净棚拍背景", "保留主体和Logo", "增加商业广告光感", "调整为小红书封面", "去除杂乱背景"])
                          : EDIT_PROMPT_CHIPS
                        ).map((ex) => (
                          <button
                            key={ex}
                            type="button"
                            onClick={() => applyLibraryPrompt(ex, "append")}
                            className="chip"
                          >
                            {ex}
                          </button>
                        ))}
                        <button
                          type="button"
                          onClick={() => setPromptLibraryOpen((open) => !open)}
                          className={`chip ${promptLibraryOpen ? "chip-active" : ""}`}
                        >
                          提示词库
                        </button>
                      </div>
                    </div>

                    <div className="grid min-w-0 gap-3 xl:grid-cols-[1fr_1.15fr]">
                      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
                        <p className="text-xs font-display font-medium text-mist">迁移规则</p>
                        <div className="mt-3 space-y-2">
                          {(isImageEditMode
                            ? (productGenerationMode
                                ? [
                                    ["锁定产品身份", "SKU、Logo、包装结构、品牌色和表面文字不改"],
                                    ["迁移商业场景", "只生成背景、道具、构图、光线和广告质感"],
                                    ["高保真输出", "产品清晰锐利，包装文字逐字保留"],
                                  ]
                                : [
                                    ["按提示词编辑", "只改用户明确要求修改的部分"],
                                    ["保留源图细节", "主体、Logo、文字和比例默认保持"],
                                    ["可选参考增强", "可用右侧参考图迁移光线、构图和质感"],
                                  ])
                            : [
                                ["保留产品身份", "Logo、包装、颜色和形状不漂移"],
                                ["迁移参考气质", "只迁移场景、构图、光线和广告质感"],
                                ["输出商业素材", "产品清晰，边缘自然融入新场景"],
                              ]
                          ).map(([title, desc]) => (
                            <div key={title} className="rounded-xl border border-line bg-base/35 px-3 py-2">
                              <p className="text-sm font-display font-medium text-snow">{title}</p>
                              <p className="mt-0.5 text-xs text-fog">{desc}</p>
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
                        <div className="flex items-center justify-between gap-3">
                          <p className="text-xs font-display font-medium text-mist">已提取风格维度</p>
                          {editStyleKeys.length > 0 && (
                            <button type="button" onClick={recompose} className="chip px-2 py-0.5">
                              重组提示词
                            </button>
                          )}
                        </div>
                        {editStyleKeys.length > 0 ? (
                          <div className="mt-3 flex flex-wrap gap-1.5">
                            {editStyleKeys.slice(0, 12).map((key) => (
                              <span key={key} className="rounded-full border border-iris/30 bg-iris/10 px-2.5 py-1 text-xs text-mist">
                                {key}
                              </span>
                            ))}
                            {editStyleKeys.length > 12 && (
                              <span className="rounded-full border border-line bg-white/5 px-2.5 py-1 text-xs text-fog">
                                +{editStyleKeys.length - 12}
                              </span>
                            )}
                          </div>
                        ) : (
                          <div className="mt-3 rounded-xl border border-dashed border-line2 bg-base/25 px-3 py-4">
                            <p className="text-sm text-mist">右侧反推后会显示可迁移的风格维度。</p>
                            <p className="mt-1 text-xs text-fog">也可以直接在上方补充产品卖点后生成。</p>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                ) : (
                  <>
                    <textarea
                      className="textarea h-36 resize-none border-0 bg-transparent px-1 text-[15px] focus:ring-0 lg:h-40"
                      placeholder={promptPlaceholder}
                      value={prompt}
                      onChange={(e) => { setPrompt(e.target.value); setPromptDirty(true); }}
                      onKeyDown={(e) => {
                        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit("preview"); }
                      }}
                    />
                    {/* example chips */}
                    <div className="mt-1.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
                      {EXAMPLES.map((ex, i) => (
                        <button key={i} onClick={() => { setPrompt(ex); setPromptDirty(true); }} className="chip" title={ex}>
                          ✦ {ex.slice(0, 12)}…
                        </button>
                      ))}
                      <button
                        onClick={() => setPromptLibraryOpen((open) => !open)}
                        className={`chip ${promptLibraryOpen ? "chip-active" : ""}`}
                      >
                        提示词库
                      </button>
                      {prompt && (
                        <button
                          type="button"
                          onClick={() => { setPrompt(""); setPromptDirty(true); }}
                          className="chip ml-auto"
                          title="清空提示词"
                        >
                          ✕ 清空
                        </button>
                      )}
                    </div>
                  </>
                )}
                <div className="mt-3 border-t border-line pt-3">
                  {renderGenerationControls()}
                </div>
              </div>

              <StudioReferencePanel
                category={category}
                creationMode={creationMode}
                imageEditProductMode={imageEditProductMode}
                isEditMode={isEditMode}
                selected={selected}
                productAsset={productAsset}
                url={url}
                setUrl={setUrl}
                parsing={parsing}
                uploading={uploading}
                assets={assets}
                refOpen={refOpen}
                setRefOpen={setRefOpen}
                reversing={reversing}
                reverseEnabled={reverseEnabled}
                selectedReverseCost={selectedReverseCost}
                selectedReverseCostLabel={selectedReverseCostLabel}
                videoAnalysisPreset={videoAnalysisPreset}
                videoAnalysisPresets={reverseVideoPresets}
                setVideoAnalysisPreset={setVideoAnalysisPreset}
                imageUploadInputRef={imageUploadInputRef}
                productUploadInputRef={productUploadInputRef}
                videoUploadInputRef={videoUploadInputRef}
                onClear={clearRef}
                onClearProductAsset={clearProductAsset}
                onParse={doParse}
                onUploadImage={doUploadImage}
                onUploadProductImage={doUploadProductImage}
                onUploadVideo={doUploadVideo}
                onPickAsset={pickAsset}
                onReverse={doReverse}
              />
            </div>

            {promptLibraryOpen && (
              <PromptLibraryBrowser
                onClose={() => setPromptLibraryOpen(false)}
                onPrimary={(item) => applyLibraryPrompt(item.prompt, "replace")}
                onSecondary={(item) => applyLibraryPrompt(item.prompt, "append")}
              />
            )}

            {/* editable reverse dimensions (surfaced so the rich reverse isn't lost) */}
            {Object.keys(structured).length > 0 && (
              <div className="mt-2.5 rounded-xl3 border border-line bg-base2/40 animate-fadeup">
                <div className="flex items-center justify-between px-3 py-2">
                  <button onClick={() => setStructOpen((o) => !o)} className="flex items-center gap-2 text-xs font-display font-medium text-mist hover:text-snow">
                    <span className={`inline-block transition-transform ${structOpen ? "rotate-90" : ""}`}>▸</span>
                    反推维度 · {Object.keys(structured).length} 项（可逐项微调）
                  </button>
                  <div className="flex items-center gap-1.5">
                      <button type="button" onClick={recompose} className="chip" title="用当前维度重新拼接提示词">↻ 重组提示词</button>
                    <button type="button" onClick={clearRef} className="chip">清除风格参考</button>
                  </div>
                </div>
                {structOpen && (
                  <div className="grid grid-cols-1 gap-2 px-3 pb-3 sm:grid-cols-2">
                    {Object.keys(structured).map((k) => (
                      <div key={k}>
                        <label className="label mb-1 normal-case">{k}</label>
                        <input
                          className="input px-2.5 py-1.5 text-xs"
                          value={structured[k] || ""}
                          onChange={(e) => {
                            setStructured({ ...structured, [k]: e.target.value });
                            setPromptDirty(false);
                          }}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

          </div>

          {msg && (
            <div className="mt-3 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad shadow-pop max-lg:fixed max-lg:inset-x-3 max-lg:bottom-[76px] max-lg:z-30 max-lg:mt-0">{msg}</div>
          )}
        </section>

        <StudioResults
          task={task}
          runningSnapshot={runningSnapshot}
          showRunningProgress={showRunningProgress}
          trackingLost={trackingLost}
          finalTaskId={finalTaskId}
          submitting={submitting}
          videoFinalCost={videoFinalCost}
          works={works}
          lightbox={lightbox}
          setLightbox={setLightbox}
          busyAssetIds={busyAssetIds}
          onRefreshActiveTask={refreshActiveTask}
          onUnlock={unlock}
          onDownload={download}
          onReport={report}
          onSubmitFinal={() => submit("final")}
        />
      </main>
      {renderSubmitBar("mobile")}
    </div>
  );
}
