"use client";

import { api } from "../lib/api";
import { errorMessage } from "../lib/errorHandling";
import {
  normalizeReverseOperation,
  normalizeReverseOperationFeedback,
  normalizeReverseResultRevisionList,
  reverseOperationResult,
} from "../lib/reverseOperations";
import { findGenerationSourceRevision } from "../app/studio/promptDraftUtils";
import { withEvidenceBackedVideoGenerationDraft } from "../app/studio/helpers";
import { reverseResultEnvelope } from "../app/studio/reverseResultRevisions";
import { workspacePatchFromReverseSnapshot } from "../app/studio/reverseSnapshot";

export function buildReverseHistoryWorkspacePatch({
  operation,
  result,
  restored,
  revisions,
  feedback,
  appliedRevision,
}) {
  const normalizedResult = withEvidenceBackedVideoGenerationDraft(result, operation.target);
  return {
    ...(restored?.workspace || {}),
    editSubjectMode: restored?.subjectMode || "general",
    imageEditProductMode: ["product", "portrait"].includes(restored?.subjectMode),
    reversing: false,
    reverseOperation: operation,
    pendingReverseResult: reverseResultEnvelope(operation, normalizedResult),
    reverseResultTab: operation.target === "video" ? "report" : "draft",
    reverseResultSchemaVersion: operation.result_schema_version || "",
    reverseAppliedVersion: operation.applied_result_version || null,
    reverseAppliedRevisionId: appliedRevision?.id || null,
    reverseResultRevisions: revisions,
    reverseFeedback: feedback,
  };
}

export default function useReverseHistoryActions({
  reverseOperationForPendingResult,
  selectedVisionModelConfigId,
  requestQuoteConfirmation,
  setRecentReverseBusyId,
  setReverseActionBusy,
  setCreationMode,
  setWorkspacePatch,
  setStructOpen,
  setRefOpen,
  setMsg,
  notify,
  upsertRecentReverseOperation,
  trackReverseOperation,
}) {
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
      setWorkspacePatch(buildReverseHistoryWorkspacePatch({
        operation,
        result,
        restored,
        revisions,
        appliedRevision,
        feedback: feedbackPayload ? normalizeReverseOperationFeedback(feedbackPayload) : null,
      }), mode);
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

  return { openRecentReverseOperation, retryReverse };
}
