"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob } from "../lib/api";
import { redirectOnAuthError, reportBackgroundError, showError } from "../lib/errorHandling";
import Nav from "../components/Nav";
import { canDownloadAsset, isAssetTakenDown } from "../components/AssetMedia";
import AssetWindowControls from "../components/AssetWindowControls";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import StudioGenerationControls from "../components/StudioGenerationControls";
import useGenerationSubmit from "../hooks/useGenerationSubmit";
import useMediaUpload from "../hooks/useMediaUpload";
import useReferenceParsing from "../hooks/useReferenceParsing";
import useStudioWorkspaceState from "../hooks/useStudioWorkspaceState";
import useTaskTracking from "../hooks/useTaskTracking";
import useVisibleItemWindow from "../hooks/useVisibleItemWindow";
import {
  CREATION_MODES,
  RATIOS,
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

export default function Home() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);

  // creation state
  const [creationMode, setCreationMode] = useState("image"); // image | video | image_edit | video_edit
  const [showNegative, setShowNegative] = useState(false);
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);

  // reference (paste link -> reverse) state
  const [refOpen, setRefOpen] = useState(false);
  const [structOpen, setStructOpen] = useState(true);

  // run state
  const [msg, setMsg] = useState("");
  const [lightbox, setLightbox] = useState(null);
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());

  // gallery
  const [works, setWorks] = useState(null);
  const worksWindow = useVisibleItemWindow(works, { initialCount: 48, step: 24 });
  const visibleWorks = works === null ? null : worksWindow.items;

  const busyAssetIdsRef = useRef(new Set());
  const revokeUploadedObjectUrlsRef = useRef(null);
  const resultsRef = useRef(null);
  const loadWorksSeqRef = useRef(0);
  const taskRef = useRef(null);
  const {
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
    api.me().then(setMe).catch((e) => redirectOnAuthError(e, router, setMsg, "studio session probe"));
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
      const draft = window.localStorage.getItem(STUDIO_DRAFT_PROMPT_KEY);
      if (draft) {
        setWorkspacePatch({ prompt: draft, promptDirty: true, promptSourceSignature: "" }, "image");
        window.localStorage.removeItem(STUDIO_DRAFT_PROMPT_KEY);
      }
      const variationDraft = window.localStorage.getItem(STUDIO_VARIATION_DRAFT_KEY);
      if (variationDraft) {
        window.localStorage.removeItem(STUDIO_VARIATION_DRAFT_KEY);
        applyVariationDraft(JSON.parse(variationDraft));
      }
    } catch (e) {
      reportBackgroundError(e, "restore studio draft");
    }
    return () => {
      stopAllTracking();
      revokeUploadedObjectUrls();
      revokeProductObjectUrls();
    };
  }, []);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((r) => r.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

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

  function switchCreationMode(kind) {
    if (!modelEnabled(kind)) {
      setMsg(`${kind === "video" || kind === "video_edit" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`);
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
      if (restoreActive && !taskRef.current) restoreActiveTaskFromList(list);
    } catch (e) {
      if (seq === loadWorksSeqRef.current) setWorks([]);
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
    const sourceUrl = variationSourceUrl(asset);
    if (!asset || asset.type !== "image" || !sourceUrl) return;
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
