"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Download,
  ExternalLink,
  FileSearch,
  ImagePlus,
  LoaderCircle,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Workflow,
  X,
  XCircle,
} from "lucide-react";
import { api, downloadBlob } from "../lib/api";
import { formatLocalDateTime } from "../lib/datetime";
import { useToast } from "./ToastProvider";
import {
  createUnifiedTaskRequestId,
  prepareCompatibleRetryRequest,
  taskAssetId,
  taskAssetRef,
  taskDetailHref,
  taskKindLabel,
  taskStatusClass,
  taskStatusLabel,
  workflowNodeKeyForAction,
} from "../lib/unifiedTasks";

const KIND_OPTIONS = [
  ["all", "全部"],
  ["generation", "生成"],
  ["reverse", "反推"],
  ["parse", "解析"],
  ["workflow", "工作流"],
];
const STATUS_OPTIONS = [
  ["all", "全部状态"],
  ["active", "处理中"],
  ["needs_attention", "待确认"],
  ["succeeded", "已完成"],
  ["failed", "失败"],
  ["canceled", "已取消"],
];

function taskActionLabel(action, kind = "") {
  if (kind === "workflow" && action === "review") return "审核并继续";
  if (kind === "workflow" && action === "retry") return "重试失败节点";
  if (kind === "reverse" && action === "retry") return "当前模型重试";
  return {
    view: "查看",
    cancel: "取消",
    retry_requote: "当前模型重试",
    retry_compatible_model: "切换模型重试",
    confirm: "确认封面",
    review: "查看状态",
    resume: "继续执行",
    apply: "打开 Studio",
    save_recipe: "查看配方",
    create_similar: "继续创作",
    download: "下载",
    use_assets: "查看素材",
  }[action] || action;
}

function TaskActionIcon({ action, size = 14 }) {
  const Icon = {
    cancel: XCircle,
    retry: RotateCcw,
    retry_requote: RefreshCw,
    retry_compatible_model: RefreshCw,
    confirm: CheckCircle2,
    download: Download,
    create_similar: ImagePlus,
    apply: Sparkles,
    save_recipe: Sparkles,
    use_assets: ImagePlus,
    view: ExternalLink,
    review: FileSearch,
    resume: PlayCircle,
  }[action] || ChevronRight;
  return <Icon size={size} aria-hidden="true" />;
}

function formatCredits(value) {
  const credits = Number(value);
  return Number.isFinite(credits) ? credits.toLocaleString("zh-CN") : "-";
}

function FailureSuggestion({ task }) {
  const suggestion = task.failure_suggestion;
  if (!suggestion) return null;
  return (
    <div className="mt-3 border-l-2 border-warn bg-warn/10 px-3 py-2" role="status">
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 shrink-0 text-warn" size={14} aria-hidden="true" />
        <div className="min-w-0">
          <p className="text-xs font-medium text-warn">{suggestion.title}</p>
          <p className="mt-1 break-words text-xs leading-5 text-mist">{suggestion.message}</p>
          <p className="mt-1 text-[11px] text-fog">建议操作：{taskActionLabel(suggestion.recommended_action, task.kind)}</p>
        </div>
      </div>
    </div>
  );
}

