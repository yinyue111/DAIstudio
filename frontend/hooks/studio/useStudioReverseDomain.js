"use client";

import { useCallback, useEffect, useRef } from "react";
import { reportBackgroundError } from "../../lib/errorHandling";
import useRecentReverseOperations from "../useRecentReverseOperations";
import useReferenceParsing from "../useReferenceParsing";
import useReverseBatchActions from "../useReverseBatchActions";
import useReverseBatches from "../useReverseBatches";
import useReverseHistoryActions from "../useReverseHistoryActions";
import useReverseResultActions from "../useReverseResultActions";
import useReverseResultReviewContext from "../useReverseResultReviewContext";
import useStoryboardActions from "../useStoryboardActions";
import useStudioRecipeActions from "../useStudioRecipeActions";

export function resolvePendingReverseOperation({
  pendingReverseResult,
  trackedOperation,
  workspaceOperation,
}) {
  const operationId = Number(
    pendingReverseResult?.operation_id
      || pendingReverseResult?.operationId
      || 0,
  );
  const operations = [trackedOperation, workspaceOperation];
  if (operationId) {
    return operations.find((item) => Number(item?.id) === operationId) || null;
  }
  return workspaceOperation || trackedOperation || null;
}

export function useStudioReverseOperationBridge(foundation) {
  const stateRef = useRef({ foundation, trackedOperation: null });
  stateRef.current.foundation = foundation;

  const bindTrackedOperation = useCallback((trackedOperation) => {
    stateRef.current.trackedOperation = trackedOperation || null;
  }, []);
  const reverseOperationForPendingResult = useCallback(() => {
    const { foundation: current, trackedOperation } = stateRef.current;
    return resolvePendingReverseOperation({
      pendingReverseResult: current.pendingReverseResult,
      trackedOperation,
      workspaceOperation: current.workspaceReverseOperation,
    });
  }, []);

  return { bindTrackedOperation, reverseOperationForPendingResult };
}

function useReverseCollections(foundation) {
  const recent = useRecentReverseOperations({
    enabled: Boolean(foundation.me?.id),
    limit: 5,
  });
  const batches = useReverseBatches({
    ownerKey: String(foundation.me?.id || ""),
    enabled: foundation.reverseBatchEnabled,
    onError: (error) => reportBackgroundError(error, "reverse batch tracking"),
  });
  return { recent, batches };
}

function useReverseReference(foundation, model) {
  return useReferenceParsing({
    creationMode: foundation.creationMode,
    category: foundation.category,
    url: foundation.url,
    parsing: foundation.parsing,
    selected: foundation.selected,
    assets: foundation.assets,
    productAsset: foundation.productAsset,
    productProfile: foundation.activeSubjectProfile,
    structured: foundation.structured,
    reverseVideoAnalysis: foundation.reverseVideoAnalysis,
    workspaceReverseOperation: foundation.workspaceReverseOperation,
    prompt: foundation.prompt,
    negative: foundation.negative,
    negativeTouched: foundation.negativeTouched,
    videoAnalysisPreset: foundation.videoAnalysisPreset,
    reverseConfig: foundation.reverseConfig,
    reverseSources: foundation.reverseSources,
    modelConfigId: model.selectedVisionModelConfigId,
    modelSelections: model.effectiveModelSelections,
    ratio: foundation.ratio,
    vDuration: foundation.vDuration,
    videoDurationMaxSeconds: foundation.cfg?.video_duration_max_seconds,
    isEditMode: foundation.isEditMode,
    subjectMode: foundation.subjectMode,
    setMsg: foundation.setMsg,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setCreationMode: foundation.setCreationMode,
    setRatio: foundation.setRatio,
    setStructOpen: foundation.setStructOpen,
    setShowNegative: foundation.setShowNegative,
    setRefOpen: foundation.setRefOpen,
    refreshMe: foundation.refreshMe,
    revokeUploadedObjectUrlsRef: foundation.refs.revokeUploadedObjectUrlsRef,
    getOwnerSession: foundation.getOwnerSession,
    ownerKey: foundation.me?.id ? String(foundation.me.id) : "",
    resumeOperations: foundation.reverseResumeOperations,
    requestQuoteConfirmation: model.requestQuoteConfirmation,
  });
}

