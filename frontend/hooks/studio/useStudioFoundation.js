"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "../../components/ToastProvider";
import { api } from "../../lib/api";
import { reportBackgroundError } from "../../lib/errorHandling";
import useStudioWorkspaceState from "../useStudioWorkspaceState";
import useStudioSharedRefs from "./useStudioSharedRefs";
import { CREATION_MODES } from "../../app/studio/constants";
import { studioCreationFacts } from "../../app/studio/viewModel";

export default function useStudioFoundation() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [creationMode, setCreationMode] = useState("image");
  const [showNegative, setShowNegative] = useState(false);
  const [promptSaveTitle, setPromptSaveTitle] = useState("");
  const [promptSaveCategory, setPromptSaveCategory] = useState("image");
  const [promptSaveFavorite, setPromptSaveFavorite] = useState(false);
  const [reverseActionBusy, setReverseActionBusy] = useState("");
  const [recentReverseBusyId, setRecentReverseBusyId] = useState(null);
  const [assetPicker, setAssetPicker] = useState(null);
  const [refOpen, setRefOpen] = useState(false);
  const [structOpen, setStructOpen] = useState(true);
  const [msg, setMsg] = useState("");

  const refs = useStudioSharedRefs();

  const workspaceState = useStudioWorkspaceState({
    creationMode,
    modes: CREATION_MODES,
  });
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
  } = workspaceState;
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
    reverseApplyConflict = null,
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

  const reverseBatchEnabled = cfg?.features?.reverse_batch_enabled !== false;
  const videoCompositionEnabled = cfg?.features?.video_composition_enabled !== false;
  const reproductionAssessmentEnabled = cfg?.features?.reproduction_assessment_enabled !== false;
  const recipesEnabled = cfg?.features?.recipes_enabled !== false;
  const toolWorkflowsEnabled = cfg?.features?.tool_workflows_enabled === true;
  const creationFacts = studioCreationFacts({
    creationMode,
    imageEditProductMode,
    editSubjectMode,
    productAsset,
  });
  const {
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
  } = creationFacts;
  const activeSubjectProfile = portraitGenerationMode ? portraitProfile : productProfile;
  const activeSubjectProfileSource = portraitGenerationMode
    ? portraitProfileSource
    : productProfileSource;

  useEffect(() => {
    if (!reverseBatchEnabled && assetPicker?.role === "reverse_batch") {
      setAssetPicker(null);
    }
  }, [assetPicker?.role, reverseBatchEnabled]);

  refs.workspacesRef.current = workspaces;

  function refreshMe() {
    return refs.latestMeRequestRef.current
      .run(() => api.me(), setMe)
      .catch((error) => reportBackgroundError(error, "refresh current user"));
  }

  return {
    router,
    notify,
    me,
    setMe,
    cfg,
    setCfg,
    creationMode,
    setCreationMode,
    showNegative,
    setShowNegative,
    promptSaveTitle,
    setPromptSaveTitle,
    promptSaveCategory,
    setPromptSaveCategory,
    promptSaveFavorite,
    setPromptSaveFavorite,
    reverseActionBusy,
    setReverseActionBusy,
    recentReverseBusyId,
    setRecentReverseBusyId,
    assetPicker,
    setAssetPicker,
    refOpen,
    setRefOpen,
    structOpen,
    setStructOpen,
    msg,
    setMsg,
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
    prompt,
    negative,
    imageEditProductMode,
    editSubjectMode,
    ratio,
    imageQuality,
    n,
    seed,
    editMaskMode,
    productPixelLockMode,
    vDuration,
    vResolution,
    productVideoTemplate,
    videoAnalysisPreset,
    url,
    appliedUrl,
    parsing,
    uploading,
    reversing,
    workspaceReverseOperation,
    profileOperation,
    assets,
    selected,
    lastFrameAsset,
    productAsset,
    productDetailAssets,
    productProfile,
    productProfileSource,
    portraitProfile,
    portraitProfileSource,
    productProfiling,
    subjectProtection,
    subjectProtectionLoading,
    subjectProtectionSource,
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
    reverseApplyConflict,
    reverseResultRevisions,
    reverseFeedback,
    batchReverseAssets,
    creationRecipeId,
    creationRecipeVersion,
    creationRecipeShareSlug,
    creationRecipeSource,
    promptSourceSignature,
    negativeTouched,
    promptDirty,
    reverseBatchEnabled,
    videoCompositionEnabled,
    reproductionAssessmentEnabled,
    recipesEnabled,
    toolWorkflowsEnabled,
    category,
    isEditMode,
    isImageEditMode,
    subjectMode,
    productGenerationMode,
    portraitGenerationMode,
    activeSubjectProfile,
    activeSubjectProfileSource,
    reverseResumeOperations: refs.reverseResumeOperationsRef.current,
    refreshMe,
    getOwnerSession: () => refs.studioOwnerSessionRef.current,
    refs,
  };
}
