"use client";

import { api } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseResultRevision,
  normalizeReverseResultRevisionList,
} from "../lib/reverseOperations";
import {
  mergeReverseResultRevisions,
  positiveRevisionId,
} from "../app/studio/reverseResultRevisions";

export default function useReproductionActions({
  setWorkspacePatch,
  setMsg,
  trackBackgroundTask,
  getOwnerSession,
  refreshMe,
  loadWorks,
}) {
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
    const ownerSession = getOwnerSession();
    trackBackgroundTask(submittedTask, {
      onTerminal: async () => {
        if (!ownerSession?.isCurrent?.()) return;
        await Promise.allSettled([refreshMe(), loadWorks()]);
      },
    });
    setMsg(`纠偏生成任务 #${submittedTask.id} 已提交，可在全局任务中心继续跟踪。`);
  }

  return {
    handleReproductionCorrectionCreated,
    handleReproductionRemediationCreated,
    handleReproductionRemediationExecutionSubmitted,
  };
}
