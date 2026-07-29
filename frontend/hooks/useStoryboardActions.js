"use client";

import { api } from "../lib/api";
import { errorMessage, reportBackgroundError } from "../lib/errorHandling";
import {
  normalizeReverseResultRevision,
  normalizeReverseResultRevisionList,
} from "../lib/reverseOperations";
import {
  clearPendingStudioActionRequest,
  pendingStudioActionRequestId,
} from "../app/studio/generationQuote";
import { promptOptimizationContextKey } from "../app/studio/promptOptimization";
import {
  composeStoryboardShotPrompt,
  storyboardResultShots,
  storyboardResultWithShots,
  storyboardShotIdentity,
} from "../app/studio/storyboard";
import {
  clearsReviewedImageEvidence,
  findReverseApplyParentRevision,
  mergeReverseResultRevisions,
  replaceReverseResultPayload,
  reverseResultPayload,
} from "../app/studio/reverseResultRevisions";

export default function useStoryboardActions({
  creationMode,
  productGenerationMode,
  ratio,
  vResolution,
  selectedGenerationModelConfigId,
  selectedGenerationModel,
  targetModelId,
  ownerId,
  pendingReverseResult,
  workspace,
  workspacesRef,
  reverseOperationForPendingResult,
  studioActionPendingRequestRef,
  promptOptimizationContext,
  savePromptOptimizationRecord,
  setReverseActionBusy,
  setWorkspacePatch,
  setMsg,
  notify,
}) {
  async function compileStoryboardShot(index, shot) {
    const mode = creationMode;
    const source = composeStoryboardShotPrompt(shot, { index });
    const sourceIdentity = storyboardShotIdentity(shot);
    const operation = reverseOperationForPendingResult();
    const operationId = Number(operation?.id || pendingReverseResult?.operation_id || 0);
    if (!source.trim()) return;
    setReverseActionBusy(`compile-shot-${index}`);
    try {
      const duration = Math.max(
        1,
        Math.round(Number(shot.end_seconds || 0) - Number(shot.start_seconds || 0)),
      );
      if (!selectedGenerationModelConfigId) {
        throw new Error("当前没有可用的视频生成模型");
      }
      const optimizationRequest = {
        prompt: source,
        category: "video",
        product_mode: productGenerationMode,
        duration,
        aspect_ratio: ratio,
        resolution: vResolution,
        mode: "target_model_adaptation",
        target_model_config_id: Number(selectedGenerationModelConfigId),
      };
      const actionRequestId = pendingStudioActionRequestId(
        studioActionPendingRequestRef,
        `storyboard-compile:${ownerId || "unknown"}:${mode}:${operationId || "draft"}:${index}`,
        optimizationRequest,
      );
      const result = await api.createStudioPromptOptimization({
        ...optimizationRequest,
        idempotency_key: actionRequestId,
      });
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      const optimized = String(result?.suggestion?.final_text || "").trim();
      if (!optimized) throw new Error("提示词模型未返回有效的镜头编译稿");

      const latestWorkspace = workspacesRef.current?.[mode] || {};
      const latestPending = latestWorkspace.pendingReverseResult;
      const latestResult = reverseResultPayload(latestPending);
      const latestShots = storyboardResultShots(latestResult);
      if (!latestShots[index] || storyboardShotIdentity(latestShots[index]) !== sourceIdentity) {
        notify.warn("镜头内容已变化，本次编译结果未覆盖新内容。");
        return;
      }
      const nextShots = latestShots.map((item, shotIndex) => (
        shotIndex === index
          ? {
              ...item,
              compiled_prompt: optimized,
              compilation: {
                source_prompt: source,
                optimization_kind: result.optimization_kind || "model_compile",
                direction: result.mode || "target_model_adaptation",
                optimizer_model_id: result.provenance?.optimizer_model_id || null,
                optimizer_model_config_id: result.provenance?.optimizer_model_config_id || null,
                optimizer_model_name: null,
                target_model_config_id: selectedGenerationModelConfigId || null,
                target_model_id: targetModelId || selectedGenerationModel?.model_id || null,
                target_model_name: selectedGenerationModel?.display_name || targetModelId || "当前视频模型",
                compiler_metadata: result.compiler_profile || null,
                change_summary: Array.isArray(result.segments)
                  ? result.segments.filter((item) => item.changed).map((item) => item.label)
                  : [],
                warnings: Array.isArray(result.warnings)
                  ? result.warnings.map((item) => item?.message || String(item)).filter(Boolean)
                  : [],
                charged_credits: Number(result.charged_credits || 0),
              },
            }
          : item
      ));
      const nextResult = storyboardResultWithShots(latestResult, nextShots);
      setWorkspacePatch({
        pendingReverseResult: replaceReverseResultPayload(latestPending, nextResult),
        reverseResultTab: "storyboard",
      }, mode);

      if (operationId) {
        try {
          const revision = normalizeReverseResultRevision(await api.createReverseOperationRevision(operationId, {
            source: "user_edit",
            payload: {
              ...nextResult,
              edit_metadata: { action: "compile_storyboard_shot", shot_index: index },
            },
          }));
          setWorkspacePatch((current) => ({
            reverseResultRevisions: [...(current.reverseResultRevisions || []), revision],
          }), mode);
        } catch (error) {
          reportBackgroundError(error, "save compiled storyboard shot revision");
          notify.warn("镜头已编译，但版本记录同步失败。");
        }
      }
      setMsg(`镜头 ${index + 1} 已编译到 ${selectedGenerationModel?.display_name || "当前视频模型"}。`);
      notify.success("镜头编译完成");
    } catch (error) {
      const text = errorMessage(error, "镜头编译失败，请稍后重试");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  async function applyStoryboardShot(index, shot) {
    const mode = creationMode;
    const latestWorkspace = workspacesRef.current?.[mode] || workspace;
    const latestPending = latestWorkspace.pendingReverseResult || pendingReverseResult;
    const result = reverseResultPayload(latestPending);
    const shots = storyboardResultShots(result);
    const currentShot = shots[index] || shot;
    const rawPrompt = composeStoryboardShotPrompt(currentShot, { index });
    const compiledPrompt = String(currentShot.compiled_prompt || "").trim();
    const finalPrompt = compiledPrompt || rawPrompt;
    if (!finalPrompt) return;
    const duration = Math.max(
      1,
      Math.round(Number(currentShot.end_seconds || 0) - Number(currentShot.start_seconds || 0)),
    );
    const compilation = currentShot.compilation && typeof currentShot.compilation === "object"
      ? currentShot.compilation
      : null;
    const optimizationRecord = compiledPrompt && compilation
      ? {
          raw_text: compilation.source_prompt || rawPrompt,
          optimized_text: compiledPrompt,
          optimizer_model_id: compilation.optimizer_model_id,
          optimizer_model_config_id: compilation.optimizer_model_config_id,
          optimization_direction: compilation.direction || "model_adaptation",
          optimization_kind: compilation.optimization_kind || "model_compile",
          compiler_metadata: compilation.compiler_metadata || null,
          change_summary: compilation.change_summary || [],
          warnings: compilation.warnings || [],
          scope_key: promptOptimizationContextKey({
            ...promptOptimizationContext,
            duration,
          }),
        }
      : null;

    const operation = reverseOperationForPendingResult();
    if (!operation?.id || !result) {
      notify.error("当前镜头缺少可验证的反推任务，无法应用。");
      return;
    }
    setReverseActionBusy(`apply-shot-${index}`);
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
      const payload = {
        ...storyboardResultWithShots(result, shots),
        final_text: finalPrompt,
        parameters: {
          ...(result.parameters && typeof result.parameters === "object" ? result.parameters : {}),
          vDuration: duration,
          vResolution: latestWorkspace.vResolution || vResolution,
          ratio: latestWorkspace.ratio || ratio,
        },
        application_mode: "storyboard_shot",
        selected_shot_index: index,
        selected_shot: currentShot,
      };
      const response = await api.applyReverseOperationResult(operation.id, {
        payload,
        parent_revision_id: parentRevision.id,
        clear_image_evidence: clearsReviewedImageEvidence(parentRevision, payload),
      });
      const editedRevision = normalizeReverseResultRevision(response.user_edit);
      const appliedRevision = normalizeReverseResultRevision(response.applied);

      savePromptOptimizationRecord(mode, optimizationRecord);
      setWorkspacePatch((current) => ({
        prompt: finalPrompt,
        promptDirty: true,
        promptSourceSignature: "",
        vDuration: duration,
        reverseResultTab: "storyboard",
        reverseAppliedVersion: appliedRevision.version,
        reverseAppliedRevisionId: appliedRevision.id,
        reverseResultRevisions: mergeReverseResultRevisions(
          current.reverseResultRevisions || knownRevisions,
          [editedRevision, appliedRevision],
        ),
      }), mode);
      setMsg(`镜头 ${index + 1} 已应用到视频工作区，可检查后直接生成。`);
      notify.success("镜头已应用到工作区");
    } catch (error) {
      reportBackgroundError(error, "apply storyboard shot revision");
      const text = errorMessage(error, "镜头应用失败，工作区未修改");
      setMsg(text);
      notify.error(text);
    } finally {
      setReverseActionBusy("");
    }
  }

  return { compileStoryboardShot, applyStoryboardShot };
}
