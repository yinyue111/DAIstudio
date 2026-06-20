"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, authenticatedObjectUrl, downloadBlob, getToken, wsUrl } from "../lib/api";
import Nav from "../components/Nav";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import AssetMedia, { assetPreviewSrc } from "../components/AssetMedia";

const RATIOS = [
  { key: "1:1", label: "1:1", hint: "头像 / 方图", w: 1, h: 1 },
  { key: "4:5", label: "4:5", hint: "社媒竖图", w: 4, h: 5 },
  { key: "5:4", label: "5:4", hint: "商品横图", w: 5, h: 4 },
  { key: "3:4", label: "3:4", hint: "竖版海报", w: 3, h: 4 },
  { key: "4:3", label: "4:3", hint: "横版构图", w: 4, h: 3 },
  { key: "2:3", label: "2:3", hint: "封面 / 写真", w: 2, h: 3 },
  { key: "3:2", label: "3:2", hint: "摄影横图", w: 3, h: 2 },
  { key: "9:16", label: "9:16", hint: "手机竖屏", w: 9, h: 16 },
  { key: "16:9", label: "16:9", hint: "宽屏视频封面", w: 16, h: 9 },
  { key: "21:9", label: "21:9", hint: "超宽横幅", w: 21, h: 9 },
  { key: "9:21", label: "9:21", hint: "长竖海报", w: 9, h: 21 },
];
const VIDEO_RATIO_KEYS = new Set(["1:1", "3:4", "4:3", "9:16", "16:9"]);

const IMAGE_QUALITY_PRESETS = [
  { key: "1k", label: "1K", hint: "快速预览", maxSide: 1024 },
  { key: "2k", label: "2K", hint: "均衡清晰", maxSide: 2048 },
  { key: "4k", label: "4K", hint: "最高质量", maxSide: 4096 },
];

const VIDEO_QUALITIES = [
  { key: "480p", label: "480p", hint: "快速预览" },
  { key: "720p", label: "720p", hint: "标准" },
  { key: "1080p", label: "1080p", hint: "高清" },
];
const VIDEO_DURATION_PRESETS = [
  { seconds: 5, label: "5s", hint: "短镜头预览" },
  { seconds: 8, label: "8s", hint: "平台常用短片" },
  { seconds: 10, label: "10s", hint: "完整短镜头" },
  { seconds: 15, label: "15s", hint: "广告片段" },
  { seconds: 30, label: "30s", hint: "短广告" },
  { seconds: 60, label: "1min", hint: "完整广告" },
  { seconds: 180, label: "3min", hint: "口播/种草" },
  { seconds: 300, label: "5min", hint: "长内容" },
  { seconds: 600, label: "10min", hint: "长视频" },
  { seconds: 900, label: "15min", hint: "最长" },
];
const TERMINAL_TASK_STATUSES = new Set(["succeeded", "failed", "needs_review"]);

const EXAMPLES = [
  "赛博朋克城市夜景，霓虹灯反射在湿漉漉的街道上，电影感，超广角",
  "一只穿宇航服的柴犬漂浮在太空，背景是绚丽星云，3D 渲染，皮克斯风格",
  "极简北欧风咖啡馆，晨光透过落地窗，暖色调，柔和景深",
  "国潮水墨山水，仙鹤掠过云海，金箔点缀，高级感海报",
];

