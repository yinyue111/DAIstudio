"use client";

import { api } from "../lib/api";
import { errorMessage, reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseOperationFeedback,
  normalizeReverseResultRevision,
  normalizeReverseResultRevisionList,
} from "../lib/reverseOperations";
import { assetSignature, withEvidenceBackedVideoGenerationDraft } from "../app/studio/helpers";
import {
  applyReverseResultApplication,
  mergeConfirmedReverseResultApplication,
  normalizePendingReverseResult,
  resolveReverseResultApplicationSelection,
  selectedImageEvidenceForApplication,
  undoReverseResultApplication,
} from "../app/studio/reverseResultApplication";
import {
  clearsReviewedImageEvidence,
  findReverseApplyParentRevision,
  mergeReverseResultRevisions,
  reverseResultEnvelope,
  reverseResultPayload,
} from "../app/studio/reverseResultRevisions";

export default function useReverseResultActions({
  creationMode,
  category,
  selected,
  workspace,
  pendingReverseResult,
  reverseUndoSnapshot,
  workspacesRef,
  reverseOperationForPendingResult,
  setWorkspacePatch,
  setReverseActionBusy,
  setMsg,
  notify,
}) {
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
    const restoredPayload = withEvidenceBackedVideoGenerationDraft(
      revision.payload,
      category,
      { preserveStored: revision.source === "user_edit" },
    );
    setWorkspacePatch({
      pendingReverseResult: reverseResultEnvelope(
        operation || {
          id: revision.operation_id,
          target: category,
        },
        restoredPayload,
        revision.source === "user_edit",
      ),
      reverseResultTab: category === "video" ? "report" : "draft",
    });
    setMsg(`已恢复版本 ${revision.version} 到待应用区，当前工作区尚未改变。`);
  }

  return {
    applyPendingReverseResult,
    undoAppliedReverseResult,
    savePendingReverseVersion,
    submitReverseFeedback,
    restoreReverseRevision,
  };
}
