"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Crosshair,
  Play,
  RefreshCw,
  ScanSearch,
  Square,
  WandSparkles,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../lib/api";
import { unifiedAssetKey } from "../../lib/unifiedAssets";
import { ReferenceAssetPreview } from "./StudioMedia";
import {
  isReproductionAssessmentTerminal,
  normalizeReproductionAssessment,
  reproductionCorrectionPayload,
  reproductionFindingTimelineStyle,
  reproductionIdempotencyKey,
  selectableReproductionFindingIds,
} from "./reproductionAssessment";
import {
  executableReproductionFindingIds,
  isReproductionRemediationActive,
  isReproductionRemediationTerminal,
  normalizeReproductionRemediation,
  normalizeReproductionRemediationPage,
  reproductionRemediationCreatePayload,
  reproductionRemediationExecutionIssue,
  reproductionRemediationPendingGenerationRequests,
} from "./reproductionRemediation";

const ACTIVE_STATUSES = new Set(["queued", "running"]);

const STATUS_LABELS = {
  queued: "排队中",
  running: "分析中",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  canceled: "已取消",
  unsupported: "不支持",
  degraded: "已降级",
};

const REMEDIATION_STATUS_LABELS = {
  planned: "待执行",
  awaiting_generation: "待生成",
  queued: "排队中",
  running: "执行中",
  generating: "生成中",
  awaiting_composition: "待合成",
  composing: "合成中",
  reassessing: "自动复评中",
  succeeded: "已完成",
  partial: "部分提交",
  failed: "执行失败",
  canceled: "已取消",
  unknown: "状态待确认",
};

const PLAN_ITEM_STATUS_LABELS = {
  planned: "待执行",
  queued: "已提交",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
  needs_review: "待审核",
  canceled: "已取消",
};

const SEVERITY_STYLES = {
  info: "border-line bg-white/[0.04] text-fog",
  low: "border-good/25 bg-good/10 text-good",
  medium: "border-warn/25 bg-warn/10 text-warn",
  high: "border-bad/35 bg-bad/10 text-bad",
  critical: "border-bad/60 bg-bad/20 text-bad",
};

function positiveInteger(value) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function canonicalAssetRef(asset) {
  const value = unifiedAssetKey(asset);
  return /^(g\.\d+|u\.[A-Za-z0-9_-]+)$/.test(value) ? value : "";
}

function statusText(status) {
  return STATUS_LABELS[status] || status || "未知";
}

function remediationStatusText(status) {
  return REMEDIATION_STATUS_LABELS[status] || status || "未知";
}

function remediationActionText(mediaType) {
  return mediaType === "video" ? "逐镜重生成" : "局部重绘";
}

function displaySeconds(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return "-";
  const minutes = Math.floor(parsed / 60);
  const seconds = (parsed % 60).toFixed(parsed < 10 ? 1 : 0).padStart(2, "0");
  return minutes ? `${minutes}:${seconds}` : `${seconds}s`;
}

function DimensionGrid({ dimensions }) {
  if (!dimensions.length) return null;
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
      {dimensions.map((dimension) => (
        <article key={dimension.key} className="rounded-lg border border-line bg-white/[0.03] p-3">
          <div className="flex items-start justify-between gap-2">
            <span className="text-xs font-medium text-mist">{dimension.label}</span>
            {dimension.status === "succeeded" ? (
              <span className="text-lg font-display font-semibold text-snow">{Math.round(dimension.score)}</span>
            ) : (
              <span className="badge text-[10px]">{statusText(dimension.status)}</span>
            )}
          </div>
          {dimension.status === "succeeded" && (
            <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.08]">
              <div className="h-full rounded-full bg-brand" style={{ width: `${dimension.score}%` }} />
            </div>
          )}
          {dimension.degraded_reason && (
            <p className="mt-2 text-[11px] leading-4 text-fog">{dimension.degraded_reason}</p>
          )}
        </article>
      ))}
    </div>
  );
}

function FindingOverlay({ findings, selectedIds, onToggle }) {
  return findings.map((finding) => {
    if (!finding.bbox) return null;
    const selected = selectedIds.has(finding.id);
    return (
      <button
        key={finding.id}
        type="button"
        title={finding.title}
        aria-label={`定位问题：${finding.title}`}
        className={`absolute border-2 ${selected ? "border-bad bg-bad/20" : "border-warn/80 bg-warn/10"}`}
        style={{
          left: `${finding.bbox.x * 100}%`,
          top: `${finding.bbox.y * 100}%`,
          width: `${finding.bbox.width * 100}%`,
          height: `${finding.bbox.height * 100}%`,
        }}
        onClick={() => onToggle(finding.id)}
      />
    );
  });
}