function useReverseEditing(foundation, model, prompt, reference, resolveOperation) {
  const recipes = useStudioRecipeActions({
    creationMode: foundation.creationMode,
    category: foundation.category,
    subjectMode: foundation.subjectMode,
    workspace: foundation.workspace,
    activeSubjectProfile: foundation.activeSubjectProfile,
    effectiveModelSelections: model.effectiveModelSelections,
    selectedGenerationModelConfigId: model.selectedGenerationModelConfigId,
    selectedVisionModelConfigId: model.selectedVisionModelConfigId,
    selectedPromptModelConfigId: model.selectedPromptModelConfigId,
    effectiveProductVideoTemplate: model.effectiveProductVideoTemplate,
    reverseOperationForPendingResult: resolveOperation,
    lastReversePromptRef: reference.lastReversePromptRef,
    promptSaveTitle: foundation.promptSaveTitle,
    promptSaveCategory: foundation.promptSaveCategory,
    promptSaveFavorite: foundation.promptSaveFavorite,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setReverseActionBusy: foundation.setReverseActionBusy,
    setMsg: foundation.setMsg,
    notify: foundation.notify,
  });
  const storyboard = useStoryboardActions({
    creationMode: foundation.creationMode,
    productGenerationMode: foundation.productGenerationMode,
    ratio: foundation.ratio,
    vResolution: foundation.vResolution,
    selectedGenerationModelConfigId: model.selectedGenerationModelConfigId,
    selectedGenerationModel: model.selectedGenerationModel,
    targetModelId: model.targetModelId,
    ownerId: foundation.me?.id,
    pendingReverseResult: foundation.pendingReverseResult,
    workspace: foundation.workspace,
    workspacesRef: foundation.refs.workspacesRef,
    reverseOperationForPendingResult: resolveOperation,
    studioActionPendingRequestRef: foundation.refs.studioActionPendingRequestRef,
    promptOptimizationContext: prompt.context,
    savePromptOptimizationRecord: prompt.saveRecord,
    setReverseActionBusy: foundation.setReverseActionBusy,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setMsg: foundation.setMsg,
    notify: foundation.notify,
  });
  const results = useReverseResultActions({
    creationMode: foundation.creationMode,
    category: foundation.category,
    selected: foundation.selected,
    workspace: foundation.workspace,
    pendingReverseResult: foundation.pendingReverseResult,
    reverseUndoSnapshot: foundation.reverseUndoSnapshot,
    workspacesRef: foundation.refs.workspacesRef,
    reverseOperationForPendingResult: resolveOperation,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setReverseActionBusy: foundation.setReverseActionBusy,
    setMsg: foundation.setMsg,
    notify: foundation.notify,
  });
  return { recipes, storyboard, results };
}

function useReverseUserActions(
  foundation,
  model,
  collections,
  reference,
  editing,
  resolveOperation,
) {
  const history = useReverseHistoryActions({
    reverseOperationForPendingResult: resolveOperation,
    selectedVisionModelConfigId: model.selectedVisionModelConfigId,
    requestQuoteConfirmation: model.requestQuoteConfirmation,
    setRecentReverseBusyId: foundation.setRecentReverseBusyId,
    setReverseActionBusy: foundation.setReverseActionBusy,
    setCreationMode: foundation.setCreationMode,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setStructOpen: foundation.setStructOpen,
    setRefOpen: foundation.setRefOpen,
    setMsg: foundation.setMsg,
    notify: foundation.notify,
    upsertRecentReverseOperation: collections.recent.upsert,
    trackReverseOperation: reference.trackReverseOperation,
  });
  const batchActions = useReverseBatchActions({
    category: foundation.category,
    creationMode: foundation.creationMode,
    reverseConfig: foundation.reverseConfig,
    batchReverseAssets: foundation.batchReverseAssets,
    effectiveModelSelections: model.effectiveModelSelections,
    selectedVisionModelConfigId: model.selectedVisionModelConfigId,
    requestQuoteConfirmation: model.requestQuoteConfirmation,
    createReverseBatch: collections.batches.createBatch,
    recipePayload: editing.recipes.recipePayload,
    refreshMe: foundation.refreshMe,
    setWorkspacePatch: foundation.setWorkspacePatch,
    setReverseActionBusy: foundation.setReverseActionBusy,
    setMsg: foundation.setMsg,
    notify: foundation.notify,
  });
  return { history, batchActions };
}

function useReverseReview(foundation, task, reference) {
  return useReverseResultReviewContext({
    task: task.task,
    creationMode: foundation.creationMode,
    pendingReverseResult: foundation.pendingReverseResult,
    workspaceReverseOperation: foundation.workspaceReverseOperation,
    reverseOperation: reference.reverseOperation,
    reverseResultRevisions: foundation.reverseResultRevisions,
    reverseAppliedRevisionId: foundation.reverseAppliedRevisionId,
    reverseAppliedVersion: foundation.reverseAppliedVersion,
    setWorkspacePatch: foundation.setWorkspacePatch,
  });
}

export default function useStudioReverseDomain(
  foundation,
  model,
  prompt,
  task,
  operationBridge,
) {
  const collections = useReverseCollections(foundation);
  const reference = useReverseReference(foundation, model);
  operationBridge.bindTrackedOperation(reference.reverseOperation);
  const resolveOperation = operationBridge.reverseOperationForPendingResult;
  const editing = useReverseEditing(foundation, model, prompt, reference, resolveOperation);
  const userActions = useReverseUserActions(
    foundation,
    model,
    collections,
    reference,
    editing,
    resolveOperation,
  );
  const review = useReverseReview(foundation, task, reference);

  useEffect(() => {
    const current = reference.reverseOperation || foundation.workspaceReverseOperation;
    if (current?.id) collections.recent.upsert(current);
  }, [
    reference.reverseOperation,
    foundation.workspaceReverseOperation,
    collections.recent.upsert,
  ]);

  return {
    ...reference,
    ...editing.recipes,
    ...editing.storyboard,
    ...editing.results,
    ...userActions.history,
    ...userActions.batchActions,
    ...review,
    reverseOperationForPendingResult: resolveOperation,
    recentReverseOperations: collections.recent.operations,
    recentReverseLoading: collections.recent.loading,
    recentReverseError: collections.recent.error,
    refreshRecentReverseOperations: collections.recent.refresh,
    upsertRecentReverseOperation: collections.recent.upsert,
    reverseBatch: collections.batches.batch,
    recentReverseBatches: collections.batches.recentBatches,
    reverseBatchLoading: collections.batches.loading,
    reverseBatchBusyAction: collections.batches.busyAction,
    createReverseBatch: collections.batches.createBatch,
    cancelReverseBatch: collections.batches.cancelBatch,
    openReverseBatch: collections.batches.openBatch,
    refreshReverseBatch: collections.batches.refreshBatch,
    refreshRecentReverseBatches: collections.batches.refreshRecent,
  };
}
