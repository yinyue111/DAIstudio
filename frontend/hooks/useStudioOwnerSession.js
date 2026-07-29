import { useEffect } from "react";

import { api, setUnauthorizedHandler } from "../lib/api";
import { reportBackgroundError, showError } from "../lib/errorHandling";
import {
  createLatestOnlyDraftWriter,
  createLatestOnlyStudioOwnerRequest,
  createStudioDraftClock,
  createStudioOwnerSessionCoordinator,
  mergeStudioWorkspaceLayers,
  saveStudioUserDraft,
} from "../lib/studioSession";
import {
  qualityKeyForSize,
  ratioKeyForSize,
} from "../app/studio/helpers";
import {
  STUDIO_SESSION_DRAFT_KEY,
} from "../app/studio/constants";
import {
  buildStudioSessionDraftFromState,
  compactStudioSessionDraftForCloud,
} from "../app/studio/studioDraftSession";
import { initializeStudioOwnerSession } from "./studioOwnerRestore";
const ACTIVE_REVERSE_STATUSES = new Set(["queued", "running", "needs_confirmation"]);

function hasActiveReverseOperations(workspaces) {
  return Object.values(workspaces || {}).some((current) => (
    ACTIVE_REVERSE_STATUSES.has(current?.reverseOperation?.status)
    || ACTIVE_REVERSE_STATUSES.has(current?.profileOperation?.status)
  ));
}

function studioAdminImagePatch(config) {
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

export default function useStudioOwnerSession({
  me,
  cfg,
  workspaces,
  creationMode,
  showNegative,
  refOpen,
  structOpen,
  effectiveModelSelections,
  refs,
  setWorkspaces,
  setCreationMode,
  setShowNegative,
  setRefOpen,
  setStructOpen,
  setWorks,
  setWorksError,
  setLightbox,
  setMsg,
  resetOwnerResources,
  applyModelSelectionPreferences,
  applyVariationDraft,
  loadWorks,
}) {
  const {
    cloudDraftLoadedRef,
    studioInitSeqRef,
    studioDraftClockRef,
    studioOwnerSessionCoordinatorRef,
    studioOwnerSessionRef,
    latestMeRequestRef,
    studioOwnerUserIdRef,
    initialStudioUiStateRef,
    cloudDraftWriterRef,
    studioUiStateRef,
    variationRestoreContextRef,
    reverseResumeOperationsRef,
    localReverseDraftRef,
    loadWorksSeqRef,
    taskRef,
    workspacesRef,
  } = refs;

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
      (draft) => api.saveDraft("studio", compactStudioSessionDraftForCloud(draft)),
    );
  }
  studioUiStateRef.current = {
    workspaces,
    creationMode,
    showNegative,
    refOpen,
    structOpen,
  };

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
    loadWorksSeqRef.current += 1;
    resetOwnerResources();
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
    setMsg("");
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
    if (typeof window === "undefined") return undefined;
    try {
      const draft = buildStudioSessionDraft(reason);
      saveStudioUserDraft(window.localStorage, STUDIO_SESSION_DRAFT_KEY, me?.id, draft);
      if (persistCloud && me?.id) {
        const queued = cloudDraftWriterRef.current(draft, me?.id);
        queued.catch((error) => reportBackgroundError(error, "save studio cloud draft"));
        return queued;
      }
    } catch (error) {
      reportBackgroundError(error, "save studio session draft");
    }
    return undefined;
  }

  function syncStudioDraftToCloud(reason = "manual", ownerSession = studioOwnerSessionRef.current) {
    if (!ownerSession?.isCurrent()) return Promise.resolve();
    return cloudDraftWriterRef.current(
      buildStudioSessionDraft(reason),
      ownerSession.ownerUserId,
    );
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
    initializeStudioOwnerSession({
      user: me,
      ownerSession,
      baseline,
      effectiveModelSelections,
      refs: {
        cloudDraftLoadedRef,
        reverseResumeOperationsRef,
        workspacesRef,
        studioUiStateRef,
        variationRestoreContextRef,
      },
      actions: {
        applyModelSelectionPreferences,
        applyVariationDraft,
        setWorkspaces,
        setCreationMode,
        setShowNegative,
        setRefOpen,
        setStructOpen,
        setMsg,
      },
    })
      .catch((error) => showError(setMsg, error, "初始化创作工作台失败"));
    loadWorks({ restoreActive: true });
    return () => cloudDraftWriterRef.current.cancelOwner(ownerUserId);
  }, [me?.id]);

  useEffect(() => {
    setUnauthorizedHandler(() => saveStudioSessionDraft("auth_expired", { persistCloud: false }));
    return () => setUnauthorizedHandler(null);
  }, [creationMode, showNegative, refOpen, structOpen, me?.id]);

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

  useEffect(() => {
    if (!me?.id || !cloudDraftLoadedRef.current) return undefined;
    const ownerSession = studioOwnerSessionRef.current;
    if (!ownerSession?.isCurrent() || ownerSession.ownerUserId !== String(me.id)) return undefined;
    studioOwnerSessionCoordinatorRef.current.schedule(ownerSession, () => {
      syncStudioDraftToCloud("auto", ownerSession)
        .catch((error) => reportBackgroundError(error, "sync studio cloud draft"));
    }, 1600);
    return () => studioOwnerSessionCoordinatorRef.current.cancelScheduled();
  }, [me?.id, workspaces, creationMode, showNegative, refOpen, structOpen]);

  return {
    saveStudioSessionDraft,
  };
}
