"use client";

import {
  Captions,
  CheckCircle2,
  Clapperboard,
  Download,
  FolderOpen,
  LoaderCircle,
  Music2,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, downloadBlob } from "../../lib/api";
import { unifiedAssetKey } from "../../lib/unifiedAssets";
import StudioVideoCompositionShot from "./StudioVideoCompositionShot";
import {
  buildVideoCompositionInput,
  normalizeVideoComposition,
  subtitlesFromStoryboard,
  videoCompositionDuration,
  videoCompositionCapabilityStatus,
  videoCompositionProblems,
  videoCompositionWorkflowRecord,
  videoCompositionWorkflowProgress,
} from "./videoComposition";

const ACTIVE_WORKFLOW_STATUSES = new Set(["queued", "running", "waiting_review", "compensating"]);
const TERMINAL_WORKFLOW_STATUSES = new Set(["succeeded", "failed", "canceled"]);
const CANVAS_PRESETS = [
  ["1280x720", "横版 16:9"],
  ["720x1280", "竖版 9:16"],
  ["1080x1080", "方形 1:1"],
];
const NODE_LABELS = { compose: "视频合成", export: "导出成片" };

function requestId(prefix) {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function finite(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function assetSnapshot(asset) {
  const assetRef = unifiedAssetKey(asset);
  if (!assetRef) return null;
  return {
    asset_ref: assetRef,
    generation_task_id: Number.isSafeInteger(Number(asset.generation_task_id ?? asset.task_id))
      && Number(asset.generation_task_id ?? asset.task_id) > 0
      ? Number(asset.generation_task_id ?? asset.task_id)
      : null,
    id: asset.id ?? null,
    origin: asset.origin || null,
    type: asset.type || null,
    filename: asset.filename || null,
    preview_url: asset.preview_url || null,
    thumb: asset.thumb || null,
    url: asset.url || null,
  };
}

function workflowStatusLabel(status) {
  return {
    queued: "等待执行",
    running: "正在合成",
    waiting_review: "等待确认",
    compensating: "正在回收失败产物",
    succeeded: "导出完成",
    failed: "合成失败",
    canceled: "已取消",
    submitting: "正在提交",
  }[status] || "尚未提交";
}

function nodeStatusLabel(status) {
  return {
    queued: "等待",
    running: "处理中",
    waiting_external: "等待外部结果",
    waiting_review: "待确认",
    succeeded: "完成",
    failed: "失败",
    canceled: "取消",
  }[status] || status || "未知";
}

export default function StudioVideoComposition({
  result = {},
  storyboardShots = [],
  operation = null,
  busyAction = "",
  onChange,
  onPickAsset,
  requestQuoteConfirmation,
}) {
  const operationId = Number(operation?.id || operation?.operation_id || result?.operation_id || 0);
  const storedWorkflow = result.video_composition_workflow && typeof result.video_composition_workflow === "object"
    ? result.video_composition_workflow
    : {};
  const draft = useMemo(() => normalizeVideoComposition(
    result.video_composition,
    storyboardShots,
    operationId,
  ), [result.video_composition, storyboardShots, operationId]);
  const [run, setRun] = useState(null);
  const [error, setError] = useState("");
  const [saveState, setSaveState] = useState("");
  const [action, setAction] = useState("");
  const resultRef = useRef(result);
  const draftRef = useRef(draft);
  const workflowRef = useRef(storedWorkflow);
  const saveSequenceRef = useRef(0);
  const persistedRunStateRef = useRef("");
  resultRef.current = result;
  draftRef.current = draft;

  const currentWorkflow = videoCompositionWorkflowRecord(run, storedWorkflow);
  workflowRef.current = currentWorkflow;
  const runId = Number(currentWorkflow.run_id || 0);
  const status = currentWorkflow.status || "";
  const output = currentWorkflow.output || null;
  const capability = videoCompositionCapabilityStatus(run || currentWorkflow);
  const problems = videoCompositionProblems(draft);
  const duration = videoCompositionDuration(draft);
  const busy = Boolean(action || busyAction || ACTIVE_WORKFLOW_STATUSES.has(status));

  useEffect(() => {
    setRun(null);
    setError("");
    setSaveState("");
    persistedRunStateRef.current = "";
  }, [operationId]);

  function applyLocal(nextDraft, nextWorkflow = workflowRef.current) {
    const nextResult = {
      ...resultRef.current,
      video_composition: nextDraft,
      video_composition_workflow: nextWorkflow || {},
    };
    resultRef.current = nextResult;
    draftRef.current = nextDraft;
    workflowRef.current = nextWorkflow || {};
    onChange?.(nextResult);
    return nextResult;
  }

  async function persist(nextDraft = draftRef.current, nextWorkflow = workflowRef.current, editAction = "save") {
    const payload = applyLocal(nextDraft, nextWorkflow);
    if (!Number.isInteger(operationId) || operationId <= 0) {
      throw new Error("当前结果缺少可持久化的反推任务，无法保存合成工程");
    }
    const sequence = ++saveSequenceRef.current;
    setSaveState("saving");
    try {
      const revision = await api.createReverseOperationRevision(operationId, {
        source: "user_edit",
        payload: {
          ...payload,
          edit_metadata: { action: `video_composition_${editAction}` },
        },
      });
      if (sequence === saveSequenceRef.current) setSaveState("saved");
      return revision;
    } catch (exception) {
      if (sequence === saveSequenceRef.current) setSaveState("error");
      throw exception;
    }
  }

  function updateDraft(updater, { invalidateWorkflow = true } = {}) {
    const current = draftRef.current;
    const currentWorkflowState = workflowRef.current || {};
    if (ACTIVE_WORKFLOW_STATUSES.has(currentWorkflowState.status)) {
      return { draft: current, workflow: currentWorkflowState, blocked: true };
    }
    const candidate = typeof updater === "function" ? updater(current) : updater;
    const next = normalizeVideoComposition(candidate, storyboardShots, operationId);
    const nextWorkflow = invalidateWorkflow && (
      currentWorkflowState.run_id
      || currentWorkflowState.status
      || currentWorkflowState.output
    ) ? {} : currentWorkflowState;
    if (nextWorkflow !== currentWorkflowState) {
      setRun({});
      persistedRunStateRef.current = "";
    }
    applyLocal(next, nextWorkflow);
    return { draft: next, workflow: nextWorkflow, blocked: false };
  }

  async function commitDraft(updater, editAction) {
    const transition = updateDraft(updater, { invalidateWorkflow: editAction !== "manual_save" });
    if (transition.blocked) return;
    setAction(editAction);
    setError("");
    try {
      await persist(transition.draft, transition.workflow, editAction);
    } catch (exception) {
      setError(exception?.message || "合成工程保存失败");
    } finally {
      setAction("");
    }
  }

  function pickShotAsset(index) {
    const shot = draftRef.current.shots[index];
    onPickAsset?.({
      role: "storyboard_shot",
      mediaType: "video",
      selected: shot?.asset ? [shot.asset] : [],
      onConfirm: (assets) => {
        const asset = assetSnapshot(assets?.[0]);
        if (!asset) return;
        commitDraft((current) => ({
          ...current,
          shots: current.shots.map((item, shotIndex) => (
            shotIndex === index
              ? {
                  ...item,
                  asset_ref: asset.asset_ref,
                  generation_task_id: asset.generation_task_id,
                  asset,
                }
              : item
          )),
        }), `bind_shot_${index + 1}`);
      },
    });
  }

  function pickAudioTrack(index = -1) {
    const track = index >= 0 ? draftRef.current.audio_tracks[index] : null;
    onPickAsset?.({
      role: "storyboard_audio",
      mediaType: "video",
      selected: track?.asset ? [track.asset] : [],
      onConfirm: (assets) => {
        const asset = assetSnapshot(assets?.[0]);
        if (!asset) return;
        commitDraft((current) => {
          const nextTrack = {
            asset_ref: asset.asset_ref,
            asset,
            start_seconds: track?.start_seconds || 0,
            source_start_seconds: track?.source_start_seconds || 0,
            source_end_seconds: track?.source_end_seconds ?? null,
            volume: track?.volume ?? 1,
            loop: track?.loop ?? true,
          };
          return {
            ...current,
            audio_tracks: index >= 0
              ? current.audio_tracks.map((item, itemIndex) => itemIndex === index ? nextTrack : item)
              : [...current.audio_tracks, nextTrack].slice(0, 8),
          };
        }, index >= 0 ? `replace_audio_${index + 1}` : "add_audio");
      },
    });
  }

  async function refreshRun(id = runId, persistTerminal = true) {
    if (!id) return null;
    try {
      const nextRun = await api.workflowRun(id);
      setRun(nextRun);
      const terminalKey = `${nextRun.id}:${nextRun.status}`;
      if (
        persistTerminal
        && TERMINAL_WORKFLOW_STATUSES.has(nextRun.status)
        && persistedRunStateRef.current !== terminalKey
      ) {
        persistedRunStateRef.current = terminalKey;
        await persist(
          draftRef.current,
          videoCompositionWorkflowRecord(nextRun, workflowRef.current),
          "workflow_terminal",
        );
      }
      return nextRun;
    } catch (exception) {
      setError(exception?.message || "合成任务状态加载失败");
      return null;
    }
  }

  useEffect(() => {
    if (!runId) return undefined;
    let stopped = false;
    let timer = null;
    const tick = async () => {
      const nextRun = await refreshRun(runId);
      if (!stopped && nextRun && ACTIVE_WORKFLOW_STATUSES.has(nextRun.status)) {
        timer = window.setTimeout(tick, 1800);
      }
    };
    tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [runId]);

  async function startComposition() {
    setAction("submit");
    setError("");
    try {
      const composition = buildVideoCompositionInput(draftRef.current);
      const previousWorkflow = workflowRef.current || {};
      const clientRequestId = (
        !runId && previousWorkflow.client_request_id && previousWorkflow.status === "submitting"
          ? previousWorkflow.client_request_id
          : requestId("storyboard-compose")
      );
      const intent = {
        client_request_id: clientRequestId,
        status: "submitting",
        run_id: null,
        output: null,
        error: null,
        updated_at: new Date().toISOString(),
      };
      const workflowRequest = {
        tool_slug: "storyboard-compose",
        client_request_id: clientRequestId,
        input: { composition },
      };
      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("视频合成服务不可用，本次未提交。");
      }
      const confirmation = await requestQuoteConfirmation({
        kind: "workflow",
        request: workflowRequest,
        clientRequestId,
        execute: async ({ request }) => {
          setRun(intent);
          await persist(draftRef.current, intent, "workflow_submit_intent");
          return api.createWorkflowRun(request);
        },
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("视频合成提交失败。");
        }
        return;
      }
      const nextRun = confirmation.result;
      setRun(nextRun);
      await persist(
        draftRef.current,
        videoCompositionWorkflowRecord(nextRun, { client_request_id: clientRequestId }),
        "workflow_created",
      );
    } catch (exception) {
      setError(exception?.message || "分镜合成提交失败");
    } finally {
      setAction("");
    }
  }

  async function cancelRun() {
    if (!runId || !window.confirm("确认取消当前合成任务？已生成的中间产物会按工作流补偿规则回收。")) return;
    setAction("cancel");
    setError("");
    try {
      const nextRun = await api.cancelWorkflowRun(runId);
      setRun(nextRun);
      await persist(
        draftRef.current,
        videoCompositionWorkflowRecord(nextRun, workflowRef.current),
        "workflow_cancel",
      );
    } catch (exception) {
      setError(exception?.message || "取消合成任务失败");
    } finally {
      setAction("");
    }
  }

  async function retryRun() {
    const failedNode = (run?.nodes || []).find((node) => node.status === "failed");
    if (!runId || !failedNode?.key) return;
    setAction("retry");
    setError("");
    try {
      const nextRun = await api.retryWorkflowNode(runId, failedNode.key, {
        reason: "studio_video_composition_retry",
      });
      setRun(nextRun);
      persistedRunStateRef.current = "";
      await persist(
        draftRef.current,
        videoCompositionWorkflowRecord(nextRun, workflowRef.current),
        "workflow_retry",
      );
    } catch (exception) {
      setError(exception?.message || "重试合成节点失败");
    } finally {
      setAction("");
    }
  }

  async function downloadResult() {
    if (!output?.download_url) return;
    setAction("download");
    setError("");
    try {
      await downloadBlob(output.download_url, output.filename || `${draft.title || "storyboard-export"}.mp4`);
    } catch (exception) {
      setError(exception?.message || "成片下载失败");
    } finally {
      setAction("");
    }
  }

  return (
    <section className="mt-6 border-t border-line pt-4" aria-labelledby="video-composition-title">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-aqua">
            <Clapperboard size={16} aria-hidden="true" />
            <h4 id="video-composition-title" className="text-sm font-display font-semibold text-snow">合成与导出</h4>
          </div>
          <p className="mt-1 text-xs text-fog">{draft.shots.length} 个镜头 · 预计 {duration.toFixed(1)} 秒 · {draft.canvas.width}x{draft.canvas.height}</p>
        </div>
        <div className="flex items-center gap-2">
          {saveState && <span className={`text-[11px] ${saveState === "error" ? "text-bad" : "text-fog"}`} role="status">
            {saveState === "saving" ? "正在保存" : saveState === "saved" ? "工程已保存" : "保存失败"}
          </span>}
          <button type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => commitDraft((current) => current, "manual_save")}>
            <Save size={14} aria-hidden="true" /> 保存工程
          </button>
        </div>
      </header>

      {error && <p className="mt-3 border-l-2 border-bad bg-bad/[0.08] px-3 py-2 text-xs text-bad" role="alert">{error}</p>}
      {capability && (
        <p className="mt-3 border-l-2 border-warn bg-warn/[0.06] px-3 py-2 text-xs text-warn" role="status">
          {capability.status === "unsupported"
            ? "当前服务器未配置 FFmpeg/FFprobe，暂时无法合成与导出视频。"
            : "当前视频合成能力处于降级状态，请根据失败节点信息重试。"}
        </p>
      )}
      {problems.length > 0 && (
        <p className="mt-3 border-l-2 border-warn bg-warn/[0.06] px-3 py-2 text-xs text-warn" role="status">
          {problems.join("；")}
        </p>
      )}

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <label className="lg:col-span-2">
          <span className="label mb-1">成片名称</span>
          <input className="input min-h-10 px-2.5 py-1.5 text-xs" maxLength={128} value={draft.title} disabled={busy} onChange={(event) => updateDraft((current) => ({ ...current, title: event.target.value }))} />
        </label>
        <label>
          <span className="label mb-1">画布</span>
          <select
            className="select min-h-10 px-2 py-1.5 text-xs"
            value={`${draft.canvas.width}x${draft.canvas.height}`}
            disabled={busy}
            onChange={(event) => {
              const [width, height] = event.target.value.split("x").map(Number);
              updateDraft((current) => ({ ...current, canvas: { ...current.canvas, width, height } }));
            }}
          >
            {CANVAS_PRESETS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label>
          <span className="label mb-1">帧率</span>
          <select className="select min-h-10 px-2 py-1.5 text-xs" value={draft.canvas.fps} disabled={busy} onChange={(event) => updateDraft((current) => ({ ...current, canvas: { ...current.canvas, fps: Number(event.target.value) } }))}>
            {[24, 25, 30, 50, 60].map((fps) => <option key={fps} value={fps}>{fps} FPS</option>)}
          </select>
        </label>
        <label>
          <span className="label mb-1">背景色</span>
          <input type="color" className="input min-h-10 w-full cursor-pointer p-1" value={draft.canvas.background_color.slice(0, 7)} disabled={busy} onChange={(event) => updateDraft((current) => ({ ...current, canvas: { ...current.canvas, background_color: event.target.value } }))} />
        </label>
      </div>

      <ol className="mt-4" aria-label="合成镜头素材">
        {draft.shots.map((shot, index) => (
          <StudioVideoCompositionShot
            key={shot.shot_id}
            draft={shot}
            storyboardShot={storyboardShots[index] || {}}
            index={index}
            busy={busy}
            onPick={() => pickShotAsset(index)}
            onClear={() => commitDraft((current) => ({
              ...current,
              shots: current.shots.map((item, itemIndex) => itemIndex === index
                ? { ...item, asset_ref: "", generation_task_id: null, asset: null }
                : item),
            }), `clear_shot_${index + 1}`)}
            onChange={(patch) => updateDraft((current) => ({
              ...current,
              shots: current.shots.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item),
            }))}
          />
        ))}
      </ol>

      <details className="border-t border-line py-3">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-display font-semibold text-mist">
          <Captions size={15} aria-hidden="true" /> 字幕 · {draft.subtitles.length}
        </summary>
        <div className="mt-3 space-y-2">
          {draft.subtitles.map((subtitle, index) => (
            <div key={`${index}-${subtitle.start_seconds}`} className="grid grid-cols-[5rem_5rem_1fr_auto] gap-2">
              <input aria-label={`字幕 ${index + 1} 开始时间`} type="number" min="0" step="0.1" className="input min-h-10 px-2 text-xs" value={subtitle.start_seconds} onChange={(event) => updateDraft((current) => ({ ...current, subtitles: current.subtitles.map((item, itemIndex) => itemIndex === index ? { ...item, start_seconds: finite(event.target.value) } : item) }))} />
              <input aria-label={`字幕 ${index + 1} 结束时间`} type="number" min="0.1" step="0.1" className="input min-h-10 px-2 text-xs" value={subtitle.end_seconds} onChange={(event) => updateDraft((current) => ({ ...current, subtitles: current.subtitles.map((item, itemIndex) => itemIndex === index ? { ...item, end_seconds: finite(event.target.value) } : item) }))} />
              <input aria-label={`字幕 ${index + 1} 内容`} className="input min-h-10 min-w-0 px-2 text-xs" maxLength={500} value={subtitle.text} onChange={(event) => updateDraft((current) => ({ ...current, subtitles: current.subtitles.map((item, itemIndex) => itemIndex === index ? { ...item, text: event.target.value } : item) }))} />
              <button type="button" className="icon-btn h-10 w-10 text-fog hover:text-bad" aria-label={`删除字幕 ${index + 1}`} onClick={() => updateDraft((current) => ({ ...current, subtitles: current.subtitles.filter((_, itemIndex) => itemIndex !== index) }))}><Trash2 size={14} aria-hidden="true" /></button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn-secondary btn-sm" disabled={busy || draft.subtitles.length >= 200} onClick={() => updateDraft((current) => ({ ...current, subtitles: [...current.subtitles, { start_seconds: 0, end_seconds: 1, text: "字幕", font_size: 42, text_color: "#ffffff", background_color: "#000000b8", bottom_margin: 48 }] }))}><Plus size={14} aria-hidden="true" /> 添加字幕</button>
            <button type="button" className="btn-secondary btn-sm" disabled={busy} onClick={() => updateDraft((current) => ({ ...current, subtitles: subtitlesFromStoryboard(storyboardShots, current) }))}><RefreshCw size={14} aria-hidden="true" /> 从分镜生成</button>
          </div>
        </div>
      </details>

      <details className="border-t border-line py-3">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-display font-semibold text-mist">
          <Music2 size={15} aria-hidden="true" /> 声音与混音 · {draft.audio_tracks.length} 条背景音轨
        </summary>
        <div className="mt-3">
          <label className="block">
            <span className="label mb-1">镜头原声音量 · {Math.round(draft.original_audio_volume * 100)}%</span>
            <input type="range" min="0" max="2" step="0.05" className="h-10 w-full accent-aqua" value={draft.original_audio_volume} onChange={(event) => updateDraft((current) => ({ ...current, original_audio_volume: finite(event.target.value, 1) }))} />
          </label>
          {draft.audio_tracks.map((track, index) => (
            <div key={`${track.asset_ref}-${index}`} className="mt-2 grid grid-cols-[1fr_auto] items-center gap-3 border-t border-line pt-2">
              <div className="min-w-0">
                <p className="truncate text-xs text-mist">{track.asset?.filename || track.asset_ref}</p>
                <label className="mt-1 block text-[11px] text-fog">音量 {Math.round(track.volume * 100)}%
                  <input type="range" min="0" max="4" step="0.05" className="mt-1 h-8 w-full accent-aqua" value={track.volume} onChange={(event) => updateDraft((current) => ({ ...current, audio_tracks: current.audio_tracks.map((item, itemIndex) => itemIndex === index ? { ...item, volume: finite(event.target.value, 1) } : item) }))} />
                </label>
              </div>
              <div className="flex gap-1">
                <button type="button" className="icon-btn h-9 w-9" aria-label={`替换背景音轨 ${index + 1}`} onClick={() => pickAudioTrack(index)}><FolderOpen size={14} aria-hidden="true" /></button>
                <button type="button" className="icon-btn h-9 w-9 text-fog hover:text-bad" aria-label={`删除背景音轨 ${index + 1}`} onClick={() => commitDraft((current) => ({ ...current, audio_tracks: current.audio_tracks.filter((_, itemIndex) => itemIndex !== index) }), `remove_audio_${index + 1}`)}><Trash2 size={14} aria-hidden="true" /></button>
              </div>
            </div>
          ))}
          <button type="button" className="btn-secondary btn-sm mt-2" disabled={busy || draft.audio_tracks.length >= 8} title="当前后端从视频素材中提取可用音轨" onClick={() => pickAudioTrack()}><Plus size={14} aria-hidden="true" /> 添加含音频视频</button>
        </div>
      </details>

      <div className="border-t border-line pt-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-display font-semibold text-mist">{workflowStatusLabel(status)}</p>
            {runId ? <p className="mt-0.5 text-[11px] text-fog">工作流 #{runId} · 进度 {videoCompositionWorkflowProgress(run || currentWorkflow)}%</p> : <p className="mt-0.5 text-[11px] text-fog">提交后依次执行视频合成和导出节点</p>}
          </div>
          <div className="flex flex-wrap gap-2">
            {ACTIVE_WORKFLOW_STATUSES.has(status) && <button type="button" className="btn-secondary btn-sm text-bad" disabled={busy} onClick={cancelRun}><XCircle size={14} aria-hidden="true" /> 取消</button>}
            {status === "failed" && <button type="button" className="btn-secondary btn-sm" disabled={busy || !(run?.nodes || []).some((node) => node.status === "failed")} onClick={retryRun}><RefreshCw size={14} aria-hidden="true" /> 重试失败节点</button>}
            {output?.download_url && <button type="button" className="btn-primary btn-sm" disabled={busy} onClick={downloadResult}><Download size={14} aria-hidden="true" /> 下载成片</button>}
            {!ACTIVE_WORKFLOW_STATUSES.has(status) && status !== "succeeded" && <button type="button" className="btn-primary btn-sm" disabled={busy || problems.length > 0} onClick={startComposition}>{action === "submit" ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Clapperboard size={14} aria-hidden="true" />} 合成并导出</button>}
          </div>
        </div>
        {Array.isArray(run?.nodes) && run.nodes.length > 0 && (
          <ol className="mt-3 grid gap-2 sm:grid-cols-2" aria-label="合成工作流节点">
            {run.nodes.map((node) => (
              <li key={node.key} className="flex items-start gap-2 border-l-2 border-line px-3 py-2">
                {node.status === "succeeded" ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-good" aria-hidden="true" /> : node.status === "failed" ? <XCircle size={15} className="mt-0.5 shrink-0 text-bad" aria-hidden="true" /> : <LoaderCircle size={15} className={node.status === "running" ? "mt-0.5 shrink-0 animate-spin text-aqua" : "mt-0.5 shrink-0 text-fog"} aria-hidden="true" />}
                <div className="min-w-0">
                  <p className="text-xs text-mist">{NODE_LABELS[node.key] || node.key} · {nodeStatusLabel(node.status)}</p>
                  {(node.error || node.error_code) && <p className="mt-0.5 break-words text-[11px] text-bad">{node.error || node.error_code}</p>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}