export default function Home() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);

  // creation state
  const [category, setCategory] = useState("image"); // image | video
  const [prompt, setPrompt] = useState("");
  const [negative, setNegative] = useState("");
  const [showNegative, setShowNegative] = useState(false);
  const [ratio, setRatio] = useState("1:1");
  const [imageQuality, setImageQuality] = useState("1k");
  const [n, setN] = useState(4);
  const [seed, setSeed] = useState("");
  const [vDuration, setVDuration] = useState(5);
  const [vResolution, setVResolution] = useState("720p");
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);

  // reference (paste link -> reverse) state
  const [refOpen, setRefOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [parsing, setParsing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [assets, setAssets] = useState([]);
  const [selected, setSelected] = useState(null);
  const [reversing, setReversing] = useState(false);
  const [structured, setStructured] = useState({}); // editable reverse dimensions
  const [structuredSource, setStructuredSource] = useState("");
  const [structOpen, setStructOpen] = useState(true);
  const [negativeTouched, setNegativeTouched] = useState(false);
  const [promptDirty, setPromptDirty] = useState(false);

  // run state
  const [task, setTask] = useState(null);
  const [runningSnapshot, setRunningSnapshot] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState("");
  const [trackingLost, setTrackingLost] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [finalTaskId, setFinalTaskId] = useState(null);

  // gallery
  const [works, setWorks] = useState(null);

  const pollRef = useRef(null);
  const wsRef = useRef(null);
  const activeIdRef = useRef(null); // id of the task currently being tracked
  const selectedRef = useRef(null);
  const refVersionRef = useRef(0);
  const parseRequestRef = useRef(0);
  const uploadRequestRef = useRef(0);
  const reverseRequestRef = useRef(0);
  const pendingGenerateRequestRef = useRef(null);
  const uploadInputRef = useRef(null);
  const objectUrlsRef = useRef(new Set());

  useEffect(() => {
    if (!getToken()) return router.push("/login");
    api.me().then(setMe).catch(() => router.push("/login"));
    api.config().then((c) => {
      setCfg(c);
      // honour admin defaults so the values we submit match the backend config
      const d = c.defaults || {};
      if (d.image_n) setN(Number(d.image_n));
      if (d.image_size) {
        const rk = ratioKeyForSize(d.image_size);
        if (rk) setRatio(rk);
        setImageQuality(qualityKeyForSize(d.image_size));
      }
    }).catch(() => {});
    loadWorks();
    try {
      const draft = window.localStorage.getItem(STUDIO_DRAFT_PROMPT_KEY);
      if (draft) {
        setPrompt(draft);
        setPromptDirty(true);
        window.localStorage.removeItem(STUDIO_DRAFT_PROMPT_KEY);
      }
    } catch (e) {}
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (wsRef.current) try { wsRef.current.close(); } catch (e) {}
      revokeUploadedObjectUrls();
    };
  }, []);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((r) => r.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  function refreshMe() { api.me().then(setMe).catch(() => {}); }

  function modelEnabled(kind) {
    if (!cfg) return true;
    return cfg.models?.[kind]?.enabled !== false;
  }

  function switchCategory(kind) {
    if (!modelEnabled(kind)) {
      setMsg(`${kind === "video" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
      return;
    }
    setMsg("");
    setCategory(kind);
  }

  async function loadWorks() {
    try {
      const list = await api.tasks(20, 0);
      const flat = [];
      for (const t of list) {
        for (const a of t.assets || []) flat.push({ ...a, _cat: t.category });
      }
      setWorks(flat);
    } catch (e) { setWorks([]); }
  }

  // ---- reference: parse + reverse ----
  function revokeUploadedObjectUrls() {
    objectUrlsRef.current.forEach((u) => URL.revokeObjectURL(u));
    objectUrlsRef.current.clear();
  }

  function clearReverseState({ clearPrompt = false } = {}) {
    setStructured({});
    setStructuredSource("");
    if (!negativeTouched) setNegative("");
    if (clearPrompt) {
      setPrompt("");
      setPromptDirty(false);
    }
  }

  async function doParse() {
    if (!url.trim() || parsing) return;
    const reqId = ++parseRequestRef.current;
    const refVersion = ++refVersionRef.current;
    reverseRequestRef.current += 1;
    revokeUploadedObjectUrls();
    setMsg(""); setAssets([]); setSelected(null); clearReverseState(); setParsing(true);
    setRefOpen(true);
    try {
      const r = await api.parse(url.trim());
      if (refVersion !== refVersionRef.current) return;
      setAssets(r.assets || []);
      if (!r.assets?.length) setMsg("未在该页面发现可用素材");
    } catch (e) {
      if (refVersion === refVersionRef.current) setMsg(e.message);
    } finally {
      if (reqId === parseRequestRef.current) setParsing(false);
    }
  }

  async function doUploadImage(file) {
    if (!file || uploading) return;
    if (!file.type?.startsWith("image/")) {
      setMsg("请选择图片文件");
      if (uploadInputRef.current) uploadInputRef.current.value = "";
      return;
    }
    const reqId = ++uploadRequestRef.current;
    const refVersion = ++refVersionRef.current;
    reverseRequestRef.current += 1;
    setMsg("");
    setUploading(true);
    try {
      const uploaded = await api.uploadImage(file);
      if (refVersion !== refVersionRef.current) return;
      const previewUrl = URL.createObjectURL(file);
      objectUrlsRef.current.add(previewUrl);
      const displayAsset = {
        ...uploaded,
        display_url: previewUrl,
        display_thumb: previewUrl,
      };
      setAssets((current) => [displayAsset, ...current]);
      pickAsset(displayAsset);
      setCategory("image");
      clearReverseState();
      setRefOpen(true);
    } catch (e) {
      if (refVersion === refVersionRef.current) setMsg(e.message);
    } finally {
      if (reqId === uploadRequestRef.current) {
        setUploading(false);
        if (uploadInputRef.current) uploadInputRef.current.value = "";
      }
    }
  }

  function pickAsset(a) {
    const previousSignature = assetSignature(selectedRef.current);
    const nextSignature = assetSignature(a);
    setSelected(a);
    if (previousSignature !== nextSignature) {
      reverseRequestRef.current += 1;
      setReversing(false);
      clearReverseState();
    }
    const dims = assetDims(a);
    if (a.type === "video") {
      setCategory("video");
      if (dims) setRatio(nearestRatio(dims.width, dims.height, videoRatioOptions()));
    } else if (dims) {
      setRatio(nearestRatio(dims.width, dims.height));
    }
  }

  async function doReverse() {
    if (!selected) return;
    const target = selected;
    const targetSignature = assetSignature(target);
    const reqId = ++reverseRequestRef.current;
    const isCurrent = () => (
      reqId === reverseRequestRef.current
      && assetSignature(selectedRef.current) === targetSignature
    );
    setReversing(true); setMsg("");
    try {
      const isVideo = target.type === "video";
      const refUrl = isVideo ? target.url || target.thumb : target.url;
      const reverseTarget = isVideo ? "video" : category;
      const r = await api.reverse(refUrl, reverseTarget, isVideo ? target.thumb : null, target.type);
      if (!isCurrent()) return;
      const s = r.structured || {};
      setStructured(s);
      setStructuredSource(targetSignature);
      setPrompt(composePromptFromStructured(s, r.final_text || ""));
      setPromptDirty(false);
      setStructOpen(true);
      if (s["负向"] && !negativeTouched) {
        setNegative(s["负向"]);
        setShowNegative(true);
      }
      if (typeof r.charged_credits === "number") {
        const suffix = r.reference_count > 1 ? `（${r.reference_count} 帧）` : "";
        setMsg(`反推完成，已扣 ${r.charged_credits} 积分${suffix}`);
      }
      refreshMe();
    } catch (e) {
      if (isCurrent()) setMsg(e.message);
    } finally {
      if (reqId === reverseRequestRef.current) setReversing(false);
    }
  }

  function clearRef() {
    refVersionRef.current += 1;
    parseRequestRef.current += 1;
    uploadRequestRef.current += 1;
    reverseRequestRef.current += 1;
    revokeUploadedObjectUrls();
    setParsing(false);
    setUploading(false);
    setReversing(false);
    if (uploadInputRef.current) uploadInputRef.current.value = "";
    setSelected(null); clearReverseState(); setAssets([]); setUrl("");
  }

  // rebuild the prompt text from the (possibly edited) reverse dimensions
  function recompose() {
    setPrompt(composePromptFromStructured(structured, prompt));
    setPromptDirty(false);
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
    const random = (
      typeof window !== "undefined" && window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    );
    const id = `studio-${stage}-${random}`;
    pendingGenerateRequestRef.current = { stage, signature, id };
    return id;
  }

  async function submit(stage = "preview") {
    if (submitting) return;  // reentrancy guard: protects every caller incl. double-clicks + Ctrl+Enter
    if (!prompt.trim() && !selected) { setMsg("请输入提示词，或从参考反推"); return; }
    setSubmitting(true); setMsg("");
    try {
      // a final render belongs to its preview task — use ITS category, not the
      // current tab (the user may have switched tabs after the preview).
      const isFinal = stage === "final" && task;
      const effCategory = isFinal ? task.category : category;
      if (!modelEnabled(effCategory)) {
        setMsg(`${effCategory === "video" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
        return;
      }
      const ratioPool = effCategory === "video" ? videoRatioOptions() : RATIOS;
      const rt = ratioPool.find((r) => r.key === ratio) || ratioPool[0];
      const imageSize = imageSizeFor(rt, imageQuality, cfg?.image_size_max_dim || 4096);
      const dims = selected ? assetDims(selected) : null;
      const refImage = selected ? (selected.type === "video" ? selected.thumb : selected.url) : null;
      const effectiveStructured = (
        selected && structuredSource && structuredSource !== assetSignature(selected)
          ? {}
          : structured
      );
      const promptText = prompt.trim();
      const finalText = (
        promptDirty
          ? promptText
          : (composePromptFromStructured(effectiveStructured) || promptText)
      ) || "生成同风格的新素材";
      // image reference chosen but not reversed -> send an instruction so the
      // worker actually feeds the reference image to the edit endpoint (图+指令→图),
      // instead of a plain text-to-image that ignores it.
      const useRefImage = !isFinal && selected && selected.type === "image" && Object.keys(effectiveStructured).length === 0;
      const payload = {
        source_asset_url: selected ? selected.url : null,
        source_type: selected ? selected.type : "image",
        category: effCategory,
        stage,
        parent_task_id: isFinal ? task.id : null,
        prompt: {
          ...(Object.keys(effectiveStructured).length ? effectiveStructured : {}),
          final_text: finalText,
          ...(useRefImage ? { instruction: prompt.trim() || "参考所选图生成同款风格的新素材" } : {}),
        },
        params:
          effCategory === "image"
            ? {
                n: boundedImageCount(n, cfg?.image_n_max || 8), size: imageSize,
                ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
                ...(seed !== "" ? { seed: Number(seed) } : {}),
                ...(negative ? { negative_prompt: negative } : {}),
              }
            : {
                duration: boundedVideoDuration(vDuration, cfg?.video_duration_max_seconds || 900),
                resolution: vResolution,
                target_resolution: vResolution,
                ratio: rt.key,
                ...(dims ? { reference_width: dims.width, reference_height: dims.height } : {}),
                ...(refImage ? { reference_image_url: refImage } : {}),
                ...(negative ? { negative_prompt: negative } : {}),
              },
      };
      payload.client_request_id = generateClientRequestId(stage, JSON.stringify(payload));
      const t = await api.generate(payload);
      pendingGenerateRequestRef.current = null;
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
        pendingGenerateRequestRef.current = null;
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
    const cost = Number(asset.unlock_cost ?? cfg?.models?.[asset.type]?.unlock_cost ?? 0);
    const balance = Number(me?.balance_credits ?? 0);
    if (!window.confirm(`解锁${asset.type === "video" ? "视频" : "图片"}高清将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`)) return;
    try {
      const updated = await api.unlock(asset.id);
      if (task) api.task(task.id).then(setTask).catch(() => {});
      if (lightbox && lightbox.id === asset.id) setLightbox(updated);
      refreshMe(); loadWorks();
    } catch (e) { setMsg(e.message); }
  }

  async function download(asset) {
    try {
      await downloadBlob(`/api/assets/${asset.id}/download`, `asset-${asset.id}`);
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
  const running = activeNonTerminalTask;
  const showRunningProgress = activeNonTerminalTask && !trackingLost;
  const unitCost = cfg?.models?.[category]?.cost_credits || 0;
  const videoPreviewCost = cfg?.models?.video?.preview_cost ?? Math.max(1, Math.floor((cfg?.models?.video?.cost_credits || 0) / 10));
  const videoFinalCost = cfg?.models?.video?.final_cost ?? cfg?.models?.video?.cost_credits ?? 0;
  const reverseCost = cfg?.models?.vision?.cost_credits || 0;
  const reverseImageCost = cfg?.reverse?.image_cost ?? reverseCost;
  const reverseVideoFrameCount = Number(cfg?.reverse?.video_frame_count || 1);
  const reverseVideoMaxCost = cfg?.reverse?.video_max_cost ?? (reverseCost * reverseVideoFrameCount);
  const selectedReverseCost = selected?.type === "video" ? reverseVideoMaxCost : reverseImageCost;
  const selectedReverseCostLabel = selected?.type === "video" && reverseVideoFrameCount > 1
    ? `${selectedReverseCost}积分(最多${reverseVideoFrameCount}帧)`
    : `${selectedReverseCost}积分`;
  const reverseEnabled = cfg?.defaults?.reverse_prompt_enabled !== false; // admin switch
  const ratioOptions = category === "video" ? videoRatioOptions() : RATIOS;
  const rt = ratioOptions.find((r) => r.key === ratio) || ratioOptions[0];
  const maxImageN = Number(cfg?.image_n_max || 8);
  const imageCount = boundedImageCount(n, maxImageN);
  const maxVideoDuration = Number(cfg?.video_duration_max_seconds || 900);
  const videoDuration = boundedVideoDuration(vDuration, maxVideoDuration);
  const currentImageSize = imageSizeFor(rt, imageQuality, cfg?.image_size_max_dim || 4096);
  const estCost = category === "image" ? unitCost * imageCount : videoPreviewCost;
  const currentModelEnabled = modelEnabled(category);
  const currentGatewayMock = cfg?.gateways?.[category]?.mock_mode ?? cfg?.mock_mode;
  const gatewayStatus = cfg === null
    ? "检测生成网关中"
    : !currentModelEnabled
      ? `${category === "video" ? "视频" : "图片"}模型未启用`
      : currentGatewayMock
        ? `${category === "video" ? "视频" : "图片"}演示模式 · 占位素材`
        : `${category === "video" ? "视频" : "图片"}网关已连接`;

  return (
    <div className="min-h-screen">
      <Nav me={me} active="studio" />

      <main className="mx-auto max-w-7xl px-4 pb-24 pt-10 sm:px-6">
        {/* hero */}
        <section className="mx-auto mb-8 max-w-3xl text-center animate-fadeup">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-line bg-white/5 px-3 py-1 text-xs text-mist">
            <span className="h-1.5 w-1.5 rounded-full bg-aqua animate-glowpulse" />
            {gatewayStatus}
          </div>
          <h1 className="text-4xl font-extrabold leading-tight sm:text-5xl">
            一句话，<span className="text-grad">生成你的画面</span>
          </h1>
          <p className="mt-3 text-[15px] text-mist">
            输入提示词即刻成图，或粘贴参考链接，让 AI 理解风格并复刻同款素材。
          </p>
        </section>

        {/* creation console */}
        <section className="mx-auto max-w-3xl animate-fadeup">
          <div className="panel p-2.5">
            {/* image / video tabs */}
            <div className="mb-2.5 flex items-center gap-1 rounded-full border border-line bg-base2/50 p-1 text-sm">
              {[["image", "✦ 文生图"], ["video", "▶ 文生视频"]].map(([k, label]) => {
                const disabled = !modelEnabled(k);
                return (
                <button
                  key={k}
                  onClick={() => switchCategory(k)}
                  disabled={disabled}
                  title={disabled ? "模型未启用，请联系管理员配置" : ""}
                  className={`flex-1 rounded-full px-4 py-1.5 font-display font-medium transition-all ${
                    disabled
                      ? "cursor-not-allowed text-fog opacity-45"
                      : category === k ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:text-snow"
                  }`}
                >
                  {label}
                </button>
                );
              })}
            </div>
            {!currentModelEnabled && (
              <p className="mb-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                当前{category === "video" ? "视频" : "图片"}模型未启用，请管理员在后台模型配置中启用后再生成。
              </p>
            )}

            {/* prompt */}
            <div className="rounded-xl3 border border-line bg-base2/40 p-3">
              <textarea
                className="textarea h-28 resize-none border-0 bg-transparent px-1 text-[15px] focus:ring-0"
                placeholder="描述你想要的画面，越具体越好（主体 / 风格 / 光线 / 色调 / 构图）… ⌘/Ctrl + Enter 生成"
                value={prompt}
                onChange={(e) => { setPrompt(e.target.value); setPromptDirty(true); }}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit("preview"); }
                }}
              />
              {/* example chips */}
              <div className="mt-1 flex flex-wrap gap-1.5">
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
              </div>
              <div className="mt-3 flex flex-col gap-2 border-t border-line pt-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => setRefOpen((v) => !v)}
                    className={`btn-secondary btn-sm border-iris/50 bg-iris/10 text-snow shadow-glow-sm hover:bg-iris/20 ${
                      selected ? "border-transparent bg-brand text-white" : ""
                    }`}
                  >
                    {selected ? "已选参考素材" : "参考链接反推"}
                  </button>
                  <button
                    type="button"
                    onClick={() => uploadInputRef.current?.click()}
                    disabled={uploading}
                    className="btn-secondary btn-sm"
                  >
                    {uploading ? "上传中…" : "上传图片参考 / 编辑源"}
                  </button>
                  <input
                    ref={uploadInputRef}
                    type="file"
                    accept="image/jpeg,image/png,image/webp,image/gif"
                    className="hidden"
                    onChange={(e) => doUploadImage(e.target.files?.[0])}
                  />
                </div>
                {selected && (
                  <div className="flex min-w-0 items-center gap-2 rounded-full border border-line bg-white/5 px-2 py-1 text-xs text-fog">
                    <span className="badge bg-iris/20 text-iris-400">{selected.type === "video" ? "视频参考" : "图片参考"}</span>
                    <span className="truncate">{selectedLabel(selected)}</span>
                  </div>
                )}
              </div>
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
                    <button type="button" onClick={clearRef} className="chip">清除参考</button>
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

            {/* controls */}
            <div className="mt-2.5 flex flex-wrap items-center gap-x-5 gap-y-3 px-1 py-1">
              {/* ratio */}
              <div className="min-w-0 flex-1 basis-full">
                <span className="text-xs text-fog">比例</span>
                <div className="mt-1 flex gap-1 overflow-x-auto pb-1">
                  {ratioOptions.map((r) => (
                    <button
                      key={r.key}
                      onClick={() => setRatio(r.key)}
                      title={r.hint}
                      className={`group flex flex-col items-center gap-1 rounded-lg border px-2 py-1.5 transition ${
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
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-fog">质量</span>
                    <div className="flex gap-1">
                      {IMAGE_QUALITY_PRESETS.map((q) => (
                        <button key={q.key} onClick={() => setImageQuality(q.key)} title={q.hint}
                          className={`chip ${imageQuality === q.key ? "chip-active" : ""}`}>{q.label}</button>
                      ))}
                    </div>
                    <span className="text-xs text-fog">{currentImageSize}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-fog">数量</span>
                    <div className="flex gap-1">
                      {[1, 2, 4, 8].map((v) => (
                        <button key={v} onClick={() => setN(v)} className={`chip ${imageCount === v ? "chip-active" : ""}`}>{v}</button>
                      ))}
                    </div>
                    <input
                      className="input w-20 px-2 py-1 text-xs"
                      type="number"
                      min="1"
                      max={maxImageN}
                      value={n}
                      onChange={(e) => setN(e.target.value)}
                      onBlur={() => setN(boundedImageCount(n, maxImageN))}
                      title={`最多 ${maxImageN} 张`}
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-fog">时长</span>
                    <div className="flex max-w-full gap-1 overflow-x-auto pb-1">
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
                      className="input w-24 px-2 py-1 text-xs"
                      type="number"
                      min="1"
                      max={maxVideoDuration}
                      value={vDuration}
                      onChange={(e) => setVDuration(e.target.value)}
                      onBlur={() => setVDuration(boundedVideoDuration(vDuration, maxVideoDuration))}
                      title={`最长 ${formatDuration(maxVideoDuration)}`}
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-fog">质量</span>
                    <div className="flex gap-1">
                      {VIDEO_QUALITIES.map((q) => (
                        <button key={q.key} onClick={() => setVResolution(q.key)} title={q.hint}
                          className={`chip ${vResolution === q.key ? "chip-active" : ""}`}>{q.label}</button>
                      ))}
                    </div>
                  </div>
                </>
              )}

              <button onClick={() => setShowNegative((s) => !s)} className={`chip ${showNegative ? "chip-active" : ""}`}>
                负向词
              </button>
              {category === "image" && (
                <label className="flex items-center gap-1.5 text-xs text-fog">
                  seed
                  <input className="input w-20 px-2 py-1 text-xs" placeholder="随机" value={seed}
                    onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))} />
                </label>
              )}
            </div>

            {showNegative && (
              <input className="input mt-2" placeholder="不想出现的元素：文字, 水印, 多余手指, 畸变…"
                value={negative} onChange={(e) => { setNegative(e.target.value); setNegativeTouched(true); }} />
            )}

            {/* generate bar */}
            <div className="mt-3 flex items-center justify-between gap-3 px-1">
              <p className="text-xs text-fog">
                {estCost ? (
                  <>预计消耗 <b className="text-mist">{category === "video" ? `预览 ${videoPreviewCost} · 完整 ${videoFinalCost} · ${formatDuration(videoDuration)} · ${vResolution}` : estCost}</b> 积分</>
                ) : "提交后冻结预估积分"}
              </p>
              <button onClick={() => submit("preview")} disabled={submitting || running || !currentModelEnabled} className="btn-primary btn-lg min-w-32">
                {submitting || running ? "生成中…" : category === "video" ? "生成预览 ▶" : "立即生成 ✦"}
              </button>
            </div>

            {/* reference expander */}
            {refOpen && (
              <div className="mt-3 rounded-xl3 border border-line bg-base2/40 p-3 animate-fadeup">
                <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
                  <input className="input" placeholder="粘贴小红书 / 抖音 / 公众号 / 网页链接，抓取参考图或视频…" value={url}
                    onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === "Enter" && doParse()} />
                  <div className="flex gap-2">
                    <button onClick={doParse} disabled={parsing} className="btn-secondary whitespace-nowrap">
                      {parsing ? "抓取中…" : "抓取"}
                    </button>
                    <button
                      type="button"
                      onClick={() => uploadInputRef.current?.click()}
                      disabled={uploading}
                      className="btn-secondary whitespace-nowrap"
                    >
                      {uploading ? "上传中…" : "上传图片"}
                    </button>
                  </div>
                </div>
                {selected?.type === "image" && selected?.url?.includes("/api/uploads/upload/") && (
                  <p className="mt-2 text-xs text-fog">
                    已选上传图片作为参考;不反推也可以直接用当前提示词进行参考编辑生成。
                  </p>
                )}
                {assets.length > 0 && (
                  <div className="mt-3 grid grid-cols-4 gap-2 sm:grid-cols-6">
                    {assets.map((a, i) => (
                      <button key={i} onClick={() => pickAsset(a)}
                        className={`relative aspect-square overflow-hidden rounded-lg border transition ${
                          selected === a ? "border-iris ring-2 ring-iris/40" : "border-line hover:border-line2"
                        }`}>
                        <ReferenceAssetPreview asset={a} />
                        <span className="badge absolute left-1 top-1 bg-black/60 text-[10px] text-white">{a.type}</span>
                      </button>
                    ))}
                  </div>
                )}
                {selected && reverseEnabled && (
                  <button onClick={doReverse} disabled={reversing} className="btn-secondary btn-sm mt-3">
                    {reversing ? "反推中…" : `✦ 反推所选${selected.type === "video" ? "视频" : "图片"}为提示词${selectedReverseCost ? ` · ${selectedReverseCostLabel}` : ""}`}
                  </button>
                )}
                {selected && !reverseEnabled && (
                  <p className="mt-3 text-xs text-fog">反推功能已被管理员关闭;已选为参考首帧/同款依据。</p>
                )}
              </div>
            )}
          </div>

          {msg && (
            <div className="mt-3 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">{msg}</div>
          )}
        </section>

        {/* live result */}
        {task && (
          <section className="mx-auto mt-8 max-w-3xl animate-fadeup">
            <div className="card p-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-sm font-display font-semibold">{taskResultTitle(task, runningSnapshot)}</span>
                <span className={`badge ${statusStyle(task.status)}`}>{statusZh(task.status)}</span>
              </div>
              {showRunningProgress && (
                <div className="mb-4">
                  <div className="mb-2 h-1.5 w-full overflow-hidden rounded-full bg-white/8">
                    <div className="h-full rounded-full bg-brand transition-all duration-500" style={{ width: `${task.progress || 8}%` }} />
                  </div>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    {Array.from({
                      length: runningSnapshot?.category === "image" ? Number(runningSnapshot.n || 1) : 1,
                    }).map((_, i) => (
                      <div
                        key={i}
                        className="skeleton"
                        style={runningSnapshot?.ratio ? { aspectRatio: `${runningSnapshot.ratio.w} / ${runningSnapshot.ratio.h}` } : { aspectRatio: "1 / 1" }}
                      />
                    ))}
                  </div>
                </div>
              )}
              {trackingLost && !isTerminalTaskStatus(task?.status) && (
                <div className="mb-4 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <span>前端已停止等待该任务，后端可能仍在生成。</span>
                    <div className="flex gap-2">
                      <button type="button" onClick={refreshActiveTask} className="btn-secondary btn-sm">刷新任务状态</button>
                      <a href="/history" className="btn-secondary btn-sm">去历史查看</a>
                    </div>
                  </div>
                </div>
              )}
              {task.error && <p className="mb-3 rounded-lg bg-bad/10 px-3 py-2 text-sm text-bad">{task.error}</p>}
              {task.partial && (
                <p className="mb-3 rounded-lg bg-warn/10 px-3 py-2 text-sm text-warn">
                  本次批量生成完成 {task.saved_count || task.assets?.length || 0}/{task.requested_count || "?"} 张，失败部分已自动退回积分。
                </p>
              )}
              {task.assets?.length > 0 && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {task.assets.map((a) => (
                    <ResultCard key={a.id} a={a} onOpen={() => setLightbox(a)} onUnlock={() => unlock(a)} onDownload={() => download(a)} />
                  ))}
                </div>
              )}
              {task.category === "video" && task.stage === "preview" && task.status === "succeeded" && finalTaskId && (
                <a href="/history" className="btn-secondary mt-4 block w-full text-center">
                  完整视频已提交 · 去历史查看
                </a>
              )}
              {task.category === "video" && task.stage === "preview" && task.status === "succeeded" && !finalTaskId && (
                <button onClick={() => submit("final")} disabled={submitting} className="btn-primary mt-4 w-full">
                  {submitting ? "提交中…" : `方向满意 → 渲染完整视频 · ${videoFinalCost}积分`}
                </button>
              )}
            </div>
          </section>
        )}

        {/* gallery / works wall */}
        <section className="mx-auto mt-14 max-w-7xl">
          <div className="mb-5 flex items-end justify-between">
            <div>
              <h2 className="text-xl font-bold">我的作品墙</h2>
              <p className="mt-1 text-sm text-fog">最近生成的创作，点击查看 / 解锁 / 下载。</p>
            </div>
            <a href="/profile" className="btn-secondary btn-sm">查看全部</a>
          </div>
          {works === null ? (
            <div className="masonry">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="skeleton" style={{ height: 160 + (i % 4) * 60 }} />
              ))}
            </div>
          ) : works.length === 0 ? (
            <div className="card flex flex-col items-center justify-center gap-2 p-16 text-center">
              <span className="text-3xl">🪄</span>
              <p className="text-sm text-mist">还没有作品，输入提示词开始你的第一次创作。</p>
            </div>
          ) : (
            <div className="masonry">
              {works.map((a) => (
                <MasonryItem key={a.id} a={a} onOpen={() => setLightbox(a)} />
              ))}
            </div>
          )}
        </section>
      </main>

      {lightbox && (
        <Lightbox a={lightbox} onClose={() => setLightbox(null)} onUnlock={() => unlock(lightbox)} onDownload={() => download(lightbox)} />
      )}
    </div>
  );
}

/* ---------- sub-components ---------- */

function srcOf(a) {
  return assetPreviewSrc(a);
}

function taskResultTitle(task, runningSnapshot) {
  if (!task) return "本次生成";
  if (task.category === "image") {
    const assetsCount = Number(task.assets?.length || 0);
    const saved = Number(task.saved_count ?? assetsCount);
    const snapshotCount = runningSnapshot?.category === "image" ? runningSnapshot.n : null;
    const requestedValue = task.requested_count ?? snapshotCount ?? assetsCount;
    const requested = Number(requestedValue || 1);
    if (task.partial) return `本次生成 · ${saved}/${requested} 张`;
    if (!isTerminalTaskStatus(task.status)) return `本次生成 · ${requested} 张`;
    return `本次生成 · ${saved || requested} 张`;
  }
  if (task.category === "video") {
    return task.stage === "final" ? "本次生成 · 完整视频" : "本次生成 · 视频预览";
  }
  return "本次生成";
}

function ResultCard({ a, onOpen, onUnlock, onDownload }) {
  const src = srcOf(a);
  const ratioStyle = mediaAspectStyle(a);
  return (
    <div className="group overflow-hidden rounded-xl2 border border-line bg-base2">
      <div className="relative cursor-zoom-in bg-black/20" style={ratioStyle} onClick={onOpen}>
        {!src ? (
          <div className="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog">
            预览暂不可用
          </div>
        ) : (
          <AssetMedia
            asset={a}
            className="h-full w-full object-contain transition group-hover:scale-105"
            fallbackClassName="flex h-full w-full items-center justify-center px-3 text-center text-xs text-fog"
          />
        )}
        {a.unlocked && <span className="badge absolute right-1.5 top-1.5 bg-brand text-white">HD</span>}
      </div>
      <div className="p-1.5">
        {a.unlocked ? (
          <button onClick={onDownload} className="btn-primary btn-sm w-full">下载高清</button>
        ) : (
          <div className="flex gap-1.5">
            <button onClick={onOpen} className="btn-secondary btn-sm flex-1">预览</button>
            <button onClick={onUnlock} className="btn-primary btn-sm flex-1">解锁</button>
          </div>
        )}
      </div>
    </div>
  );
}

function MasonryItem({ a, onOpen }) {
  const src = srcOf(a);
  const ratioStyle = mediaAspectStyle(a);
  return (
    <button onClick={onOpen} className="group relative block w-full overflow-hidden rounded-xl2 border border-line bg-base2" style={ratioStyle}>
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
      <div className="pointer-events-none absolute inset-0 flex items-end bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition group-hover:opacity-100">
        <span className="m-2 flex items-center gap-1 text-xs text-white/90">
          {a.unlocked ? "已解锁 · HD" : "点击预览 / 解锁"}
        </span>
      </div>
      {a.unlocked && <span className="badge absolute right-2 top-2 bg-brand text-white">HD</span>}
    </button>
  );
}

function Lightbox({ a, onClose, onUnlock, onDownload }) {
  const src = srcOf(a);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="panel max-h-[92vh] max-w-3xl overflow-auto p-3" onClick={(e) => e.stopPropagation()}>
        {!src ? (
          <div className="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog">
            {a.unlocked ? "预览暂不可用，请稍后重试。" : "预览暂不可用，请先解锁后再下载高清。"}
          </div>
        ) : (
          <AssetMedia
            asset={a}
            interactive
            controls
            autoPlay
            muted={false}
            className="mx-auto max-h-[76vh] w-auto rounded-xl2"
            fallbackClassName="flex min-h-64 items-center justify-center rounded-xl2 bg-black/30 px-6 text-sm text-fog"
          />
        )}
        <div className="mt-3 flex items-center justify-between gap-2 text-sm">
          <span className="text-fog">
            {a.unlocked ? "预览 · 已解锁，可下载高清" : "预览 · 带水印"}
            {a.width ? ` · ${a.width}×${a.height}` : ""}
          </span>
          <div className="flex gap-2">
            {!a.unlocked && <button onClick={onUnlock} className="btn-primary btn-sm">解锁高清</button>}
            {a.unlocked && <button onClick={onDownload} className="btn-primary btn-sm">下载</button>}
            <button onClick={onClose} className="btn-secondary btn-sm">关闭</button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------- helpers ---------- */

function assetDims(a) {
  if (!a) return null;
  // take width+height as a PAIR — never mix original width with thumb height,
  // which would fabricate a wrong ratio (e.g. portrait ref -> landscape output).
  let w = Number(a.width), h = Number(a.height);
  if (!w || !h) { w = Number(a.thumb_width); h = Number(a.thumb_height); }
  if (!w || !h) return null;
  return { width: w, height: h };
}

function selectedLabel(a) {
  if (!a) return "";
  const dims = assetDims(a);
  const dimText = dims ? `${dims.width}x${dims.height}` : "未识别尺寸";
  if (a.original_url) return `已本地化 · ${dimText}`;
  if (a.original_thumb) return `视频封面 · ${dimText}`;
  if (a.url?.includes("/api/uploads/upload/")) return `上传图片 · ${dimText}`;
  return `链接素材 · ${dimText}`;
}

function mediaThumbSrc(a) {
  return a?.display_thumb || a?.display_url || a?.thumb || a?.url || "";
}

function ReferenceAssetPreview({ asset }) {
  const [failed, setFailed] = useState(false);
  const [secureSrc, setSecureSrc] = useState("");
  const rawSrc = mediaThumbSrc(asset);
  useEffect(() => {
    setFailed(false);
    setSecureSrc("");
    if (!rawSrc || !rawSrc.includes("/api/uploads/")) return;
    let cancelled = false;
    let objectUrl = "";
    authenticatedObjectUrl(rawSrc)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSecureSrc(url);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rawSrc]);
  if (failed) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-1 bg-base2 text-[10px] text-fog">
        <span>{asset?.type === "video" ? "🎬" : "图片"}</span>
        <span>预览不可用</span>
      </div>
    );
  }
  if (asset?.type === "video") {
    if (asset.thumb) {
      return <img src={secureSrc || rawSrc} alt="" className="h-full w-full object-cover" onError={() => setFailed(true)} />;
    }
    if (asset.url) {
      return <video src={asset.url} muted preload="metadata" className="h-full w-full object-cover" onError={() => setFailed(true)} />;
    }
    return <div className="flex h-full w-full items-center justify-center text-fog">🎬</div>;
  }
  const src = secureSrc || rawSrc;
  if (!src) return <div className="flex h-full w-full items-center justify-center bg-base2 text-xs text-fog">无预览</div>;
  return <img src={src} alt="" className="h-full w-full object-cover" onError={() => setFailed(true)} />;
}

function assetSignature(a) {
  if (!a) return "";
  return [a.type || "", a.url || "", a.thumb || ""].join("|");
}

function composePromptFromStructured(structured, fallbackText = "") {
  const fallback = String(fallbackText || "").trim();
  if (!structured || !Object.keys(structured).length) return fallback;
  const finalText = String(structured.final_text || structured["final_text"] || fallbackText || "").trim();
  const order = [
    "主体", "商品服装", "细节特征", "场景背景", "广告目标", "风格", "景别", "构图",
    "视角镜头", "视角构图", "主体动作", "镜头运动", "运动节奏", "剪辑节奏", "时序分镜",
    "字幕卖点", "光线", "色调配色", "材质纹理", "氛围情绪", "后期质感", "转场", "一致性约束",
  ];
  const skip = new Set(["负向", "标签", "文字水印", "时长建议", "final_text"]);
  const parts = [];
  for (const k of order) if (structured[k] && !skip.has(k)) parts.push(structured[k]);
  for (const k of Object.keys(structured))
    if (!order.includes(k) && !skip.has(k) && structured[k]) parts.push(structured[k]);
  let text = parts.join(", ");
  if (structured["标签"]) text += (text ? ", " : "") + structured["标签"];
  return text.trim() || finalText;
}

function ratioKeyForSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || ""));
  if (!m) return null;
  return nearestRatio(Number(m[1]), Number(m[2]));
}

function qualityKeyForSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || ""));
  if (!m) return "1k";
  const maxSide = Math.max(Number(m[1]), Number(m[2]));
  if (maxSide >= 3500) return "4k";
  if (maxSide >= 1500) return "2k";
  return "1k";
}

function imageSizeFor(ratio, quality, maxDim = 4096) {
  const preset = IMAGE_QUALITY_PRESETS.find((q) => q.key === quality) || IMAGE_QUALITY_PRESETS[0];
  const maxSide = Math.min(Number(maxDim) || 4096, preset.maxSide);
  if (ratio.w >= ratio.h) {
    const width = maxSide;
    const height = roundImageDim((maxSide * ratio.h) / ratio.w, maxDim);
    return `${width}x${height}`;
  }
  const height = maxSide;
  const width = roundImageDim((maxSide * ratio.w) / ratio.h, maxDim);
  return `${width}x${height}`;
}

function roundImageDim(value, maxDim = 4096) {
  const capped = Math.max(64, Math.min(Number(maxDim) || 4096, Math.round(value)));
  return Math.max(64, Math.min(Number(maxDim) || 4096, Math.round(capped / 8) * 8));
}

function boundedImageCount(value, max = 8) {
  const limit = Math.max(1, Number(max) || 8);
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 1;
  return Math.max(1, Math.min(limit, parsed));
}

function boundedVideoDuration(value, max = 900) {
  const limit = Math.max(1, Number(max) || 900);
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) return 5;
  return Math.max(1, Math.min(limit, parsed));
}

function formatDuration(seconds) {
  const s = Math.max(1, Number.parseInt(seconds, 10) || 1);
  if (s < 60) return `${s}s`;
  const minutes = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${minutes}m${rest}s` : `${minutes}min`;
}

function videoRatioOptions() {
  return RATIOS.filter((r) => VIDEO_RATIO_KEYS.has(r.key));
}

function nearestRatio(w, h, options = RATIOS) {
  const r = w / h;
  let best = options[0] || RATIOS[0], diff = Infinity;
  for (const x of options) {
    const d = Math.abs(r - x.w / x.h);
    if (d < diff) { diff = d; best = x; }
  }
  return best.key;
}

function mediaAspectStyle(a, fallback = "1 / 1") {
  const dims = assetDims(a);
  if (!dims) return fallback ? { aspectRatio: fallback } : null;
  return { aspectRatio: `${dims.width} / ${dims.height}` };
}

function isTerminalTaskStatus(s) {
  return TERMINAL_TASK_STATUSES.has(s);
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

function isRequestTimeoutError(e) {
  return String(e?.message || "").includes("请求超时");
}
