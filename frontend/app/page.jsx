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
import AssetPickerDialog from "../components/AssetPickerDialog";
import { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import StudioGenerationControls from "../components/StudioGenerationControls";
import { useToast } from "../components/ToastProvider";
import useGenerationSubmit from "../hooks/useGenerationSubmit";
import useMediaUpload from "../hooks/useMediaUpload";
import useRecentReverseOperations from "../hooks/useRecentReverseOperations";
import useReverseBatches from "../hooks/useReverseBatches";
import useReferenceParsing from "../hooks/useReferenceParsing";
import useStudioWorkspaceState from "../hooks/useStudioWorkspaceState";
import useTaskTracking from "../hooks/useTaskTracking";
import useStudioQuoteConfirmation from "../hooks/useStudioQuoteConfirmation";
import useVisibleItemWindow from "../hooks/useVisibleItemWindow";
import {
  normalizeReverseOperation,
  normalizeReverseOperationFeedback,
  normalizeReverseResultRevision,
  normalizeReverseResultRevisionList,
  reverseOperationResult,
  reverseOperationResumeCandidates,
} from "../lib/reverseOperations";
import { assetReferenceUrl, dedupeAssets, unifiedAssetKey } from "../lib/unifiedAssets";
import { buildReverseBatchItemPayload, MAX_REVERSE_BATCH_ITEMS } from "../lib/reverseBatches";
import {
  CREATION_MODES,
  MAX_PRODUCT_DETAIL_IMAGES,
  RATIOS,
  STUDIO_SESSION_DRAFT_KEY,
  STUDIO_VARIATION_DRAFT_KEY,
  VIDEO_RATIO_KEYS,
  creationModeLabel,
} from "./studio/constants";
import StudioMessageBar from "./studio/StudioMessageBar";
import {
  filterModelOptions,
  MODEL_SELECTION_USES,
  normalizeModelOptions,
  readModelSelections,
  resolveModelConfigId,
  strictMultiReferenceLimit,
  validateMultiReferenceSelection,
  writeModelSelections,
} from "./studio/StudioModelSelector";
import StudioModeTabs from "./studio/StudioModeTabs";
import StudioPromptWorkspace from "./studio/StudioPromptWorkspace";
import StudioReferencePanel from "./studio/StudioReferencePanel";
import StudioRecentReversePanel from "./studio/StudioRecentReversePanel";
import StudioReverseResultPanel from "./studio/StudioReverseResultPanel";
import StudioResults from "./studio/StudioResults";
import StudioStructuredEditor from "./studio/StudioStructuredEditor";
import {
  clearPendingStudioActionRequest,
  pendingStudioActionRequestId,
} from "./studio/generationQuote";
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
import {
  buildStudioDerivedViewState,
  buildStudioQuoteInputRevision,
  modelEnabledForConfig,
  studioCreationFacts,
} from "./studio/viewModel";
import {
  applyPromptOptimizationDecision,
  isPromptOptimizationResultCurrent,
  promptOptimizationContextKey,
  undoPromptOptimization,
} from "./studio/promptOptimization";
import {
  normalizeProductVideoStrategyKey,
  resolveProductVideoStrategySelection,
} from "./studio/productVideoStrategy";
import { normalizeReverseConfig, validateReverseConfig } from "./studio/reverseConfig";
import {
  resolveCatalogModelIntent,
  resolveStudioWorkflowPreset,
  studioWorkflowSlug,
} from "./studio/workflowPreset";
import {
  buildReverseOperationRequestSnapshotV3,
  buildReverseSnapshotV3,
  workspacePatchFromReverseSnapshot,
} from "./studio/reverseSnapshot";
import {
  applyReverseResultApplication,
  mergeConfirmedReverseResultApplication,
  normalizePendingReverseResult,
  resolveReverseResultApplicationSelection,
  selectedImageEvidenceForApplication,
  undoReverseResultApplication,
} from "./studio/reverseResultApplication";
import {
  composeStoryboardShotPrompt,
  storyboardResultShots,
  storyboardResultWithShots,
  storyboardShotIdentity,
} from "./studio/storyboard";
import {
  bindGenerationTaskToVideoCompositionShot,
  normalizeVideoComposition,
  videoCompositionShotTracksGenerationTask,
} from "./studio/videoComposition";
import {
  compactPendingReverseResultForStudioDraft,
  compactReverseOperationForStudioDraft,
  compactStudioDraftValue,
} from "./studio/studioDraft";
import { clearAllWorkspaceContent, clearWorkspaceContent } from "./studio/workspaceReset";

function positivePromptDraftInteger(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function normalizedPromptDraft(raw) {
  const reverseSnapshot = raw.reverse_snapshot_v3 || raw.reverse_snapshot_v2 || null;
  const structured = raw.structured && typeof raw.structured === "object" && !Array.isArray(raw.structured)
    ? raw.structured
    : {};
  const generation = raw.generation && typeof raw.generation === "object" && !Array.isArray(raw.generation)
    ? raw.generation
    : {};
  const snapshotRecipeId = reverseSnapshot?.creation_recipe_id;
  const snapshotRecipeVersion = reverseSnapshot?.creation_recipe_version;
  const snapshotShareSlug = reverseSnapshot?.creation_recipe_share_slug;
  const snapshotSource = reverseSnapshot?.creation_recipe_source;
  const requestedSource = String(raw.creationRecipeSource || snapshotSource || "");
  return {
    prompt: String(raw.prompt || ""),
    negative: String(raw.negative || ""),
    structured,
    generation,
    category: String(raw.category || ""),
    creationMode: String(raw.creationMode || ""),
    creationRecipeId: positivePromptDraftInteger(raw.creationRecipeId || snapshotRecipeId),
    creationRecipeVersion: positivePromptDraftInteger(raw.creationRecipeVersion || snapshotRecipeVersion),
    creationRecipeShareSlug: String(raw.creationRecipeShareSlug || snapshotShareSlug || ""),
    creationRecipeSource: ["owner", "public", "share"].includes(requestedSource) ? requestedSource : "",
    savedAt: Number(raw.savedAt || 0),
    reverseSnapshot,
    legacyReverse: Boolean(raw.legacy_reverse),
  };
}

function parsePromptDraft(raw) {
  if (raw && typeof raw === "object") return normalizedPromptDraft(raw);
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") return normalizedPromptDraft(parsed);
  } catch (_e) {
    // Legacy prompt drafts were stored as plain strings.
  }
  return {
    prompt: String(raw || ""),
    negative: "",
    structured: {},
    generation: {},
    category: "",
    creationMode: "",
    creationRecipeId: null,
    creationRecipeVersion: null,
    creationRecipeShareSlug: "",
    creationRecipeSource: "",
    savedAt: 0,
    reverseSnapshot: null,
    legacyReverse: false,
  };
}

function reverseResultPayload(pending) {
  if (!pending || typeof pending !== "object") return null;
  if (pending.payload && typeof pending.payload === "object") return pending.payload;
  if (pending.result && typeof pending.result === "object") return pending.result;
  return pending;
}

function replaceReverseResultPayload(pending, payload, dirty = true) {
  if (!pending || typeof pending !== "object") return payload;
  if (pending.payload && typeof pending.payload === "object") {
    return { ...pending, payload, dirty };
  }
  if (pending.result && typeof pending.result === "object") {
    return { ...pending, result: payload, dirty };
  }
  return { ...payload, dirty };
}

function reverseResultEnvelope(operation, result = reverseOperationResult(operation), dirty = false) {
  if (!operation || !result) return null;
  const snapshot = operation.workspace_snapshot_v3 || operation.workspace_snapshot_v2 || {};
  const sourceSignature = operation.request_context?.source_signature || snapshot.source_signature || "";
  return {
    kind: "pending_reverse_review",
    operation_id: operation.id,
    category: operation.target === "video" ? "video" : "image",
    target: operation.target,
    source_signature: sourceSignature,
    received_at: new Date().toISOString(),
    dirty: Boolean(dirty),
    result,
  };
}

function bindGenerationTaskToPendingReverseResult(pending, {
  operationId,
  shotId,
  task,
}) {
  const result = reverseResultPayload(pending);
  if (!result) return null;
  const composition = normalizeVideoComposition(
    result.video_composition,
    storyboardResultShots(result),
    operationId,
  );
  const binding = bindGenerationTaskToVideoCompositionShot(composition, {
    shotId,
    task,
  });
  return {
    binding,
    result: binding.changed
      ? { ...result, video_composition: binding.composition }
      : result,
  };
}

const GENERATION_SOURCE_REVISION_SOURCES = new Set(["applied"]);
const REVERSE_APPLY_PARENT_SOURCES = new Set(["normalized", "user_edit"]);

function positiveRevisionId(value) {
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : null;
}

function findGenerationSourceRevision(revisions, { revisionId, version, operationId }) {
  const id = positiveRevisionId(revisionId);
  const appliedVersion = positiveRevisionId(version);
  const ownerOperationId = positiveRevisionId(operationId);
  const candidates = (Array.isArray(revisions) ? revisions : []).filter((revision) => (
    GENERATION_SOURCE_REVISION_SOURCES.has(String(revision?.source || ""))
    && (!ownerOperationId || Number(revision?.operation_id) === ownerOperationId)
  ));
  return (id ? candidates.find((revision) => Number(revision?.id) === id) : null)
    || (appliedVersion
      ? candidates.find((revision) => Number(revision?.version) === appliedVersion)
      : null)
    || null;
}

function findReverseApplyParentRevision(revisions, operationId) {
  const ownerOperationId = positiveRevisionId(operationId);
  return (Array.isArray(revisions) ? revisions : [])
    .filter((revision) => (
      REVERSE_APPLY_PARENT_SOURCES.has(String(revision?.source || ""))
      && (!ownerOperationId || Number(revision?.operation_id) === ownerOperationId)
    ))
    .sort((left, right) => Number(right.version || 0) - Number(left.version || 0))[0] || null;
}

function mergeReverseResultRevisions(current, additions) {
  const merged = [...(Array.isArray(current) ? current : [])];
  for (const revision of additions || []) {
    if (revision?.id && !merged.some((item) => Number(item?.id) === Number(revision.id))) {
      merged.push(revision);
    }
  }
  return merged.sort((left, right) => Number(left.version || 0) - Number(right.version || 0));
}

function clearsReviewedImageEvidence(parentRevision, payload) {
  const parentEvidence = parentRevision?.payload?.image_evidence;
  return Array.isArray(parentEvidence)
    && parentEvidence.length > 0
    && Array.isArray(payload?.image_evidence)
    && payload.image_evidence.length === 0;
}

function promptDraftMode(draft) {
  if (draft.creationMode && CREATION_MODES.some((item) => item.key === draft.creationMode)) {
    return draft.creationMode;
  }
  return draft.category === "video" ? "video" : "image";
}

function promptDraftGenerationPatch(generation) {
  if (!generation || typeof generation !== "object" || Array.isArray(generation)) return {};
  const patch = {};
  if (generation.ratio) patch.ratio = String(generation.ratio);
  if (generation.image_quality) patch.imageQuality = String(generation.image_quality);
  const count = positivePromptDraftInteger(generation.count ?? generation.n);
  if (count) patch.n = count;
  if (generation.seed !== undefined && generation.seed !== null) patch.seed = String(generation.seed);
  const duration = positivePromptDraftInteger(generation.duration);
  if (duration) patch.vDuration = duration;
  if (generation.resolution) patch.vResolution = String(generation.resolution);
  const productVideoTemplate = normalizeProductVideoStrategyKey(
    generation.product_video_template ?? generation.productVideoTemplate,
  );
  if (productVideoTemplate) patch.productVideoTemplate = productVideoTemplate;
  return patch;
}

const ACTIVE_REVERSE_STATUSES = new Set(["queued", "running", "needs_confirmation"]);

function hasActiveReverseOperations(workspaces) {
  return Object.values(workspaces || {}).some((current) => (
    ACTIVE_REVERSE_STATUSES.has(current?.reverseOperation?.status)
    || ACTIVE_REVERSE_STATUSES.has(current?.profileOperation?.status)
  ));
}

function reverseSnapshotModelSelections(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const nested = snapshot.workspace_snapshot_v2 && typeof snapshot.workspace_snapshot_v2 === "object"
    ? snapshot.workspace_snapshot_v2
    : {};
  const selections = snapshot.model_selections || nested.model_selections;
  return selections && typeof selections === "object" ? selections : null;
}

export default function Home() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [modelSelections, setModelSelections] = useState({
    vision: null,
    image: null,
    video: null,
    prompt: null,
  });

  // creation state
  const [creationMode, setCreationMode] = useState("image"); // image | video | image_edit | video_edit
  const [showNegative, setShowNegative] = useState(false);
  const [promptSaveTitle, setPromptSaveTitle] = useState("");
  const [promptSaveCategory, setPromptSaveCategory] = useState("image");
  const [promptSaveFavorite, setPromptSaveFavorite] = useState(false);
  const [optimizingPromptMode, setOptimizingPromptMode] = useState("");
  const [promptOptimizationProposals, setPromptOptimizationProposals] = useState({});
  const [promptOptimizationSettings, setPromptOptimizationSettings] = useState({});
  const [promptOptimizationUndos, setPromptOptimizationUndos] = useState({});
  const [reverseActionBusy, setReverseActionBusy] = useState("");
  const [recentReverseBusyId, setRecentReverseBusyId] = useState(null);
  const [assetPicker, setAssetPicker] = useState(null);

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
  const subjectProfileOperationsRef = useRef({});
  const reverseResumeOperationsRef = useRef([]);
  const localReverseDraftRef = useRef({ owner: "", active: false });
  const optimizePromptRequestRef = useRef({});
  const optimizePromptContextRef = useRef({});
  const promptOptimizationRecordsRef = useRef({});
  const studioActionPendingRequestRef = useRef([]);
  const shotGenerationRevisionQueueRef = useRef(new Map());
  const modelSelectionsRef = useRef(modelSelections);
  const modelSelectionOwnerRef = useRef("");
  const configRefreshSeqRef = useRef(0);
  const workflowBootstrapRef = useRef("");
  const catalogModelBootstrapRef = useRef("");
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
    setProductVideoTemplate,
    setVideoAnalysisPreset,
    setReverseConfig,
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
    productVideoTemplate = "prompt_driven",
    videoAnalysisPreset,
    url,
    appliedUrl,
    parsing,
    uploading,
    reversing,
    reverseOperation: workspaceReverseOperation,
    profileOperation,
    assets,
    selected,
    lastFrameAsset,
    productAsset,
    productDetailAssets = [],
    productProfile,
    productProfileSource,
    portraitProfile,
    portraitProfileSource,
    productProfiling,
    subjectProtection = null,
    subjectProtectionLoading = false,
    subjectProtectionSource = "",
    variationSource,
    structured,
    structuredBaseline,
    structuredDirty,
    structuredSource,
    reverseVideoAnalysis,
    reverseSources,
    reverseConfig,
    pendingReverseResult,
    reverseResultTab,
    reverseResultSchemaVersion,
    reverseAppliedVersion,
    reverseAppliedRevisionId,
    reverseUndoSnapshot,
    reverseResultRevisions,
    reverseFeedback,
    batchReverseAssets = [],
    creationRecipeId,
    creationRecipeVersion,
    creationRecipeShareSlug,
    creationRecipeSource,
    promptSourceSignature,
    negativeTouched,
    promptDirty,
  } = workspace;
  const promptOptimizationProposal = promptOptimizationProposals[creationMode] || null;
  const promptOptimizationSetting = promptOptimizationSettings[creationMode] || {
    direction: "faithful",
    targetLanguage: "en",
  };
  const reverseBatchEnabled = cfg?.features?.reverse_batch_enabled !== false;
  const videoCompositionEnabled = cfg?.features?.video_composition_enabled !== false;
  const reproductionAssessmentEnabled = cfg?.features?.reproduction_assessment_enabled !== false;
  const recipesEnabled = cfg?.features?.recipes_enabled !== false;
  const {
    operations: recentReverseOperations,
    loading: recentReverseLoading,
    error: recentReverseError,
    refresh: refreshRecentReverseOperations,
    upsert: upsertRecentReverseOperation,
  } = useRecentReverseOperations({ enabled: Boolean(me?.id), limit: 5 });
  const {
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
  } = studioCreationFacts({ creationMode, imageEditProductMode, editSubjectMode, productAsset });
  const {
    batch: reverseBatch,
    recentBatches: recentReverseBatches,
    busyAction: reverseBatchBusyAction,
    createBatch: createReverseBatch,
    cancelBatch: cancelReverseBatch,
    openBatch: openReverseBatch,
  } = useReverseBatches({
    ownerKey: String(me?.id || ""),
    enabled: reverseBatchEnabled,
    onError: (error) => reportBackgroundError(error, "reverse batch tracking"),
  });

  useEffect(() => {
    if (!reverseBatchEnabled && assetPicker?.role === "reverse_batch") {
      setAssetPicker(null);
    }
  }, [assetPicker?.role, reverseBatchEnabled]);
  const activeSubjectProfile = portraitGenerationMode ? portraitProfile : productProfile;
  const activeSubjectProfileSource = portraitGenerationMode ? portraitProfileSource : productProfileSource;
  const optimizingPrompt = optimizingPromptMode === creationMode;
  const promptReadyForOptimization = Boolean(prompt.trim() && promptDirty);
  const productAssetSignature = assetSignature(productAsset);
  const reverseResumeOperations = reverseResumeOperationsRef.current;
  const allModelOptions = normalizeModelOptions(cfg);
  const generationModelOptions = filterModelOptions(allModelOptions[category], {
    use: category,
    creationMode,
    selected,
    productAsset,
    subjectMode,
  });
  const visionModelOptions = filterModelOptions(allModelOptions.vision, {
    use: "vision",
    creationMode,
    selected,
    productAsset,
    subjectMode,
  });
  const promptModelOptions = filterModelOptions(allModelOptions.prompt, {
    use: "prompt",
    creationMode,
    selected,
    productAsset,
  });
  const modelOptionsForSelection = {
    ...allModelOptions,
    [category]: generationModelOptions,
    vision: visionModelOptions,
    prompt: promptModelOptions,
  };
  const selectedGenerationModelConfigId = resolveModelConfigId(
    generationModelOptions,
    modelSelections[category],
  );
  const selectedVisionModelConfigId = resolveModelConfigId(
    visionModelOptions,
    modelSelections.vision,
  );
  const selectedPromptModelConfigId = resolveModelConfigId(
    promptModelOptions,
    modelSelections.prompt,
  );
  const effectiveModelSelections = {
    ...modelSelections,
    [category]: selectedGenerationModelConfigId,
    vision: selectedVisionModelConfigId,
    prompt: selectedPromptModelConfigId,
  };
  const selectedGenerationModel = generationModelOptions.find(
    (option) => option.id === selectedGenerationModelConfigId,
  ) || null;
  const modelDeclaresFirstLastFrame = Boolean(
    selectedGenerationModel?.capabilities
    && !Array.isArray(selectedGenerationModel.capabilities)
    && selectedGenerationModel.capabilities.first_last_frame === true,
  );
  const firstLastFrameEnabled = Boolean(
    category === "video"
    && creationMode === "video"
    && subjectMode === "general"
    && modelDeclaresFirstLastFrame,
  );
  const effectiveLastFrameAsset = firstLastFrameEnabled ? lastFrameAsset : null;
  const referenceSignature = [
    productAssetSignature,
    assetSignature(selected),
    assetSignature(effectiveLastFrameAsset),
  ].join("|");
  const productVideoStrategySelection = resolveProductVideoStrategySelection(
    selectedGenerationModel?.capabilities,
    productVideoTemplate,
  );
  const effectiveProductVideoTemplate = productVideoStrategySelection.effectiveValue;
  const productVideoStrategyUnsupported = Boolean(
    category === "video"
    && productGenerationMode
    && !productVideoStrategySelection.supported,
  );
  const baseProductReferenceUrls = [productAsset, selected]
    .map(assetReferenceUrl)
    .filter(Boolean);
  const productDetailUrls = productDetailAssets.map(assetReferenceUrl).filter(Boolean);
  const totalProductReferenceCount = new Set([...baseProductReferenceUrls, ...productDetailUrls]).size;
  const productDetailValidation = productAsset
    ? validateMultiReferenceSelection(
        selectedGenerationModel,
        totalProductReferenceCount,
        productDetailUrls.length,
      )
    : { ok: false, limit: 0, message: "请先选择产品主题图" };
  const productDetailModelLimit = strictMultiReferenceLimit(selectedGenerationModel);
  const productDetailLimit = Math.min(
    MAX_PRODUCT_DETAIL_IMAGES,
    Math.max(
      0,
      productDetailModelLimit
        - new Set(baseProductReferenceUrls).size
        - (productAsset ? 0 : 1),
    ),
  );
  const selectedPromptModel = promptModelOptions.find(
    (option) => option.id === selectedPromptModelConfigId,
  ) || null;
  const studioCfg = cfg?.model_options ? {
    ...cfg,
    models: {
      ...(cfg.models || {}),
      [category]: {
        ...(cfg.models?.[category] || {}),
        ...(selectedGenerationModel || {}),
        enabled: generationModelOptions.length > 0,
      },
    },
  } : cfg;
  const targetModel = cfg?.models?.[category] || {};
  const targetGateway = cfg?.gateways?.[category] || {};
  const targetModelId = selectedGenerationModel?.model_id || targetModel.model_id || targetGateway.model_id || "";
  const targetModelProvider = selectedGenerationModel?.provider || targetModel.provider || targetGateway.provider || "";
  const promptOptimizationContext = {
    creationMode,
    category,
    subjectMode,
    productGenerationMode,
    duration: category === "video" ? Number(vDuration) : "",
    aspectRatio: category === "video" ? ratio : "",
    resolution: category === "video" ? vResolution : "",
    productLockMode: category === "video" && productGenerationMode ? "locked" : "",
    productVideoTemplate: category === "video" && productGenerationMode
      ? effectiveProductVideoTemplate || ""
      : "",
    referenceSignature,
    subjectProfileSource: activeSubjectProfileSource,
    targetModelId,
    targetModelProvider,
    optimizationDirection: promptOptimizationSetting.direction,
    optimizationTargetLanguage: promptOptimizationSetting.targetLanguage,
    targetModelConfigId: selectedGenerationModelConfigId,
    optimizerModelConfigId: selectedPromptModelConfigId,
  };
  const currentPromptOptimizationContextKey = promptOptimizationContextKey({
    ...promptOptimizationContext,
    promptText: prompt,
  });
  optimizePromptContextRef.current[creationMode] = currentPromptOptimizationContextKey;
  const promptOptimizationScopeKey = promptOptimizationContextKey(promptOptimizationContext);
  const promptOptimizationRecord = promptOptimizationRecordsRef.current[creationMode];
  const promptForGeneration = (
    promptOptimizationRecord?.scope_key === promptOptimizationScopeKey
      ? {
          text: prompt,
          raw_text: promptOptimizationRecord.raw_text,
          optimized_text: promptOptimizationRecord.optimized_text,
          optimizer_model_id: promptOptimizationRecord.optimizer_model_id,
          optimizer_model_config_id: promptOptimizationRecord.optimizer_model_config_id,
          optimization_direction: promptOptimizationRecord.optimization_direction,
          optimization_kind: promptOptimizationRecord.optimization_kind,
          compiler_metadata: promptOptimizationRecord.compiler_metadata,
          change_summary: promptOptimizationRecord.change_summary,
          warnings: promptOptimizationRecord.warnings,
        }
      : prompt
  );
  const {
    requestQuoteConfirmation,
    invalidateQuote,
  } = useStudioQuoteConfirmation({ balanceCredits: me?.balance_credits });
  const quoteInputRevision = buildStudioQuoteInputRevision({
    owner_id: me?.id || null,
    creation_mode: creationMode,
    category,
    prompt: promptForGeneration,
    negative,
    selected: assetSignature(selected),
    last_frame_asset: assetSignature(effectiveLastFrameAsset),
    product_asset: assetSignature(productAsset),
    product_detail_assets: productDetailAssets.map(assetSignature),
    batch_reverse_assets: batchReverseAssets.map(assetSignature),
    structured,
    ratio,
    image_quality: imageQuality,
    count: n,
    seed,
    edit_mask_mode: editMaskMode,
    product_pixel_lock_mode: productPixelLockMode,
    video_duration: vDuration,
    video_resolution: vResolution,
    product_video_template: category === "video" && productGenerationMode
      ? effectiveProductVideoTemplate
      : null,
    reverse_config: reverseConfig,
    reverse_sources: reverseSources,
    model_selections: effectiveModelSelections,
    prompt_optimization_direction: promptOptimizationSetting.direction,
    prompt_optimization_target_language: promptOptimizationSetting.targetLanguage,
    reverse_operation_id: pendingReverseResult?.operation_id || workspaceReverseOperation?.id || null,
    reverse_revision_id: reverseAppliedRevisionId || null,
    pending_reverse_result: pendingReverseResult,
  });
  const previousQuoteInputRevisionRef = useRef(quoteInputRevision);

  useEffect(() => {
    if (previousQuoteInputRevisionRef.current !== quoteInputRevision) {
      previousQuoteInputRevisionRef.current = quoteInputRevision;
      invalidateQuote("工作区输入或模型已变化，本次操作已取消，请重新执行。");
    }
  }, [quoteInputRevision, invalidateQuote]);

  useEffect(() => {
    if (!lastFrameAsset || !selectedGenerationModelConfigId) return;
    if (firstLastFrameEnabled) return;
    setWorkspacePatch({ lastFrameAsset: null });
    if (category === "video" && creationMode === "video") {
      setMsg("当前模式或模型不支持首尾帧，已清除尾帧素材。");
    }
  }, [
    category,
    creationMode,
    firstLastFrameEnabled,
    lastFrameAsset,
    selectedGenerationModelConfigId,
  ]);

  useEffect(() => {
    if (
      category !== "video"
      || !productGenerationMode
      || !productVideoStrategySelection.supported
      || !effectiveProductVideoTemplate
      || effectiveProductVideoTemplate === productVideoTemplate
    ) return;
    setProductVideoTemplate(effectiveProductVideoTemplate);
    invalidatePromptOptimization(creationMode);
  }, [
    category,
    creationMode,
    productGenerationMode,
    productVideoStrategySelection.supported,
    effectiveProductVideoTemplate,
    productVideoTemplate,
  ]);

  useEffect(() => {
    workspacesRef.current = workspaces;
  }, [workspaces]);

  modelSelectionsRef.current = modelSelections;

  useEffect(() => {
    const owner = String(me?.id || "");
    if (!owner || !cfg) return;
    let preferred = modelSelectionsRef.current;
    if (modelSelectionOwnerRef.current !== owner) {
      preferred = readModelSelections(window.localStorage, owner);
      modelSelectionOwnerRef.current = owner;
    }
    const next = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [
      use,
      resolveModelConfigId(modelOptionsForSelection[use], preferred?.[use]),
    ]));
    const fallbackUses = MODEL_SELECTION_USES.filter((use) => (
      preferred?.[use]
      && next[use] !== Number(preferred[use])
      && allModelOptions[use].length > 0
    ));
    modelSelectionsRef.current = next;
    setModelSelections((current) => (
      JSON.stringify(current) === JSON.stringify(next) ? current : next
    ));
    writeModelSelections(window.localStorage, owner, next);
    if (fallbackUses.length > 0) {
      const labels = fallbackUses.map((use) => ({
        vision: "反推",
        image: "图片",
        video: "视频",
        prompt: "提示词优化",
      }[use])).join("、");
      notify.warn(`${labels}已选模型不再可用，已切换为默认模型。`);
    }
  }, [
    me?.id,
    cfg,
    creationMode,
    category,
    selected?.type,
    selected?.id,
    selected?.url,
    productAsset?.id,
    productAsset?.url,
  ]);

  useEffect(() => {
    const owner = String(me?.id || "");
    const previous = localReverseDraftRef.current;
    if (!owner || !cloudDraftLoadedRef.current) {
      localReverseDraftRef.current = { owner, active: false };
      return;
    }
    const active = hasActiveReverseOperations(workspaces);
    if (previous.owner === owner && !active && !previous.active) return;
    localReverseDraftRef.current = { owner, active };
    saveStudioSessionDraft(active ? "reverse_operation_active" : "reverse_operation_settled", {
      persistCloud: false,
    });
  }, [me?.id, workspaces, creationMode, showNegative, refOpen, structOpen]);

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
    cancelOwnerProfileOperationsAndReset();
    resetOwnerGenerationSubmit();
    reverseResumeOperationsRef.current = [];
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
      const persistedSelections = readModelSelections(window.localStorage, u?.id);
      const hasPersistedSelection = MODEL_SELECTION_USES.some((use) => persistedSelections?.[use]);
      const draftSelections = localDraft?.modelSelections || cloudDraft?.modelSelections;
      if (!hasPersistedSelection && draftSelections) {
        applyModelSelectionPreferences(draftSelections, u?.id);
      }
      const restoredWorkspaces = mergeStudioWorkspaceLayers(baseline.workspaces, {
        localDraft,
        cloudDraft,
      });
      reverseResumeOperationsRef.current = reverseOperationResumeCandidates(restoredWorkspaces);
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
      const restoredNotice = String(localDraft?.restoreNotice || "").trim();
      if (restoredNotice) setMsg(restoredNotice);

      if (localDraft) {
        removeStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, u?.id);
      }

      const promptTransferPresent = !!promptDraft;
      let promptTransferApplied = false;
      let promptTransferState = null;
      let promptTransferSelections = null;
      let variationTransferApplied = false;
      if (promptDraft) {
        const parsedDraft = parsePromptDraft(promptDraft);
        const snapshotRestore = workspacePatchFromReverseSnapshot(parsedDraft.reverseSnapshot);
        const snapshotSelections = reverseSnapshotModelSelections(parsedDraft.reverseSnapshot);
        const draftMode = snapshotRestore?.creationMode || promptDraftMode(parsedDraft);
        promptTransferApplied = canApplyStudioPromptTransfer(baseline, currentState, draftMode);
        if (promptTransferApplied) {
          if (snapshotSelections) applyModelSelectionPreferences(snapshotSelections, u?.id);
          const restoredWorkspace = snapshotRestore?.workspace || {};
          const restoredSelectedSignature = assetSignature(restoredWorkspace.selected);
          const purePromptWorkspace = snapshotRestore ? {} : {
            ...promptDraftGenerationPatch(parsedDraft.generation),
            negative: parsedDraft.negative,
            structured: parsedDraft.structured,
            structuredBaseline: parsedDraft.structured,
            structuredDirty: false,
          };
          const promptWorkspacePatch = {
            ...restoredWorkspace,
            ...purePromptWorkspace,
            editSubjectMode: snapshotRestore?.subjectMode || restoredWorkspace.editSubjectMode || "general",
            imageEditProductMode: ["product", "portrait"].includes(snapshotRestore?.subjectMode),
            prompt: snapshotRestore
              ? (restoredWorkspace.prompt || parsedDraft.prompt || "")
              : (parsedDraft.prompt || ""),
            promptDirty: snapshotRestore ? Boolean(restoredWorkspace.promptDirty) : true,
            promptSourceSignature: snapshotRestore ? restoredSelectedSignature : "",
            structuredSource: snapshotRestore ? restoredSelectedSignature : "",
            creationRecipeId: parsedDraft.creationRecipeId || restoredWorkspace.creationRecipeId || null,
            creationRecipeVersion: parsedDraft.creationRecipeVersion || restoredWorkspace.creationRecipeVersion || null,
            creationRecipeShareSlug: parsedDraft.creationRecipeShareSlug || restoredWorkspace.creationRecipeShareSlug || "",
            creationRecipeSource: parsedDraft.creationRecipeSource || restoredWorkspace.creationRecipeSource || "",
          };
          const restoreNotice = snapshotRestore
            ? snapshotRestore.expiredAssetsSkipped
              ? "已恢复反推文字、结构和分析证据；过期素材已跳过，请重新上传。"
              : "已恢复反推文字、结构、素材和分析证据。"
            : parsedDraft.legacyReverse
              ? "旧版反推记录仅支持恢复提示词文字，素材和分析证据未保存。"
              : parsedDraft.creationRecipeId
                ? "已恢复创作配方的提示词、结构和生成参数。"
                : "";
          promptTransferState = {
            ...nextState,
            creationMode: draftMode,
            structOpen: snapshotRestore
              ? true
              : parsedDraft.creationRecipeId
                ? Object.keys(parsedDraft.structured || {}).length > 0
                : nextState.structOpen,
            refOpen: snapshotRestore
              ? Boolean(restoredWorkspace.selected || restoredWorkspace.productAsset)
              : nextState.refOpen,
            restoreNotice,
            workspaces: {
              ...nextState.workspaces,
              [draftMode]: {
                ...(nextState.workspaces?.[draftMode] || {}),
                ...promptWorkspacePatch,
              },
            },
          };
          promptTransferSelections = snapshotSelections
            ? { ...effectiveModelSelections, ...snapshotSelections }
            : effectiveModelSelections;
          workspacesRef.current = promptTransferState.workspaces;
          studioUiStateRef.current = promptTransferState;
          setWorkspaces(promptTransferState.workspaces);
          setCreationMode(promptTransferState.creationMode);
          setStructOpen(promptTransferState.structOpen);
          setRefOpen(promptTransferState.refOpen);
          if (restoreNotice) setMsg(restoreNotice);
        }
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
      if (promptDraft) {
        if (promptTransferApplied && promptTransferState) {
          // Persist the adopted state before consuming the one-shot handoff.
          const savedAt = ownerSession.next();
          const checkpoint = buildStudioSessionDraftFromState(
            "prompt_transfer_applied",
            promptTransferState,
            savedAt,
            promptTransferSelections,
          );
          const checkpointSaved = saveStudioUserDraft(
            window.localStorage,
            STUDIO_SESSION_DRAFT_KEY,
            u?.id,
            checkpoint,
          );
          if (checkpointSaved) {
            removeStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, u?.id);
          }
        } else {
          removeStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, u?.id);
          setMsg("当前工作区已发生编辑，为避免覆盖，未应用待恢复的提示词或创作配方。");
        }
      }
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
    const onFocus = () => {
      refreshStudioConfig().catch((e) => reportBackgroundError(e, "refresh studio config on focus"));
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") onFocus();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
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
    if (!me?.id || !cloudDraftLoadedRef.current || typeof window === "undefined") return;
    const slug = studioWorkflowSlug(window.location.search);
    if (!slug) return;
    const loadingKey = `loading:${slug}`;
    if (
      workflowBootstrapRef.current === loadingKey
      || workflowBootstrapRef.current === `failed:${slug}`
      || workflowBootstrapRef.current.startsWith(`applied:${slug}:`)
    ) return;
    workflowBootstrapRef.current = loadingKey;
    let active = true;

    api.toolCatalogDetail(slug)
      .then((tool) => {
        if (!active) return;
        const resolution = resolveStudioWorkflowPreset(window.location.search, tool);
        if (resolution.status !== "ready" || !resolution.preset) {
          workflowBootstrapRef.current = `failed:${slug}`;
          setMsg(resolution.message);
          notify.warn(resolution.message);
          return;
        }
        const preset = resolution.preset;
        workflowBootstrapRef.current = `applied:${slug}:${preset.versionId}`;
        const nextCategory = preset.creationMode.startsWith("video") ? "video" : "image";
        setCreationMode(preset.creationMode);
        setWorkspacePatch((current) => ({
          reverseConfig: normalizeReverseConfig({
            ...(current.reverseConfig || {}),
            ...(preset.analysisFocus ? { analysis_focus: preset.analysisFocus } : {}),
            ...(preset.outputPurpose ? { output_purpose: preset.outputPurpose } : {}),
          }, { category: nextCategory }),
          ...(preset.referenceRoles ? {
            reverseSources: preset.referenceRoles.map((role) => ({
              asset_url: "",
              source_type: "image",
              role,
            })),
          } : {}),
        }), preset.creationMode);
        setRefOpen(true);
        setStructOpen(true);
        setMsg(preset.message);
        window.requestAnimationFrame(() => {
          document.getElementById("studio-reference-panel")?.scrollIntoView({
            behavior: "smooth",
            block: "center",
          });
        });
      })
      .catch((error) => {
        if (!active) return;
        workflowBootstrapRef.current = `failed:${slug}`;
        const message = error?.status === 404
          ? `工作流「${slug}」不存在、已禁用或没有可用版本，当前草稿未改变。`
          : `工作流「${slug}」加载失败，当前草稿未改变。`;
        setMsg(message);
        notify.warn(message);
      });
    return () => {
      active = false;
      if (workflowBootstrapRef.current === loadingKey) workflowBootstrapRef.current = "";
    };
  }, [me?.id, workspaces, setWorkspacePatch]);

  useEffect(() => {
    if (!me?.id || !cfg || typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const modelConfigId = Number(params.get("model_config_id"));
    const modelUse = String(params.get("model_use") || "");
    const requestedMode = String(params.get("studio_mode") || "");
    if (!Number.isInteger(modelConfigId) || modelConfigId <= 0 || !MODEL_SELECTION_USES.includes(modelUse)) return;
    const bootstrapKey = `${modelUse}:${modelConfigId}:${requestedMode || "auto"}`;
    if (
      catalogModelBootstrapRef.current === `done:${bootstrapKey}`
      || catalogModelBootstrapRef.current === `failed:${bootstrapKey}`
    ) return;
    const option = allModelOptions[modelUse]?.find((item) => item.id === modelConfigId);
    if (!option) {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      const message = "目录中的模型当前不可用，已保留现有模型选择。";
      setMsg(message);
      notify.warn(message);
      return;
    }
    const resolution = resolveCatalogModelIntent(option, requestedMode);
    if (resolution.status !== "ready") {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      setMsg(resolution.message);
      notify.warn(resolution.message);
      return;
    }
    if (resolution.creationMode && creationMode !== resolution.creationMode) {
      catalogModelBootstrapRef.current = `mode:${bootstrapKey}`;
      setCreationMode(resolution.creationMode);
      return;
    }
    const compatibleOption = modelOptionsForSelection[modelUse]?.find((item) => item.id === modelConfigId);
    if (!compatibleOption) {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      const message = "该模型与当前 Studio 素材和能力约束不兼容，已保留现有模型选择。";
      setMsg(message);
      notify.warn(message);
      return;
    }
    changeModelSelection(modelUse, modelConfigId);
    catalogModelBootstrapRef.current = `done:${bootstrapKey}`;
    setMsg(`已选择 ${option.display_name || option.model_id}。`);
  }, [me?.id, cfg, creationMode]);

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

  async function refreshStudioConfig() {
    const seq = ++configRefreshSeqRef.current;
    const next = await api.config();
    if (seq === configRefreshSeqRef.current) setCfg(next);
    return next;
  }

  function applyModelSelectionPreferences(preferred, ownerId = me?.id) {
    const next = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [
      use,
      resolveModelConfigId(modelOptionsForSelection[use], preferred?.[use]),
    ]));
    modelSelectionsRef.current = next;
    setModelSelections(next);
    if (typeof window !== "undefined" && ownerId) {
      writeModelSelections(window.localStorage, String(ownerId), next);
    }
    return next;
  }

  function changeModelSelection(use, modelConfigId) {
    const next = {
      ...effectiveModelSelections,
      [use]: resolveModelConfigId(modelOptionsForSelection[use], modelConfigId),
    };
    modelSelectionsRef.current = next;
    setModelSelections(next);
    if (typeof window !== "undefined" && me?.id) {
      writeModelSelections(window.localStorage, String(me.id), next);
    }
    if (use === "prompt" || use === category) invalidatePromptOptimization();
  }

  function changeProductVideoTemplate(value) {
    const next = normalizeProductVideoStrategyKey(value);
    if (!next || !productVideoStrategySelection.strategies.includes(next)) return;
    if (next === productVideoTemplate) return;
    setProductVideoTemplate(next);
    invalidatePromptOptimization();
  }

  async function changeGenerationModelSelection(modelConfigId) {
    const nextModelId = resolveModelConfigId(
      modelOptionsForSelection[category],
      modelConfigId,
    );
    changeModelSelection(category, nextModelId);
    const operation = reverseOperationForPendingResult();
    if (!operation?.id || !reverseAppliedRevisionId || !prompt.trim() || !nextModelId) return;
    const mode = creationMode;
    const promptSnapshot = prompt;
    setOptimizingPromptMode(mode);
    try {
      const optimizationRequest = {
        reverse_operation_id: Number(operation.id),
        reverse_revision_id: Number(reverseAppliedRevisionId),
        mode: "target_model_adaptation",
        target_model_config_id: Number(nextModelId),
      };
      const actionRequestId = pendingStudioActionRequestId(
        studioActionPendingRequestRef,
        `prompt-optimization:${me?.id || "unknown"}:${mode}:target-model-adaptation`,
        optimizationRequest,
      );
      const result = await api.createStudioPromptOptimization({
        ...optimizationRequest,
        idempotency_key: actionRequestId,
      });
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      const currentWorkspace = workspacesRef.current?.[mode];
      if (
        String(currentWorkspace?.prompt || "") !== promptSnapshot
        || Number(currentWorkspace?.reverseAppliedRevisionId || 0) !== Number(reverseAppliedRevisionId)
        || Number(modelSelectionsRef.current?.[category] || 0) !== Number(nextModelId)
      ) return;
      const selectedTarget = generationModelOptions.find((item) => item.id === Number(nextModelId));
      setPromptOptimizationSettings((current) => ({
        ...current,
        [mode]: { ...promptOptimizationSetting, direction: "target_model_adaptation" },
      }));
      setPromptOptimizationProposals((current) => ({
        ...current,
        [mode]: {
          ...result,
          raw_text: String(result?.original?.final_text || promptSnapshot),
          optimized_text: String(result?.suggestion?.final_text || promptSnapshot),
          optimizer_model_id: "",
          optimizer_model_config_id: null,
          scope_key: promptOptimizationScopeKey,
          model_name: selectedTarget?.display_name || selectedTarget?.name || "目标生成模型",
          direction: "target_model_adaptation",
          target_language: null,
          optimization_kind: "model_compile",
          compiler_metadata: result.compiler_profile || null,
          change_summary: (result.segments || []).filter((item) => item.changed).map((item) => item.label),
          warnings: Array.isArray(result.warnings) ? result.warnings : [],
          charged_credits: 0,
        },
      }));
      setMsg("目标模型已切换，编译预览已生成；反推素材未重新分析。");
    } catch (error) {
      const text = errorMessage(error, "目标模型编译失败");
      setMsg(text);
      notify.error(text);
    } finally {
      setOptimizingPromptMode((current) => (current === mode ? "" : current));
    }
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
    confirmReverseCover,
    cancelReverseOperationForMode,
    cancelRecoveredProfileOperationForMode,
    trackReverseOperation,
    trackProfileReverseOperation,
    cancelAllReverseOperations,
    reverseOperation,
    lastReversePromptRef,
    resetOwnerReferenceParsing,
  } = useReferenceParsing({
    creationMode,
    category,
    url,
    parsing,
    selected,
    assets,
    productAsset,
    productProfile: activeSubjectProfile,
    structured,
    reverseVideoAnalysis,
    workspaceReverseOperation,
    prompt,
    negative,
    negativeTouched,
    videoAnalysisPreset,
    reverseConfig,
    reverseSources,
    modelConfigId: selectedVisionModelConfigId,
    modelSelections: effectiveModelSelections,
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
    ownerKey: me?.id ? String(me.id) : "",
    resumeOperations: reverseResumeOperations,
    requestQuoteConfirmation,
  });

  useEffect(() => {
    const current = reverseOperation || workspaceReverseOperation;
    if (current?.id) upsertRecentReverseOperation(current);
  }, [reverseOperation, workspaceReverseOperation, upsertRecentReverseOperation]);

  const pendingReverseOperationId = Number(
    pendingReverseResult?.operation_id
    || pendingReverseResult?.operationId
    || workspaceReverseOperation?.id
    || 0,
  );
  const generationSourceRevision = findGenerationSourceRevision(reverseResultRevisions, {
    revisionId: reverseAppliedRevisionId,
    version: reverseAppliedVersion,
    operationId: pendingReverseOperationId,
  });
  const generationReverseRevisionId = positiveRevisionId(reverseAppliedRevisionId)
    || positiveRevisionId(generationSourceRevision?.id);
  const generationReverseOperationId = generationReverseRevisionId && pendingReverseOperationId > 0
    ? pendingReverseOperationId
    : null;
  const reproductionOperationId = positiveRevisionId(task?.reverse_operation_id)
    || generationReverseOperationId;
  const reproductionResult = reverseResultPayload(pendingReverseResult);
  const reproductionVideoComposition = (
    task?.category === "video"
    && reproductionOperationId
    && reproductionOperationId === pendingReverseOperationId
    && reproductionResult
  ) ? normalizeVideoComposition(
      reproductionResult.video_composition,
      storyboardResultShots(reproductionResult),
      reproductionOperationId,
    ) : null;
  useEffect(() => {
    if (!Number.isInteger(pendingReverseOperationId) || pendingReverseOperationId <= 0) return undefined;
    let active = true;
    Promise.all([
      api.reverseOperationRevisions(pendingReverseOperationId),
      api.reverseOperationFeedback(pendingReverseOperationId),
    ]).then(([revisionPayload, feedbackPayload]) => {
      if (!active) return;
      const fetchedRevisions = normalizeReverseResultRevisionList(revisionPayload);
      const operationAppliedVersion = positiveRevisionId(
        [reverseOperation, workspaceReverseOperation]
          .find((item) => Number(item?.id) === pendingReverseOperationId)
          ?.applied_result_version,
      );
      setWorkspacePatch((current) => {
        const revisions = [...fetchedRevisions];
        for (const localRevision of current.reverseResultRevisions || []) {
          if (!revisions.some((revision) => revision.id === localRevision.id)) revisions.push(localRevision);
        }
        revisions.sort((left, right) => Number(left.version) - Number(right.version));
        const appliedRevision = findGenerationSourceRevision(revisions, {
          revisionId: current.reverseAppliedRevisionId,
          version: current.reverseAppliedVersion || operationAppliedVersion,
          operationId: pendingReverseOperationId,
        });
        return {
          reverseAppliedVersion: appliedRevision?.version || operationAppliedVersion,
          reverseAppliedRevisionId: appliedRevision?.id || null,
          reverseResultRevisions: revisions,
          reverseFeedback: feedbackPayload ? normalizeReverseOperationFeedback(feedbackPayload) : null,
        };
      });
    }).catch((error) => reportBackgroundError(error, "load reverse result review context"));
    return () => { active = false; };
  }, [creationMode, pendingReverseOperationId]);

  async function persistShotGenerationBinding({
    task: generationTask,
    operationId,
    shotId,
    workspaceMode,
    editAction,
    ownerSession,
  }) {
    if (!ownerSession?.isCurrent?.()) return null;
    const currentWorkspace = workspacesRef.current?.[workspaceMode];
    const currentPending = currentWorkspace?.pendingReverseResult;
    const currentOperationId = positiveRevisionId(
      currentPending?.operation_id
      || currentPending?.operationId
      || currentWorkspace?.reverseOperation?.id,
    );
    if (!currentPending || currentOperationId !== operationId) return null;

    if (editAction === "video_composition_generation_terminal") {
      const currentResult = reverseResultPayload(currentPending);
      const currentComposition = normalizeVideoComposition(
        currentResult?.video_composition,
        storyboardResultShots(currentResult || {}),
        operationId,
      );
      if (!videoCompositionShotTracksGenerationTask(
        currentComposition,
        shotId,
        generationTask?.id || generationTask?.task_id,
      )) return null;
    }

    const transition = bindGenerationTaskToPendingReverseResult(currentPending, {
      operationId,
      shotId,
      task: generationTask,
    });
    if (!transition?.binding.changed) return transition?.binding || null;

    setWorkspacePatch((current) => {
      const pending = current.pendingReverseResult;
      const operationMatches = positiveRevisionId(
        pending?.operation_id || pending?.operationId || current.reverseOperation?.id,
      ) === operationId;
      if (!pending || !operationMatches) return {};
      const latest = bindGenerationTaskToPendingReverseResult(pending, {
        operationId,
        shotId,
        task: generationTask,
      });
      if (!latest?.binding.changed) return {};
      return {
        pendingReverseResult: replaceReverseResultPayload(
          pending,
          latest.result,
          Boolean(pending.dirty),
        ),
      };
    }, workspaceMode);

    const revision = normalizeReverseResultRevision(
      await api.createReverseOperationRevision(operationId, {
        source: "user_edit",
        payload: {
          ...transition.result,
          edit_metadata: {
            ...(transition.result.edit_metadata && typeof transition.result.edit_metadata === "object"
              ? transition.result.edit_metadata
              : {}),
            action: editAction,
            shot_id: transition.binding.shotId,
            generation_task_id: transition.binding.generationTaskId,
          },
        },
      }),
    );
    if (!ownerSession.isCurrent()) return transition.binding;
    setWorkspacePatch((current) => ({
      reverseResultRevisions: mergeReverseResultRevisions(
        current.reverseResultRevisions || [],
        [revision],
      ),
    }), workspaceMode);
    return transition.binding;
  }

  function enqueueShotGenerationBinding(options) {
    const key = Number(options.operationId);
    const previous = shotGenerationRevisionQueueRef.current.get(key) || Promise.resolve();
    const run = previous
      .catch(() => undefined)
      .then(() => persistShotGenerationBinding(options));
    shotGenerationRevisionQueueRef.current.set(key, run);
    run.finally(() => {
      if (shotGenerationRevisionQueueRef.current.get(key) === run) {
        shotGenerationRevisionQueueRef.current.delete(key);
      }
    }).catch(() => undefined);
    return run;
  }

  function trackShotGenerationTask({ task: generationTask, operationId, shotId, workspaceMode, ownerSession }) {
    trackBackgroundTask(generationTask, {
      onTerminal: async (terminalTask) => {
        try {
          const binding = await enqueueShotGenerationBinding({
            task: terminalTask,
            operationId,
            shotId,
            workspaceMode,
            editAction: "video_composition_generation_terminal",
            ownerSession,
          });
          if (binding?.status === "bound" && ownerSession.isCurrent()) {
            setMsg(`镜头 ${binding.shotId} 生成完成，已自动绑定到合成工程。`);
          }
        } catch (error) {
          reportBackgroundError(error, "persist terminal storyboard generation binding");
        }
      },
    });
  }

  async function handleGenerationSubmitted({ task: submittedTask, prepared = null, shot = null }) {
    const submittedMode = creationMode;
    const ownerSession = studioOwnerSessionRef.current;
    const operationId = positiveRevisionId(
      prepared?.reverse_operation_id || submittedTask?.reverse_operation_id,
    );
    const shotId = String(
      shot?.shot_id
      || prepared?.shot_id
      || submittedTask?.params?.shot_id
      || "",
    ).trim();
    if (operationId && shotId && submittedTask?.id) {
      try {
        await enqueueShotGenerationBinding({
          task: submittedTask,
          operationId,
          shotId,
          workspaceMode: submittedMode,
          editAction: "video_composition_generation_submitted",
          ownerSession,
        });
      } catch (error) {
        reportBackgroundError(error, "persist submitted storyboard generation binding");
      }
      trackShotGenerationTask({
        task: submittedTask,
        operationId,
        shotId,
        workspaceMode: submittedMode,
        ownerSession,
      });
    }

    const sourceRevisionId = positiveRevisionId(submittedTask?.source_revision_id);
    const compiledRevisionId = positiveRevisionId(submittedTask?.compiled_revision_id);
    const generationRevisionId = positiveRevisionId(submittedTask?.generation_revision_id);
    if (!operationId || !sourceRevisionId || !compiledRevisionId || !generationRevisionId) return;
    const patchCurrentLineage = (current, revisionPatch = {}) => {
      const currentOperationId = positiveRevisionId(
        current.pendingReverseResult?.operation_id
        || current.pendingReverseResult?.operationId
        || current.reverseOperation?.id,
      );
      if (currentOperationId !== operationId) return {};
      return {
        reverseAppliedRevisionId: sourceRevisionId,
        ...revisionPatch,
      };
    };
    setWorkspacePatch((current) => patchCurrentLineage(current), submittedMode);
    try {
      const revisions = normalizeReverseResultRevisionList(
        await api.reverseOperationRevisions(operationId),
      );
      const sourceRevision = findGenerationSourceRevision(revisions, {
        revisionId: sourceRevisionId,
        operationId,
      });
      setWorkspacePatch((current) => patchCurrentLineage(current, {
        reverseAppliedVersion: sourceRevision?.version || current.reverseAppliedVersion || null,
        reverseResultRevisions: revisions,
      }), submittedMode);
    } catch (error) {
      reportBackgroundError(error, "refresh reverse revisions from generation lineage");
    }
  }

  async function handleReproductionCorrectionCreated({ assessment, response }) {
    const operationId = positiveRevisionId(assessment?.reverse_operation_id);
    if (!operationId || !response?.edited_revision) {
      setMsg("修正版本已创建，可在对应反推任务的版本记录中继续审阅。");
      return;
    }
    try {
      const editedRevision = normalizeReverseResultRevision(response.edited_revision);
      setWorkspacePatch((current) => {
        const currentOperationId = positiveRevisionId(
          current.pendingReverseResult?.operation_id
          || current.pendingReverseResult?.operationId
          || current.reverseOperation?.id,
        );
        if (currentOperationId !== operationId) return {};
        const revisions = [...(current.reverseResultRevisions || [])];
        const existingIndex = revisions.findIndex((revision) => revision.id === editedRevision.id);
        if (existingIndex >= 0) revisions[existingIndex] = editedRevision;
        else revisions.push(editedRevision);
        revisions.sort((left, right) => Number(left.version) - Number(right.version));
        return { reverseResultRevisions: revisions };
      });
      setMsg(`修正版本 #${editedRevision.version} 已创建，原提示词和生成结果未被覆盖。`);
    } catch (error) {
      reportBackgroundError(error, "normalize reproduction correction revision");
      setMsg("修正版本已创建，可在对应反推任务的版本记录中继续审阅。");
    }
  }

  async function handleReproductionRemediationCreated({ assessment, remediation }) {
    const operationId = positiveRevisionId(assessment?.reverse_operation_id);
    if (!operationId) {
      setMsg(`纠偏计划 #${remediation?.id || ""} 已创建。`);
      return;
    }
    try {
      const revisions = normalizeReverseResultRevisionList(
        await api.reverseOperationRevisions(operationId),
      );
      setWorkspacePatch((current) => {
        const currentOperationId = positiveRevisionId(
          current.pendingReverseResult?.operation_id
          || current.pendingReverseResult?.operationId
          || current.reverseOperation?.id,
        );
        if (currentOperationId !== operationId) return {};
        return {
          reverseResultRevisions: mergeReverseResultRevisions(
            current.reverseResultRevisions || [],
            revisions,
          ),
        };
      });
    } catch (error) {
      reportBackgroundError(error, "refresh remediation revision lineage");
    }
    setMsg(`纠偏计划 #${remediation?.id || ""} 已创建，原提示词和生成结果保持不变。`);
  }

  async function handleReproductionRemediationExecutionSubmitted({ task: submittedTask }) {
    if (!submittedTask?.id) return;
    const ownerSession = studioOwnerSessionRef.current;
    trackBackgroundTask(submittedTask, {
      onTerminal: async () => {
        if (!ownerSession?.isCurrent?.()) return;
        await Promise.allSettled([refreshMe(), loadWorks()]);
      },
    });
    setMsg(`纠偏生成任务 #${submittedTask.id} 已提交，可在全局任务中心继续跟踪。`);
  }

  useEffect(() => {
    const ownerSession = studioOwnerSessionRef.current;
    if (!ownerSession?.isCurrent?.()) return;
    for (const [workspaceMode, current] of Object.entries(workspaces || {})) {
      const pending = current?.pendingReverseResult;
      const operationId = positiveRevisionId(
        pending?.operation_id || pending?.operationId || current?.reverseOperation?.id,
      );
      const result = reverseResultPayload(pending);
      if (!operationId || !result) continue;
      const composition = normalizeVideoComposition(
        result.video_composition,
        storyboardResultShots(result),
        operationId,
      );
      for (const compositionShot of composition.shots) {
        const taskId = positiveRevisionId(compositionShot.generation_task_id);
        if (!taskId || compositionShot.asset_ref) continue;
        trackShotGenerationTask({
          task: {
            id: taskId,
            category: "video",
            status: "queued",
            params: { shot_id: compositionShot.shot_id },
          },
          operationId,
          shotId: compositionShot.shot_id,
          workspaceMode,
          ownerSession,
        });
      }
    }
  }, [workspaces, trackBackgroundTask]);

  const {
    imageUploadInputRef,
    productUploadInputRef,
    productDetailUploadInputRef,
    lastFrameUploadInputRef,
    videoUploadInputRef,
    bumpUploadRequest,
    bumpProductUploadRequest,
    revokeUploadedObjectUrls,
    revokeProductObjectUrl,
    revokeProductObjectUrls,
    cancelProfileOperation,
    cancelAllProfileOperations,
    cancelOwnerProfileOperationsAndReset,
    resetOwnerMediaUpload,
    doUploadImage,
    doUploadProductImage,
    doUploadProductDetailImages,
    doUploadLastFrameImage,
    selectProductAsset,
    selectLastFrameAsset,
    doUploadVideo,
  } = useMediaUpload({
    cfg,
    creationMode,
    category,
    isEditMode,
    subjectMode,
    productAsset,
    productDetailAssets,
    productDetailLimit,
    visionModelConfigId: selectedVisionModelConfigId,
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
    subjectProfileOperationsRef,
    profileOperation,
    cancelRecoveredProfileOperation: cancelRecoveredProfileOperationForMode,
    trackProfileReverseOperation,
    getOwnerSession: () => studioOwnerSessionRef.current,
    requestQuoteConfirmation,
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
    lastFrameAsset: effectiveLastFrameAsset,
    firstLastFrameEnabled,
    productAsset,
    productDetailAssets,
    productProfile,
    productProfileSource,
    portraitProfile,
    portraitProfileSource,
    variationSource,
    structured,
    structuredDirty,
    structuredSource,
    reverseVideoAnalysis,
    analysisFocus: reverseConfig?.analysis_focus || "",
    ratio,
    imageQuality,
    n,
    seed,
    editMaskMode,
    productPixelLockMode,
    vDuration,
    vResolution,
    productVideoTemplate: effectiveProductVideoTemplate || productVideoTemplate,
    modelConfigId: selectedGenerationModelConfigId,
    reverseOperationId: generationReverseOperationId,
    reverseRevisionId: generationReverseRevisionId,
    reviewedImageEvidence: generationSourceRevision?.payload?.image_evidence || null,
    reverseEvidenceOperation: reverseOperationForPendingResult(),
    modelOption: selectedGenerationModel,
    visionModelConfigId: selectedVisionModelConfigId,
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
    onGenerationSubmitted: handleGenerationSubmitted,
    subjectProfilePendingRequestRef,
    subjectProfileResultCacheRef,
    subjectProfileOperationsRef,
    trackProfileReverseOperation,
    getOwnerSession: () => studioOwnerSessionRef.current,
    requestQuoteConfirmation,
  });

  useEffect(() => {
    revokeUploadedObjectUrlsRef.current = revokeUploadedObjectUrls;
  }, [revokeUploadedObjectUrls]);

  function modelEnabled(kind) {
    const use = String(kind || "").startsWith("video") ? "video" : "image";
    if (cfg?.model_options) {
      return filterModelOptions(allModelOptions[use], {
        use,
        creationMode: kind,
        selected: kind === creationMode ? selected : null,
        productAsset: kind === creationMode ? productAsset : null,
        subjectMode: kind === creationMode ? subjectMode : "general",
      }).length > 0;
    }
    return modelEnabledForConfig(cfg, kind);
  }

  function sanitizeAssetForDraft(asset) {
    if (!asset) return null;
    const clean = {};
    for (const key of [
      "id", "asset_ref", "origin", "type", "url", "thumb", "preview_url", "original_url", "original_thumb",
      "source_page_url", "source_captured_at", "width", "height", "thumb_width", "thumb_height",
      "duration", "retention_expires_at", "expired", "available", "filename",
      "unlock_cost", "unlocked", "favorite", "retained",
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
      productVideoTemplate: normalizeProductVideoStrategyKey(current.productVideoTemplate) || "prompt_driven",
      editMaskMode: current.editMaskMode || "protect_subject",
      productPixelLockMode: current.productPixelLockMode || "auto",
      videoAnalysisPreset: current.videoAnalysisPreset || "standard",
      url: current.url || "",
      appliedUrl: current.appliedUrl || "",
      assets: (current.assets || []).map(sanitizeAssetForDraft).filter(Boolean).slice(0, 12),
      selected: sanitizeAssetForDraft(current.selected),
      lastFrameAsset: sanitizeAssetForDraft(current.lastFrameAsset),
      productAsset: sanitizeAssetForDraft(current.productAsset),
      productDetailAssets: (current.productDetailAssets || [])
        .map(sanitizeAssetForDraft)
        .filter(Boolean)
        .slice(0, MAX_PRODUCT_DETAIL_IMAGES),
      productProfile: compactStudioDraftValue(current.productProfile || null),
      productProfileSource: current.productProfileSource || "",
      portraitProfile: compactStudioDraftValue(current.portraitProfile || null),
      portraitProfileSource: current.portraitProfileSource || "",
      variationSource: sanitizeAssetForDraft(current.variationSource),
      structured: compactStudioDraftValue(current.structured || {}),
      structuredBaseline: compactStudioDraftValue(current.structuredBaseline || {}),
      structuredDirty: !!current.structuredDirty,
      structuredSource: current.structuredSource || "",
      reverseVideoAnalysis: compactStudioDraftValue(current.reverseVideoAnalysis || null),
      reverseSources: compactStudioDraftValue(
        Array.isArray(current.reverseSources) ? current.reverseSources.slice(0, 12) : [],
      ),
      reverseConfig: compactStudioDraftValue(current.reverseConfig || null),
      pendingReverseResult: compactPendingReverseResultForStudioDraft(current.pendingReverseResult),
      reverseResultTab: current.reverseResultTab || "draft",
      reverseResultSchemaVersion: current.reverseResultSchemaVersion || "",
      reverseAppliedVersion: current.reverseAppliedVersion || null,
      reverseAppliedRevisionId: current.reverseAppliedRevisionId || null,
      reverseUndoSnapshot: compactStudioDraftValue(current.reverseUndoSnapshot || null),
      // Revisions are immutable server state and are reloaded by operation id.
      reverseResultRevisions: [],
      reverseFeedback: compactStudioDraftValue(current.reverseFeedback || null),
      batchReverseAssets: (current.batchReverseAssets || [])
        .map(sanitizeAssetForDraft)
        .filter(Boolean)
        .slice(0, MAX_REVERSE_BATCH_ITEMS),
      creationRecipeId: current.creationRecipeId || null,
      creationRecipeVersion: current.creationRecipeVersion || null,
      creationRecipeShareSlug: current.creationRecipeShareSlug || "",
      creationRecipeSource: current.creationRecipeSource || "",
      reverseOperation: current.reverseOperation && (
        ["queued", "running", "needs_confirmation"].includes(current.reverseOperation.status)
        || current.pendingReverseResult
      )
        ? compactReverseOperationForStudioDraft(current.reverseOperation)
        : null,
      profileOperation: current.profileOperation && ["queued", "running", "needs_confirmation"].includes(current.profileOperation.status)
        ? compactReverseOperationForStudioDraft(current.profileOperation)
        : null,
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

  function buildStudioSessionDraftFromState(
    reason,
    state,
    savedAt,
    selections = effectiveModelSelections,
  ) {
    const snapshot = state?.workspaces || {};
    const savedWorkspaces = Object.fromEntries(
      Object.entries(snapshot || {}).map(([mode, current]) => [mode, sanitizeWorkspaceForDraft(current)]),
    );
    return {
      version: 1,
      reason,
      savedAt,
      creationMode: state?.creationMode || "image",
      showNegative: Boolean(state?.showNegative),
      refOpen: Boolean(state?.refOpen),
      structOpen: state?.structOpen !== false,
      modelSelections: selections || effectiveModelSelections,
      workspaces: savedWorkspaces,
      activeTaskId: state?.activeTaskId || null,
      ...(String(state?.restoreNotice || "").trim()
        ? { restoreNotice: String(state.restoreNotice).trim() }
        : {}),
    };
  }

  function buildStudioSessionDraft(reason = "manual") {
    const savedAt = studioDraftClockRef.current.next();
    return buildStudioSessionDraftFromState(
      reason,
      {
        workspaces: workspacesRef.current || workspaces,
        creationMode,
        showNegative,
        refOpen,
        structOpen,
        activeTaskId: taskRef.current?.id || null,
      },
      savedAt,
      effectiveModelSelections,
    );
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

  async function clearProductAsset() {
    try {
      await cancelRecoveredProfileOperationForMode(creationMode);
      await cancelProfileOperation(creationMode);
    } catch (e) {
      reportBackgroundError(e, "cancel profile while clearing subject");
      setMsg(errorMessage(e, "主体档案任务取消失败，已保留当前素材。"));
      return;
    }
    bumpProductUploadRequest(creationMode);
    revokeProductObjectUrl(creationMode);
    setWorkspacePatch({
      productAsset: null,
      productDetailAssets: [],
      productProfile: null,
      productProfileSource: "",
      portraitProfile: null,
      portraitProfileSource: "",
      productProfiling: false,
      subjectProtection: null,
      subjectProtectionLoading: false,
      subjectProtectionSource: "",
      variationSource: null,
    });
    if (productUploadInputRef.current) productUploadInputRef.current.value = "";
  }

  async function clearRef() {
    const mode = creationMode;
    try {
      await cancelReverseOperationForMode(mode);
    } catch (e) {
      reportBackgroundError(e, "cancel reverse while clearing reference");
      setMsg(errorMessage(e, "反推任务取消失败，已保留当前参考素材。"));
      return;
    }
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpUploadRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    if (imageUploadInputRef.current) imageUploadInputRef.current.value = "";
    if (lastFrameUploadInputRef.current) lastFrameUploadInputRef.current.value = "";
    if (videoUploadInputRef.current) videoUploadInputRef.current.value = "";
    clearSelectedForMode(mode);
    setWorkspacePatch((current) => ({
      selected: null,
      lastFrameAsset: null,
      assets: [],
      variationSource: null,
      url: "",
      appliedUrl: "",
      parsing: false,
      uploading: false,
      reversing: false,
      structured: {},
      structuredBaseline: {},
      structuredDirty: false,
      structuredSource: "",
      reverseVideoAnalysis: null,
      reverseOperation: null,
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
    setWorkspacePatch({ url: value, parsing: false }, mode);
  }

  // rebuild the prompt text from the (possibly edited) reverse dimensions
  function recompose() {
    const nextPrompt = isEditMode
      ? composeStyleTransferPrompt(structured, prompt, { video: category === "video", subject: subjectMode })
      : composePromptFromStructured(structured, prompt, { target: category });
    setWorkspacePatch({
      prompt: nextPrompt,
      promptDirty: false,
      structuredBaseline: structured,
      structuredDirty: false,
      promptSourceSignature: structuredSource || assetSignature(selected) || "",
    });
  }

  function undoStructuredChanges() {
    setWorkspacePatch({
      structured: structuredBaseline || {},
      structuredDirty: false,
    });
  }

  function promptOptimizationRequestOptions({
    direction = promptOptimizationSetting.direction,
    duration = category === "video" ? Number(vDuration) : undefined,
  } = {}) {
    const operation = reverseOperationForPendingResult();
    const lineage = operation?.id && reverseAppliedRevisionId
      ? {
          reverse_operation_id: Number(operation.id),
          reverse_revision_id: Number(reverseAppliedRevisionId),
        }
      : null;
    return {
      ...(lineage || {
        prompt: String(prompt || "").trim(),
        category,
        product_mode: productGenerationMode,
        duration: category === "video" ? Math.max(1, Math.round(Number(duration) || Number(vDuration) || 1)) : undefined,
        aspect_ratio: category === "video" ? ratio : undefined,
        resolution: category === "video" ? vResolution : undefined,
      }),
      mode: direction,
      target_language: promptOptimizationSetting.targetLanguage,
      optimizer_model_config_id: selectedPromptModelConfigId || undefined,
      target_model_config_id: selectedGenerationModelConfigId,
    };
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
      const optimizationRequest = promptOptimizationRequestOptions();
      const actionRequestId = pendingStudioActionRequestId(
        studioActionPendingRequestRef,
        `prompt-optimization:${me?.id || "unknown"}:${mode}`,
        optimizationRequest,
      );
      const executableRequest = {
        ...optimizationRequest,
        idempotency_key: actionRequestId,
      };
      const compileOnly = ["model_adaptation", "target_model_adaptation"]
        .includes(String(optimizationRequest.mode || ""));
      const requiresQuote = !compileOnly && (
        selectedPromptModel == null || Number(selectedPromptModel.cost_credits || 0) > 0
      );
      let result;
      if (requiresQuote) {
        const confirmation = await requestQuoteConfirmation({
          kind: "prompt_optimization",
          request: executableRequest,
          clientRequestId: actionRequestId,
          execute: ({ request: confirmedRequest }) => (
            api.createStudioPromptOptimization(confirmedRequest)
          ),
        });
        if (confirmation.status !== "executed") {
          if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
            throw confirmation.error || new Error("提示词优化执行失败。");
          }
          if (confirmation.status === "invalidated") {
            setMsg(confirmation.reason || "参数已变化，请重新执行提示词优化。");
          }
          return;
        }
        result = confirmation.result;
      } else {
        result = await api.createStudioPromptOptimization(executableRequest);
      }
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      if (!isPromptOptimizationResultCurrent(
        request,
        optimizePromptRequestRef.current[mode],
        optimizePromptContextRef.current[mode],
      )) return;
      const optimized = String(result?.suggestion?.final_text || "").trim();
      if (!optimized) throw new Error("优化模型未返回有效提示词");
      setPromptOptimizationProposals((current) => ({
        ...current,
        [mode]: {
        ...result,
        raw_text: String(result?.original?.final_text || source),
        optimized_text: optimized,
        optimizer_model_id: result.provenance?.optimizer_model_id || "",
        optimizer_model_config_id: result.provenance?.optimizer_model_config_id || selectedPromptModelConfigId || null,
        scope_key: promptOptimizationScopeKey,
        model_name: selectedPromptModel?.display_name || "提示词模型",
        direction: result.mode || promptOptimizationSetting.direction,
        target_language: promptOptimizationSetting.targetLanguage || null,
        optimization_kind: result.optimization_kind || "rewrite",
        compiler_metadata: result.compiler_profile || null,
        change_summary: Array.isArray(result.segments)
          ? result.segments.filter((item) => item.changed).map((item) => item.label)
          : [],
        warnings: Array.isArray(result.warnings) ? result.warnings : [],
        charged_credits: Number(result.charged_credits || 0),
        },
      }));
      setMsg("优化建议已生成，请对比后选择接受或拒绝。");
      notify.success("优化建议已生成，当前提示词尚未改变。");
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

  async function compileStoryboardShot(index, shot) {
    const mode = creationMode;
    const source = composeStoryboardShotPrompt(shot, { index });
    const sourceIdentity = storyboardShotIdentity(shot);
    const operation = reverseOperationForPendingResult();
    const operationId = Number(operation?.id || pendingReverseResult?.operation_id || 0);
    if (!source.trim()) return;
    setReverseActionBusy(`compile-shot-${index}`);
    try {
      const duration = Math.max(
        1,
        Math.round(Number(shot.end_seconds || 0) - Number(shot.start_seconds || 0)),
      );
      if (!selectedGenerationModelConfigId) {
        throw new Error("当前没有可用的视频生成模型");
      }
      const optimizationRequest = {
        prompt: source,
        category: "video",
        product_mode: productGenerationMode,
        duration,
        aspect_ratio: ratio,
        resolution: vResolution,
        mode: "target_model_adaptation",
        target_model_config_id: Number(selectedGenerationModelConfigId),
      };
      const actionRequestId = pendingStudioActionRequestId(
        studioActionPendingRequestRef,
        `storyboard-compile:${me?.id || "unknown"}:${mode}:${operationId || "draft"}:${index}`,
        optimizationRequest,
      );
      const result = await api.createStudioPromptOptimization({
        ...optimizationRequest,
        idempotency_key: actionRequestId,
      });
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      const optimized = String(result?.suggestion?.final_text || "").trim();
      if (!optimized) throw new Error("提示词模型未返回有效的镜头编译稿");

      const latestWorkspace = workspacesRef.current?.[mode] || {};
      const latestPending = latestWorkspace.pendingReverseResult;
      const latestResult = reverseResultPayload(latestPending);
      const latestShots = storyboardResultShots(latestResult);
      if (!latestShots[index] || storyboardShotIdentity(latestShots[index]) !== sourceIdentity) {
        notify.warn("镜头内容已变化，本次编译结果未覆盖新内容。");
        return;
      }
      const nextShots = latestShots.map((item, shotIndex) => (
        shotIndex === index
          ? {
              ...item,
              compiled_prompt: optimized,
              compilation: {
                source_prompt: source,
                optimization_kind: result.optimization_kind || "model_compile",
                direction: result.mode || "target_model_adaptation",
                optimizer_model_id: result.provenance?.optimizer_model_id || null,
                optimizer_model_config_id: result.provenance?.optimizer_model_config_id || null,
                optimizer_model_name: null,
                target_model_config_id: selectedGenerationModelConfigId || null,
                target_model_id: targetModelId || selectedGenerationModel?.model_id || null,
                target_model_name: selectedGenerationModel?.display_name || targetModelId || "当前视频模型",
                compiler_metadata: result.compiler_profile || null,
                change_summary: Array.isArray(result.segments)
                  ? result.segments.filter((item) => item.changed).map((item) => item.label)
                  : [],
                warnings: Array.isArray(result.warnings)
                  ? result.warnings.map((item) => item?.message || String(item)).filter(Boolean)
                  : [],
                charged_credits: Number(result.charged_credits || 0),
              },
            }
          : item
      ));
      const nextResult = storyboardResultWithShots(latestResult, nextShots);
      setWorkspacePatch({
        pendingReverseResult: replaceReverseResultPayload(latestPending, nextResult),
        reverseResultTab: "storyboard",
      }, mode);

      if (operationId) {
        try {
          const revision = normalizeReverseResultRevision(await api.createReverseOperationRevision(operationId, {
            source: "user_edit",
            payload: {
              ...nextResult,
              edit_metadata: { action: "compile_storyboard_shot", shot_index: index },
            },
          }));
          setWorkspacePatch((current) => ({
            reverseResultRevisions: [...(current.reverseResultRevisions || []), revision],
          }), mode);
        } catch (error) {
          reportBackgroundError(error, "save compiled storyboard shot revision");
          notify.warn("镜头已编译，但版本记录同步失败。");
        }
      }
      setMsg(`镜头 ${index + 1} 已编译到 ${selectedGenerationModel?.display_name || "当前视频模型"}。`);
      notify.success("镜头编译完成");
    } catch (error) {
      const text = errorMessage(error, "镜头编译失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function applyStoryboardShot(index, shot) {
    const mode = creationMode;
    const latestWorkspace = workspacesRef.current?.[mode] || workspace;
    const latestPending = latestWorkspace.pendingReverseResult || pendingReverseResult;
    const result = reverseResultPayload(latestPending);
    const shots = storyboardResultShots(result);
    const currentShot = shots[index] || shot;
    const rawPrompt = composeStoryboardShotPrompt(currentShot, { index });
    const compiledPrompt = String(currentShot.compiled_prompt || "").trim();
    const finalPrompt = compiledPrompt || rawPrompt;
    if (!finalPrompt) return;
    const duration = Math.max(
      1,
      Math.round(Number(currentShot.end_seconds || 0) - Number(currentShot.start_seconds || 0)),
    );
    const compilation = currentShot.compilation && typeof currentShot.compilation === "object"
      ? currentShot.compilation
      : null;
    const optimizationRecord = compiledPrompt && compilation
      ? {
        raw_text: compilation.source_prompt || rawPrompt,
        optimized_text: compiledPrompt,
        optimizer_model_id: compilation.optimizer_model_id,
        optimizer_model_config_id: compilation.optimizer_model_config_id,
        optimization_direction: compilation.direction || "model_adaptation",
        optimization_kind: compilation.optimization_kind || "model_compile",
        compiler_metadata: compilation.compiler_metadata || null,
        change_summary: compilation.change_summary || [],
        warnings: compilation.warnings || [],
        scope_key: promptOptimizationContextKey({
          ...promptOptimizationContext,
          duration,
        }),
      }
      : null;

    const operation = reverseOperationForPendingResult();
    if (!operation?.id || !result) {
      notify.error("当前镜头缺少可验证的反推任务，无法应用。");
      return;
    }
    setReverseActionBusy(`apply-shot-${index}`);
    try {
      let knownRevisions = latestWorkspace.reverseResultRevisions || [];
      let parentRevision = findReverseApplyParentRevision(knownRevisions, operation.id);
      if (!parentRevision) {
        knownRevisions = normalizeReverseResultRevisionList(
          await api.reverseOperationRevisions(operation.id),
        );
        parentRevision = findReverseApplyParentRevision(knownRevisions, operation.id);
      }
      if (!parentRevision) throw new Error("反推结果缺少可应用的正式版本");
      const payload = {
        ...storyboardResultWithShots(result, shots),
        final_text: finalPrompt,
        parameters: {
          ...(result.parameters && typeof result.parameters === "object" ? result.parameters : {}),
          vDuration: duration,
          vResolution: latestWorkspace.vResolution || vResolution,
          ratio: latestWorkspace.ratio || ratio,
        },
        application_mode: "storyboard_shot",
        selected_shot_index: index,
        selected_shot: currentShot,
      };
      const response = await api.applyReverseOperationResult(operation.id, {
        payload,
        parent_revision_id: parentRevision.id,
        clear_image_evidence: clearsReviewedImageEvidence(parentRevision, payload),
      });
      const editedRevision = normalizeReverseResultRevision(response.user_edit);
      const appliedRevision = normalizeReverseResultRevision(response.applied);

      if (optimizationRecord) promptOptimizationRecordsRef.current[mode] = optimizationRecord;
      else delete promptOptimizationRecordsRef.current[mode];
      setWorkspacePatch((current) => ({
        prompt: finalPrompt,
        promptDirty: true,
        promptSourceSignature: "",
        vDuration: duration,
        reverseResultTab: "storyboard",
        reverseAppliedVersion: appliedRevision.version,
        reverseAppliedRevisionId: appliedRevision.id,
        reverseResultRevisions: mergeReverseResultRevisions(
          current.reverseResultRevisions || knownRevisions,
          [editedRevision, appliedRevision],
        ),
      }), mode);
      setMsg(`镜头 ${index + 1} 已应用到视频工作区，可检查后直接生成。`);
      notify.success("镜头已应用到工作区");
    } catch (error) {
      reportBackgroundError(error, "apply storyboard shot revision");
      const text = errorMessage(error, "镜头应用失败，工作区未修改");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  function invalidatePromptOptimization(mode = creationMode) {
    optimizePromptRequestRef.current[mode] = (optimizePromptRequestRef.current[mode] || 0) + 1;
    setOptimizingPromptMode((current) => (current === mode ? "" : current));
    setPromptOptimizationProposals((current) => {
      if (!current[mode]) return current;
      const next = { ...current };
      delete next[mode];
      return next;
    });
  }

  async function acceptPromptOptimization(acceptedSegmentIds = []) {
    const mode = creationMode;
    const proposal = promptOptimizationProposals[mode];
    if (!proposal?.optimized_text) return;
    const allSegmentIds = (proposal.segments || []).map((item) => item.id);
    const accepted = acceptedSegmentIds.length ? acceptedSegmentIds : allSegmentIds;
    const rejected = allSegmentIds.filter((id) => !accepted.includes(id));
    let decision;
    try {
      decision = await api.acceptStudioPromptOptimization(proposal.proposal_id, {
        proposal_version: proposal.proposal_version,
        idempotency_key: crypto.randomUUID(),
        accepted_segment_ids: accepted,
        rejected_segment_ids: rejected,
      });
    } catch (error) {
      const text = errorMessage(error, "接受优化建议失败");
      setMsg(text);
      notify.error(text);
      return;
    }
    const latestWorkspace = workspacesRef.current?.[mode] || workspace;
    const applied = applyPromptOptimizationDecision(
      latestWorkspace,
      proposal,
      decision?.result,
      decision?.accepted_segment_ids || accepted,
    );
    if (applied.stale) {
      setMsg("当前工作区已变更，服务端已记录决策，但未覆盖本地内容。");
      notify.warn("工作区已变更，未自动覆盖");
      setPromptOptimizationProposals((current) => {
        const next = { ...current };
        delete next[mode];
        return next;
      });
      return;
    }
    const promptChanged = applied.changedFields.includes("prompt");
    const negativeChanged = applied.changedFields.includes("negative");
    const structuredChanged = applied.changedFields.includes("structured");
    promptOptimizationRecordsRef.current[mode] = {
      raw_text: proposal.raw_text,
      optimized_text: applied.workspace.prompt,
      optimizer_model_id: proposal.optimizer_model_id,
      optimizer_model_config_id: proposal.optimizer_model_config_id,
      optimization_direction: proposal.direction,
      optimization_kind: proposal.optimization_kind,
      compiler_metadata: decision?.result?.compiler_metadata || proposal.compiler_metadata,
      change_summary: proposal.change_summary,
      warnings: proposal.warnings.map((item) => item.message || String(item)),
      proposal_id: proposal.proposal_id,
      scope_key: promptOptimizationContextKey({
        ...promptOptimizationContext,
        duration: category === "video" ? Number(applied.workspace.vDuration) : "",
        aspectRatio: category === "video" ? applied.workspace.ratio : "",
        resolution: category === "video" ? applied.workspace.vResolution : "",
      }),
    };
    setWorkspacePatch({
      ...(applied.applied ? applied.workspace : {}),
      ...(promptChanged ? { promptDirty: true, promptSourceSignature: "" } : {}),
      ...(negativeChanged ? { negativeTouched: true } : {}),
      ...(structuredChanged ? {
        structuredBaseline: applied.workspace.structured,
        structuredDirty: !promptChanged,
      } : {}),
      ...(decision?.revision ? {
        reverseAppliedVersion: decision.revision.version,
        reverseAppliedRevisionId: decision.revision.id,
        reverseResultRevisions: [...(latestWorkspace.reverseResultRevisions || []), decision.revision],
      } : {}),
    }, mode);
    if (applied.undo) {
      setPromptOptimizationUndos((current) => ({ ...current, [mode]: applied.undo }));
    }
    setPromptOptimizationProposals((current) => {
      const next = { ...current };
      delete next[mode];
      return next;
    });
    setMsg(`已接受优化建议 · ${proposal.model_name}`);
  }

  async function rejectPromptOptimization() {
    const proposal = promptOptimizationProposals[creationMode];
    if (!proposal?.proposal_id) return;
    try {
      await api.rejectStudioPromptOptimization(proposal.proposal_id, {
        proposal_version: proposal.proposal_version,
        idempotency_key: crypto.randomUUID(),
      });
    } catch (error) {
      const text = errorMessage(error, "拒绝优化建议失败");
      setMsg(text);
      notify.error(text);
      return;
    }
    setPromptOptimizationProposals((current) => {
      if (!current[creationMode]) return current;
      const next = { ...current };
      delete next[creationMode];
      return next;
    });
    setMsg("已保留原提示词。");
  }

  function undoAcceptedPromptOptimization() {
    const mode = creationMode;
    const undo = promptOptimizationUndos[mode];
    const latestWorkspace = workspacesRef.current?.[mode] || workspace;
    const restored = undoPromptOptimization(latestWorkspace, undo);
    if (restored.stale) {
      notify.warn("工作区已继续编辑，不会自动覆盖。");
      return;
    }
    if (!restored.applied) return;
    const promptChanged = undo.changedFields.includes("prompt");
    const structuredChanged = undo.changedFields.includes("structured");
    setWorkspacePatch({
      ...restored.workspace,
      ...(promptChanged ? { promptDirty: true, promptSourceSignature: "" } : {}),
      ...(undo.changedFields.includes("negative") ? { negativeTouched: true } : {}),
      ...(structuredChanged ? {
        structuredBaseline: restored.workspace.structured,
        structuredDirty: !promptChanged,
      } : {}),
    }, mode);
    setPromptOptimizationUndos((current) => {
      const next = { ...current };
      delete next[mode];
      return next;
    });
    delete promptOptimizationRecordsRef.current[mode];
    setMsg("已撤销上一次提示词优化应用。");
  }

  function changePromptOptimizationSetting(patch) {
    invalidatePromptOptimization();
    setPromptOptimizationSettings((current) => ({
      ...current,
      [creationMode]: { ...promptOptimizationSetting, ...patch },
    }));
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

  async function clearCurrentWorkspace() {
    const mode = creationMode;
    try {
      await cancelReverseOperationForMode(mode);
      await cancelRecoveredProfileOperationForMode(mode);
      await cancelProfileOperation(mode);
    } catch (e) {
      reportBackgroundError(e, "cancel reverse operation while clearing workspace");
      setMsg(errorMessage(e, "后台任务取消失败，已保留当前模式内容。"));
      return;
    }
    invalidatePromptOptimization(mode);
    bumpRefVersion(mode);
    bumpParseRequest(mode);
    bumpUploadRequest(mode);
    bumpProductUploadRequest(mode);
    bumpReverseRequest(mode);
    revokeUploadedObjectUrls(mode);
    revokeProductObjectUrl(mode);
    clearSelectedForMode(mode);
    subjectProfilePendingRequestRef.current = null;
    subjectProfileResultCacheRef.current = null;
    variationRestoreContextRef.current = null;
    delete optimizePromptContextRef.current[mode];
    delete promptOptimizationRecordsRef.current[mode];
    setWorkspacePatch((current) => clearWorkspaceContent(current), mode);
    setStructOpen(true);
    setMsg("");
  }

  async function clearAllWorkspaces() {
    if (!window.confirm("确认清空全部创作模式？所有模式中的提示词、素材和反推结果都会被移除。")) return;
    try {
      await cancelAllReverseOperations();
      await cancelAllProfileOperations();
    } catch (e) {
      reportBackgroundError(e, "cancel reverse operations while clearing all workspaces");
      setMsg(errorMessage(e, "部分后台任务取消失败，已保留全部模式内容。"));
      return;
    }
    for (const { key: mode } of CREATION_MODES) invalidatePromptOptimization(mode);
    resetOwnerReferenceParsing();
    resetOwnerMediaUpload();
    subjectProfilePendingRequestRef.current = null;
    subjectProfileResultCacheRef.current = null;
    variationRestoreContextRef.current = null;
    optimizePromptContextRef.current = {};
    promptOptimizationRecordsRef.current = {};
    setWorkspaces(clearAllWorkspaceContent);
    setStructOpen(true);
    setMsg("");
  }

  function reverseOperationForPendingResult() {
    const operationId = Number(
      pendingReverseResult?.operation_id
      || pendingReverseResult?.operationId
      || 0,
    );
    return [reverseOperation, workspaceReverseOperation]
      .find((item) => item?.id && (!operationId || Number(item.id) === operationId))
      || workspaceReverseOperation
      || reverseOperation
      || null;
  }

  function recipeGenerationSettings(overrides = {}) {
    return {
      model_selections: effectiveModelSelections,
      generation_model_config_id: selectedGenerationModelConfigId || null,
      vision_model_config_id: selectedVisionModelConfigId || null,
      prompt_model_config_id: selectedPromptModelConfigId || null,
      ratio,
      image_quality: imageQuality,
      count: n,
      seed,
      duration: vDuration,
      resolution: vResolution,
      product_video_template: effectiveProductVideoTemplate || productVideoTemplate,
      ...overrides,
    };
  }

  function currentReverseSnapshotV3() {
    const generation = recipeGenerationSettings();
    return {
      ...buildReverseSnapshotV3({
        creationMode,
        subjectMode,
        target: category,
        selected,
        productAsset,
        assets,
        sources: reverseSources,
        structured,
        finalText: prompt,
        promptDirty,
        videoAnalysisPreset,
        videoAnalysis: reverseVideoAnalysis,
        subjectProfile: activeSubjectProfile,
        reverseConfig,
        pendingResult: pendingReverseResult,
        resultTab: reverseResultTab,
        resultSchemaVersion: reverseResultSchemaVersion,
        appliedVersion: reverseAppliedVersion,
        appliedRevisionId: reverseAppliedRevisionId,
        undoSnapshot: reverseUndoSnapshot,
        resultRevisions: reverseResultRevisions,
        feedback: reverseFeedback,
        creationRecipeId,
        creationRecipeVersion,
        creationRecipeShareSlug,
        creationRecipeSource,
      }),
      model_selections: effectiveModelSelections,
      model_config_id: selectedVisionModelConfigId || null,
      generation,
    };
  }

  function recipePayload(sourceOperation = null) {
    const currentOperation = reverseOperationForPendingResult();
    const useCurrentWorkspace = !sourceOperation
      || (currentOperation?.id && String(sourceOperation.id) === String(currentOperation.id));
    if (useCurrentWorkspace) {
      return {
        schema_version: "creation-recipe.v1",
        reverse_snapshot_v3: currentReverseSnapshotV3(),
        prompt,
        negative,
        structured,
        reverse_result: reverseResultPayload(pendingReverseResult),
        generation: recipeGenerationSettings(),
      };
    }

    const operationResult = reverseOperationResult(sourceOperation) || {};
    const sourceSnapshot = sourceOperation.workspace_snapshot_v3 || sourceOperation.workspace_snapshot_v2 || {};
    const restored = workspacePatchFromReverseSnapshot(sourceSnapshot);
    const restoredWorkspace = restored?.workspace || {};
    const restoredCategory = sourceOperation.target === "video" ? "video" : "image";
    const sourceGeneration = sourceSnapshot.generation && typeof sourceSnapshot.generation === "object"
      ? sourceSnapshot.generation
      : {};
    const generation = recipeGenerationSettings({
      product_video_template: restoredWorkspace.productVideoTemplate || "prompt_driven",
      ...sourceGeneration,
    });
    const upgradedSnapshot = {
      ...buildReverseSnapshotV3({
        creationMode: restored?.creationMode || (restoredCategory === "video" ? "video" : "image"),
        subjectMode: restored?.subjectMode || "general",
        target: restoredCategory,
        selected: restoredWorkspace.selected,
        productAsset: restoredWorkspace.productAsset,
        assets: restoredWorkspace.assets || [],
        sources: sourceSnapshot.sources || [],
        structured: operationResult.structured || restoredWorkspace.structured || {},
        finalText: operationResult.final_text || restoredWorkspace.prompt || "",
        videoAnalysisPreset: sourceOperation.analysis_precision,
        videoAnalysis: operationResult.video_analysis || restoredWorkspace.reverseVideoAnalysis,
        reverseConfig: restoredWorkspace.reverseConfig,
        pendingResult: reverseResultEnvelope(sourceOperation, operationResult),
        resultSchemaVersion: sourceOperation.result_schema_version,
        appliedVersion: sourceOperation.applied_result_version,
        appliedRevisionId: restoredWorkspace.reverseAppliedRevisionId,
        resultRevisions: restoredWorkspace.reverseResultRevisions || [],
      }),
      model_selections: sourceSnapshot.model_selections || effectiveModelSelections,
      model_config_id: sourceOperation.model_config_id || null,
      generation,
    };
    return {
      schema_version: "creation-recipe.v1",
      reverse_snapshot_v3: upgradedSnapshot,
      prompt: String(operationResult.final_text || restoredWorkspace.prompt || ""),
      negative: String(operationResult.negative || operationResult.negative_prompt || restoredWorkspace.negative || ""),
      structured: operationResult.structured || restoredWorkspace.structured || {},
      reverse_result: operationResult,
      generation,
    };
  }

  async function saveCreationRecipe(sourceOperation = null, { reuseCurrent = true } = {}) {
    const operation = sourceOperation || reverseOperationForPendingResult();
    const payload = recipePayload(sourceOperation);
    const finalPrompt = String(payload.prompt || "").trim();
    if (!finalPrompt && !Object.keys(payload.structured || {}).length) {
      notify.warn("当前没有可保存的反推结果。");
      return;
    }
    setReverseActionBusy("recipe");
    try {
      if (reuseCurrent && creationRecipeId && (!creationRecipeSource || creationRecipeSource === "owner")) {
        const version = await api.createCreationRecipeVersion(creationRecipeId, { payload });
        setWorkspacePatch({
          creationRecipeVersion: version.version,
          creationRecipeShareSlug: "",
          creationRecipeSource: "owner",
        });
        setMsg(`创作配方已更新到版本 ${version.version}。`);
        notify.success(`创作配方已保存为版本 ${version.version}`);
        return;
      }
      const snapshot = payload.reverse_snapshot_v3 || {};
      const coverAssetUrl = snapshot.selected?.preview_url
        || snapshot.selected?.thumb
        || snapshot.selected?.url
        || null;
      const created = await api.createCreationRecipe({
        title: promptSaveTitle.trim() || (payload.reverse_snapshot_v3?.target === "video" ? "视频反推创作配方" : "图片反推创作配方"),
        category: payload.reverse_snapshot_v3?.target === "video" ? "video" : "image",
        visibility: "private",
        favorite: Boolean(promptSaveFavorite),
        source_operation_id: operation?.status === "succeeded" && ["image", "video"].includes(operation.target)
          ? Number(operation.id)
          : null,
        cover_asset_url: coverAssetUrl,
        payload,
      });
      if (reuseCurrent) setWorkspacePatch({
        creationRecipeId: created.id,
        creationRecipeVersion: created.current_version,
        creationRecipeShareSlug: "",
        creationRecipeSource: "owner",
      });
      setMsg("已保存完整创作配方，可在提示词库中恢复。");
      notify.success("创作配方已保存");
    } catch (error) {
      const text = errorMessage(error, "保存创作配方失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function applyPendingReverseResult(mode, selection) {
    const workspaceMode = creationMode;
    const latestWorkspace = workspacesRef.current?.[workspaceMode] || workspace;
    const latestPending = latestWorkspace.pendingReverseResult || pendingReverseResult;
    if (!latestPending) return;
    const pending = normalizePendingReverseResult(latestPending, {
      mediaType: category,
      sourceSignature: latestPending.source_signature || assetSignature(selected),
    });
    const resolvedSelection = resolveReverseResultApplicationSelection(pending, selection);
    const applied = selection === undefined
      ? applyReverseResultApplication(latestWorkspace, pending, mode)
      : applyReverseResultApplication(latestWorkspace, pending, mode, resolvedSelection);
    const selectedImageEvidence = selectedImageEvidenceForApplication(pending, resolvedSelection);
    if (!applied.changedFields.length && !resolvedSelection.imageEvidence) {
      notify.warn("当前结果在该应用方式下没有可更新内容。");
      return;
    }
    const operation = reverseOperationForPendingResult();
    if (!operation?.id) {
      notify.error("当前结果缺少可验证的反推任务，无法应用。");
      return;
    }
    setReverseActionBusy("apply");
    try {
      let knownRevisions = latestWorkspace.reverseResultRevisions || [];
      let parentRevision = findReverseApplyParentRevision(knownRevisions, operation.id);
      if (!parentRevision) {
        knownRevisions = normalizeReverseResultRevisionList(
          await api.reverseOperationRevisions(operation.id),
        );
        parentRevision = findReverseApplyParentRevision(knownRevisions, operation.id);
      }
      if (!parentRevision) throw new Error("反推结果缺少可应用的正式版本");
      const currentResultPayload = reverseResultPayload(latestPending) || {};
      const applicationBasePayload = Object.fromEntries(
        Object.entries(currentResultPayload).filter(([key]) => key !== "image_evidence"),
      );
      const changedFields = [
        ...applied.changedFields,
        ...(category === "image" && resolvedSelection.imageEvidence ? ["image_evidence"] : []),
      ];
      const payload = {
        ...applicationBasePayload,
        final_text: String(applied.workspace.prompt || ""),
        negative: String(applied.workspace.negative || ""),
        structured: applied.workspace.structured && typeof applied.workspace.structured === "object"
          ? applied.workspace.structured
          : {},
        parameters: {
          ratio: applied.workspace.ratio || null,
          ...(category === "video" ? {
            vDuration: applied.workspace.vDuration || null,
            vResolution: applied.workspace.vResolution || null,
          } : {}),
        },
        ...(category === "video" ? {
          video_analysis: applied.workspace.reverseVideoAnalysis || null,
        } : {
          image_evidence: selectedImageEvidence,
        }),
        application_mode: mode,
        application_selection: resolvedSelection,
        changed_fields: changedFields,
      };
      const response = await api.applyReverseOperationResult(operation.id, {
        payload,
        parent_revision_id: parentRevision.id,
        clear_image_evidence: clearsReviewedImageEvidence(parentRevision, payload),
      });
      const editedRevision = normalizeReverseResultRevision(response.user_edit);
      const appliedRevision = normalizeReverseResultRevision(response.applied);
      const confirmedRevisions = [editedRevision, appliedRevision];
      const currentWorkspace = workspacesRef.current?.[workspaceMode] || latestWorkspace;
      const currentPending = currentWorkspace.pendingReverseResult || latestPending;
      const currentNormalizedPending = normalizePendingReverseResult(currentPending, {
        mediaType: category,
        sourceSignature: currentPending?.source_signature || assetSignature(currentWorkspace.selected),
      });
      const currentSelectedImageEvidence = selectedImageEvidenceForApplication(
        currentNormalizedPending,
        resolvedSelection,
      );
      const evidenceChangedWhilePending = Boolean(
        category === "image"
        && resolvedSelection.imageEvidence
        && JSON.stringify(currentSelectedImageEvidence) !== JSON.stringify(selectedImageEvidence),
      );
      const confirmedApplication = mergeConfirmedReverseResultApplication(
        currentWorkspace,
        latestWorkspace,
        applied,
      );
      const applicationConflicted = evidenceChangedWhilePending
        || confirmedApplication.status === "conflict";
      setWorkspacePatch((current) => ({
        ...(() => {
          const latestMerge = mergeConfirmedReverseResultApplication(
            current,
            latestWorkspace,
            applied,
          );
          const latestPendingResult = current.pendingReverseResult || latestPending;
          const latestNormalizedPending = normalizePendingReverseResult(latestPendingResult, {
            mediaType: category,
            sourceSignature: latestPendingResult?.source_signature || assetSignature(current.selected),
          });
          const latestEvidenceConflict = Boolean(
            category === "image"
            && resolvedSelection.imageEvidence
            && JSON.stringify(selectedImageEvidenceForApplication(
              latestNormalizedPending,
              resolvedSelection,
            )) !== JSON.stringify(selectedImageEvidence),
          );
          if (latestEvidenceConflict || latestMerge.status === "conflict") {
            return {
              reverseApplyConflict: {
                revision_id: appliedRevision.id,
                fields: latestEvidenceConflict
                  ? ["image_evidence", ...latestMerge.conflictFields]
                  : latestMerge.conflictFields,
              },
            };
          }
          return {
            ...Object.fromEntries(
              latestMerge.changedFields.map((field) => [field, latestMerge.workspace[field]]),
            ),
            reverseUndoSnapshot: latestMerge.status === "unchanged"
              ? current.reverseUndoSnapshot
              : latestMerge.undo,
            reverseApplyConflict: null,
          };
        })(),
        reverseAppliedVersion: appliedRevision.version,
        reverseAppliedRevisionId: appliedRevision.id,
        reverseResultRevisions: mergeReverseResultRevisions(
          current.reverseResultRevisions || knownRevisions,
          confirmedRevisions,
        ),
      }), workspaceMode);
      if (applicationConflicted) {
        const conflictText = "应用期间选中字段已被修改，已保留当前编辑；服务端版本已记录，请重新审阅后应用。";
        setMsg(conflictText);
        notify.warn(conflictText);
      } else {
        setMsg("反推结果已应用，可随时撤销本次字段变更。");
        notify.success("反推结果已应用");
      }
    } catch (error) {
      reportBackgroundError(error, "apply reverse result revision");
      const text = errorMessage(error, "反推结果应用失败，工作区未修改");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  function undoAppliedReverseResult() {
    if (!reverseUndoSnapshot) return;
    const restored = undoReverseResultApplication(workspace, reverseUndoSnapshot);
    if (!restored.changedFields.length) return;
    setWorkspacePatch({
      ...restored.workspace,
      reverseAppliedVersion: null,
      reverseAppliedRevisionId: null,
      reverseUndoSnapshot: null,
    });
    setMsg("已撤销上一次反推结果应用。");
    notify.success("已撤销应用");
  }

  async function savePendingReverseVersion() {
    const operation = reverseOperationForPendingResult();
    const payload = reverseResultPayload(pendingReverseResult);
    if (!operation?.id || !payload) return;
    setReverseActionBusy("version");
    try {
      const revision = normalizeReverseResultRevision(await api.createReverseOperationRevision(operation.id, {
        source: "user_edit",
        payload,
      }));
      setWorkspacePatch((current) => ({
        pendingReverseResult: current.pendingReverseResult
          ? { ...current.pendingReverseResult, dirty: false }
          : current.pendingReverseResult,
        reverseResultRevisions: [...(current.reverseResultRevisions || []), revision],
      }));
      setMsg(`已保存反推结果版本 ${revision.version}。`);
      notify.success("当前版本已保存");
    } catch (error) {
      const text = errorMessage(error, "保存反推结果版本失败");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function submitReverseFeedback(input) {
    const operation = reverseOperationForPendingResult();
    if (!operation?.id) return;
    const body = typeof input === "string" ? { rating: input, issue_types: [] } : input;
    setReverseActionBusy("feedback");
    try {
      const feedback = normalizeReverseOperationFeedback(
        await api.updateReverseOperationFeedback(operation.id, body),
      );
      setWorkspacePatch({ reverseFeedback: feedback });
      notify.success("反馈已保存");
    } catch (error) {
      const text = errorMessage(error, "反馈保存失败");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  function restoreReverseRevision(revision) {
    const operation = reverseOperationForPendingResult();
    setWorkspacePatch({
      pendingReverseResult: reverseResultEnvelope(
        operation || {
          id: revision.operation_id,
          target: category,
        },
        revision.payload,
        revision.source === "user_edit",
      ),
      reverseResultTab: "draft",
    });
    setMsg(`已恢复版本 ${revision.version} 到待应用区，当前工作区尚未改变。`);
  }

  async function openRecentReverseOperation(candidate) {
    if (!candidate?.id) return;
    setRecentReverseBusyId(candidate.id);
    try {
      const operation = normalizeReverseOperation(await api.reverseOperation(candidate.id));
      const result = reverseOperationResult(operation);
      if (!result) throw new Error("该反推任务没有可恢复结果");
      const restored = workspacePatchFromReverseSnapshot(
        operation.workspace_snapshot_v3 || operation.workspace_snapshot_v2,
      );
      const mode = restored?.creationMode || (operation.target === "video" ? "video" : "image");
      const [revisionPayload, feedbackPayload] = await Promise.all([
        api.reverseOperationRevisions(operation.id),
        api.reverseOperationFeedback(operation.id),
      ]);
      setCreationMode(mode);
      const revisions = normalizeReverseResultRevisionList(revisionPayload);
      const appliedRevision = findGenerationSourceRevision(revisions, {
        version: operation.applied_result_version,
        operationId: operation.id,
      });
      setWorkspacePatch({
        ...(restored?.workspace || {}),
        editSubjectMode: restored?.subjectMode || "general",
        imageEditProductMode: ["product", "portrait"].includes(restored?.subjectMode),
        reverseOperation: operation,
        pendingReverseResult: reverseResultEnvelope(operation, result),
        reverseResultTab: "draft",
        reverseResultSchemaVersion: operation.result_schema_version || "",
        reverseAppliedVersion: operation.applied_result_version || null,
        reverseAppliedRevisionId: appliedRevision?.id || null,
        reverseResultRevisions: revisions,
        reverseFeedback: feedbackPayload ? normalizeReverseOperationFeedback(feedbackPayload) : null,
      }, mode);
      setStructOpen(true);
      setRefOpen(Boolean(restored?.workspace?.selected || restored?.workspace?.productAsset));
      setMsg("已恢复历史反推结果到待应用区。");
    } catch (error) {
      const text = errorMessage(error, "恢复历史反推失败");
      setMsg(text);
      notify.error(text);
    } finally {
      setRecentReverseBusyId(null);
    }
  }

  function toggleBatchReverseAsset(asset) {
    if (!assetReferenceUrl(asset)) return;
    if (category === "image" && asset.type !== "image") {
      notify.warn("图片反推批次只能选择图片素材。");
      return;
    }
    setWorkspacePatch((current) => {
      const existing = current.batchReverseAssets || [];
      const key = unifiedAssetKey(asset) || assetReferenceUrl(asset);
      const found = existing.some((item) => (
        (unifiedAssetKey(item) || assetReferenceUrl(item)) === key
      ));
      if (found) {
        return { batchReverseAssets: existing.filter((item) => (
          (unifiedAssetKey(item) || assetReferenceUrl(item)) !== key
        )) };
      }
      if (existing.length >= MAX_REVERSE_BATCH_ITEMS) {
        notify.warn(`单次最多选择 ${MAX_REVERSE_BATCH_ITEMS} 个素材。`);
        return {};
      }
      return { batchReverseAssets: dedupeAssets([...existing, asset], MAX_REVERSE_BATCH_ITEMS) };
    });
  }

  function removeBatchReverseAsset(asset) {
    const key = unifiedAssetKey(asset) || assetReferenceUrl(asset);
    setWorkspacePatch((current) => ({
      batchReverseAssets: (current.batchReverseAssets || []).filter((item) => (
        (unifiedAssetKey(item) || assetReferenceUrl(item)) !== key
      )),
    }));
  }

  async function startReverseBatch(itemOverrides = {}, overrideCapability = { supported: false, fields: [], audio_policies: [] }) {
    const selectedAssets = dedupeAssets(batchReverseAssets, MAX_REVERSE_BATCH_ITEMS);
    if (selectedAssets.length < 2) {
      notify.warn("请至少选择 2 个素材进行批量反推。");
      return;
    }
    if (category === "image" && selectedAssets.some((asset) => asset.type !== "image")) {
      notify.warn("图片反推批次不能包含视频素材。");
      return;
    }
    const everyVideo = selectedAssets.every((asset) => asset.type === "video");
    const normalizedConfig = normalizeReverseConfig({
      ...(reverseConfig || {}),
      source_range: null,
      source_ranges: [],
      custom_keyframes: [],
      include_audio: everyVideo && Boolean(reverseConfig?.include_audio),
    }, { category });
    for (const asset of selectedAssets) {
      const validation = validateReverseConfig(normalizedConfig, {
        category,
        selectedType: asset.type,
        duration: asset.duration,
      });
      if (!validation.valid) {
        notify.warn(validation.errors[0]?.message || "批量反推设置不适用于所选素材。");
        return;
      }
    }
    const clientRequestId = `reverse-batch-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    const items = selectedAssets.map((asset) => {
      const url = assetReferenceUrl(asset);
      const sources = [{
        asset_url: url,
        source_type: asset.type,
        role: "primary",
        ...(Number.isInteger(Number(asset.id)) && Number(asset.id) > 0 ? { asset_id: Number(asset.id) } : {}),
      }];
      const item = {
        asset_url: url,
        source_type: asset.type,
        fallback_image: asset.type === "video" ? (asset.thumb || asset.preview_url || null) : null,
        sources,
        workspace_snapshot_v3: {
          ...buildReverseOperationRequestSnapshotV3({
            creationMode,
            subjectMode: "general",
            target: category,
            selected: asset,
            assets: selectedAssets,
            sources,
            videoAnalysisPreset: normalizedConfig.analysis_precision,
            reverseConfig: normalizedConfig,
          }),
          source_signature: assetSignature(asset),
          model_selections: effectiveModelSelections,
          ...(selectedVisionModelConfigId ? { model_config_id: selectedVisionModelConfigId } : {}),
        },
      };
      return buildReverseBatchItemPayload(
        item,
        itemOverrides[unifiedAssetKey(asset) || assetReferenceUrl(asset)],
        overrideCapability,
      );
    });
    try {
      const batchRequest = {
        client_request_id: clientRequestId,
        name: `${category === "video" ? "视频" : "图片"}批量反推 · ${selectedAssets.length} 项`,
        target: category,
        analysis_focus: normalizedConfig.analysis_focus,
        analysis_precision: normalizedConfig.analysis_precision,
        output_purpose: normalizedConfig.output_purpose,
        custom_instruction: normalizedConfig.custom_instruction || null,
        include_audio: normalizedConfig.include_audio,
        model_config_id: selectedVisionModelConfigId || null,
        items,
      };
      const confirmation = await requestQuoteConfirmation({
        kind: "reverse_batch",
        request: batchRequest,
        clientRequestId,
        execute: ({ request: confirmed }) => createReverseBatch(confirmed),
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("批量反推提交失败。");
        }
        return;
      }
      setMsg(`已创建 ${selectedAssets.length} 项批量反推，结果会逐项返回。`);
      notify.success("批量反推已开始");
      refreshMe();
    } catch (error) {
      const text = errorMessage(error, "创建批量反推失败");
      setMsg(text);
      notify.error(text);
    }
  }

  async function saveReverseBatchRecipes(operations) {
    const successful = (operations || []).filter((operation) => operation?.status === "succeeded");
    if (!successful.length) return;
    setReverseActionBusy("batch-recipes");
    try {
      const results = await Promise.allSettled(successful.map((operation, index) => {
        const payload = recipePayload(operation);
        const snapshot = payload.reverse_snapshot_v3 || {};
        const coverAssetUrl = snapshot.selected?.preview_url
          || snapshot.selected?.thumb
          || snapshot.selected?.url
          || null;
        return api.createCreationRecipe({
          title: `${operation.target === "video" ? "视频" : "图片"}批量反推配方 ${index + 1}`,
          category: operation.target === "video" ? "video" : "image",
          visibility: "private",
          favorite: false,
          source_operation_id: Number(operation.id),
          cover_asset_url: coverAssetUrl,
          payload,
        });
      }));
      const saved = results.filter((item) => item.status === "fulfilled").length;
      const failed = results.length - saved;
      setMsg(`批量保存完成：成功 ${saved}，失败 ${failed}。`);
      if (failed) notify.warn(`已保存 ${saved} 个配方，${failed} 个保存失败。`);
      else notify.success(`已保存 ${saved} 个创作配方`);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function retryReverse(candidate = null) {
    const operation = candidate || reverseOperationForPendingResult();
    if (!operation?.id) return;
    setRecentReverseBusyId(operation.id);
    setReverseActionBusy("retry");
    try {
      const clientRequestId = `reverse-retry-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
      const retryRequest = {
        reverse_operation_id: Number(operation.id),
        client_request_id: clientRequestId,
        model_config_id: selectedVisionModelConfigId || undefined,
      };
      const confirmation = await requestQuoteConfirmation({
        kind: "reverse",
        request: retryRequest,
        clientRequestId,
        execute: ({ request: confirmed }) => api.retryReverseOperation(operation.id, {
          client_request_id: confirmed.client_request_id,
          model_config_id: confirmed.model_config_id,
          quote_id: confirmed.quote_id,
        }),
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("反推重试提交失败。");
        }
        return;
      }
      const retried = normalizeReverseOperation(confirmation.result);
      const restored = workspacePatchFromReverseSnapshot(
        retried.workspace_snapshot_v3 || retried.workspace_snapshot_v2,
      );
      const mode = restored?.creationMode || (retried.target === "video" ? "video" : "image");
      setCreationMode(mode);
      setWorkspacePatch({
        ...(restored?.workspace || {}),
        reverseOperation: retried,
        reversing: true,
        pendingReverseResult: null,
        reverseAppliedVersion: null,
        reverseAppliedRevisionId: null,
        reverseResultRevisions: [],
        reverseFeedback: null,
        reverseUndoSnapshot: null,
      }, mode);
      upsertRecentReverseOperation(retried);
      await trackReverseOperation(retried, mode);
      setMsg("已重新发起反推，新任务会保留与原任务的关联。");
    } catch (error) {
      const text = errorMessage(error, "再次反推失败");
      setMsg(text);
      notify.error(text);
    } finally {
      setRecentReverseBusyId(null);
      setReverseActionBusy("");
    }
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
          reverse_snapshot_v3: {
            ...currentReverseSnapshotV3(),
            final_text: text,
          },
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
      structuredBaseline: {},
      structuredDirty: false,
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
      portraitProfile: null,
      portraitProfileSource: "",
      productProfiling: false,
      variationSource: nextAsset,
      selected: null,
      assets: [],
      structured: {},
      structuredBaseline: {},
      structuredDirty: false,
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
    const quoteRequest = { asset_id: Number(asset.id) };
    const actionRequestId = pendingStudioActionRequestId(
      studioActionPendingRequestRef,
      `asset-unlock:${me?.id || "unknown"}`,
      quoteRequest,
    );
    busyAssetIdsRef.current.add(asset.id);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
    try {
      const confirmation = await requestQuoteConfirmation({
        kind: "asset_unlock",
        request: quoteRequest,
        clientRequestId: actionRequestId,
        execute: ({ request: confirmedRequest }) => {
          if (!ownerRequest.isCurrent()) throw new Error("账号已切换，本次解锁已取消。");
          return api.unlock(asset.id, { quote_id: Number(confirmedRequest.quote_id) });
        },
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("高清解锁失败。");
        }
        if (confirmation.status === "invalidated") {
          ownerRequest.commit(() => setMsg(confirmation.reason || "页面状态已变化，请重新解锁高清素材。"));
        }
        return;
      }
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      const updated = confirmation.result;
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
    reverseImageCost,
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
    cfg: studioCfg,
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
    const estimatedCredits = category === "video" ? videoFinalCost : estCost;
    const imageUnitCredits = category === "image" && imageCount > 0
      ? estCost / imageCount
      : 0;
    return (
      <div
        className={isMobile
          ? "fixed inset-x-0 bottom-0 z-30 flex min-w-0 items-center justify-between gap-3 border-t border-line bg-base/90 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-pop backdrop-blur-xl lg:hidden"
          : "mt-3 hidden min-w-0 items-center justify-between gap-3 px-1 lg:flex"
        }
      >
        <div className="min-w-0" aria-live="polite">
          {estCost ? (
            <>
              <div className="flex min-w-0 items-baseline gap-1 whitespace-nowrap">
                <span className="text-[11px] text-fog">预计消耗</span>
                <strong className="font-display text-base font-semibold text-snow">{estimatedCredits}</strong>
                <span className="text-xs font-medium text-mist">积分</span>
              </div>
              <p className="mt-0.5 truncate text-[10px] leading-snug text-fog">
                {category === "video"
                  ? `${formatDuration(videoDuration)} · ${vResolution}`
                  : `${imageCount} 张 × ${imageUnitCredits} 积分/张 · 可连续提交`}
              </p>
            </>
          ) : (
            <p className="text-xs leading-snug text-fog">费用将在提交时按当前参数预估并冻结</p>
          )}
        </div>
        <button
          onClick={() => submit(category === "video" ? "final" : "preview")}
          disabled={missingRequiredSource || productVideoStrategyUnsupported || structuredDirty || generationSubmitDisabled({
            submitting,
            busy: parsing || uploading || reversing || productProfiling,
            currentTask: task,
            nextCategory: category,
            currentModelEnabled,
          })}
          title={missingRequiredSource
            ? `${missingRequiredSourceLabel}后再生成`
            : productVideoStrategyUnsupported
              ? "当前视频模型未提供可用的产品视频策略，请先切换模型"
              : structuredDirty
                ? "请先应用结构修改或撤销结构修改"
                : undefined}
          className="btn-primary btn-lg min-w-28 shrink-0 px-4 sm:min-w-32 sm:px-6"
        >
          {(submitting || running) && (
            <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-white/35 border-t-white" aria-hidden />
          )}
          {missingRequiredSource
            ? missingRequiredSourceLabel
            : productVideoStrategyUnsupported ? "请切换视频模型" : submitLabel}
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
                  readySteps={editReadySteps}
                  editStyleKeys={editStyleKeys}
                  onPromptChange={updatePromptFromUser}
                  onPromptDirty={setPromptDirty}
                  onOptimizePrompt={optimizeDirectPrompt}
                  canOptimizePrompt={Boolean(
                    promptReadyForOptimization
                    && (!cfg?.model_options || selectedPromptModelConfigId)
                    && selectedGenerationModelConfigId
                  )}
                  optimizingPrompt={optimizingPrompt}
                  optimizationProposal={promptOptimizationProposal}
                  optimizationDirection={promptOptimizationSetting.direction}
                  optimizationTargetLanguage={promptOptimizationSetting.targetLanguage}
                  optimizationEstimatedCredits={Number(selectedPromptModel?.cost_credits || 0)}
                  onOptimizationSettingsChange={changePromptOptimizationSetting}
                  onAcceptOptimization={acceptPromptOptimization}
                  onRejectOptimization={rejectPromptOptimization}
                  optimizationUndoAvailable={Boolean(promptOptimizationUndos[creationMode])}
                  onUndoOptimization={undoAcceptedPromptOptimization}
                  generationModelOptions={generationModelOptions}
                  selectedGenerationModelConfigId={selectedGenerationModelConfigId}
                  onGenerationModelChange={changeGenerationModelSelection}
                  productVideoStrategyOptions={productVideoStrategySelection.options}
                  productVideoTemplate={effectiveProductVideoTemplate}
                  productVideoStrategySupported={productVideoStrategySelection.supported}
                  onProductVideoTemplateChange={changeProductVideoTemplate}
                  promptModelOptions={promptModelOptions}
                  selectedPromptModelConfigId={selectedPromptModelConfigId}
                  onPromptModelChange={(id) => changeModelSelection("prompt", id)}
                  onClearWorkspace={clearCurrentWorkspace}
                  onClearAllWorkspaces={clearAllWorkspaces}
                  canClearWorkspace={Boolean(
                    prompt.trim()
                    || negative.trim()
                    || url.trim()
                    || selected
                    || lastFrameAsset
                    || productAsset
                    || assets.length
                    || Object.keys(structured || {}).length
                    || productProfile
                    || portraitProfile
                    || variationSource
                    || parsing
                    || uploading
                    || reversing
                    || batchReverseAssets.length
                    || productProfiling
                    || subjectProtection
                    || optimizingPrompt
                    || pendingReverseResult
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
                lastFrameAsset={effectiveLastFrameAsset}
                firstLastFrameEnabled={firstLastFrameEnabled}
                productAsset={productAsset}
                productDetailAssets={productDetailAssets}
                productDetailValidation={productDetailValidation}
                productDetailLimit={productDetailLimit}
                url={url}
                appliedUrl={appliedUrl}
                setUrl={updateReferenceUrl}
                parsing={parsing}
                uploading={uploading || submitting}
                productBusy={submitting}
                productProfiling={productProfiling}
                profileOperation={profileOperation}
                assets={assets}
                refOpen={refOpen}
                setRefOpen={setRefOpen}
                reversing={reversing}
                reverseEnabled={Boolean(
                  reverseEnabled
                  && (!cfg?.model_options || selectedVisionModelConfigId)
                )}
                reverseImageCost={reverseImageCost}
                selectedReverseCost={selectedReverseCost}
                selectedReverseCostLabel={selectedReverseCostLabel}
                reverseConfig={reverseConfig}
                setReverseConfig={setReverseConfig}
                reverseSources={reverseSources}
                setReverseSources={(value) => setWorkspacePatch({ reverseSources: value })}
                videoAnalysisPreset={videoAnalysisPreset}
                videoAnalysisPresets={reverseVideoPresets}
                reverseVideoAnalysis={reverseVideoAnalysis}
                reverseOperation={reverseOperation || workspaceReverseOperation}
                batchReverseAssets={batchReverseAssets}
                reverseBatch={reverseBatch}
                recentReverseBatches={recentReverseBatches}
                reverseBatchBusyAction={reverseBatchBusyAction || reverseActionBusy}
                reverseBatchCapabilities={cfg?.reverse?.batch_capabilities || cfg?.reverse?.capabilities || null}
                reverseBatchEnabled={reverseBatchEnabled}
                visionModelOptions={visionModelOptions}
                selectedVisionModelConfigId={selectedVisionModelConfigId}
                onVisionModelChange={(id) => changeModelSelection("vision", id)}
                setVideoAnalysisPreset={setVideoAnalysisPreset}
                imageUploadInputRef={imageUploadInputRef}
                productUploadInputRef={productUploadInputRef}
                productDetailUploadInputRef={productDetailUploadInputRef}
                lastFrameUploadInputRef={lastFrameUploadInputRef}
                videoUploadInputRef={videoUploadInputRef}
                onClear={clearRef}
                onClearProductAsset={clearProductAsset}
                onParse={doParse}
                onUploadImage={doUploadImage}
                onUploadProductImage={doUploadProductImage}
                onUploadProductDetailImages={doUploadProductDetailImages}
                onUploadLastFrameImage={doUploadLastFrameImage}
                onClearLastFrameAsset={() => setWorkspacePatch({ lastFrameAsset: null })}
                onOpenAssetPicker={(role) => {
                  if (role === "product_detail" && !productAsset) {
                    setMsg("请先选择产品主题图");
                    return;
                  }
                  setAssetPicker({ role });
                }}
                onRemoveProductDetail={(index) => setWorkspacePatch((current) => ({
                  productDetailAssets: (current.productDetailAssets || []).filter((_, itemIndex) => itemIndex !== index),
                }))}
                onMoveProductDetail={(index, direction) => setWorkspacePatch((current) => {
                  const next = [...(current.productDetailAssets || [])];
                  const target = index + direction;
                  if (target < 0 || target >= next.length) return {};
                  [next[index], next[target]] = [next[target], next[index]];
                  return { productDetailAssets: next };
                })}
                onUploadVideo={doUploadVideo}
                onPickAsset={pickAsset}
                onReverse={doReverse}
                onConfirmCover={(fallbackFile) => confirmReverseCover(fallbackFile).catch((e) => {
                  setMsg(e.message || "封面分析确认失败，请重试。");
                  reportBackgroundError(e, "confirm reverse cover");
                })}
                onCancelReverse={() => cancelReverseOperationForMode(creationMode).catch((e) => {
                  reportBackgroundError(e, "cancel reverse from reference panel");
                })}
                onToggleBatchAsset={toggleBatchReverseAsset}
                onRemoveBatchAsset={removeBatchReverseAsset}
                onOpenBatchAssetPicker={() => {
                  if (reverseBatchEnabled) setAssetPicker({ role: "reverse_batch" });
                }}
                onStartReverseBatch={startReverseBatch}
                onCancelReverseBatch={() => cancelReverseBatch().then(() => {
                  setMsg("已请求取消批量反推，运行中的单项会按任务状态完成取消。");
                }).catch((error) => {
                  const text = errorMessage(error, "取消批量反推失败");
                  setMsg(text);
                  notify.error(text);
                })}
                onOpenReverseBatch={openReverseBatch}
                onOpenReverseBatchItem={openRecentReverseOperation}
                onRetryReverseBatchItem={retryReverse}
                onSaveReverseBatchRecipes={saveReverseBatchRecipes}
              />
            </div>

            <StudioReverseResultPanel
              pending={pendingReverseResult}
              operation={reverseOperationForPendingResult()}
              activeTab={reverseResultTab}
              revisions={reverseResultRevisions}
              appliedVersion={reverseAppliedVersion}
              appliedRevisionId={reverseAppliedRevisionId}
              feedback={reverseFeedback}
              canUndo={Boolean(reverseUndoSnapshot)}
              busyAction={reverseActionBusy}
              generationModelName={selectedGenerationModel?.display_name || targetModelId || "当前视频模型"}
              onTabChange={(value) => setWorkspacePatch({ reverseResultTab: value })}
              onResultChange={(value) => setWorkspacePatch({ pendingReverseResult: value })}
              onApply={applyPendingReverseResult}
              onUndo={undoAppliedReverseResult}
              onClose={() => setWorkspacePatch({ pendingReverseResult: null })}
              onRetry={() => retryReverse()}
              onSavePrompt={saveReversePromptToLibrary}
              onSaveVersion={savePendingReverseVersion}
              onSaveRecipe={() => saveCreationRecipe()}
              onFeedback={submitReverseFeedback}
              onRestoreRevision={restoreReverseRevision}
              onCompileStoryboardShot={compileStoryboardShot}
              onApplyStoryboardShot={applyStoryboardShot}
              onGenerationSubmitted={handleGenerationSubmitted}
              onPickCompositionAsset={(request) => setAssetPicker(request)}
              requestQuoteConfirmation={requestQuoteConfirmation}
              videoCompositionEnabled={videoCompositionEnabled}
              recipesEnabled={recipesEnabled}
            />

            <StudioRecentReversePanel
              operations={recentReverseOperations.filter((item) => ["image", "video"].includes(item.target))}
              loading={recentReverseLoading}
              error={recentReverseError}
              busyOperationId={recentReverseBusyId}
              onRefresh={refreshRecentReverseOperations}
              onOpen={openRecentReverseOperation}
              onRetry={retryReverse}
              onSaveRecipe={(item) => saveCreationRecipe(item, { reuseCurrent: false })}
              recipesEnabled={recipesEnabled}
            />

            <StudioStructuredEditor
              structured={structured}
              open={structOpen}
              onToggleOpen={() => setStructOpen((open) => !open)}
              onRecompose={recompose}
              onUndo={undoStructuredChanges}
              onClear={clearRef}
              structuredDirty={structuredDirty}
              promptDirty={promptDirty}
              onChange={(key, value) => {
                const nextStructured = { ...structured, [key]: value };
                if (promptDirty) {
                  setWorkspacePatch({ structured: nextStructured, structuredDirty: true });
                  return;
                }
                const nextPrompt = isEditMode
                  ? composeStyleTransferPrompt(nextStructured, prompt, { video: category === "video", subject: subjectMode })
                  : composePromptFromStructured(nextStructured, prompt, { target: category });
                setWorkspacePatch({
                  structured: nextStructured,
                  structuredBaseline: nextStructured,
                  structuredDirty: false,
                  prompt: nextPrompt,
                  promptDirty: false,
                  promptSourceSignature: structuredSource || assetSignature(selected) || "",
                });
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

          <StudioMessageBar message={msg} onDismiss={setMsg} />
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
          sourceAsset={selected}
          reverseOperationId={generationReverseOperationId}
          reverseRevisionId={generationReverseRevisionId}
          reproductionModelConfigId={selectedGenerationModelConfigId}
          reproductionVideoComposition={reproductionVideoComposition}
          requestQuoteConfirmation={requestQuoteConfirmation}
          onReproductionCorrectionCreated={handleReproductionCorrectionCreated}
          onReproductionRemediationCreated={handleReproductionRemediationCreated}
          onReproductionRemediationExecutionSubmitted={handleReproductionRemediationExecutionSubmitted}
          reproductionAssessmentEnabled={reproductionAssessmentEnabled}
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
      <AssetPickerDialog
        open={Boolean(assetPicker && (reverseBatchEnabled || assetPicker.role !== "reverse_batch"))}
        role={assetPicker?.role || "product_theme"}
        multiple={assetPicker?.multiple ?? ["product_detail", "reverse_batch"].includes(assetPicker?.role)}
        maxSelection={assetPicker?.maxSelection ?? (assetPicker?.role === "reverse_batch"
          ? MAX_REVERSE_BATCH_ITEMS
          : assetPicker?.role === "product_detail"
            ? Math.max(0, productDetailLimit - productDetailAssets.length)
            : 1)}
        selected={assetPicker?.selected || (assetPicker?.role === "product_theme"
          ? (productAsset ? [productAsset] : [])
          : assetPicker?.role === "last_frame"
            ? (effectiveLastFrameAsset ? [effectiveLastFrameAsset] : [])
          : assetPicker?.role === "reverse_batch"
            ? batchReverseAssets
            : [])}
        mediaType={assetPicker?.mediaType || (assetPicker?.role === "reverse_batch" && category === "video" ? "all" : "image")}
        excludedRefs={assetPicker?.role === "product_detail" && productAsset
          ? [unifiedAssetKey(productAsset)]
          : assetPicker?.role === "last_frame" && selected
            ? [unifiedAssetKey(selected)]
            : []}
        excludedUrls={assetPicker?.role === "product_detail" && productAsset
          ? [assetReferenceUrl(productAsset)]
          : assetPicker?.role === "last_frame" && selected
            ? [assetReferenceUrl(selected)]
            : []}
        onClose={() => setAssetPicker(null)}
        onUploadRequest={assetPicker?.role === "reverse_batch" ? undefined : () => {
          const role = assetPicker?.role;
          setAssetPicker(null);
          if (["storyboard_shot", "storyboard_audio"].includes(role)) videoUploadInputRef.current?.click();
          else if (role === "product_detail") productDetailUploadInputRef.current?.click();
          else if (role === "last_frame") lastFrameUploadInputRef.current?.click();
          else productUploadInputRef.current?.click();
        }}
        onConfirm={(picked) => {
          if (typeof assetPicker?.onConfirm === "function") {
            assetPicker.onConfirm(picked);
          } else if (assetPicker?.role === "reverse_batch" && reverseBatchEnabled) {
            setWorkspacePatch({
              batchReverseAssets: dedupeAssets(picked, MAX_REVERSE_BATCH_ITEMS),
            });
          } else if (assetPicker?.role === "product_theme") {
            const next = picked[0];
            if (next) {
              const url = assetReferenceUrl(next);
              const clean = { ...next, url };
              setWorkspacePatch((current) => ({
                productDetailAssets: (current.productDetailAssets || []).filter((item) => assetReferenceUrl(item) !== url),
              }));
              selectProductAsset(clean);
            }
          } else if (assetPicker?.role === "last_frame") {
            const next = picked[0];
            const url = assetReferenceUrl(next);
            if (next && url) selectLastFrameAsset({ ...next, url });
          } else {
            if (!productAsset) {
              setMsg("请先选择产品主题图");
              setAssetPicker(null);
              return;
            }
            setWorkspacePatch((current) => {
              const mainUrl = assetReferenceUrl(current.productAsset);
              const existing = current.productDetailAssets || [];
              const combined = dedupeAssets([...existing, ...picked]);
              const next = combined.filter((item) => assetReferenceUrl(item) !== mainUrl);
              if (next.length > 3) return {};
              return { productDetailAssets: next };
            });
          }
          setAssetPicker(null);
        }}
      />
      {renderSubmitBar("mobile")}
    </div>
  );
}