export function UnifiedTaskFilters({ center, compact = false }) {
  return (
    <div className={`flex ${compact ? "gap-2" : "flex-wrap gap-2"}`}>
      <label className="sr-only" htmlFor={compact ? "global-task-kind" : "history-task-kind"}>任务类型</label>
      <select
        id={compact ? "global-task-kind" : "history-task-kind"}
        value={center.filters.kind}
        onChange={(event) => center.setFilters({ kind: event.target.value })}
        className="select h-9 min-w-0 py-1.5 text-xs"
      >
        {KIND_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      <label className="sr-only" htmlFor={compact ? "global-task-status" : "history-task-status"}>任务状态</label>
      <select
        id={compact ? "global-task-status" : "history-task-status"}
        value={center.filters.status}
        onChange={(event) => center.setFilters({ status: event.target.value })}
        className="select h-9 min-w-0 py-1.5 text-xs"
      >
        {STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
      {!compact && (
        <>
          <label className="sr-only" htmlFor="history-task-category">素材类型</label>
          <select
            id="history-task-category"
            value={center.filters.category}
            onChange={(event) => center.setFilters({ category: event.target.value })}
            className="select h-9 min-w-0 py-1.5 text-xs"
          >
            <option value="all">图片与视频</option>
            <option value="image">仅图片</option>
            <option value="video">仅视频</option>
          </select>
        </>
      )}
    </div>
  );
}

export function UnifiedTaskList({ center, variant = "drawer", onNavigate, onCreateSimilar }) {
  const router = useRouter();
  const notify = useToast();
  const [busyKey, setBusyKey] = useState("");
  const [retrySelections, setRetrySelections] = useState({});
  const busyRef = useRef("");
  const sameModelRetryIdsRef = useRef(new Map());

  const navigate = (href) => {
    onNavigate?.();
    router.push(href);
  };

  function selectedCompatibleCandidate(task) {
    const candidates = task.compatible_retry_models || [];
    const selectedId = retrySelections[task.key]?.model_config_id;
    return candidates.find((candidate) => candidate.model_config_id === selectedId) || candidates[0] || null;
  }

  function selectCompatibleModel(task, modelConfigId) {
    setRetrySelections((current) => {
      const next = prepareCompatibleRetryRequest(task, modelConfigId, current[task.key]);
      return next ? { ...current, [task.key]: next } : current;
    });
  }

  function sameModelRetryRequestId(task, action) {
    const key = `${task.key}:${action}`;
    const existing = sameModelRetryIdsRef.current.get(key);
    if (existing) return existing;
    const prefix = task.kind === "reverse" ? "reverse-retry" : "generation-retry";
    const requestId = createUnifiedTaskRequestId(prefix);
    sameModelRetryIdsRef.current.set(key, requestId);
    return requestId;
  }

  async function quoteReverseRetry(task, clientRequestId, modelConfigId = null) {
    const request = {
      reverse_operation_id: Number(task.id),
      client_request_id: clientRequestId,
      ...(Number.isSafeInteger(Number(modelConfigId)) && Number(modelConfigId) > 0
        ? { model_config_id: Number(modelConfigId) }
        : {}),
    };
    const quote = await api.studioQuote({
      kind: "reverse",
      client_request_id: clientRequestId,
      request,
    });
    const quoteId = Number(quote?.quote_id);
    if (!Number.isSafeInteger(quoteId) || quoteId <= 0) {
      throw new Error("反推重试计费校验失败，请稍后重试");
    }
    return { request, quote_id: quoteId };
  }

  async function runCompatibleRetry(task) {
    if (busyRef.current || busyKey) return;
    const candidate = selectedCompatibleCandidate(task);
    if (!candidate) {
      notify.error("当前没有可用的兼容模型，请刷新任务后重试");
      return;
    }
    const request = prepareCompatibleRetryRequest(
      task,
      candidate.model_config_id,
      retrySelections[task.key],
    );
    if (!request) {
      notify.error("重试模型已失效，请重新选择");
      return;
    }
    setRetrySelections((current) => ({ ...current, [task.key]: request }));
    const busy = `${task.key}:retry_compatible_model`;
    busyRef.current = busy;
    setBusyKey(busy);
    try {
      if (task.kind === "generation") {
        await api.requoteRetryTask(task.id, {
          client_request_id: request.client_request_id,
          model_config_id: candidate.model_config_id,
        });
      } else if (task.kind === "reverse") {
        const quoted = await quoteReverseRetry(
          task,
          request.client_request_id,
          candidate.model_config_id,
        );
        await api.retryReverseOperation(task.id, {
          client_request_id: quoted.request.client_request_id,
          model_config_id: quoted.request.model_config_id,
          quote_id: quoted.quote_id,
        });
      } else {
        throw new Error("当前任务类型不支持切换模型重试");
      }
      await center.refresh({ silent: true });
      const currentModelId = Number(task.model_config_id);
      const switchedModel = Number.isSafeInteger(currentModelId)
        && currentModelId > 0
        && currentModelId !== candidate.model_config_id;
      notify.success(switchedModel
        ? `已切换至「${candidate.display_name}」并创建新的重试任务`
        : `已使用当前模型「${candidate.display_name}」创建新的重试任务`);
      setRetrySelections((current) => {
        const next = { ...current };
        delete next[task.key];
        return next;
      });
    } catch (actionError) {
      notify.error(actionError?.message || "切换模型重试失败");
    } finally {
      if (busyRef.current === busy) busyRef.current = "";
      setBusyKey("");
    }
  }

  async function runAction(task, action) {
    const busy = `${task.key}:${action}`;
    const sameModelRetryKey = (
      (action === "retry" && task.kind === "reverse")
      || (action === "retry_requote" && task.kind === "generation")
    ) ? busy : "";
    if (busyRef.current || busyKey) return;
    if (action === "retry_compatible_model") return runCompatibleRetry(task);
    if (action === "view" || (action === "review" && task.kind !== "workflow")) {
      return navigate(taskDetailHref(task));
    }
    if (action === "apply" || (action === "create_similar" && !onCreateSimilar)) return navigate(`/?task=${encodeURIComponent(task.key)}`);
    if (action === "save_recipe") return navigate("/prompts");
    if (action === "use_assets") return navigate("/profile");
    if (action === "cancel") {
      const wording = task.status === "running" ? "确认提交取消请求？" : "确认取消这个任务？";
      if (!window.confirm(wording)) return;
    }
    if (action === "review" && task.kind === "workflow") {
      if (!window.confirm("确认审核通过当前节点并继续执行工作流？")) return;
    }
    if (action === "retry" && task.kind === "workflow") {
      if (!window.confirm("确认仅重试当前失败节点？已完成节点不会重复执行。")) return;
    }
    busyRef.current = busy;
    setBusyKey(busy);
    try {
      if (action === "create_similar" && onCreateSimilar) {
        const handled = await onCreateSimilar(task);
        if (handled !== false) return;
        return navigate(`/?task=${encodeURIComponent(task.key)}`);
      }
      if (action === "cancel") {
        if (task.kind === "generation") await api.cancelTask(task.id);
        if (task.kind === "reverse") await api.cancelReverseOperation(task.id);
        if (task.kind === "workflow") await api.cancelWorkflowRun(task.id);
      } else if (action === "retry") {
        if (task.kind === "reverse") {
          const clientRequestId = sameModelRetryRequestId(task, action);
          const quoted = await quoteReverseRetry(task, clientRequestId);
          await api.retryReverseOperation(task.id, {
            client_request_id: quoted.request.client_request_id,
            quote_id: quoted.quote_id,
          });
        }
        if (task.kind === "workflow") {
          const nodeKey = workflowNodeKeyForAction(task, "retry");
          if (!nodeKey) throw new Error("未找到可重试的失败节点，请刷新后重试");
          await api.retryWorkflowNode(task.id, nodeKey, { reason: "task_center_retry" });
        }
      } else if (action === "retry_requote" && task.kind === "generation") {
        await api.requoteRetryTask(task.id, {
          client_request_id: sameModelRetryRequestId(task, action),
        });
      } else if (action === "confirm" && task.kind === "reverse") {
        await api.confirmReverseOperationCover(task.id);
      } else if (action === "resume" && task.kind === "workflow") {
        await api.resumeWorkflowRun(task.id);
      } else if (action === "review" && task.kind === "workflow") {
        const nodeKey = workflowNodeKeyForAction(task, "review");
        if (!nodeKey) throw new Error("未找到待审核节点，请刷新后重试");
        await api.reviewWorkflowNode(task.id, nodeKey, {
          decision: "approve",
          output: {},
          note: "task_center_approved",
        });
      } else if (action === "download") {
        const assetRef = taskAssetRef(task);
        if (!assetRef) throw new Error("当前任务没有可下载的结果素材");
        const generatedId = taskAssetId(assetRef);
        const normalizedRef = generatedId ? `g.${generatedId}` : assetRef;
        await downloadBlob(`/api/me/assets/download?asset_ref=${encodeURIComponent(normalizedRef)}`);
      }
      await center.refresh({ silent: true });
      if (sameModelRetryKey) sameModelRetryIdsRef.current.delete(sameModelRetryKey);
      const successMessage = action === "retry"
        ? task.kind === "workflow"
          ? "已提交失败节点重试"
          : "已使用当前模型创建新的反推重试任务"
        : action === "retry_requote"
          ? "已使用当前模型创建新的重试任务"
          : `${taskActionLabel(action, task.kind)}已提交`;
      notify.success(successMessage);
    } catch (actionError) {
      notify.error(actionError?.message || `${taskActionLabel(action, task.kind)}失败`);
    } finally {
      if (busyRef.current === busy) busyRef.current = "";
      setBusyKey("");
    }
  }

  if (center.loading && !center.items.length) {
    return <TaskSkeleton rows={variant === "drawer" ? 4 : 6} />;
  }
  if (center.error && !center.items.length) {
    return (
      <div role="alert" className="flex flex-col items-center gap-3 px-4 py-12 text-center">
        <XCircle className="text-bad" size={28} aria-hidden="true" />
        <p className="text-sm text-mist">{center.error}</p>
        <button type="button" className="btn-secondary btn-sm" onClick={() => center.refresh()}>重新加载</button>
      </div>
    );
  }
  if (!center.items.length) {
    return (
      <div className="flex flex-col items-center gap-3 px-4 py-14 text-center" role="status">
        <FileSearch className="text-fog" size={30} aria-hidden="true" />
        <p className="text-sm text-mist">没有符合条件的任务</p>
        <Link href="/" onClick={onNavigate} className="btn-primary btn-sm"><Sparkles size={14} aria-hidden="true" /> 去创作</Link>
      </div>
    );
  }

  return (
    <>
      <div className={variant === "drawer" ? "divide-y divide-line/70" : "space-y-3"}>
        {center.items.map((task) => {
          const isBusy = busyKey.startsWith(`${task.key}:`);
          const progress = Math.max(task.status_group === "active" ? 3 : 0, Math.min(100, Number(task.progress) || 0));
          const actions = task.available_actions || [];
          const compatibleCandidate = selectedCompatibleCandidate(task);
          const canRetryWithCompatibleModel = actions.includes("retry_compatible_model") && Boolean(compatibleCandidate);
          return (
            <article key={task.key} className={variant === "drawer" ? "px-4 py-4" : "card p-4"}>
              <div className="flex min-w-0 items-start gap-3">
                <TaskKindMark kind={task.kind} />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <h3 className="min-w-0 truncate text-sm font-display font-semibold text-snow">{task.title || taskKindLabel(task.kind)}</h3>
                    <span className={`badge ${taskStatusClass(task.status_group)}`}>{taskStatusLabel(task.status, task.status_group)}</span>
                    <span className="ml-auto shrink-0 text-[11px] text-fog">{formatLocalDateTime(task.created_at)}</span>
                  </div>
                  {task.summary && <p className="mt-1 line-clamp-2 break-words text-xs leading-5 text-mist">{task.summary}</p>}
                  {task.status_group === "active" && (
                    <div className="mt-2 flex items-center gap-2" aria-label={`进度 ${progress}%`}>
                      <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-white/10">
                        <span className="block h-full rounded-full bg-aqua transition-[width]" style={{ width: `${progress}%` }} />
                      </div>
                      <span className="w-8 text-right text-[11px] tabular-nums text-fog">{progress}%</span>
                    </div>
                  )}
                  <TaskMeta task={task} />
                  <FailureSuggestion task={task} />
                  {(task.error_message || task.status_group === "needs_attention") && (
                    <p className={`mt-2 text-xs leading-5 ${task.status_group === "needs_attention" ? "text-warn" : "text-bad"}`}>
                      {task.error_message || (task.kind === "workflow" ? "当前节点等待审核后继续" : "任务需要进一步确认")}
                    </p>
                  )}
                  {canRetryWithCompatibleModel && (
                    <div className="mt-3 border-y border-line py-3">
                      <label className="text-[11px] text-fog" htmlFor={`compatible-retry-${task.kind}-${task.id}`}>兼容重试模型</label>
                      <div className="mt-1.5 flex min-w-0 flex-wrap gap-2">
                        <select
                          id={`compatible-retry-${task.kind}-${task.id}`}
                          className="select h-9 min-w-0 flex-1 basis-48 py-1.5 text-xs"
                          value={compatibleCandidate.model_config_id}
                          onChange={(event) => selectCompatibleModel(task, event.target.value)}
                          disabled={isBusy}
                        >
                          {task.compatible_retry_models.map((candidate) => (
                            <option key={candidate.model_config_id} value={candidate.model_config_id}>
                              {candidate.display_name} · {formatCredits(candidate.estimated_credits)} 积分
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="btn-secondary btn-sm shrink-0"
                          onClick={() => runAction(task, "retry_compatible_model")}
                          disabled={isBusy}
                        >
                          {busyKey === `${task.key}:retry_compatible_model`
                            ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" />
                            : <TaskActionIcon action="retry_compatible_model" />}
                          切换并重试
                        </button>
                      </div>
                    </div>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2">
                    {actions.filter((action) => action !== "retry_compatible_model").map((action) => (
                      <button
                        type="button"
                        key={action}
                        onClick={() => runAction(task, action)}
                        disabled={isBusy}
                        className={action === "cancel" ? "btn-ghost btn-sm text-bad" : "btn-secondary btn-sm"}
                      >
                        {isBusy && busyKey === `${task.key}:${action}` ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <TaskActionIcon action={action} />}
                        {taskActionLabel(action, task.kind)}
                      </button>
                    ))}
                    {variant !== "drawer" && (task.result_refs || []).length > 1 && (
                      <span className="chip pointer-events-none">{task.result_refs.length} 个结果</span>
                    )}
                  </div>
                </div>
              </div>
            </article>
          );
        })}
        {center.hasMore && (
          <div className="py-4 text-center">
            <button type="button" className="btn-secondary btn-sm" onClick={center.loadMore} disabled={center.loadingMore}>
              {center.loadingMore ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
              {center.loadingMore ? "加载中" : "加载更多"}
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function TaskKindMark({ kind }) {
  const Icon = kind === "generation" ? Sparkles : kind === "reverse" ? PlayCircle : kind === "workflow" ? Workflow : FileSearch;
  const className = kind === "generation"
    ? "bg-iris/15 text-iris-300"
    : kind === "reverse"
      ? "bg-aqua/15 text-aqua"
      : kind === "workflow"
        ? "bg-warn/15 text-warn"
        : "bg-white/10 text-mist";
  return <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${className}`}><Icon size={16} aria-hidden="true" /></span>;
}

function TaskMeta({ task }) {
  const model = task.model_name || task.model_id;
  const label = task.category === "image" ? "图片" : task.category === "video" ? "视频" : "";
  const workflowNodeKey = task.workflow_current_node_key || task.workflow_failed_node_key;
  return (
    <div className="mt-2 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-[11px] text-fog">
      {label && <span>{label}</span>}
      {model && <span className="max-w-full truncate" title={model}>{model}</span>}
      {task.kind === "workflow" && workflowNodeKey ? <span title={`当前节点 ${workflowNodeKey}`}>节点 {workflowNodeKey}</span> : null}
      {task.cost_settled > 0 ? <span>结算 {task.cost_settled} 积分</span> : task.cost_frozen > 0 ? <span>冻结 {task.cost_frozen} 积分</span> : null}
      {task.retry_of_task_id ? (
        <Link
          href={`/history?task=${encodeURIComponent(`generation:${task.retry_of_task_id}`)}`}
          className="text-aqua hover:text-snow"
          title={`来源任务 #${task.retry_of_task_id}`}
        >
          重试自任务 #{task.retry_of_task_id}
        </Link>
      ) : null}
      {(task.project_ids || []).map((projectId) => (
        <Link key={projectId} href={`/projects?project=${projectId}`} className="text-aqua hover:text-snow" title={`项目 #${projectId}`}>项目 #{projectId}</Link>
      ))}
    </div>
  );
}

function TaskSkeleton({ rows }) {
  return <div className="space-y-3 p-4">{Array.from({ length: rows }, (_, index) => <div key={index} className="skeleton h-24" />)}</div>;
}

export default function GlobalTaskCenter({ center, open, onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    if (open) closeRef.current?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60]" role="presentation">
      <button type="button" className="absolute inset-0 cursor-default bg-black/55" onClick={onClose} aria-label="关闭任务中心" />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col border-l border-line bg-base shadow-pop" role="dialog" aria-modal="true" aria-label="任务中心">
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-display font-semibold text-snow">任务中心</h2>
            <p className="mt-0.5 text-xs text-fog">{center.total} 个任务{center.transport === "events" ? " · 实时更新" : " · 自动刷新"}</p>
          </div>
          <button type="button" className="btn-ghost btn-sm h-9 w-9 px-0" onClick={() => center.refresh()} aria-label="刷新任务"><RefreshCw size={16} aria-hidden="true" /></button>
          <button ref={closeRef} type="button" className="btn-ghost btn-sm h-9 w-9 px-0" onClick={onClose} aria-label="关闭任务中心"><X size={17} aria-hidden="true" /></button>
        </header>
        <div className="border-b border-line px-4 py-3"><UnifiedTaskFilters center={center} compact /></div>
        <div className="min-h-0 flex-1 overflow-y-auto"><UnifiedTaskList center={center} onNavigate={onClose} /></div>
        <footer className="border-t border-line px-4 py-3">
          <Link href="/history" onClick={onClose} className="btn-secondary btn-sm w-full"><FileSearch size={14} aria-hidden="true" /> 打开全部任务</Link>
        </footer>
      </aside>
    </div>
  );
}
