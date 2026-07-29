"use client";

import { useRef, useState } from "react";
import { api } from "../lib/api";
import { errorMessage } from "../lib/errorHandling";
import {
  clearPendingStudioActionRequest,
  pendingStudioActionRequestId,
} from "../app/studio/generationQuote";
import { resolveModelConfigId } from "../app/studio/StudioModelSelector";
import {
  applyPromptOptimizationDecision,
  isPromptOptimizationResultCurrent,
  promptOptimizationContextKey,
  undoPromptOptimization,
} from "../app/studio/promptOptimization";

const DEFAULT_SETTING = {
  direction: "faithful",
  targetLanguage: "en",
};

export default function usePromptOptimization({
  creationMode,
  category,
  subjectMode,
  productGenerationMode,
  prompt,
  vDuration,
  ratio,
  vResolution,
  effectiveProductVideoTemplate,
  referenceSignature,
  activeSubjectProfileSource,
  targetModelId,
  targetModelProvider,
  selectedGenerationModelConfigId,
  selectedPromptModelConfigId,
  selectedPromptModel,
  reverseAppliedRevisionId,
  reverseOperationForPendingResult,
  me,
  requestQuoteConfirmation,
  workspace,
  workspacesRef,
  setWorkspacePatch,
  setMsg,
  notify,
  setPrompt,
  setEditSubjectMode,
  studioActionPendingRequestRef,
  generationModelOptions,
  modelSelectionsRef,
  changeModelSelection,
}) {
  const [optimizingPromptMode, setOptimizingPromptMode] = useState("");
  const [proposals, setProposals] = useState({});
  const [settings, setSettings] = useState({});
  const [undos, setUndos] = useState({});
  const requestRef = useRef({});
  const contextRef = useRef({});
  const recordsRef = useRef({});

  const setting = settings[creationMode] || DEFAULT_SETTING;
  const proposal = proposals[creationMode] || null;
  const optimizing = optimizingPromptMode === creationMode;
  const ready = Boolean(String(prompt || "").trim());
  const context = {
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
    optimizationDirection: setting.direction,
    optimizationTargetLanguage: setting.targetLanguage,
    targetModelConfigId: selectedGenerationModelConfigId,
    optimizerModelConfigId: selectedPromptModelConfigId,
  };
  const currentContextKey = promptOptimizationContextKey({
    ...context,
    promptText: prompt,
  });
  contextRef.current[creationMode] = currentContextKey;
  const scopeKey = promptOptimizationContextKey(context);
  const record = recordsRef.current[creationMode];
  const promptForGeneration = record?.scope_key === scopeKey
    ? {
        text: prompt,
        raw_text: record.raw_text,
        optimized_text: record.optimized_text,
        optimizer_model_id: record.optimizer_model_id,
        optimizer_model_config_id: record.optimizer_model_config_id,
        optimization_direction: record.optimization_direction,
        optimization_kind: record.optimization_kind,
        compiler_metadata: record.compiler_metadata,
        change_summary: record.change_summary,
        warnings: record.warnings,
      }
    : prompt;

  function invalidate(mode = creationMode) {
    requestRef.current[mode] = (requestRef.current[mode] || 0) + 1;
    setOptimizingPromptMode((current) => (current === mode ? "" : current));
    setProposals((current) => {
      if (!current[mode]) return current;
      const next = { ...current };
      delete next[mode];
      return next;
    });
  }

  function requestOptions({
    direction = setting.direction,
    duration = category === "video" ? Number(vDuration) : undefined,
    promptText = String(prompt || "").trim(),
  } = {}) {
    return {
      prompt: String(promptText || "").trim(),
      category,
      product_mode: productGenerationMode,
      duration: category === "video"
        ? Math.max(1, Math.round(Number(duration) || Number(vDuration) || 1))
        : undefined,
      aspect_ratio: category === "video" ? ratio : undefined,
      resolution: category === "video" ? vResolution : undefined,
      mode: direction,
      target_language: setting.targetLanguage,
      optimizer_model_config_id: selectedPromptModelConfigId || undefined,
      target_model_config_id: selectedGenerationModelConfigId,
    };
  }

  async function optimize() {
    const source = String(prompt || "").trim();
    if (!source || optimizing) return;
    const mode = creationMode;
    const requestId = (requestRef.current[mode] || 0) + 1;
    requestRef.current[mode] = requestId;
    const request = { id: requestId, contextKey: currentContextKey };
    setOptimizingPromptMode(mode);
    try {
      const optimizationRequest = requestOptions({ promptText: source });
      const compileOnly = ["model_adaptation", "target_model_adaptation"]
        .includes(String(optimizationRequest.mode || ""));
      const requiresQuote = !compileOnly && (
        selectedPromptModel == null || Number(selectedPromptModel.cost_credits || 0) > 0
      );
      let result;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const actionRequestId = pendingStudioActionRequestId(
          studioActionPendingRequestRef,
          `prompt-optimization:${me?.id || "unknown"}:${mode}`,
          optimizationRequest,
        );
        const executableRequest = {
          ...optimizationRequest,
          idempotency_key: actionRequestId,
        };
        try {
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
              clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
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
          break;
        } catch (error) {
          const failedAndRefunded = Number(error?.status) === 409
            && errorMessage(error, "").includes("已失败并退款");
          if (failedAndRefunded || Number.isFinite(Number(error?.status))) {
            clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
          }
          if (failedAndRefunded && attempt === 0) continue;
          throw error;
        }
      }
      if (!isPromptOptimizationResultCurrent(
        request,
        requestRef.current[mode],
        contextRef.current[mode],
      )) return;
      const optimized = String(result?.suggestion?.final_text || "").trim();
      if (!optimized) throw new Error("优化模型未返回有效提示词");
      setProposals((current) => ({
        ...current,
        [mode]: {
          ...result,
          raw_text: String(result?.original?.final_text || source),
          optimized_text: optimized,
          optimizer_model_id: result.provenance?.optimizer_model_id || "",
          optimizer_model_config_id: result.provenance?.optimizer_model_config_id || selectedPromptModelConfigId || null,
          scope_key: scopeKey,
          model_name: selectedPromptModel?.display_name || "提示词模型",
          direction: result.mode || setting.direction,
          target_language: setting.targetLanguage || null,
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
    } catch (error) {
      if (!isPromptOptimizationResultCurrent(
        request,
        requestRef.current[mode],
        contextRef.current[mode],
      )) return;
      const text = errorMessage(error, "提示词优化失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      if (requestId === requestRef.current[mode]) {
        setOptimizingPromptMode((current) => (current === mode ? "" : current));
      }
    }
  }

  async function accept(acceptedSegmentIds = []) {
    const mode = creationMode;
    const currentProposal = proposals[mode];
    if (!currentProposal?.optimized_text) return;
    const allSegmentIds = (currentProposal.segments || []).map((item) => item.id);
    const acceptedIds = acceptedSegmentIds.length ? acceptedSegmentIds : allSegmentIds;
    const rejectedIds = allSegmentIds.filter((id) => !acceptedIds.includes(id));
    let decision;
    try {
      decision = await api.acceptStudioPromptOptimization(currentProposal.proposal_id, {
        proposal_version: currentProposal.proposal_version,
        idempotency_key: crypto.randomUUID(),
        accepted_segment_ids: acceptedIds,
        rejected_segment_ids: rejectedIds,
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
      currentProposal,
      decision?.result,
      decision?.accepted_segment_ids || acceptedIds,
    );
    if (applied.stale) {
      setMsg("当前工作区已变更，服务端已记录决策，但未覆盖本地内容。");
      notify.warn("工作区已变更，未自动覆盖");
      setProposals((current) => {
        const next = { ...current };
        delete next[mode];
        return next;
      });
      return;
    }
    const promptChanged = applied.changedFields.includes("prompt");
    const negativeChanged = applied.changedFields.includes("negative");
    const structuredChanged = applied.changedFields.includes("structured");
    recordsRef.current[mode] = {
      raw_text: currentProposal.raw_text,
      optimized_text: applied.workspace.prompt,
      optimizer_model_id: currentProposal.optimizer_model_id,
      optimizer_model_config_id: currentProposal.optimizer_model_config_id,
      optimization_direction: currentProposal.direction,
      optimization_kind: currentProposal.optimization_kind,
      compiler_metadata: decision?.result?.compiler_metadata || currentProposal.compiler_metadata,
      change_summary: currentProposal.change_summary,
      warnings: (currentProposal.warnings || []).map((item) => item.message || String(item)),
      proposal_id: currentProposal.proposal_id,
      scope_key: promptOptimizationContextKey({
        ...context,
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
    if (applied.undo) setUndos((current) => ({ ...current, [mode]: applied.undo }));
    setProposals((current) => {
      const next = { ...current };
      delete next[mode];
      return next;
    });
    setMsg(`已接受优化建议 · ${currentProposal.model_name}`);
  }

  async function reject() {
    const currentProposal = proposals[creationMode];
    if (!currentProposal?.proposal_id) return;
    try {
      await api.rejectStudioPromptOptimization(currentProposal.proposal_id, {
        proposal_version: currentProposal.proposal_version,
        idempotency_key: crypto.randomUUID(),
      });
    } catch (error) {
      const text = errorMessage(error, "拒绝优化建议失败");
      setMsg(text);
      notify.error(text);
      return;
    }
    setProposals((current) => {
      if (!current[creationMode]) return current;
      const next = { ...current };
      delete next[creationMode];
      return next;
    });
    setMsg("已保留原提示词。");
  }

  function undoAccepted() {
    const mode = creationMode;
    const undo = undos[mode];
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
    setUndos((current) => {
      const next = { ...current };
      delete next[mode];
      return next;
    });
    delete recordsRef.current[mode];
    setMsg("已撤销上一次提示词优化应用。");
  }

  function changeSetting(patch) {
    invalidate();
    setSettings((current) => ({
      ...current,
      [creationMode]: { ...setting, ...patch },
    }));
  }

  function updatePromptFromUser(valueOrUpdater) {
    invalidate();
    setPrompt(valueOrUpdater);
  }

  function changeEditSubjectMode(value) {
    invalidate();
    delete recordsRef.current[creationMode];
    setEditSubjectMode(value);
  }

  async function changeGenerationModelSelection(modelConfigId) {
    const nextModelId = resolveModelConfigId(generationModelOptions, modelConfigId);
    changeModelSelection(category, nextModelId);
    const operation = reverseOperationForPendingResult();
    if (!operation?.id || !reverseAppliedRevisionId || !String(prompt || "").trim() || !nextModelId) return;
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
      setSettings((current) => ({
        ...current,
        [mode]: { ...setting, direction: "target_model_adaptation" },
      }));
      setProposals((current) => ({
        ...current,
        [mode]: {
          ...result,
          raw_text: String(result?.original?.final_text || promptSnapshot),
          optimized_text: String(result?.suggestion?.final_text || promptSnapshot),
          optimizer_model_id: "",
          optimizer_model_config_id: null,
          scope_key: scopeKey,
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

  function saveRecord(mode, nextRecord) {
    if (nextRecord) recordsRef.current[mode] = nextRecord;
    else delete recordsRef.current[mode];
  }

  function clearMode(mode) {
    invalidate(mode);
    delete contextRef.current[mode];
    delete recordsRef.current[mode];
  }

  function resetAll(modes) {
    for (const mode of modes) invalidate(mode);
    contextRef.current = {};
    recordsRef.current = {};
  }

  return {
    proposal,
    setting,
    optimizing,
    ready,
    undoAvailable: Boolean(undos[creationMode]),
    context,
    scopeKey,
    promptForGeneration,
    invalidate,
    optimize,
    accept,
    reject,
    undoAccepted,
    changeSetting,
    updatePromptFromUser,
    changeEditSubjectMode,
    changeGenerationModelSelection,
    saveRecord,
    clearMode,
    resetAll,
  };
}
