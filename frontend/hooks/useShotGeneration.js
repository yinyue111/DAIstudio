"use client";

import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseResultRevision,
  normalizeReverseResultRevisionList,
} from "../lib/reverseOperations";
import { findGenerationSourceRevision } from "../app/studio/promptDraftUtils";
import {
  bindGenerationTaskToPendingReverseResult,
  mergeReverseResultRevisions,
  positiveRevisionId,
  replaceReverseResultPayload,
  reverseResultPayload,
} from "../app/studio/reverseResultRevisions";
import { storyboardResultShots } from "../app/studio/storyboard";
import {
  normalizeVideoComposition,
  videoCompositionShotTracksGenerationTask,
} from "../app/studio/videoComposition";

export default function useShotGeneration({
  creationMode,
  workspaces,
  workspacesRef,
  setWorkspacePatch,
  trackBackgroundTask,
  getOwnerSession,
  setMsg,
}) {
  const revisionQueueRef = useRef(new Map());

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
    const previous = revisionQueueRef.current.get(key) || Promise.resolve();
    const run = previous
      .catch(() => undefined)
      .then(() => persistShotGenerationBinding(options));
    revisionQueueRef.current.set(key, run);
    run.finally(() => {
      if (revisionQueueRef.current.get(key) === run) {
        revisionQueueRef.current.delete(key);
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
    const ownerSession = getOwnerSession();
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

  useEffect(() => {
    const ownerSession = getOwnerSession();
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

  return { handleGenerationSubmitted };
}
