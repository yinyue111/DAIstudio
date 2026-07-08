"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, setUnauthorizedHandler } from "../lib/api";
import { errorMessage, redirectOnAuthError, reportBackgroundError, showError } from "../lib/errorHandling";
import Nav from "../components/Nav";
import { canDownloadAsset, isAssetTakenDown } from "../components/AssetMedia";
import AssetWindowControls from "../components/AssetWindowControls";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import StudioGenerationControls from "../components/StudioGenerationControls";
import { useToast } from "../components/ToastProvider";
import useGenerationSubmit from "../hooks/useGenerationSubmit";
import useMediaUpload from "../hooks/useMediaUpload";
import useReferenceParsing from "../hooks/useReferenceParsing";
import useStudioWorkspaceState from "../hooks/useStudioWorkspaceState";
import useTaskTracking from "../hooks/useTaskTracking";
import useVisibleItemWindow from "../hooks/useVisibleItemWindow";
import {
  CREATION_MODES,
  RATIOS,
  STUDIO_SESSION_DRAFT_KEY,
  STUDIO_VARIATION_DRAFT_KEY,
  VIDEO_RATIO_KEYS,
  creationModeLabel,
} from "./studio/constants";
import StudioMessageBar from "./studio/StudioMessageBar";
import StudioModeTabs from "./studio/StudioModeTabs";
import StudioPromptWorkspace from "./studio/StudioPromptWorkspace";
import StudioReferencePanel from "./studio/StudioReferencePanel";
import StudioResults from "./studio/StudioResults";
import StudioStructuredEditor from "./studio/StudioStructuredEditor";
import { assetVariationSourceUrl } from "./studio/assetActions";
import { generationSubmitDisabled } from "./studio/taskConcurrency";
import {
  assetSignature,
  composePromptFromStructured,
  composeStyleTransferPrompt,
  formatDuration,
  nearestRatio,
  qualityKeyForSize,
  ratioKeyForSize,
  videoRatioOptions,
} from "./studio/helpers";
import { buildStudioDerivedViewState, modelEnabledForConfig, studioCreationFacts } from "./studio/viewModel";

function parsePromptDraft(raw) {
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      return {
        prompt: String(parsed.prompt || ""),
        category: String(parsed.category || ""),
        creationMode: String(parsed.creationMode || ""),
        savedAt: Number(parsed.savedAt || 0),
      };
    }
  } catch (_e) {
    // Legacy prompt drafts were stored as plain strings.
  }
  return { prompt: String(raw || ""), category: "", creationMode: "", savedAt: 0 };
}

function promptDraftMode(draft) {
  if (draft.creationMode && CREATION_MODES.some((item) => item.key === draft.creationMode)) {
    return draft.creationMode;
  }
  return draft.category === "video" ? "video" : "image";
}

