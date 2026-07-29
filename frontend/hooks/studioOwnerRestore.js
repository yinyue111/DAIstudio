import { api } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  canApplyStudioPromptTransfer,
  mergeStudioDraftMetadataLayers,
  mergeStudioLiveDraftState,
  mergeStudioWorkspaceLayers,
  readStudioUserDraft,
  removeStudioUserDraft,
  saveStudioUserDraft,
} from "../lib/studioSession";
import { reverseOperationResumeCandidates } from "../lib/reverseOperations";
import { STUDIO_DRAFT_PROMPT_KEY } from "../components/PromptLibraryBrowser";
import {
  CREATION_MODES,
  STUDIO_SESSION_DRAFT_KEY,
  STUDIO_VARIATION_DRAFT_KEY,
} from "../app/studio/constants";
import { MODEL_SELECTION_USES, readModelSelections } from "../app/studio/StudioModelSelector";
import {
  assetSignature,
  normalizeLegacyVideoGenerationPrompt,
  withEvidenceBackedVideoGenerationDraft,
} from "../app/studio/helpers";
import {
  promptDraftGenerationPatch,
  reverseSnapshotModelSelections,
} from "../app/studio/reverseResultRevisions";
import { workspacePatchFromReverseSnapshot } from "../app/studio/reverseSnapshot";
import { buildStudioSessionDraftFromState } from "../app/studio/studioDraftSession";
import { parsePromptDraft, promptDraftMode } from "../app/studio/promptDraftUtils";

async function loadOwnerDrafts(userId) {
  let localDraft = null;
  let promptDraft = null;
  let variationDraft = null;
  try {
    localDraft = readStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, userId);
    promptDraft = readStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, userId);
    variationDraft = readStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, userId);
  } catch (error) {
    reportBackgroundError(error, "restore scoped studio drafts");
  }

  let cloudDraft = null;
  try {
    const row = await api.getDraft("studio");
    cloudDraft = row?.payload?.workspaces ? row.payload : null;
  } catch (error) {
    reportBackgroundError(error, "load studio cloud draft");
  }
  return { localDraft, promptDraft, variationDraft, cloudDraft };
}

function normalizedRestoredMetadata(localDraft, cloudDraft) {
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
  return restoredMetadata;
}

export function normalizeRestoredVideoPrompt(workspaces) {
  const video = workspaces?.video;
  if (!video) return workspaces;
  let changed = false;
  const nextVideo = { ...video };
  if (video.promptDirty !== true) {
    const prompt = String(video.prompt || "");
    const rebuilt = withEvidenceBackedVideoGenerationDraft({
      final_text: prompt,
      structured: video.structured,
      video_analysis: video.reverseVideoAnalysis,
    });
    const normalized = normalizeLegacyVideoGenerationPrompt(rebuilt.final_text);
    if (normalized && normalized !== prompt) {
      nextVideo.prompt = normalized;
      changed = true;
    }
  }
  const pending = video.pendingReverseResult;
  const pendingResult = pending?.result;
  if (pending && pending.dirty !== true && pendingResult && typeof pendingResult === "object") {
    const finalText = String(pendingResult.final_text || "");
    const rebuiltResult = withEvidenceBackedVideoGenerationDraft(pendingResult);
    const normalized = normalizeLegacyVideoGenerationPrompt(rebuiltResult.final_text);
    if (normalized && (normalized !== finalText || rebuiltResult !== pendingResult)) {
      nextVideo.pendingReverseResult = {
        ...pending,
        result: {
          ...rebuiltResult,
          final_text: normalized,
        },
      };
      changed = true;
    }
  }
  if (!changed) return workspaces;
  return {
    ...workspaces,
    video: nextVideo,
  };
}