function VideoDifferenceTimeline({ findings, duration, selectedIds, onToggle }) {
  const ranged = findings.filter((finding) => finding.start_seconds !== null && finding.end_seconds !== null);
  if (!ranged.length || !duration) return null;
  return (
    <div className="rounded-lg border border-line bg-black/20 p-3">
      <div className="mb-2 flex items-center justify-between text-[11px] text-fog">
        <span>差异时间线</span>
        <span>{displaySeconds(duration)}</span>
      </div>
      <div className="relative h-10 overflow-hidden rounded-md bg-white/[0.06]">
        {ranged.map((finding) => {
          const style = reproductionFindingTimelineStyle(finding, duration);
          if (!style) return null;
          const selected = selectedIds.has(finding.id);
          return (
            <button
              key={finding.id}
              type="button"
              title={`${finding.title} · ${displaySeconds(finding.start_seconds)}-${displaySeconds(finding.end_seconds)}`}
              aria-label={`选择时间线问题：${finding.title}`}
              className={`absolute inset-y-1 rounded-sm border ${selected ? "border-bad bg-bad/70" : "border-warn/70 bg-warn/40"}`}
              style={style}
              onClick={() => onToggle(finding.id)}
            />
          );
        })}
      </div>
    </div>
  );
}

export default function StudioReproductionAssessment({
  sourceAsset,
  generatedAssets = [],
  generationTaskId,
  reverseOperationId = null,
  reverseRevisionId = null,
  modelConfigId = null,
  remediationParams = {},
  videoComposition = null,
  requestQuoteConfirmation,
  onCorrectionCreated,
  onRemediationCreated,
  onRemediationExecutionSubmitted,
}) {
  const sourceRef = canonicalAssetRef(sourceAsset);
  const eligibleAssets = useMemo(() => generatedAssets.filter((asset) => (
    canonicalAssetRef(asset).startsWith("g.") && asset?.type === sourceAsset?.type
  )), [generatedAssets, sourceAsset?.type]);
  const [generatedRef, setGeneratedRef] = useState("");
  const generatedAsset = eligibleAssets.find((asset) => canonicalAssetRef(asset) === generatedRef)
    || eligibleAssets[0]
    || null;
  const activeGeneratedRef = canonicalAssetRef(generatedAsset);
  const [open, setOpen] = useState(false);
  const [assessment, setAssessment] = useState(null);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [promptPatch, setPromptPatch] = useState("");
  const [negativePatch, setNegativePatch] = useState("");
  const [autoReassess, setAutoReassess] = useState(true);
  const [remediation, setRemediation] = useState(null);
  const [remediationLoaded, setRemediationLoaded] = useState(false);
  const [remediationNotice, setRemediationNotice] = useState("");
  const [remediationError, setRemediationError] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const selectionInitializedForRef = useRef(null);
  const requestVersionRef = useRef(0);
  const assessmentAttemptRef = useRef(0);
  const remediationAttemptRef = useRef(0);
  const remediationRequestVersionRef = useRef(0);

  useEffect(() => {
    if (!eligibleAssets.length) {
      setGeneratedRef("");
      return;
    }
    if (!eligibleAssets.some((asset) => canonicalAssetRef(asset) === generatedRef)) {
      setGeneratedRef(canonicalAssetRef(eligibleAssets[0]));
    }
  }, [eligibleAssets, generatedRef]);

  useEffect(() => {
    requestVersionRef.current += 1;
    setAssessment(null);
    setSelectedIds(new Set());
    setPromptPatch("");
    setNegativePatch("");
    setAutoReassess(true);
    setRemediation(null);
    setRemediationLoaded(false);
    setRemediationNotice("");
    setRemediationError("");
    setError("");
    selectionInitializedForRef.current = null;
    assessmentAttemptRef.current = 0;
    remediationAttemptRef.current = 0;
    remediationRequestVersionRef.current += 1;
  }, [sourceRef, activeGeneratedRef, generationTaskId]);

  useEffect(() => {
    if (!assessment || !ACTIVE_STATUSES.has(assessment.status)) return undefined;
    const requestVersion = requestVersionRef.current;
    let canceled = false;
    const timer = window.setTimeout(async () => {
      try {
        const next = normalizeReproductionAssessment(await api.reproductionAssessment(assessment.id));
        if (!canceled && requestVersion === requestVersionRef.current) {
          setAssessment(next);
          setError("");
        }
      } catch (pollError) {
        if (!canceled && requestVersion === requestVersionRef.current) {
          setError(pollError?.message || "复刻度评估状态刷新失败");
        }
      }
    }, 1600);
    return () => {
      canceled = true;
      window.clearTimeout(timer);
    };
  }, [assessment]);

  useEffect(() => {
    if (!assessment || !isReproductionAssessmentTerminal(assessment)) return;
    if (selectionInitializedForRef.current === assessment.id) return;
    selectionInitializedForRef.current = assessment.id;
    setSelectedIds(new Set(selectableReproductionFindingIds(assessment)));
  }, [assessment]);

  useEffect(() => {
    if (!assessment?.id || !["succeeded", "partial"].includes(assessment.status)) {
      setRemediationLoaded(false);
      return undefined;
    }
    const requestVersion = ++remediationRequestVersionRef.current;
    let canceled = false;
    setRemediationLoaded(false);
    setRemediationError("");
    api.reproductionRemediations(assessment.id, { limit: 20, offset: 0 })
      .then((value) => {
        if (canceled || requestVersion !== remediationRequestVersionRef.current) return;
        const page = normalizeReproductionRemediationPage(value, assessment.id);
        const latest = [...page.items].sort((left, right) => right.id - left.id)[0] || null;
        setRemediation(latest);
        if (latest) setAutoReassess(latest.auto_reassess);
      })
      .catch((loadError) => {
        if (canceled || requestVersion !== remediationRequestVersionRef.current) return;
        setRemediationError(loadError?.message || "纠偏计划恢复失败");
      })
      .finally(() => {
        if (!canceled && requestVersion === remediationRequestVersionRef.current) {
          setRemediationLoaded(true);
        }
      });
    return () => {
      canceled = true;
    };
  }, [assessment?.id, assessment?.status]);

  useEffect(() => {
    if (!assessment?.id || !remediation?.id || !isReproductionRemediationActive(remediation)) {
      return undefined;
    }
    const requestVersion = remediationRequestVersionRef.current;
    let canceled = false;
    const timer = window.setTimeout(async () => {
      try {
        const next = normalizeReproductionRemediation(
          await api.reproductionRemediation(assessment.id, remediation.id),
          assessment.id,
        );
        if (!canceled && requestVersion === remediationRequestVersionRef.current) {
          setRemediation(next);
          setRemediationError("");
        }
      } catch (pollError) {
        if (!canceled && requestVersion === remediationRequestVersionRef.current) {
          setRemediationError(pollError?.message || "纠偏执行状态刷新失败");
        }
      }
    }, 1800);
    return () => {
      canceled = true;
      window.clearTimeout(timer);
    };
  }, [assessment, remediation]);

  if (!sourceRef || !eligibleAssets.length || !positiveInteger(generationTaskId)) return null;

  const active = Boolean(assessment && ACTIVE_STATUSES.has(assessment.status));
  const actionable = Boolean(assessment && ["succeeded", "partial"].includes(assessment.status));
  const parentRevisionId = positiveInteger(assessment?.reverse_revision_id || reverseRevisionId);
  const remediationModelConfigId = positiveInteger(modelConfigId);
  const executableIds = new Set(executableReproductionFindingIds(assessment));
  const selectedExecutionFindingIds = [...selectedIds]
    .filter((findingId) => executableIds.has(findingId))
    .sort((left, right) => left - right);
  const selectedNonExecutableCount = selectedIds.size - selectedExecutionFindingIds.length;
  const pendingGenerationRequests = reproductionRemediationPendingGenerationRequests(remediation);
  const remediationExecutionIssue = reproductionRemediationExecutionIssue(remediation);
  const remediationActive = isReproductionRemediationActive(remediation);
  const remediationTerminal = isReproductionRemediationTerminal(remediation);
  const remediationAction = remediationActionText(assessment?.media_type);
  const settledRemediationItems = (remediation?.plan_items || []).filter((item) => (
    ["succeeded", "failed", "needs_review", "canceled"].includes(item.status)
  )).length;
  const duration = Math.max(
    Number(sourceAsset?.duration || 0),
    Number(generatedAsset?.duration || 0),
    ...(assessment?.findings || []).map((finding) => Number(finding.end_seconds || 0)),
  );

  async function startAssessment({ retry = false } = {}) {
    if (retry) assessmentAttemptRef.current += 1;
    setBusyAction("create");
    setError("");
    setRemediation(null);
    setRemediationLoaded(false);
    setRemediationNotice("");
    setRemediationError("");
    remediationRequestVersionRef.current += 1;
    requestVersionRef.current += 1;
    const requestVersion = requestVersionRef.current;
    try {
      const created = normalizeReproductionAssessment(await api.createReproductionAssessment({
        source_asset_ref: sourceRef,
        generated_asset_ref: activeGeneratedRef,
        generation_task_id: positiveInteger(generationTaskId),
        reverse_operation_id: positiveInteger(reverseOperationId),
        reverse_revision_id: positiveInteger(reverseRevisionId),
        idempotency_key: reproductionIdempotencyKey(
          "reproduction-assessment",
          generationTaskId,
          sourceRef,
          activeGeneratedRef,
          assessmentAttemptRef.current,
        ),
      }));
      if (requestVersion === requestVersionRef.current) setAssessment(created);
    } catch (createError) {
      if (requestVersion === requestVersionRef.current) {
        setError(createError?.message || "创建复刻度评估失败");
      }
    } finally {
      if (requestVersion === requestVersionRef.current) setBusyAction("");
    }
  }

  async function cancelAssessment() {
    if (!assessment?.id) return;
    setBusyAction("cancel");
    setError("");
    try {
      setAssessment(normalizeReproductionAssessment(await api.cancelReproductionAssessment(assessment.id)));
    } catch (cancelError) {
      setError(cancelError?.message || "取消复刻度评估失败");
    } finally {
      setBusyAction("");
    }
  }

  function toggleFinding(findingId) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(findingId)) next.delete(findingId);
      else next.add(findingId);
      return next;
    });
  }

  async function createCorrection() {
    if (!assessment?.id || !parentRevisionId) return;
    setBusyAction("correct");
    setError("");
    try {
      const body = reproductionCorrectionPayload({
        idempotencyKey: reproductionIdempotencyKey(
          "reproduction-correction",
          assessment.id,
          parentRevisionId,
          [...selectedIds].sort().join(","),
          promptPatch,
          negativePatch,
        ),
        parentRevisionId,
        findingIds: [...selectedIds],
        promptPatch,
        negativePatch,
      });
      const response = await api.createReproductionCorrection(assessment.id, body);
      const correctionRevisionId = positiveInteger(response?.edited_revision?.id);
      setAssessment((current) => current ? { ...current, correction_revision_id: correctionRevisionId } : current);
      await onCorrectionCreated?.({ assessment, response });
    } catch (correctionError) {
      setError(correctionError?.message || "创建修正版本失败");
    } finally {
      setBusyAction("");
    }
  }

  async function refreshRemediation({ showBusy = true } = {}) {
    if (!assessment?.id || !remediation?.id) return null;
    if (showBusy) setBusyAction("refresh-remediation");
    setRemediationError("");
    try {
      const next = normalizeReproductionRemediation(
        await api.reproductionRemediation(assessment.id, remediation.id),
        assessment.id,
      );
      setRemediation(next);
      return next;
    } catch (refreshError) {
      setRemediationError(refreshError?.message || "纠偏状态恢复失败");
      return null;
    } finally {
      if (showBusy) setBusyAction("");
    }
  }

  async function createRemediation({ retry = false } = {}) {
    if (!assessment?.id || !parentRevisionId) return;
    if (!remediationLoaded) {
      setRemediationError("正在恢复既有纠偏计划，请稍后再创建。");
      return;
    }
    if (retry) remediationAttemptRef.current += 1;
    setBusyAction("create-remediation");
    setRemediationError("");
    setRemediationNotice("");
    try {
      const body = reproductionRemediationCreatePayload({
        assessment,
        idempotencyKey: reproductionIdempotencyKey(
          "reproduction-remediation",
          assessment.id,
          parentRevisionId,
          selectedExecutionFindingIds.join(","),
          promptPatch,
          negativePatch,
          modelConfigId,
          autoReassess,
          JSON.stringify(remediationParams || {}),
          JSON.stringify(videoComposition || {}),
          remediationAttemptRef.current,
        ),
        parentRevisionId,
        findingIds: selectedExecutionFindingIds,
        modelConfigId,
        promptPatch,
        negativePatch,
        params: remediationParams,
        videoComposition,
        autoReassess,
        parentRemediationId: retry ? remediation?.id : null,
      });
      const response = await api.createReproductionRemediation(assessment.id, body);
      const created = normalizeReproductionRemediation(response, assessment.id);
      setRemediation(created);
      setRemediationLoaded(true);
      setAssessment((current) => current ? {
        ...current,
        correction_revision_id: created.applied_revision_id || current.correction_revision_id,
      } : current);
      setRemediationNotice(
        `${remediationAction}计划 #${created.id} 已保存，原结果保持不变，可直接执行。`,
      );
      await onRemediationCreated?.({ assessment, remediation: created, response });
    } catch (createError) {
      setRemediationError(createError?.message || `创建${remediationAction}计划失败`);
    } finally {
      setBusyAction("");
    }
  }

  async function executeRemediation() {
    if (!assessment?.id || !remediation?.id) return;
    if (remediationExecutionIssue) {
      setRemediationError(remediationExecutionIssue);
      return;
    }
    if (typeof requestQuoteConfirmation !== "function") {
      setRemediationError("纠偏执行服务不可用，计划已保留，本次未执行也未扣费。");
      return;
    }
    setBusyAction("execute-remediation");
    setRemediationError("");
    setRemediationNotice("");
    let submittedCount = 0;
    try {
      for (let index = 0; index < pendingGenerationRequests.length; index += 1) {
        const request = pendingGenerationRequests[index];
        const confirmation = await requestQuoteConfirmation({
          kind: "generation",
          request,
          clientRequestId: String(request.client_request_id || ""),
          execute: ({ request: confirmedRequest }) => api.generate(confirmedRequest),
        });
        if (confirmation.status !== "executed") {
          if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
            throw confirmation.error || new Error(`${remediationAction}提交失败`);
          }
          setRemediationNotice(
            submittedCount
              ? `已提交 ${submittedCount}/${pendingGenerationRequests.length} 项，其余计划已保留，可稍后继续执行。`
              : "纠偏计划仍保留，本次未扣费。",
          );
          break;
        }
        submittedCount += 1;
        await onRemediationExecutionSubmitted?.({
          assessment,
          remediation,
          request: confirmation.request,
          task: confirmation.result,
          quote: confirmation.quote?.raw,
          index,
          total: pendingGenerationRequests.length,
        });
      }
      const next = await refreshRemediation({ showBusy: false });
      if (submittedCount === pendingGenerationRequests.length) {
        const shouldReassess = next?.auto_reassess ?? remediation.auto_reassess;
        setRemediationNotice(
          `${submittedCount} 项生成任务已提交，${shouldReassess ? "成片后将自动复评。" : "成片后不会自动复评。"}`,
        );
      }
    } catch (executeError) {
      await refreshRemediation({ showBusy: false });
      setRemediationError(executeError?.message || `${remediationAction}执行失败`);
    } finally {
      setBusyAction("");
    }
  }

  if (!open) {
    return (
      <div className="mt-4 flex justify-end">
        <button type="button" className="btn-secondary btn-sm inline-flex items-center gap-1.5" onClick={() => setOpen(true)}>
          <ScanSearch size={15} aria-hidden="true" />
          评估复刻度
        </button>
      </div>
    );
  }

  return (
    <section className="mt-4 rounded-xl2 border border-brand/30 bg-brand/[0.06] p-4" aria-label="复刻度评估">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Crosshair size={17} className="text-brand" aria-hidden="true" />
            <h3 className="text-sm font-display font-semibold text-snow">复刻度评估</h3>
            {assessment && <span className="badge">{statusText(assessment.status)}</span>}
          </div>
          {assessment?.phase && <p className="mt-1 text-xs text-fog">{assessment.phase}</p>}
        </div>
        <div className="flex items-center gap-2">
          {eligibleAssets.length > 1 && (
            <select
              className="input px-2 py-1.5 text-xs"
              aria-label="选择生成结果"
              value={activeGeneratedRef}
              onChange={(event) => setGeneratedRef(event.target.value)}
            >
              {eligibleAssets.map((asset, index) => (
                <option key={canonicalAssetRef(asset)} value={canonicalAssetRef(asset)}>结果 {index + 1}</option>
              ))}
            </select>
          )}
          <button type="button" className="icon-btn" title="关闭复刻度评估" aria-label="关闭复刻度评估" onClick={() => setOpen(false)}>
            <X size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <article>
          <p className="mb-2 text-xs font-medium text-mist">源素材</p>
          <div className="aspect-video overflow-hidden rounded-lg border border-line bg-black/20">
            <ReferenceAssetPreview asset={sourceAsset} />
          </div>
        </article>
        <article>
          <p className="mb-2 text-xs font-medium text-mist">生成结果</p>
          <div className="relative aspect-video overflow-hidden rounded-lg border border-line bg-black/20">
            <ReferenceAssetPreview asset={generatedAsset} />
            {assessment?.media_type === "image" && (
              <FindingOverlay findings={assessment.findings} selectedIds={selectedIds} onToggle={toggleFinding} />
            )}
          </div>
        </article>
      </div>

      {!assessment && (
        <div className="mt-4 flex justify-end">
          <button type="button" className="btn-primary btn-sm inline-flex items-center gap-1.5" disabled={busyAction === "create"} onClick={() => startAssessment()}>
            {busyAction === "create" ? <RefreshCw size={15} className="animate-spin" aria-hidden="true" /> : <ScanSearch size={15} aria-hidden="true" />}
            {busyAction === "create" ? "创建中…" : "开始评估"}
          </button>
        </div>
      )}

      {active && (
        <div className="mt-4">
          <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
            <div className="h-full rounded-full bg-brand transition-all" style={{ width: `${Math.max(4, assessment.progress)}%` }} />
          </div>
          <div className="mt-2 flex justify-end">
            <button type="button" className="btn-ghost btn-sm text-bad" disabled={busyAction === "cancel"} onClick={cancelAssessment}>
              <Square size={13} aria-hidden="true" />
              取消
            </button>
          </div>
        </div>
      )}

      {assessment && <div className="mt-4"><DimensionGrid dimensions={assessment.dimensions} /></div>}

      {assessment?.media_type === "video" && (
        <div className="mt-4">
          <VideoDifferenceTimeline
            findings={assessment.findings}
            duration={duration}
            selectedIds={selectedIds}
            onToggle={toggleFinding}
          />
        </div>
      )}

      {actionable && assessment.findings.length > 0 && (
        <div className="mt-4 space-y-2">
          <div className="flex items-center justify-between gap-3">
            <h4 className="text-xs font-semibold text-mist">问题定位</h4>
            <button
              type="button"
              className="btn-ghost btn-sm"
              onClick={() => setSelectedIds(new Set(selectableReproductionFindingIds(assessment)))}
            >
              选择可修正项
            </button>
          </div>
          {assessment.findings.map((finding) => (
            <label key={finding.id} className="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-white/[0.025] p-3">
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-brand"
                checked={selectedIds.has(finding.id)}
                onChange={() => toggleFinding(finding.id)}
              />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-snow">{finding.title}</span>
                  <span className={`rounded border px-1.5 py-0.5 text-[10px] ${SEVERITY_STYLES[finding.severity]}`}>
                    {finding.severity}
                  </span>
                  {finding.shot_id && <span className="text-[11px] text-fog">镜头 {finding.shot_id}</span>}
                  {finding.start_seconds !== null && (
                    <span className="text-[11px] text-fog">
                      {displaySeconds(finding.start_seconds)}-{displaySeconds(finding.end_seconds)}
                    </span>
                  )}
                </span>
                {finding.detail && <span className="mt-1 block text-xs leading-5 text-fog">{finding.detail}</span>}
              </span>
            </label>
          ))}
        </div>
      )}

      {actionable && parentRevisionId && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <textarea
            className="input min-h-20 resize-y px-3 py-2 text-xs"
            aria-label="修正提示词补丁"
            placeholder="提示词修正（可选）"
            maxLength={2000}
            value={promptPatch}
            onChange={(event) => setPromptPatch(event.target.value)}
          />
          <textarea
            className="input min-h-20 resize-y px-3 py-2 text-xs"
            aria-label="修正负向提示词"
            placeholder="负向约束（可选）"
            maxLength={1000}
            value={negativePatch}
            onChange={(event) => setNegativePatch(event.target.value)}
          />
          <div className="flex flex-col gap-3 border-t border-line pt-3 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-mist">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-brand"
                  checked={autoReassess}
                  disabled={Boolean(remediation && !remediationTerminal)}
                  onChange={(event) => setAutoReassess(event.target.checked)}
                />
                生成完成后自动复评
              </label>
              <p className="mt-1 text-[11px] leading-4 text-fog">
                {selectedExecutionFindingIds.length} 项可执行{selectedNonExecutableCount > 0
                  ? `，另有 ${selectedNonExecutableCount} 项仅支持修正提示词`
                  : ""}。{remediationModelConfigId ? "创建计划不会扣费。" : "请先选择生成模型。"}
              </p>
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className="btn-secondary btn-sm inline-flex items-center gap-1.5"
                disabled={!selectedIds.size || Boolean(busyAction)}
                onClick={createCorrection}
              >
                {busyAction === "correct" ? <RefreshCw size={15} className="animate-spin" aria-hidden="true" /> : <WandSparkles size={15} aria-hidden="true" />}
                {busyAction === "correct" ? "创建中…" : "仅创建修正版本"}
              </button>
              <button
                type="button"
                className="btn-primary btn-sm inline-flex items-center gap-1.5"
                disabled={
                  !selectedExecutionFindingIds.length
                  || !remediationModelConfigId
                  || !remediationLoaded
                  || Boolean(busyAction)
                  || Boolean(remediation && !remediationTerminal)
                }
                onClick={() => createRemediation()}
              >
                {busyAction === "create-remediation"
                  ? <RefreshCw size={15} className="animate-spin" aria-hidden="true" />
                  : <WandSparkles size={15} aria-hidden="true" />}
                {busyAction === "create-remediation" ? "创建中…" : `创建${remediationAction}计划`}
              </button>
            </div>
          </div>
        </div>
      )}

      {actionable && parentRevisionId && remediationLoaded && !remediation && !remediationError && (
        <div className="mt-4 rounded-lg border border-line bg-white/[0.025] px-3 py-2 text-xs text-fog" role="status">
          当前评估还没有纠偏执行记录。
        </div>
      )}

      {remediation && (
        <section className="mt-4 border-t border-line pt-4" aria-label={`${remediationAction}执行状态`}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-xs font-semibold text-mist">{remediationAction}计划 #{remediation.id}</h4>
                <span className="badge">{remediationStatusText(remediation.status)}</span>
                <span className="text-[11px] text-fog">
                  {remediation.mode === "video_shot_regenerate"
                    ? `${remediation.selected_shot_ids.length} 个镜头`
                    : `${remediation.selected_finding_ids.length} 个区域`}
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-4 text-fog">
                已提交 {remediation.generation_task_ids.length}/{remediation.generation_requests.length || remediation.plan_items.length} 项
                {remediation.auto_reassess ? " · 完成后自动复评" : " · 不自动复评"}
              </p>
              {remediation.phase && <p className="mt-1 text-[11px] text-mist">{remediation.phase}</p>}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className="btn-ghost btn-sm inline-flex items-center gap-1.5"
                disabled={Boolean(busyAction)}
                onClick={() => refreshRemediation()}
              >
                <RefreshCw
                  size={14}
                  className={busyAction === "refresh-remediation" ? "animate-spin" : ""}
                  aria-hidden="true"
                />
                刷新恢复
              </button>
              {pendingGenerationRequests.length > 0 && (
                <button
                  type="button"
                  className="btn-primary btn-sm inline-flex items-center gap-1.5"
                  disabled={Boolean(remediationExecutionIssue) || Boolean(busyAction)}
                  title={remediationExecutionIssue || "执行纠偏计划"}
                  onClick={executeRemediation}
                >
                  {busyAction === "execute-remediation"
                    ? <RefreshCw size={14} className="animate-spin" aria-hidden="true" />
                    : <Play size={14} aria-hidden="true" />}
                  {busyAction === "execute-remediation"
                    ? "提交中…"
                    : `执行计划${pendingGenerationRequests.length > 1 ? ` (${pendingGenerationRequests.length})` : ""}`}
                </button>
              )}
              {remediationTerminal && remediation.status !== "succeeded" && (
                <button
                  type="button"
                  className="btn-secondary btn-sm inline-flex items-center gap-1.5"
                  disabled={!selectedExecutionFindingIds.length || !remediationModelConfigId || Boolean(busyAction)}
                  onClick={() => createRemediation({ retry: true })}
                >
                  <RefreshCw size={14} aria-hidden="true" />
                  按当前选择重建计划
                </button>
              )}
            </div>
          </div>

          {remediationActive && (
            <div
              className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/[0.08]"
              role="progressbar"
              aria-label="纠偏任务执行中"
              aria-valuemin={0}
              aria-valuemax={remediation.plan_items.length || 1}
              aria-valuenow={settledRemediationItems}
            >
              <div
                className="h-full min-w-[4%] rounded-full bg-brand transition-all"
                style={{
                  width: `${Math.max(4, (settledRemediationItems / Math.max(1, remediation.plan_items.length)) * 100)}%`,
                }}
              />
            </div>
          )}

          {remediation.plan_items.length > 0 && (
            <ul className="mt-3 divide-y divide-line border-y border-line" aria-label="纠偏计划项">
              {remediation.plan_items.map((item, index) => (
                <li key={item.id} className="flex flex-wrap items-start justify-between gap-2 py-2 text-xs">
                  <div className="min-w-0">
                    <p className="text-mist">
                      {remediation.mode === "video_shot_regenerate"
                        ? `镜头 ${item.shot_id || index + 1}`
                        : `局部区域 ${index + 1}`}
                    </p>
                    <p className="mt-0.5 text-[10px] text-fog">
                      {item.finding_ids.length} 个问题
                      {item.generation_task_id ? ` · 任务 #${item.generation_task_id}` : ""}
                      {item.asset_ref ? ` · ${item.asset_ref}` : ""}
                    </p>
                    {item.error && <p className="mt-1 text-[11px] text-bad">{item.error}</p>}
                  </div>
                  <span className="badge">{PLAN_ITEM_STATUS_LABELS[item.status] || item.status}</span>
                </li>
              ))}
            </ul>
          )}

          {!remediationActive && remediationExecutionIssue && !remediationTerminal && (
            <p className="mt-3 rounded-lg border border-warn/25 bg-warn/10 px-3 py-2 text-xs text-warn">
              {remediationExecutionIssue}
            </p>
          )}

          {typeof requestQuoteConfirmation !== "function" && pendingGenerationRequests.length > 0 && (
            <p className="mt-3 rounded-lg border border-warn/25 bg-warn/10 px-3 py-2 text-xs text-warn">
              当前页面尚未接入纠偏执行回调；计划已保存，但不会直接执行或扣费。
            </p>
          )}

          {remediation.final_asset_ref && (
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-good/30 bg-good/10 px-3 py-2 text-xs text-good">
              <CheckCircle2 size={15} aria-hidden="true" />
              最终结果 {remediation.final_asset_ref} 已生成
              {remediation.successor_assessment_id
                ? `，自动复评 #${remediation.successor_assessment_id} 已创建。`
                : remediation.auto_reassess ? "，正在等待自动复评。" : "。"}
            </div>
          )}

          {remediation.error && (
            <div className="mt-3 flex items-start gap-2 rounded-lg border border-bad/25 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span>{remediation.error}</span>
            </div>
          )}
        </section>
      )}

      {actionable && !parentRevisionId && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
          当前生成任务未关联已应用的反推版本，只能查看评估结果。
        </div>
      )}

      {assessment?.correction_revision_id && (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-good/30 bg-good/10 px-3 py-2 text-xs text-good">
          <CheckCircle2 size={15} aria-hidden="true" />
          修正版本 #{assessment.correction_revision_id} 已创建，原结果保持不变。
        </div>
      )}

      {remediationNotice && (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-good/30 bg-good/10 px-3 py-2 text-xs text-good" role="status">
          <CheckCircle2 size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>{remediationNotice}</span>
        </div>
      )}
      {remediationError && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-bad/25 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">
          <span>{remediationError}</span>
          {remediation?.id && (
            <button
              type="button"
              className="btn-ghost btn-sm"
              disabled={Boolean(busyAction)}
              onClick={() => refreshRemediation()}
            >
              刷新恢复
            </button>
          )}
        </div>
      )}

      {(assessment?.warnings || []).map((warning) => (
        <div key={warning} className="mt-3 rounded-lg border border-warn/25 bg-warn/10 px-3 py-2 text-xs text-warn">{warning}</div>
      ))}
      {(error || assessment?.error) && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-lg border border-bad/25 bg-bad/10 px-3 py-2 text-xs text-bad">
          <span>{error || assessment.error}</span>
          {assessment && isReproductionAssessmentTerminal(assessment) && (
            <button
              type="button"
              className="btn-ghost btn-sm"
              disabled={busyAction === "create"}
              onClick={() => startAssessment({ retry: true })}
            >
              重试
            </button>
          )}
        </div>
      )}
    </section>
  );
}