export default function Home() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);

  // creation state
  const [creationMode, setCreationMode] = useState("image"); // image | video | image_edit | video_edit
  const [showNegative, setShowNegative] = useState(false);
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);
  const [promptSaveTitle, setPromptSaveTitle] = useState("");
  const [promptSaveCategory, setPromptSaveCategory] = useState("image");
  const [promptSaveFavorite, setPromptSaveFavorite] = useState(false);

  // reference (paste link -> reverse) state
  const [refOpen, setRefOpen] = useState(false);
  const [structOpen, setStructOpen] = useState(true);

  // run state
  const [msg, setMsg] = useState("");
  const [lightbox, setLightbox] = useState(null);
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());

  // gallery
  const [works, setWorks] = useState(null);
  const [worksError, setWorksError] = useState("");
  const worksWindow = useVisibleItemWindow(works, { initialCount: 48, step: 24, resetKey: "studio-works" });
  const visibleWorks = works === null ? null : worksWindow.items;

  const busyAssetIdsRef = useRef(new Set());
  const revokeUploadedObjectUrlsRef = useRef(null);
  const resultsRef = useRef(null);
  const loadWorksSeqRef = useRef(0);
  const taskRef = useRef(null);
  const workspacesRef = useRef(null);
  const draftSyncTimerRef = useRef(null);
  const cloudDraftLoadedRef = useRef(false);
  const restoredLocalDraftAtRef = useRef(0);
  const {
    workspaces,
    setWorkspaces,
    workspace,
    setWorkspacePatch,
    setPrompt,
    setNegative,
    setEditSubjectMode,
    setRatio,
    setImageQuality,
    setN,
    setSeed,
    setEditMaskMode,
    setVDuration,
    setVResolution,
    setVideoProductLockMode,
    setVideoAnalysisPreset,
    setStructured,
    setNegativeTouched,
    setPromptDirty,
  } = useStudioWorkspaceState({ creationMode, modes: CREATION_MODES });
  const {
    prompt,
    negative,
    imageEditProductMode,
    editSubjectMode,
    ratio,
    imageQuality,
    n,
    seed,
    editMaskMode,
    vDuration,
    vResolution,
    videoProductLockMode,
    videoAnalysisPreset,
    url,
    parsing,
    uploading,
    reversing,
    assets,
    selected,
    productAsset,
    productProfile,
    productProfileSource,
    productProfiling,
    variationSource,
    structured,
    structuredSource,
    promptSourceSignature,
    negativeTouched,
    promptDirty,
  } = workspace;
  const {
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
  } = studioCreationFacts({ creationMode, imageEditProductMode, editSubjectMode });

  useEffect(() => {
    workspacesRef.current = workspaces;
  }, [workspaces]);

  useEffect(() => {
    setUnauthorizedHandler(() => saveStudioSessionDraft("auth_expired"));
    return () => setUnauthorizedHandler(null);
  }, [creationMode, showNegative, refOpen, structOpen, me?.id]);

  useEffect(() => {
    api.me().then((u) => {
      setMe(u);
      loadStudioCloudDraft();
    }).catch((e) => redirectOnAuthError(e, router, setMsg, "studio session probe"));
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
    }).catch((e) => showError(setMsg, e, "加载创作配置失败"));
    loadWorks({ restoreActive: true });
    try {
      const draftRaw = window.localStorage.getItem(STUDIO_DRAFT_PROMPT_KEY);
      if (draftRaw) {
        const parsedDraft = parsePromptDraft(draftRaw);
        const draftMode = promptDraftMode(parsedDraft);
        setCreationMode(draftMode);
        setWorkspacePatch({
          prompt: parsedDraft.prompt,
          promptDirty: true,
          promptSourceSignature: "",
        }, draftMode);
        restoredLocalDraftAtRef.current = Number(parsedDraft.savedAt || Date.now());
        window.localStorage.removeItem(STUDIO_DRAFT_PROMPT_KEY);
      }
      const variationDraft = window.localStorage.getItem(STUDIO_VARIATION_DRAFT_KEY);
      if (variationDraft) {
        if (applyVariationDraft(JSON.parse(variationDraft))) {
          window.localStorage.removeItem(STUDIO_VARIATION_DRAFT_KEY);
        }
      }
      const sessionDraft = window.localStorage.getItem(STUDIO_SESSION_DRAFT_KEY);
      if (sessionDraft) {
        window.localStorage.removeItem(STUDIO_SESSION_DRAFT_KEY);
        const parsedDraft = JSON.parse(sessionDraft);
        restoredLocalDraftAtRef.current = Number(parsedDraft.savedAt || Date.now());
        restoreStudioSessionDraft(parsedDraft);
      }
    } catch (e) {
      reportBackgroundError(e, "restore studio draft");
    }
    return () => {
      if (draftSyncTimerRef.current) window.clearTimeout(draftSyncTimerRef.current);
      stopAllTracking();
      revokeUploadedObjectUrls();
      revokeProductObjectUrls();
    };
  }, []);

  useEffect(() => {
    if (!me?.id || !cloudDraftLoadedRef.current) return undefined;
    if (draftSyncTimerRef.current) window.clearTimeout(draftSyncTimerRef.current);
    draftSyncTimerRef.current = window.setTimeout(() => {
      syncStudioDraftToCloud("auto").catch((e) => reportBackgroundError(e, "sync studio cloud draft"));
    }, 1600);
    return () => {
      if (draftSyncTimerRef.current) window.clearTimeout(draftSyncTimerRef.current);
    };
  }, [me?.id, workspaces, creationMode, showNegative, refOpen, structOpen]);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((r) => r.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

  useEffect(() => {
    setPromptSaveCategory(category === "video" ? "video" : "image");
  }, [category]);

  function refreshMe() { api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user")); }

  const {
    task,
    setTask,
	    runningSnapshot,
	    setRunningSnapshot,
	    trackingLost,
	    setTrackingLost,
	    backgroundTasks,
    dismissBackgroundTask,
    cancelBackgroundTask,
    showRunningProgress,
    taskEtaText,
    restoreActiveTaskFromList,
    startTracking,
    trackBackgroundTask,
    refreshActiveTask,
    cancelActiveTask,
    stopAllTracking,
  } = useTaskTracking({
    setCreationMode,
    setMsg,
    refreshMe,
    loadWorks,
  });

  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  const {
    bumpRefVersion,
    isRefVersionCurrent,
    bumpParseRequest,
    bumpReverseRequest,
    isModeVisible,
    clearSelectedForMode,
    selectAssetForMode,
    pickAsset,
    doParse,
    doReverse,
    lastReversePromptRef,
  } = useReferenceParsing({
    creationMode,
    category,
    url,
    parsing,
    selected,
    prompt,
    negative,
    negativeTouched,
    videoAnalysisPreset,
    isEditMode,
    subjectMode,
    setMsg,
    setWorkspacePatch,
    setCreationMode,
    setRatio,
    setStructOpen,
    setShowNegative,
    setRefOpen,
    refreshMe,
    revokeUploadedObjectUrlsRef,
  });

  const {
    imageUploadInputRef,
    productUploadInputRef,
    videoUploadInputRef,
    bumpUploadRequest,
    bumpProductUploadRequest,
    revokeUploadedObjectUrls,
    revokeProductObjectUrl,
    revokeProductObjectUrls,
    doUploadImage,
    doUploadProductImage,
    doUploadVideo,
  } = useMediaUpload({
    cfg,
    creationMode,
    category,
    isEditMode,
    subjectMode,
    uploading,
    setMsg,
    setWorkspacePatch,
    setCreationMode,
    setRefOpen,
    bumpRefVersion,
    isRefVersionCurrent,
    bumpReverseRequest,
    isModeVisible,
    selectAssetForMode,
  });

  const {
    submitting,
    submit,
  } = useGenerationSubmit({
    cfg,
    task,
    category,
    creationMode,
    isEditMode,
    isImageEditMode,
    subjectMode,
    parsing,
    uploading,
    reversing,
    productProfiling,
    prompt,
    negative,
    promptDirty,
    promptSourceSignature,
    selected,
    productAsset,
    productProfile,
    productProfileSource,
    variationSource,
    structured,
    structuredSource,
    ratio,
    imageQuality,
    n,
    seed,
    editMaskMode,
    vDuration,
    vResolution,
    videoProductLockMode,
    resultsRef,
	    modelEnabled,
	    setMsg,
	    setTask,
	    setRunningSnapshot,
    setTrackingLost,
    setWorkspacePatch,
    trackBackgroundTask,
    refreshMe,
    startTracking,
  });

  useEffect(() => {
    revokeUploadedObjectUrlsRef.current = revokeUploadedObjectUrls;
  }, [revokeUploadedObjectUrls]);

  function modelEnabled(kind) {
    return modelEnabledForConfig(cfg, kind);
  }

  function sanitizeAssetForDraft(asset) {
    if (!asset) return null;
    const clean = {};
    for (const key of [
      "id", "type", "url", "thumb", "preview_url", "original_url", "original_thumb",
      "source_page_url", "source_captured_at", "width", "height", "thumb_width", "thumb_height",
      "unlock_cost", "unlocked", "favorite",
    ]) {
      if (asset[key] !== undefined && asset[key] !== null) clean[key] = asset[key];
    }
    return clean;
  }

  function sanitizeWorkspaceForDraft(current) {
    if (!current) return null;
    return {
      prompt: current.prompt || "",
      negative: current.negative || "",
      imageEditProductMode: !!current.imageEditProductMode,
      editSubjectMode: current.editSubjectMode || "general",
      ratio: current.ratio || "1:1",
      imageQuality: current.imageQuality || "1k",
      n: current.n || 1,
      seed: current.seed || "",
      vDuration: current.vDuration || 5,
      vResolution: current.vResolution || "720p",
      editMaskMode: current.editMaskMode || "protect_subject",
      videoProductLockMode: current.videoProductLockMode || "locked",
      videoAnalysisPreset: current.videoAnalysisPreset || "standard",
      url: current.url || "",
      assets: (current.assets || []).map(sanitizeAssetForDraft).filter(Boolean).slice(0, 12),
      selected: sanitizeAssetForDraft(current.selected),
      productAsset: sanitizeAssetForDraft(current.productAsset),
      productProfile: current.productProfile || null,
      productProfileSource: current.productProfileSource || "",
      variationSource: sanitizeAssetForDraft(current.variationSource),
      structured: current.structured || {},
      structuredSource: current.structuredSource || "",
      promptSourceSignature: current.promptSourceSignature || "",
      negativeTouched: !!current.negativeTouched,
      promptDirty: !!current.promptDirty,
      parsing: false,
      uploading: false,
      reversing: false,
      productProfiling: false,
    };
  }

  function buildStudioSessionDraft(reason = "manual") {
    const snapshot = workspacesRef.current || workspaces;
    const savedWorkspaces = Object.fromEntries(
      Object.entries(snapshot || {}).map(([mode, current]) => [mode, sanitizeWorkspaceForDraft(current)]),
    );
    return {
      version: 1,
      reason,
      savedAt: Date.now(),
      creationMode,
      showNegative,
      refOpen,
      structOpen,
      workspaces: savedWorkspaces,
      activeTaskId: taskRef.current?.id || null,
    };
  }

  function saveStudioSessionDraft(reason = "manual") {
    if (typeof window === "undefined") return;
    try {
      const draft = buildStudioSessionDraft(reason);
      window.localStorage.setItem(STUDIO_SESSION_DRAFT_KEY, JSON.stringify(draft));
      if (me?.id) {
        api.saveDraft("studio", draft).catch((e) => reportBackgroundError(e, "save studio cloud draft"));
      }
    } catch (e) {
      reportBackgroundError(e, "save studio session draft");
    }
  }

  async function syncStudioDraftToCloud(reason = "manual") {
    if (!me?.id) return;
    await api.saveDraft("studio", buildStudioSessionDraft(reason));
  }

  async function loadStudioCloudDraft() {
    try {
      const row = await api.getDraft("studio");
      cloudDraftLoadedRef.current = true;
      const draft = row?.payload;
      if (!draft?.workspaces) return;
      const remoteSavedAt = Number(draft.savedAt || 0);
      if (restoredLocalDraftAtRef.current && remoteSavedAt <= restoredLocalDraftAtRef.current) return;
      restoreStudioSessionDraft({ ...draft, reason: draft.reason || "cloud" });
    } catch (e) {
      cloudDraftLoadedRef.current = true;
      reportBackgroundError(e, "load studio cloud draft");
    }
  }

  function restoreStudioSessionDraft(draft) {
    if (!draft?.workspaces || typeof draft.workspaces !== "object") return;
    const validModes = new Set(CREATION_MODES.map((item) => item.key));
    const restored = Object.fromEntries(
      Object.entries(draft.workspaces)
        .filter(([mode, current]) => validModes.has(mode) && current)
        .map(([mode, current]) => [mode, { ...current }]),
    );
    if (!Object.keys(restored).length) return;
    setWorkspaces((prev) => ({ ...prev, ...restored }));
    if (validModes.has(draft.creationMode)) setCreationMode(draft.creationMode);
    setShowNegative(!!draft.showNegative);
    setRefOpen(!!draft.refOpen);
    setStructOpen(draft.structOpen !== false);
    setMsg("已恢复上次登录过期前的工作台草稿。");
    notify.info(draft.reason === "cloud" ? "已恢复云端工作台草稿。" : "已恢复上次工作台草稿。");
  }

  function switchCreationMode(kind) {
    if (!modelEnabled(kind)) {
      const text = `${kind === "video" || kind === "video_edit" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`;
      setMsg(text);
      notify.warn(text);
      return;
    }
    setMsg("");
    setCreationMode(kind);
  }

  async function loadWorks({ restoreActive = false } = {}) {
    const seq = ++loadWorksSeqRef.current;
    try {
      const list = await api.tasks(20, 0);
      if (seq !== loadWorksSeqRef.current) return;
      const flat = [];
      for (const t of list) {
        for (const a of t.assets || []) {
          flat.push({
            ...a,
            task_id: a.task_id || t.id,
            _cat: t.category,
            _task_status: t.status,
            _task_stage: t.stage,
          });
        }
      }
      setWorks(flat);
      setWorksError("");
      if (restoreActive && !taskRef.current) restoreActiveTaskFromList(list);
    } catch (e) {
      if (seq !== loadWorksSeqRef.current) return;
      reportBackgroundError(e, "load studio works");
      const detail = errorMessage(e, "作品加载失败，请重试");
      setWorksError(`${detail}。已保留当前作品列表。`);
      setMsg("作品加载失败，请重试，当前作品列表已保留。");
      notify.error("作品加载失败，请重试，当前作品列表已保留。");
    }
  }

  function clearProductAsset() {
    bumpProductUploadRequest(creationMode);
    revokeProductObjectUrl(creationMode);
    setWorkspacePatch({
      productAsset: null,
      productProfile: null,
      productProfileSource: "",
      productProfiling: false,
      variationSource: null,
    });
    if (productUploadInputRef.current) productUploadInputRef.current.value = "";
  }

  function clearRef() {
    const mode = creationMode;
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpUploadRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
    if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
    clearSelectedForMode(mode);
    setWorkspacePatch((current) => ({
      selected: null,
      assets: [],
      variationSource: null,
      url: "",
      parsing: false,
      uploading: false,
      reversing: false,
      structured: {},
      structuredSource: "",
      ...(current.promptSourceSignature && !current.promptDirty
        ? { prompt: "", promptSourceSignature: "", promptDirty: false }
        : { promptSourceSignature: "" }),
      ...(current.negativeTouched ? {} : { negative: "" }),
    }), mode);
  }

  function updateReferenceUrl(value) {
    const mode = creationMode;
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    clearSelectedForMode(mode);
    setWorkspacePatch((current) => ({
      url: value,
      selected: null,
      assets: [],
      variationSource: null,
      parsing: false,
      reversing: false,
      structured: {},
      structuredSource: "",
      ...(current.promptSourceSignature && !current.promptDirty
        ? { prompt: "", promptSourceSignature: "", promptDirty: false }
        : { promptSourceSignature: "" }),
      ...(current.negativeTouched ? {} : { negative: "" }),
    }), mode);
  }

  // rebuild the prompt text from the (possibly edited) reverse dimensions
  function recompose() {
    const nextPrompt = isEditMode
      ? composeStyleTransferPrompt(structured, prompt, { video: category === "video", subject: subjectMode })
      : composePromptFromStructured(structured, prompt);
    setWorkspacePatch({
      prompt: nextPrompt,
      promptDirty: false,
      promptSourceSignature: structuredSource || assetSignature(selected) || "",
    });
  }

  async function saveReversePromptToLibrary() {
    const text = String(prompt || "").trim();
    if (!text) {
      setMsg("当前没有可保存的提示词。");
      notify.warn("当前没有可保存的提示词。");
      return;
    }
    const reverseSource = lastReversePromptRef.current?.[creationMode] || {};
    try {
      await api.createPromptHistory({
        title: promptSaveTitle.trim() || (category === "video" ? "反推视频提示词" : "反推图片提示词"),
        prompt: text,
        category: promptSaveCategory || (category === "video" ? "video" : "image"),
        source: "reverse",
        favorite: Boolean(promptSaveFavorite),
        params: {
          creation_mode: creationMode,
          subject_mode: subjectMode,
          source_signature: reverseSource.sourceSignature || structuredSource || promptSourceSignature || assetSignature(selected),
          structured,
        },
      });
      setMsg("已保存到“我的提示词”。");
      notify.success("已保存到“我的提示词”。");
    } catch (e) {
      const text = errorMessage(e, "保存提示词失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    }
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

  function createImageVariation(asset) {
    const sourceUrl = assetVariationSourceUrl(asset, { respectUnlock: true });
    if (!sourceUrl) {
      setMsg("当前图片暂不可作为变体来源，请先确认预览可用。");
      return;
    }
    const nextAsset = {
      ...asset,
      type: "image",
      url: sourceUrl,
      thumb: asset.preview_url || sourceUrl,
    };
    const dims = asset.width && asset.height ? nearestRatio(asset.width, asset.height, RATIOS) : ratio;
    setCreationMode("image_edit");
    setWorkspacePatch({
      prompt: "基于这张图生成同主体、同风格的近似变体；保留主体结构、构图、光线、色调和广告质感，只做轻微差异化，不新增无关主体。",
      negative: "",
      imageEditProductMode: false,
      editSubjectMode: "general",
      productAsset: nextAsset,
      variationSource: nextAsset,
      selected: null,
      assets: [],
      structured: {},
      structuredSource: "",
      promptSourceSignature: "",
      promptDirty: true,
      negativeTouched: false,
      ratio: dims,
    }, "image_edit");
    setStructOpen(false);
    setRefOpen(false);
    setShowNegative(false);
    setMsg("已切到图片编辑，可直接生成同款变体，也可以先微调提示词。");
    if (typeof window !== "undefined") {
      window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
    }
  }

  function applyVariationDraft(draft) {
    const asset = draft?.asset;
    const sourceUrl = assetVariationSourceUrl(asset, { respectUnlock: true });
    if (!asset || asset.type !== "image" || !sourceUrl) return false;
    const nextAsset = {
      ...asset,
      type: "image",
      url: sourceUrl,
      thumb: asset.preview_url || asset.thumb || sourceUrl,
    };
    const dims = asset.width && asset.height ? nearestRatio(asset.width, asset.height, RATIOS) : "1:1";
    setCreationMode("image_edit");
    setWorkspacePatch({
      prompt: draft.prompt || "基于这张图生成同主体、同构图、同光线和同广告质感的近似变体；保留主体结构、产品文字、Logo、比例和核心视觉，只做轻微差异化。",
      negative: "",
      imageEditProductMode: false,
      editSubjectMode: "general",
      productAsset: nextAsset,
      productProfile: null,
      productProfileSource: "",
      productProfiling: false,
      variationSource: nextAsset,
      selected: null,
      assets: [],
      structured: {},
      structuredSource: "",
      promptSourceSignature: "",
      promptDirty: true,
      negativeTouched: false,
      ratio: dims,
    }, "image_edit");
    setStructOpen(false);
    setRefOpen(false);
    setShowNegative(false);
    setMsg("已带入历史图片，可直接生成变体，也可以先微调提示词。");
    return true;
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
      if (task) api.task(task.id).then(setTask).catch((e) => reportBackgroundError(e, "refresh active task after unlock"));
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
    if (!canDownloadAsset(asset)) {
      setMsg(isAssetTakenDown(asset) ? "素材已下架，不能继续下载。" : "请先解锁后再下载。");
      return;
    }
    busyAssetIdsRef.current.add(asset.id);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      const filename = await downloadBlob(
        `/api/assets/${asset.id}/download`,
        asset.type === "video" ? `asset-${asset.id}.mp4` : undefined,
      );
      setMsg(`已开始下载 ${filename}`);
    } catch (e) {
      setMsg(e.message);
    } finally {
      busyAssetIdsRef.current.delete(asset.id);
      setBusyAssetIds(new Set(busyAssetIdsRef.current));
    }
  }

  const {
    running,
    reverseVideoPresets,
    selectedReverseCost,
    selectedReverseCostLabel,
    reverseEnabled,
    ratioOptions,
    maxImageN,
    imageCount,
    maxVideoDuration,
    videoDuration,
    currentImageSize,
    videoFinalCost,
    estCost,
    currentModelEnabled,
    gatewayStatus,
    promptPlaceholder,
    editStyleKeys,
    editReadySteps,
    submitLabel,
  } = buildStudioDerivedViewState({
    cfg,
    creationMode,
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
    task,
    submitting,
    selected,
    productAsset,
    structured,
    prompt,
    ratio,
    imageQuality,
    n,
    vDuration,
    vResolution,
    videoAnalysisPreset,
  });

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
            <>预计消耗 <b className="text-mist">{category === "video" ? `${videoFinalCost} · ${formatDuration(videoDuration)} · ${vResolution}` : estCost}</b> 积分{category === "image" ? " · 可连续提交" : ""}</>
          ) : "提交后冻结预估积分"}
        </p>
        <button
          onClick={() => submit(category === "video" ? "final" : "preview")}
          disabled={generationSubmitDisabled({
            submitting,
            busy: parsing || uploading || reversing || productProfiling,
            currentTask: task,
            nextCategory: category,
            currentModelEnabled,
          })}
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
            <StudioModeTabs
              modes={CREATION_MODES}
              activeMode={creationMode}
              isModelEnabled={modelEnabled}
              onModeChange={switchCreationMode}
            />
            {!currentModelEnabled && (
              <p className="mb-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                当前{creationModeLabel(creationMode)}模型未启用，请管理员在后台模型配置中启用后再生成。
              </p>
            )}

            {/* prompt + reference */}
            <div className="grid min-w-0 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="min-w-0 rounded-xl3 border border-line bg-base2/40 p-3 lg:self-start">
                <StudioPromptWorkspace
                  category={category}
                  creationModeLabel={creationModeLabel(creationMode)}
                  isEditMode={isEditMode}
                  isImageEditMode={isImageEditMode}
                  subjectMode={subjectMode}
                  portraitGenerationMode={portraitGenerationMode}
                  productGenerationMode={productGenerationMode}
                  prompt={prompt}
                  placeholder={promptPlaceholder}
                  promptLibraryOpen={promptLibraryOpen}
                  readySteps={editReadySteps}
                  editStyleKeys={editStyleKeys}
                  onPromptChange={setPrompt}
                  onPromptDirty={setPromptDirty}
                  onAppendPrompt={(text) => applyLibraryPrompt(text, "append")}
                  onTogglePromptLibrary={() => setPromptLibraryOpen((open) => !open)}
                  onSubjectModeChange={setEditSubjectMode}
                  onRecompose={recompose}
                  onSubmitPreview={() => submit(category === "video" ? "final" : "preview")}
                />
                <div className="mt-3 border-t border-line pt-3">
                  <StudioGenerationControls
                    category={category}
                    ratioOptions={ratioOptions}
                    ratio={ratio}
                    onRatioChange={setRatio}
                    imageQuality={imageQuality}
                    onImageQualityChange={setImageQuality}
                    currentImageSize={currentImageSize}
                    maxImageN={maxImageN}
                    imageCount={imageCount}
                    n={n}
                    onImageCountChange={setN}
                    maxVideoDuration={maxVideoDuration}
                    videoDuration={videoDuration}
                    vDuration={vDuration}
                    onVideoDurationChange={setVDuration}
                    vResolution={vResolution}
                    onVideoResolutionChange={setVResolution}
                    isEditMode={isEditMode}
                    productGenerationMode={productGenerationMode}
                    portraitGenerationMode={portraitGenerationMode}
                    videoProductLockMode={videoProductLockMode}
                    onVideoProductLockModeChange={setVideoProductLockMode}
                    showNegative={showNegative}
                    onToggleNegative={() => setShowNegative((s) => !s)}
                    seed={seed}
                    onSeedChange={setSeed}
                    editMaskMode={editMaskMode}
                    onEditMaskModeChange={setEditMaskMode}
                    negative={negative}
                    onNegativeChange={setNegative}
                    onNegativeTouched={setNegativeTouched}
                    submitBar={renderSubmitBar("desktop")}
                  />
                </div>
              </div>

              <StudioReferencePanel
                category={category}
                creationMode={creationMode}
                imageEditProductMode={imageEditProductMode}
                editSubjectMode={subjectMode}
                isEditMode={isEditMode}
                selected={selected}
                productAsset={productAsset}
                url={url}
                setUrl={updateReferenceUrl}
                parsing={parsing}
                uploading={uploading || submitting}
                productBusy={submitting || productProfiling}
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

            <StudioStructuredEditor
              structured={structured}
              open={structOpen}
              onToggleOpen={() => setStructOpen((open) => !open)}
              onRecompose={recompose}
              onClear={clearRef}
              onChange={(key, value) => {
                setStructured({ ...structured, [key]: value });
              }}
            />
            {prompt?.trim() && (structuredSource || promptSourceSignature || lastReversePromptRef.current?.[creationMode]?.prompt) && (
              <div className="mt-3 flex flex-wrap items-center justify-end gap-2 rounded-xl2 border border-line bg-white/[0.03] p-2">
                <input
                  className="input min-w-0 flex-1 px-3 py-2 text-xs sm:max-w-[220px]"
                  placeholder="保存标题（可选）"
                  value={promptSaveTitle}
                  onChange={(e) => setPromptSaveTitle(e.target.value)}
                />
                <select
                  className="input px-3 py-2 text-xs"
                  value={promptSaveCategory}
                  onChange={(e) => setPromptSaveCategory(e.target.value)}
                  aria-label="提示词分类"
                >
                  <option value="image">图片</option>
                  <option value="video">视频</option>
                  <option value="general">通用</option>
                </select>
                <label className="chip cursor-pointer gap-1.5 px-3 py-2 text-xs">
                  <input
                    type="checkbox"
                    checked={promptSaveFavorite}
                    onChange={(e) => setPromptSaveFavorite(e.target.checked)}
                    className="h-3.5 w-3.5 accent-brand"
                  />
                  收藏
                </label>
                <button type="button" onClick={saveReversePromptToLibrary} className="btn-secondary btn-sm">
                  保存到我的提示词
                </button>
              </div>
            )}

          </div>

          <StudioMessageBar message={msg} />
        </section>

        <StudioResults
          ref={resultsRef}
          task={task}
          runningSnapshot={runningSnapshot}
          showRunningProgress={showRunningProgress}
          trackingLost={trackingLost}
          backgroundTasks={backgroundTasks}
          submitting={submitting}
          works={visibleWorks}
          lightbox={lightbox}
          setLightbox={setLightbox}
          busyAssetIds={busyAssetIds}
          onRefreshActiveTask={refreshActiveTask}
          onCancelTask={cancelActiveTask}
          taskEtaText={taskEtaText}
          worksError={worksError}
          onReloadWorks={() => loadWorks()}
          onDismissBackgroundTask={dismissBackgroundTask}
          onCancelBackgroundTask={cancelBackgroundTask}
          onUnlock={unlock}
          onDownload={download}
          onVariation={createImageVariation}
        />
        <AssetWindowControls
          totalCount={worksWindow.totalCount}
          visibleCount={worksWindow.visibleCount}
          initialCount={worksWindow.initialCount}
          hasMore={worksWindow.hasMore}
          onShowMore={worksWindow.showMore}
          onReset={worksWindow.reset}
        />
      </main>
      {renderSubmitBar("mobile")}
    </div>
  );
}