function buildPromptTransfer({
  promptDraft,
  baseline,
  currentState,
  nextState,
  effectiveModelSelections,
}) {
  if (!promptDraft) return null;
  const parsedDraft = parsePromptDraft(promptDraft);
  const snapshotRestore = workspacePatchFromReverseSnapshot(parsedDraft.reverseSnapshot);
  const snapshotSelections = reverseSnapshotModelSelections(parsedDraft.reverseSnapshot);
  const draftMode = snapshotRestore?.creationMode || promptDraftMode(parsedDraft);
  if (!canApplyStudioPromptTransfer(baseline, currentState, draftMode)) {
    return { applied: false };
  }

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
  return {
    applied: true,
    snapshotSelections,
    selections: snapshotSelections
      ? { ...effectiveModelSelections, ...snapshotSelections }
      : effectiveModelSelections,
    state: {
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
    },
  };
}

export async function initializeStudioOwnerSession({
  user,
  ownerSession,
  baseline,
  effectiveModelSelections,
  refs,
  actions,
}) {
  const { localDraft, promptDraft, variationDraft, cloudDraft } = await loadOwnerDrafts(user?.id);
  const {
    cloudDraftLoadedRef,
    reverseResumeOperationsRef,
    workspacesRef,
    studioUiStateRef,
    variationRestoreContextRef,
  } = refs;
  const {
    applyModelSelectionPreferences,
    applyVariationDraft,
    setWorkspaces,
    setCreationMode,
    setShowNegative,
    setRefOpen,
    setStructOpen,
    setMsg,
  } = actions;

  return ownerSession.commit(() => {
    cloudDraftLoadedRef.current = true;
    const persistedSelections = readModelSelections(window.localStorage, user?.id);
    const hasPersistedSelection = MODEL_SELECTION_USES.some((use) => persistedSelections?.[use]);
    const draftSelections = localDraft?.modelSelections || cloudDraft?.modelSelections;
    if (!hasPersistedSelection && draftSelections) {
      applyModelSelectionPreferences(draftSelections, user?.id);
    }
    const restoredWorkspaces = normalizeRestoredVideoPrompt(
      mergeStudioWorkspaceLayers(baseline.workspaces, {
        localDraft,
        cloudDraft,
      }),
    );
    reverseResumeOperationsRef.current = reverseOperationResumeCandidates(restoredWorkspaces);
    const currentState = studioUiStateRef.current || baseline;
    const restoredMetadata = normalizedRestoredMetadata(localDraft, cloudDraft);
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
      removeStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, user?.id);
    }

    const promptTransfer = buildPromptTransfer({
      promptDraft,
      baseline,
      currentState,
      nextState,
      effectiveModelSelections,
    });
    if (promptTransfer?.applied) {
      if (promptTransfer.snapshotSelections) {
        applyModelSelectionPreferences(promptTransfer.snapshotSelections, user?.id);
      }
      workspacesRef.current = promptTransfer.state.workspaces;
      studioUiStateRef.current = promptTransfer.state;
      setWorkspaces(promptTransfer.state.workspaces);
      setCreationMode(promptTransfer.state.creationMode);
      setStructOpen(promptTransfer.state.structOpen);
      setRefOpen(promptTransfer.state.refOpen);
      if (promptTransfer.state.restoreNotice) setMsg(promptTransfer.state.restoreNotice);
    }

    let variationTransferApplied = false;
    if (!promptDraft && variationDraft) {
      variationRestoreContextRef.current = { baseline, current: currentState };
      try {
        if (applyVariationDraft(variationDraft)) {
          removeStudioUserDraft(window.localStorage, STUDIO_VARIATION_DRAFT_KEY, user?.id);
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
      promptTransferApplied: Boolean(promptTransfer?.applied),
      variationDraft,
      variationTransferApplied,
    });

    if (!promptDraft) return;
    if (promptTransfer?.applied) {
      const savedAt = ownerSession.next();
      const checkpoint = buildStudioSessionDraftFromState(
        "prompt_transfer_applied",
        promptTransfer.state,
        savedAt,
        promptTransfer.selections,
      );
      const checkpointSaved = saveStudioUserDraft(
        window.localStorage,
        STUDIO_SESSION_DRAFT_KEY,
        user?.id,
        checkpoint,
      );
      if (checkpointSaved) {
        removeStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, user?.id);
      }
      return;
    }
    removeStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, user?.id);
    setMsg("当前工作区已发生编辑，为避免覆盖，未应用待恢复的提示词或创作配方。");
  });
}
