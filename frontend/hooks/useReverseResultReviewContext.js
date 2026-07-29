import { useEffect } from "react";

import { api } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseOperationFeedback,
  normalizeReverseResultRevisionList,
} from "../lib/reverseOperations";
import { findGenerationSourceRevision } from "../app/studio/promptDraftUtils";
import {
  positiveRevisionId,
  reverseResultPayload,
} from "../app/studio/reverseResultRevisions";
import { storyboardResultShots } from "../app/studio/storyboard";
import { normalizeVideoComposition } from "../app/studio/videoComposition";

export default function useReverseResultReviewContext({
  task,
  creationMode,
  pendingReverseResult,
  workspaceReverseOperation,
  reverseOperation,
  reverseResultRevisions,
  reverseAppliedRevisionId,
  reverseAppliedVersion,
  setWorkspacePatch,
}) {
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
  const generationReverseOperationId = generationReverseRevisionId
    && pendingReverseOperationId > 0
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
    if (!Number.isInteger(pendingReverseOperationId) || pendingReverseOperationId <= 0) {
      return undefined;
    }
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
          if (!revisions.some((revision) => revision.id === localRevision.id)) {
            revisions.push(localRevision);
          }
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
          reverseFeedback: feedbackPayload
            ? normalizeReverseOperationFeedback(feedbackPayload)
            : null,
        };
      });
    }).catch((error) => reportBackgroundError(error, "load reverse result review context"));
    return () => { active = false; };
  }, [creationMode, pendingReverseOperationId]);

  return {
    pendingReverseOperationId,
    generationSourceRevision,
    generationReverseRevisionId,
    generationReverseOperationId,
    reproductionVideoComposition,
  };
}
