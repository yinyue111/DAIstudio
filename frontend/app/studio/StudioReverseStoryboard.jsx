"use client";

import { Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import StudioReverseStoryboardShot from "./StudioReverseStoryboardShot";
import {
  buildStoryboardRevisionRequest,
  buildStoryboardShotGenerationPrepareRequest,
  buildStoryboardEditRequest,
  clearStoryboardShotCompilation,
  createStoryboardMutationCoordinator,
  mergeStoryboardShots,
  moveStoryboardBoundary,
  moveStoryboardShot,
  splitStoryboardShot,
  StoryboardEditError,
  toggleStoryboardShotLock,
  storyboardShotsFromRevision,
} from "./storyboard";

function numberValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function requestId(prefix) {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

export default function StudioReverseStoryboard({
  shots = [],
  frames = [],
  result = {},
  operation = null,
  appliedRevision = null,
  selectedShotIndex = -1,
  busyAction = "",
  targetModelName = "当前视频模型",
  onChange,
  onSelectShot,
  onCompileShot,
  onApplyShot,
  onGenerationSubmitted,
  requestQuoteConfirmation,
}) {
  const [error, setError] = useState("");
  const [persistingAction, setPersistingAction] = useState("");
  const [shotActionState, setShotActionState] = useState({});
  const shotsRef = useRef(shots);
  const onChangeRef = useRef(onChange);
  const persistenceCoordinatorRef = useRef(null);
  onChangeRef.current = onChange;
  if (!persistenceCoordinatorRef.current) {
    persistenceCoordinatorRef.current = createStoryboardMutationCoordinator({
      initialShots: shots,
      onChange: (nextShots) => {
        shotsRef.current = nextShots;
        onChangeRef.current?.(nextShots);
      },
      onError: (exception) => {
        if (!exception) {
          setError("");
          return;
        }
        setError(
          exception instanceof StoryboardEditError
            ? exception.message
            : exception instanceof Error ? exception.message : "分镜操作失败，请检查时间和来源片段。",
        );
      },
      onPendingChange: setPersistingAction,
    });
  }
  const busy = Boolean(
    busyAction
    || persistingAction
    || Object.values(shotActionState).some((value) => String(value || "").startsWith("正在")),
  );

  useEffect(() => {
    shotsRef.current = shots;
    persistenceCoordinatorRef.current?.syncConfirmed(shots);
  }, [shots]);
  useEffect(() => () => {
    persistenceCoordinatorRef.current?.dispose();
  }, []);

  function commit(run, nextSelectedIndex = selectedShotIndex, action = "update", shotIndex = nextSelectedIndex, detail = {}) {
    const operationId = Number(operation?.id || operation?.operation_id);
    if (!Number.isInteger(operationId) || operationId <= 0) {
      setError("当前结果缺少可持久化的反推任务，无法保存分镜操作。");
      return;
    }
    const mutationRequestId = requestId("shot-edit");
    const accepted = persistenceCoordinatorRef.current?.enqueue({
      action,
      apply: run,
      request: (confirmedShots, optimisticShots) => {
        const editRequest = ["split", "merge", "reorder", "lock", "boundary"].includes(action)
          ? buildStoryboardEditRequest(confirmedShots, action, shotIndex, mutationRequestId, detail)
          : null;
        return editRequest
          ? api.editReverseOperationShots(operationId, editRequest)
          : api.createReverseOperationRevision(
            operationId,
            buildStoryboardRevisionRequest(result, optimisticShots, action, shotIndex),
          );
      },
      resolve: (revision, optimisticShots) => {
        const serverShots = storyboardShotsFromRevision(revision);
        return serverShots.length ? serverShots : optimisticShots;
      },
    });
    if (accepted && nextSelectedIndex >= 0) {
      onSelectShot?.(Math.min(nextSelectedIndex, shotsRef.current.length - 1));
    }
  }

  function updateShot(index, patch) {
    commit((currentShots) => currentShots.map((shot, shotIndex) => (
      shotIndex === index ? clearStoryboardShotCompilation({ ...shot, ...patch }) : shot
    )), index);
  }

  async function reanalyzeShot(index, shot) {
    const operationId = Number(operation?.id || operation?.operation_id);
    const shotId = String(shot?.shot_id || "").trim();
    if (!operationId || !shotId) {
      setError("当前镜头没有稳定 shot_id，服务端不支持单镜头重分析。");
      return;
    }
    const clientRequestId = requestId("shot-reanalysis");
    const request = {
      client_request_id: clientRequestId,
      shot_id: shotId,
    };
    setShotActionState((current) => ({ ...current, [index]: "正在提交重分析…" }));
    try {
      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("重分析服务不可用，本次未执行。");
      }
      const confirmation = await requestQuoteConfirmation({
        kind: "reverse",
        request: { reverse_operation_id: operationId, ...request },
        clientRequestId,
        execute: ({ request: confirmed }) => api.reanalyzeReverseOperationShot(operationId, {
          client_request_id: confirmed.client_request_id,
          shot_id: confirmed.shot_id,
          quote_id: confirmed.quote_id,
        }),
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("镜头重分析提交失败。");
        }
        setShotActionState((current) => ({ ...current, [index]: "" }));
        return;
      }
      const nextOperation = confirmation.result;
      setShotActionState((current) => ({ ...current, [index]: `重分析任务 #${nextOperation.id} 已提交` }));
      setError("");
    } catch (exception) {
      setShotActionState((current) => ({ ...current, [index]: "" }));
      setError(exception instanceof Error ? exception.message : "单镜头重分析提交失败。");
    }
  }

  async function generateShot(index, shot) {
    const operationId = Number(operation?.id || operation?.operation_id);
    const prepareRequest = buildStoryboardShotGenerationPrepareRequest(
      shot,
      appliedRevision,
      requestId("shot-generation"),
    );
    if (!operationId || !prepareRequest) {
      setError("请先应用/确认当前分镜，并等待服务端保存已验证版本后再生成。");
      return;
    }
    setShotActionState((current) => ({ ...current, [index]: "正在准备镜头生成…" }));
    try {
      const prepared = await api.prepareReverseOperationShotGeneration(operationId, {
        ...prepareRequest,
      });
      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("镜头生成服务不可用，本次未执行。");
      }
      const confirmation = await requestQuoteConfirmation({
        kind: "generation",
        request: prepared.request,
        clientRequestId: prepared.request.client_request_id,
        execute: ({ request }) => api.generate(request),
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("镜头生成提交失败。");
        }
        setShotActionState((current) => ({ ...current, [index]: "" }));
        return;
      }
      const submitted = {
        task: confirmation.result,
        quote: confirmation.quote?.raw,
        payload: confirmation.request,
      };
      const taskId = Number(submitted.task?.id || submitted.task?.task_id);
      const credits = Number(submitted.quote?.estimated_credits ?? submitted.quote?.cost_credits ?? 0);
      setShotActionState((current) => ({
        ...current,
        [index]: `生成任务 #${taskId || "-"} 已提交${credits > 0 ? ` · ${credits} 积分` : ""}`,
      }));
      await onGenerationSubmitted?.({
        task: submitted.task,
        prepared,
        quote: submitted.quote,
        shot,
      });
      setError("");
    } catch (exception) {
      setShotActionState((current) => ({ ...current, [index]: "" }));
      setError(exception instanceof Error ? exception.message : "镜头生成提交失败。");
    }
  }

  function addShot() {
    const nextIndex = shotsRef.current.length;
    commit((currentShots) => {
      const previous = currentShots.at(-1) || {};
      const previousEnd = numberValue(previous.end_seconds, 0);
      const nextShot = {
        ...(previous.source_segment_index ? { source_segment_index: previous.source_segment_index } : {}),
        start_seconds: previousEnd,
        end_seconds: previousEnd + 1,
        visual: "",
        action: "",
        camera: "",
        transition: "",
        audio_cue: "未分析",
        evidence_frame_indices: [],
        confidence: 0,
        locked: false,
      };
      return [...currentShots, nextShot];
    }, nextIndex, "add", nextIndex);
  }

  if (shots.length === 0) {
    return (
      <div className="py-8 text-center" role="status">
        <p className="text-sm text-mist">当前结果没有可验证的分镜。</p>
        <p className="mt-1 text-xs text-fog">可添加镜头草稿，或提高分析精度后再次反推。</p>
        <button type="button" onClick={addShot} className="btn-secondary btn-sm mt-3">
          <Plus size={14} aria-hidden="true" /> 添加镜头
        </button>
      </div>
    );
  }

  return (
    <div>
      {error && (
        <p className="mb-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn" role="alert">
          {error}
        </p>
      )}
      <ol className="space-y-3" aria-label="反推分镜">
        {shots.map((shot, index) => (
          <StudioReverseStoryboardShot
            key={`${shot.source_segment_index || 0}-${shot.start_seconds}-${shot.end_seconds}-${index}`}
            shot={shot}
            nextShot={shots[index + 1] || null}
            index={index}
            total={shots.length}
            selected={selectedShotIndex === index}
            busy={busy}
            targetModelName={targetModelName}
            onSelect={() => onSelectShot?.(index)}
            onUpdate={(patch) => updateShot(index, patch)}
            onDelete={() => commit((currentShots) => {
              if (currentShots[index]?.locked) throw new StoryboardEditError("镜头已锁定，无法删除。");
              return currentShots.filter((_, shotIndex) => shotIndex !== index);
            }, Math.max(0, index - 1), "delete", index)}
            onSplit={(splitAt) => commit(
              (currentShots) => splitStoryboardShot(currentShots, index, splitAt, frames),
              index + 1,
              "split",
              index,
              { split_seconds: splitAt },
            )}
            onMerge={() => commit((currentShots) => mergeStoryboardShots(currentShots, index), index, "merge", index)}
            onMoveBoundary={(boundarySeconds) => commit(
              (currentShots) => moveStoryboardBoundary(currentShots, index, boundarySeconds, frames),
              index,
              "boundary",
              index,
              { boundary_seconds: boundarySeconds },
            )}
            onMove={(direction) => commit(
              (currentShots) => moveStoryboardShot(currentShots, index, direction),
              index + direction,
              "reorder",
              index,
              { direction },
            )}
            onToggleLock={() => commit((currentShots) => toggleStoryboardShotLock(currentShots, index), index, "lock", index)}
            onCompile={() => onCompileShot?.(index, shot)}
            onApply={() => onApplyShot?.(index, shot)}
            onReanalyze={() => reanalyzeShot(index, shot)}
            onGenerate={() => generateShot(index, shot)}
            generationDisabledReason={!appliedRevision ? "请先应用/确认分镜，并等待已验证版本保存完成" : ""}
            actionStatus={shotActionState[index] || ""}
          />
        ))}
      </ol>
      {persistingAction && <p className="mt-2 text-[10px] text-fog" role="status">正在保存分镜操作…</p>}
      <button type="button" onClick={addShot} disabled={busy} className="btn-secondary btn-sm mt-3">
        <Plus size={14} aria-hidden="true" /> 添加镜头
      </button>
    </div>
  );
}
