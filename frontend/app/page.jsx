"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, setUnauthorizedHandler } from "../lib/api";
import { errorMessage, redirectOnAuthError, reportBackgroundError, showError } from "../lib/errorHandling";
import {
  applyStudioVariationTransferState,
  canApplyStudioPromptTransfer,
  createLatestOnlyDraftWriter,
  createLatestOnlyStudioOwnerRequest,
  createStudioDraftClock,
  createStudioOwnerRequestContext,
  createStudioOwnerSessionCoordinator,
  mergeStudioDraftMetadataLayers,
  mergeStudioLiveDraftState,
  mergeStudioWorkspaceLayers,
  readStudioUserDraft,
  removeStudioUserDraft,
  saveStudioUserDraft,
} from "../lib/studioSession";
import { startSubjectProtectionPreview } from "../lib/studioSubjectProtection";
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
import {
  isPromptOptimizationResultCurrent,
  promptOptimizationContextKey,
} from "./studio/promptOptimization";
import { clearAllWorkspaceContent } from "./studio/workspaceReset";

function parsePromptDraft(raw) {
  if (raw && typeof raw === "object") {
    return {
      prompt: String(raw.prompt || ""),
      category: String(raw.category || ""),
      creationMode: String(raw.creationMode || ""),
      savedAt: Number(raw.savedAt || 0),
    };
  }
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
  const [optimizingPromptMode, setOptimizingPromptMode] = useState("");

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
  const cloudDraftLoadedRef = useRef(false);
  const studioInitSeqRef = useRef(0);
  const studioDraftClockRef = useRef(null);
  const studioOwnerSessionCoordinatorRef = useRef(null);
  const studioOwnerSessionRef = useRef(null);
  const latestMeRequestRef = useRef(null);
  const assetOwnerRequestContextRef = useRef(null);
  const studioOwnerUserIdRef = useRef("");
  const initialStudioUiStateRef = useRef(null);
  const cloudDraftWriterRef = useRef(null);
  const studioUiStateRef = useRef(null);
  const variationRestoreContextRef = useRef(null);
  const subjectProfilePendingRequestRef = useRef(null);
  const subjectProfileResultCacheRef = useRef(null);
  const optimizePromptRequestRef = useRef({});
  const optimizePromptContextRef = useRef({});
  const promptOptimizationRecordsRef = useRef({});
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
    setProductPixelLockMode,
    setVDuration,
    setVResolution,
    setVideoAnalysisPreset,
    setStructured,
    setNegativeTouched,
    setPromptDirty,
  } = useStudioWorkspaceState({ creationMode, modes: CREATION_MODES });
  if (!studioDraftClockRef.current) {
    studioDraftClockRef.current = createStudioDraftClock();
  }
  if (!studioOwnerSessionCoordinatorRef.current) {
    studioOwnerSessionCoordinatorRef.current = createStudioOwnerSessionCoordinator({
      clock: studioDraftClockRef.current,
    });
  }
  if (!latestMeRequestRef.current) {
    latestMeRequestRef.current = createLatestOnlyStudioOwnerRequest(
      () => studioOwnerSessionRef.current,
    );
  }
  if (!assetOwnerRequestContextRef.current) {
    assetOwnerRequestContextRef.current = createStudioOwnerRequestContext(
      () => studioOwnerSessionRef.current,
    );
  }
  if (!initialStudioUiStateRef.current) {
    initialStudioUiStateRef.current = {
      workspaces,
      creationMode,
      showNegative,
      refOpen,
      structOpen,
    };
  }
  if (!cloudDraftWriterRef.current) {
    cloudDraftWriterRef.current = createLatestOnlyDraftWriter(
      (draft) => api.saveDraft("studio", draft),
    );
  }
  studioUiStateRef.current = {
    workspaces,
    creationMode,
    showNegative,
    refOpen,
    structOpen,
  };
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
    productPixelLockMode = "auto",
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
    productProfile,
    productProfileSource,
    productProfiling,
    subjectProtection = null,
    subjectProtectionLoading = false,
    subjectProtectionSource = "",
    variationSource,
    structured,
    structuredSource,
    reverseVideoAnalysis,
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
  } = studioCreationFacts({ creationMode, imageEditProductMode, editSubjectMode, productAsset });
  const optimizingPrompt = optimizingPromptMode === creationMode;
  const productAssetSignature = assetSignature(productAsset);
  const referenceSignature = [productAssetSignature, assetSignature(selected)].join("|");
  const targetModel = cfg?.models?.[category] || {};
  const targetGateway = cfg?.gateways?.[category] || {};
  const targetModelId = targetModel.model_id || targetGateway.model_id || "";
  const targetModelProvider = targetModel.provider || targetGateway.provider || "";
  const promptOptimizationContext = {
    creationMode,
    category,
    subjectMode,
    productGenerationMode,
    duration: category === "video" ? Number(vDuration) : "",
    aspectRatio: category === "video" ? ratio : "",
    resolution: category === "video" ? vResolution : "",
    referenceSignature,
    subjectProfileSource: productProfileSource,
    targetModelId,
    targetModelProvider,
  };
  const currentPromptOptimizationContextKey = promptOptimizationContextKey({
    ...promptOptimizationContext,
    promptText: prompt,
  });
  optimizePromptContextRef.current[creationMode] = currentPromptOptimizationContextKey;
  const promptOptimizationScopeKey = promptOptimizationContextKey(promptOptimizationContext);
  const promptOptimizationRecord = promptOptimizationRecordsRef.current[creationMode];
  const promptForGeneration = (
    category === "video"
    && promptOptimizationRecord?.scope_key === promptOptimizationScopeKey
      ? {
          text: prompt,
          raw_text: promptOptimizationRecord.raw_text,
          optimized_text: promptOptimizationRecord.optimized_text,
          optimizer_model_id: promptOptimizationRecord.optimizer_model_id,
        }
      : prompt
  );

  useEffect(() => {
    workspacesRef.current = workspaces;
  }, [workspaces]);

  function studioAdminImagePatch(config = cfg) {
    const patch = {};
    const defaults = config?.defaults || {};
    if (defaults.image_n) patch.n = Number(defaults.image_n);
    if (defaults.image_size) {
      const ratioKey = ratioKeyForSize(defaults.image_size);
      if (ratioKey) patch.ratio = ratioKey;
      patch.imageQuality = qualityKeyForSize(defaults.image_size);
    }
    return patch;
  }

  function cleanStudioOwnerBaseline(config = cfg) {
    const initial = initialStudioUiStateRef.current;
    return {
      ...initial,
      workspaces: mergeStudioWorkspaceLayers(initial.workspaces, {
        adminImagePatch: studioAdminImagePatch(config),
      }),
    };
  }

  function resetStudioOwnerWorkspace(baseline) {
    latestMeRequestRef.current.invalidate();
    assetOwnerRequestContextRef.current.invalidate();
    loadWorksSeqRef.current += 1;
    resetOwnerTracking();
    resetOwnerReferenceParsing();
    resetOwnerMediaUpload();
    resetOwnerGenerationSubmit();
    workspacesRef.current = baseline.workspaces;
    studioUiStateRef.current = baseline;
    setWorkspaces(baseline.workspaces);
    setCreationMode(baseline.creationMode);
    setShowNegative(baseline.showNegative);
    setRefOpen(baseline.refOpen);
    setStructOpen(baseline.structOpen);
    taskRef.current = null;
    setWorks(null);
    setWorksError("");
    setLightbox(null);
    busyAssetIdsRef.current.clear();
    setBusyAssetIds(new Set());
    setMsg("");
  }

  async function initializeStudioOwnerSession(u, ownerSession, baseline) {
    let localDraft = null;
    let promptDraft = null;
    let variationDraft = null;
    try {
      localDraft = readStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, u?.id);
      promptDraft = readStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, u?.id);
      variationDraft = readStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, u?.id);
    } catch (e) {
      reportBackgroundError(e, "restore scoped studio drafts");
    }

    let cloudDraft = null;
    try {
      const row = await api.getDraft("studio");
      cloudDraft = row?.payload?.workspaces ? row.payload : null;
    } catch (e) {
      reportBackgroundError(e, "load studio cloud draft");
    }

    return ownerSession.commit(() => {
      cloudDraftLoadedRef.current = true;
      const restoredWorkspaces = mergeStudioWorkspaceLayers(baseline.workspaces, {
        localDraft,
        cloudDraft,
      });
      const currentState = studioUiStateRef.current || baseline;
      const restoredMetadata = mergeStudioDraftMetadataLayers(localDraft, cloudDraft);
      const validModes = new Set(CREATION_MODES.map((item) => item.key));
      if (
        Object.prototype.hasOwnProperty.call(restoredMetadata, "creationMode")
        && !validModes.has(restoredMetadata.creationMode)
      ) {
        delete restoredMetadata.creationMode;
      }
      for (const field of ["showNegative", "refOpen"]) {
        if (Object.prototype.hasOwnProperty.call(restoredMetadata, field)) {
          restoredMetadata[field] = !!restoredMetadata[field];
        }
      }
      if (Object.prototype.hasOwnProperty.call(restoredMetadata, "structOpen")) {
        restoredMetadata.structOpen = restoredMetadata.structOpen !== false;
      }
      const nextState = mergeStudioLiveDraftState(baseline, currentState, {
        restoredWorkspaces,
        restoredMetadata,
      });
      setWorkspaces((current) => mergeStudioLiveDraftState(
        baseline,
        { ...currentState, workspaces: current },
        { restoredWorkspaces, restoredMetadata },
      ).workspaces);
      setCreationMode(nextState.creationMode);
      setShowNegative(nextState.showNegative);
      setRefOpen(nextState.refOpen);
      setStructOpen(nextState.structOpen);

      if (localDraft) {
        removeStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, u?.id);
      }

      const promptTransferPresent = !!promptDraft;
      let promptTransferApplied = false;
      let variationTransferApplied = false;
      if (promptDraft) {
        const parsedDraft = parsePromptDraft(promptDraft);
        const draftMode = promptDraftMode(parsedDraft);
        promptTransferApplied = canApplyStudioPromptTransfer(baseline, currentState, draftMode);
        if (promptTransferApplied) {
          setCreationMode(draftMode);
          setWorkspacePatch({
            prompt: parsedDraft.prompt,
            promptDirty: true,
            promptSourceSignature: "",
          }, draftMode);
        }
        removeStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, u?.id);
      }
      if (!promptTransferPresent && variationDraft) {
        variationRestoreContextRef.current = { baseline, current: currentState };
        try {
          if (variationDraft && applyVariationDraft(variationDraft)) {
            removeStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, u?.id);
            variationTransferApplied = true;
          }
        } finally {
          variationRestoreContextRef.current = null;
        }
      }
      ownerSession.observeRestore({
        localDraft,
        cloudDraft,
        promptDraft,
        promptTransferApplied,
        variationDraft,
        variationTransferApplied,
      });
    });
  }

  useEffect(() => {
    const ownerUserId = String(me?.id ?? "").trim();
    const previousOwnerUserId = studioOwnerUserIdRef.current;
    if (!ownerUserId) {
      if (previousOwnerUserId) {
        const seq = ++studioInitSeqRef.current;
        studioOwnerSessionCoordinatorRef.current.cancelScheduled();
        cloudDraftLoadedRef.current = false;
        studioOwnerSessionRef.current = studioOwnerSessionCoordinatorRef.current.bindOwner("", seq);
        studioOwnerUserIdRef.current = "";
        resetStudioOwnerWorkspace(cleanStudioOwnerBaseline());
      }
      cloudDraftWriterRef.current.setOwner(null);
      return undefined;
    }
    if (ownerUserId === previousOwnerUserId && studioOwnerSessionRef.current?.isCurrent()) {
      cloudDraftWriterRef.current.setOwner(ownerUserId);
      return () => cloudDraftWriterRef.current.cancelOwner(ownerUserId);
    }

    const seq = ++studioInitSeqRef.current;
    studioOwnerSessionCoordinatorRef.current.cancelScheduled();
    cloudDraftLoadedRef.current = false;
    if (previousOwnerUserId) cloudDraftWriterRef.current.cancelOwner(previousOwnerUserId);
    cloudDraftWriterRef.current.setOwner(ownerUserId);
    const baseline = cleanStudioOwnerBaseline();
    resetStudioOwnerWorkspace(baseline);
    studioOwnerUserIdRef.current = ownerUserId;
    const ownerSession = studioOwnerSessionCoordinatorRef.current.bindOwner(me?.id, seq);
    studioOwnerSessionRef.current = ownerSession;
    initializeStudioOwnerSession(me, ownerSession, baseline)
      .catch((e) => showError(setMsg, e, "初始化创作工作台失败"));
    loadWorks({ restoreActive: true });
    return () => cloudDraftWriterRef.current.cancelOwner(ownerUserId);
  }, [me?.id]);

  useEffect(() => {
    setUnauthorizedHandler(() => saveStudioSessionDraft("auth_expired", { persistCloud: false }));
    return () => setUnauthorizedHandler(null);
  }, [creationMode, showNegative, refOpen, structOpen, me?.id]);

  useEffect(() => {
    let canceled = false;

    async function initializeStudioSession() {
      const [meResult, configResult] = await Promise.allSettled([api.me(), api.config()]);
      if (canceled) return;

      if (configResult.status === "fulfilled") {
        setCfg(configResult.value);
      } else {
        showError(setMsg, configResult.reason, "加载创作配置失败");
      }

      if (meResult.status === "rejected") {
        redirectOnAuthError(meResult.reason, router, setMsg, "studio session probe");
        return;
      }
      setMe(meResult.value);
    }

    initializeStudioSession().catch((e) => showError(setMsg, e, "初始化创作工作台失败"));
    return () => {
      canceled = true;
      const seq = ++studioInitSeqRef.current;
      studioOwnerSessionCoordinatorRef.current.cancelScheduled();
      studioOwnerSessionCoordinatorRef.current.bindOwner("", seq);
      stopAllTracking();
      revokeUploadedObjectUrls();
      revokeProductObjectUrls();
    };
  }, []);

  useEffect(() => {
    if (!me?.id || !cloudDraftLoadedRef.current) return undefined;
    const ownerSession = studioOwnerSessionRef.current;
    if (!ownerSession?.isCurrent() || ownerSession.ownerUserId !== String(me.id)) return undefined;
    studioOwnerSessionCoordinatorRef.current.schedule(ownerSession, () => {
      syncStudioDraftToCloud("auto", ownerSession)
        .catch((e) => reportBackgroundError(e, "sync studio cloud draft"));
    }, 1600);
    return () => studioOwnerSessionCoordinatorRef.current.cancelScheduled();
  }, [me?.id, workspaces, creationMode, showNegative, refOpen, structOpen]);

  useEffect(() => {
    if (category !== "video" || VIDEO_RATIO_KEYS.has(ratio)) return;
    const current = RATIOS.find((r) => r.key === ratio) || RATIOS[0];
    setRatio(nearestRatio(current.w, current.h, videoRatioOptions()));
  }, [category, ratio]);

  useEffect(() => {
    const mode = creationMode;
    const shouldPreview = (
      isImageEditMode
      && productGenerationMode
      && !portraitGenerationMode
      && productAsset?.url
    );
    if (!shouldPreview) {
      setWorkspacePatch({
        subjectProtection: null,
        subjectProtectionLoading: false,
        subjectProtectionSource: "",
      }, mode);
      return undefined;
    }
    const requestedMode = editMaskMode || "protect_subject";
    const source = `${productAssetSignature}|${requestedMode}`;
    setWorkspacePatch({
      subjectProtectionLoading: true,
      subjectProtectionSource: source,
    }, mode);
    const request = startSubjectProtectionPreview({
      load: () => api.subjectProtectionPreview(productAsset.url, requestedMode),
      onSuccess: (preview) => {
        setWorkspacePatch((current) => {
          const stillCurrent = (
            assetSignature(current.productAsset) === productAssetSignature
            && (current.editMaskMode || "protect_subject") === requestedMode
          );
          if (!stillCurrent) return {};
          return {
            subjectProtection: preview,
            subjectProtectionLoading: false,
            subjectProtectionSource: source,
          };
        }, mode);
      },
      onError: (e) => {
        reportBackgroundError(e, "subject protection preview");
        setWorkspacePatch((current) => {
          const stillCurrent = (
            assetSignature(current.productAsset) === productAssetSignature
            && (current.editMaskMode || "protect_subject") === requestedMode
          );
          if (!stillCurrent) return {};
          return {
            subjectProtection: {
              mode: "none",
              confidence: 0,
              bbox: null,
              width: 0,
              height: 0,
              mask_data_uri: null,
              will_send_mask: false,
              pixel_lock_recommended: false,
              risk_level: "high",
              title: "主体保护预检失败",
              message: errorMessage(e, "无法预检主体保护，请稍后重试或使用透明 PNG。"),
              recommendations: ["可先继续生成，但产品文字和边缘稳定性会下降。"],
            },
            subjectProtectionLoading: false,
            subjectProtectionSource: source,
          };
        }, mode);
      },
    });
    return request.cancel;
  }, [
    creationMode,
    isImageEditMode,
    productGenerationMode,
    portraitGenerationMode,
    productAssetSignature,
    productAsset?.url,
    editMaskMode,
  ]);

  useEffect(() => {
    setPromptSaveCategory(category === "video" ? "video" : "image");
  }, [category]);

  function refreshMe() {
    return latestMeRequestRef.current
      .run(() => api.me(), setMe)
      .catch((e) => reportBackgroundError(e, "refresh current user"));
  }

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
    resetOwnerTracking,
  } = useTaskTracking({
    setCreationMode,
    setMsg,
    refreshMe,
    loadWorks,
    getOwnerSession: () => studioOwnerSessionRef.current,
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
    resetOwnerReferenceParsing,
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
    ratio,
    vDuration,
    videoDurationMaxSeconds: cfg?.video_duration_max_seconds,
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
    getOwnerSession: () => studioOwnerSessionRef.current,
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
    resetOwnerMediaUpload,
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
    subjectProfilePendingRequestRef,
    subjectProfileResultCacheRef,
    getOwnerSession: () => studioOwnerSessionRef.current,
  });

  const {
    submitting,
    submit,
    resetOwnerGenerationSubmit,
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
    prompt: promptForGeneration,
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
    productPixelLockMode,
    vDuration,
    vResolution,
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
    subjectProfilePendingRequestRef,
    subjectProfileResultCacheRef,
    getOwnerSession: () => studioOwnerSessionRef.current,
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
      productPixelLockMode: current.productPixelLockMode || "auto",
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
      subjectProtection: null,
      subjectProtectionLoading: false,
      subjectProtectionSource: "",
    };
  }

  function buildStudioSessionDraft(reason = "manual") {
    const snapshot = workspacesRef.current || workspaces;
    const savedWorkspaces = Object.fromEntries(
      Object.entries(snapshot || {}).map(([mode, current]) => [mode, sanitizeWorkspaceForDraft(current)]),
    );
    const savedAt = studioDraftClockRef.current.next();
    return {
      version: 1,
      reason,
      savedAt,
      creationMode,
      showNegative,
      refOpen,
      structOpen,
      workspaces: savedWorkspaces,
      activeTaskId: taskRef.current?.id || null,
    };
  }

  function saveStudioSessionDraft(reason = "manual", { persistCloud = true } = {}) {
    if (typeof window === "undefined") return;
    try {
      const draft = buildStudioSessionDraft(reason);
      saveStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, me?.id, draft);
      if (persistCloud && me?.id) {
        const queued = cloudDraftWriterRef.current(draft, me?.id);
        queued.catch((e) => reportBackgroundError(e, "save studio cloud draft"));
        return queued;
      }
    } catch (e) {
      reportBackgroundError(e, "save studio session draft");
    }
  }

  function syncStudioDraftToCloud(reason = "manual", ownerSession = studioOwnerSessionRef.current) {
    if (!ownerSession?.isCurrent()) return Promise.resolve();
    return cloudDraftWriterRef.current(
      buildStudioSessionDraft(reason),
      ownerSession.ownerUserId,
    );
  }

  function switchCreationMode(kind) {
    if (!modelEnabled(kind)) {
      const text = `${kind === "video" || kind === "video_edit" ? "视频" : "图片"}模型未启用，请联系管理员配置后再使用。`;
      setMsg(text);
      notify.warn(text);
      return;
    }
    setMsg("");
    invalidatePromptOptimization(creationMode);
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
      subjectProtection: null,
      subjectProtectionLoading: false,
      subjectProtectionSource: "",
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
      reverseVideoAnalysis: null,
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
      reverseVideoAnalysis: null,
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

  async function optimizeDirectPrompt() {
    const source = String(prompt || "").trim();
    if (!source || !promptDirty || optimizingPrompt) return;
    const mode = creationMode;
    const requestId = (optimizePromptRequestRef.current[mode] || 0) + 1;
    optimizePromptRequestRef.current[mode] = requestId;
    const request = { id: requestId, contextKey: currentPromptOptimizationContextKey };
    setOptimizingPromptMode(mode);
    try {
      const referenceType = productAsset
        ? (subjectMode === "portrait" ? "portrait_image" : "product_image")
        : selected?.type === "video"
          ? "reference_video"
          : selected
            ? "reference_image"
            : "text";
      const subjectProfile = productProfile?.structured
        ? {
            ...productProfile.structured,
            ...(productProfile.final_text ? { final_text: productProfile.final_text } : {}),
          }
        : null;
      const result = await api.optimizePrompt(source, {
        category,
        product_mode: productGenerationMode,
        duration: category === "video" ? Number(vDuration) : undefined,
        aspect_ratio: category === "video" ? ratio : undefined,
        resolution: category === "video" ? vResolution : undefined,
        product_lock_mode: category === "video" && productGenerationMode ? "locked" : undefined,
        product_video_template: category === "video" && productGenerationMode ? "prompt_driven" : undefined,
        subject_mode: subjectMode,
        reference_type: referenceType,
        subject_profile: subjectProfile,
        target_model_id: targetModelId || undefined,
        target_model_provider: targetModelProvider || undefined,
      });
      if (!isPromptOptimizationResultCurrent(
        request,
        optimizePromptRequestRef.current[mode],
        optimizePromptContextRef.current[mode],
      )) return;
      const optimized = String(result?.prompt || "").trim();
      if (!optimized) throw new Error("优化模型未返回有效提示词");
      promptOptimizationRecordsRef.current[mode] = {
        raw_text: source,
        optimized_text: optimized,
        optimizer_model_id: result.optimizer_model_id || result.model_id || "",
        scope_key: promptOptimizationScopeKey,
      };
      setWorkspacePatch({
        prompt: optimized,
        promptDirty: true,
        promptSourceSignature: "",
      }, mode);
      setMsg(`提示词已优化 · ${result.model_id || "gemini-3.5-flash-low"}`);
      notify.success("提示词已优化，可继续修改或直接生成。 ");
    } catch (e) {
      if (!isPromptOptimizationResultCurrent(
        request,
        optimizePromptRequestRef.current[mode],
        optimizePromptContextRef.current[mode],
      )) return;
      const text = errorMessage(e, "提示词优化失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      if (requestId === optimizePromptRequestRef.current[mode]) {
        setOptimizingPromptMode((current) => (current === mode ? "" : current));
      }
    }
  }

  function invalidatePromptOptimization(mode = creationMode) {
    optimizePromptRequestRef.current[mode] = (optimizePromptRequestRef.current[mode] || 0) + 1;
    setOptimizingPromptMode((current) => (current === mode ? "" : current));
  }

  function updatePromptFromUser(valueOrUpdater) {
    invalidatePromptOptimization();
    setPrompt(valueOrUpdater);
  }

  function changeEditSubjectMode(value) {
    invalidatePromptOptimization();
    delete promptOptimizationRecordsRef.current[creationMode];
    setEditSubjectMode(value);
  }

  function clearCurrentWorkspace() {
    for (const { key: mode } of CREATION_MODES) {
      invalidatePromptOptimization(mode);
    }
    resetOwnerReferenceParsing();
    resetOwnerMediaUpload();
    subjectProfilePendingRequestRef.current = null;
    subjectProfileResultCacheRef.current = null;
    variationRestoreContextRef.current = null;
    optimizePromptContextRef.current = {};
    promptOptimizationRecordsRef.current = {};
    setWorkspaces(clearAllWorkspaceContent);
    setPromptLibraryOpen(false);
    setStructOpen(true);
    setMsg("");
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
    updatePromptFromUser((current) => {
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
    const workspacePatch = {
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
    };
    const liveState = studioUiStateRef.current || {
      workspaces: { image_edit: workspace },
      creationMode,
      showNegative,
      refOpen,
      structOpen,
    };
    const restoreContext = variationRestoreContextRef.current || {
      baseline: liveState,
      current: liveState,
    };
    const transition = applyStudioVariationTransferState(
      restoreContext.baseline,
      restoreContext.current,
      {
        metadataPatch: {
          creationMode: "image_edit",
          showNegative: false,
          refOpen: false,
          structOpen: false,
        },
        workspacePatch,
      },
    );
    if (!transition.applied) return false;
    setCreationMode(transition.state.creationMode);
    setWorkspacePatch(transition.workspacePatch, "image_edit");
    setStructOpen(transition.state.structOpen);
    setRefOpen(transition.state.refOpen);
    setShowNegative(transition.state.showNegative);
    setMsg("已带入历史图片，可直接生成变体，也可以先微调提示词。");
    return true;
  }

  async function unlock(asset) {
    const ownerRequest = assetOwnerRequestContextRef.current.capture();
    if (!ownerRequest.isCurrent()) return;
    if (busyAssetIdsRef.current.has(asset.id)) return;
    const cost = Number(asset.unlock_cost ?? cfg?.models?.[asset.type]?.unlock_cost ?? 0);
    const balance = Number(me?.balance_credits ?? 0);
    if (!window.confirm(`解锁${asset.type === "video" ? "视频" : "图片"}高清将扣除 ${cost} 积分，当前余额 ${balance}，确认继续？`)) return;
    busyAssetIdsRef.current.add(asset.id);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      const updated = await api.unlock(asset.id);
      if (!ownerRequest.isCurrent()) return;
      const capturedTaskId = task?.id;
      if (capturedTaskId) {
        api.task(capturedTaskId)
          .then((nextTask) => ownerRequest.commit(() => setTask(
            (prev) => prev?.id === capturedTaskId ? nextTask : prev,
          )))
          .catch((e) => {
            if (ownerRequest.isCurrent()) reportBackgroundError(e, "refresh active task after unlock");
          });
      }
      ownerRequest.commit(() => {
        if (lightbox && lightbox.id === asset.id) setLightbox(updated);
        refreshMe();
        loadWorks();
      });
    } catch (e) {
      ownerRequest.commit(() => setMsg(e.message));
    } finally {
      ownerRequest.commit(() => {
        busyAssetIdsRef.current.delete(asset.id);
        setBusyAssetIds(new Set(busyAssetIdsRef.current));
      });
    }
  }

  async function download(asset) {
    const ownerRequest = assetOwnerRequestContextRef.current.capture();
    if (!ownerRequest.isCurrent()) return;
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
      ownerRequest.commit(() => setMsg(`已开始下载 ${filename}`));
    } catch (e) {
      ownerRequest.commit(() => setMsg(e.message));
    } finally {
      ownerRequest.commit(() => {
        busyAssetIdsRef.current.delete(asset.id);
        setBusyAssetIds(new Set(busyAssetIdsRef.current));
      });
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
  const missingRequiredSource = isEditMode && !productAsset;
  const missingRequiredSourceLabel = portraitGenerationMode
    ? "请先上传人物"
    : productGenerationMode
      ? "请先上传产品"
      : "请先上传图片";

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
          disabled={missingRequiredSource || generationSubmitDisabled({
            submitting,
            busy: parsing || uploading || reversing || productProfiling,
            currentTask: task,
            nextCategory: category,
            currentModelEnabled,
          })}
          title={missingRequiredSource ? `${missingRequiredSourceLabel}后再生成` : undefined}
          className="btn-primary btn-lg min-w-28 shrink-0 px-4 sm:min-w-32 sm:px-6"
        >
          {(submitting || running) && (
            <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-white/35 border-t-white" aria-hidden />
          )}
          {missingRequiredSource ? missingRequiredSourceLabel : submitLabel}
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
                  onPromptChange={updatePromptFromUser}
                  onPromptDirty={setPromptDirty}
                  onAppendPrompt={(text) => applyLibraryPrompt(text, "append")}
                  onTogglePromptLibrary={() => setPromptLibraryOpen((open) => !open)}
                  onOptimizePrompt={optimizeDirectPrompt}
                  canOptimizePrompt={Boolean(prompt.trim() && promptDirty)}
                  optimizingPrompt={optimizingPrompt}
                  onClearWorkspace={clearCurrentWorkspace}
                  canClearWorkspace={Boolean(
                    prompt.trim()
                    || negative.trim()
                    || url.trim()
                    || selected
                    || productAsset
                    || assets.length
                    || Object.keys(structured || {}).length
                    || productProfile
                    || variationSource
                    || parsing
                    || uploading
                    || reversing
                    || productProfiling
                    || subjectProtection
                    || optimizingPrompt
                  )}
                  onSubjectModeChange={changeEditSubjectMode}
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
                    showNegative={showNegative}
                    onToggleNegative={() => setShowNegative((s) => !s)}
                    seed={seed}
                    onSeedChange={setSeed}
                    editMaskMode={editMaskMode}
                    onEditMaskModeChange={setEditMaskMode}
                    productPixelLockMode={productPixelLockMode}
                    onProductPixelLockModeChange={setProductPixelLockMode}
                    subjectProtection={subjectProtection}
                    subjectProtectionLoading={subjectProtectionLoading}
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
                reverseVideoAnalysis={reverseVideoAnalysis}
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
