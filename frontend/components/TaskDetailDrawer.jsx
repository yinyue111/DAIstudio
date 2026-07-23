"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Coins,
  Download,
  ExternalLink,
  FileInput,
  FileOutput,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  X,
} from "lucide-react";
import { api, downloadBlob } from "../lib/api";
import { formatLocalDateTime } from "../lib/datetime";
import {
  parseUnifiedTaskKey,
  taskAssetId,
  taskKindLabel,
  taskStatusClass,
  taskStatusLabel,
} from "../lib/unifiedTasks";

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function compactRecord(value) {
  return Object.fromEntries(Object.entries(record(value)).filter(([, item]) => (
    item !== null
    && item !== undefined
    && item !== ""
    && (!Array.isArray(item) || item.length > 0)
    && (typeof item !== "object" || Array.isArray(item) || Object.keys(item).length > 0)
  )));
}

function statusGroup(status) {
  if (["queued", "running", "compensating", "waiting_external"].includes(status)) return "active";
  if (["succeeded", "done"].includes(status)) return "succeeded";
  if (status === "failed") return "failed";
  if (status === "canceled") return "canceled";
  return "needs_attention";
}

async function loadDetail(parsed) {
  if (parsed.kind === "generation") return api.task(parsed.id);
  if (parsed.kind === "reverse") return api.reverseOperation(parsed.id);
  if (parsed.kind === "parse") return api.parseStatus(parsed.id);
  return api.workflowRun(parsed.id);
}

function detailStatus(detail, summary) {
  return String(detail?.status || summary?.status || "unknown");
}

function detailInput(parsed, detail) {
  if (parsed.kind === "generation") {
    return compactRecord({
      prompt: detail.prompt_text,
      request_prompt: detail.request_prompt_text,
      generation_prompt: detail.generation_prompt_text,
      params: detail.params,
      category: detail.category,
      stage: detail.stage,
    });
  }
  if (parsed.kind === "reverse") {
    return compactRecord({
      target: detail.target,
      source_type: detail.source_type,
      analysis_focus: detail.analysis_focus,
      analysis_precision: detail.analysis_precision,
      output_purpose: detail.output_purpose,
      include_audio: detail.include_audio,
      source_ranges: detail.source_ranges,
      request_context: detail.request_context,
      workspace_snapshot_v3: detail.workspace_snapshot_v3,
    });
  }
  if (parsed.kind === "parse") return compactRecord({ url: detail.url });
  return compactRecord(detail.input);
}

function detailResult(parsed, detail) {
  if (parsed.kind === "generation") {
    return compactRecord({
      assets: detail.assets,
      final_task_id: detail.final_task_id,
      final_status: detail.final_status,
      partial: detail.partial,
      partial_errors: detail.partial_errors,
    });
  }
  if (parsed.kind === "reverse") {
    return compactRecord({ result: detail.result, video_analysis: detail.video_analysis });
  }
  if (parsed.kind === "parse") return compactRecord({ assets: detail.assets });
  return compactRecord({ output: detail.output, nodes: detail.nodes });
}

function collectAssetRefs(value, refs = new Set()) {
  if (Array.isArray(value)) {
    value.forEach((item) => collectAssetRefs(item, refs));
  } else if (value && typeof value === "object") {
    const assetRef = typeof value.asset_ref === "string" ? value.asset_ref.trim() : "";
    if (assetRef) refs.add(assetRef);
    Object.values(value).forEach((item) => collectAssetRefs(item, refs));
  }
  return refs;
}

function collectDownloadTargets(parsed, detail, summary) {
  const targets = new Map();
  const addAssetRef = (value) => {
    const assetRef = typeof value === "string" ? value.trim() : "";
    if (!assetRef) return;
    const generatedId = taskAssetId(assetRef);
    const normalizedRef = generatedId ? `g.${generatedId}` : assetRef;
    const key = generatedId ? `generated:${generatedId}` : `ref:${normalizedRef}`;
    targets.set(key, {
      key,
      label: generatedId ? `素材 #${generatedId}` : normalizedRef,
      path: `/api/me/assets/download?asset_ref=${encodeURIComponent(normalizedRef)}`,
    });
  };

  collectAssetRefs(detailResult(parsed, detail)).forEach(addAssetRef);
  (summary?.result_refs || []).forEach(addAssetRef);

  if (parsed.kind === "generation") {
    for (const asset of Array.isArray(detail.assets) ? detail.assets : []) {
      if (asset?.asset_ref) {
        addAssetRef(asset.asset_ref);
        continue;
      }
      const assetId = Number(asset?.id);
      if (!Number.isSafeInteger(assetId) || assetId <= 0) continue;
      const key = `generated:${assetId}`;
      if (!targets.has(key)) {
        targets.set(key, {
          key,
          label: `素材 #${assetId}`,
          path: `/api/assets/${assetId}/download`,
        });
      }
    }
  }

  return [...targets.values()];
}

function DetailJson({ title, icon: Icon, value, emptyText }) {
  const compact = compactRecord(value);
  const hasValue = Object.keys(compact).length > 0;
  return (
    <section className="border-t border-line py-5">
      <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-snow">
        <Icon size={15} aria-hidden="true" /> {title}
      </h3>
      {hasValue ? (
        <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-line bg-black/20 p-3 text-xs leading-5 text-mist">
          {JSON.stringify(compact, null, 2)}
        </pre>
      ) : <p className="mt-2 text-sm text-fog">{emptyText}</p>}
    </section>
  );
}

function MetaItem({ label, children }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-fog">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-mist">{children}</dd>
    </div>
  );
}

function recommendedActionLabel(action) {
  return {
    view: "查看详情",
    review: "查看审核状态",
    retry: "使用当前模型重试",
    retry_requote: "使用当前模型重试",
    retry_compatible_model: "切换已验证的兼容模型重试",
  }[action] || action || "查看详情";
}

export default function TaskDetailDrawer({ taskKey, summary, onClose }) {
  const drawerRef = useRef(null);
  const parsed = useMemo(() => parseUnifiedTaskKey(taskKey), [taskKey]);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState("");

  useEffect(() => {
    if (!taskKey) return undefined;
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement;
    document.body.style.overflow = "hidden";
    drawerRef.current?.focus();
    const handleKeyDown = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus?.();
    };
  }, [onClose, taskKey]);

  useEffect(() => {
    let active = true;
    setDetail(null);
    setError("");
    if (!parsed) {
      setLoading(false);
      setError("任务地址无效，请返回任务列表后重新打开。");
      return () => { active = false; };
    }
    setLoading(true);
    loadDetail(parsed)
      .then((payload) => {
        if (active) setDetail(payload || {});
      })
      .catch((loadError) => {
        if (active) setError(loadError?.message || "任务详情加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [parsed]);

  if (!taskKey) return null;

  const status = detailStatus(detail, summary);
  const group = summary?.status_group || statusGroup(status);
  const frozen = Number(detail?.cost_frozen ?? summary?.cost_frozen ?? 0) || 0;
  const settled = Number(detail?.cost_settled ?? summary?.cost_settled ?? 0) || 0;
  const refunded = Number(detail?.cost_refunded ?? Math.max(0, frozen - settled)) || 0;
  const downloadTargets = parsed
    ? collectDownloadTargets(parsed, detail || {}, summary)
    : [];
  const sourceGenerationId = detail?.retry_of_task_id || null;
  const sourceReverseId = detail?.retry_of_operation_id || null;
  const errors = [...new Set([
    detail?.error_message,
    detail?.error,
    ...(Array.isArray(detail?.partial_errors) ? detail.partial_errors : []),
  ]
    .filter((message) => typeof message === "string" && message.trim())
    .map((message) => message.trim()))];
  const failureSuggestion = detail?.failure_suggestion || summary?.failure_suggestion || null;
  const compatibleRetryModels = Array.isArray(summary?.compatible_retry_models)
    ? summary.compatible_retry_models
    : [];

  async function downloadAsset(target) {
    if (!target?.path || downloading) return;
    setDownloading(target.key);
    try {
      await downloadBlob(
        target.path,
        `task-${parsed?.id || "result"}`,
      );
    } catch (downloadError) {
      setError(downloadError?.message || "结果下载失败");
    } finally {
      setDownloading("");
    }
  }

  return (
    <div className="fixed inset-0 z-[70] flex justify-end bg-black/65" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose?.()}>
      <aside
        ref={drawerRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
        className="flex h-full w-full max-w-2xl flex-col border-l border-line bg-base shadow-2xl outline-none sm:w-[min(92vw,42rem)]"
      >
        <header className="flex min-h-16 items-start justify-between gap-3 border-b border-line px-4 py-4 sm:px-6">
          <div className="min-w-0">
            <p className="text-xs text-fog">{parsed ? `${taskKindLabel(parsed.kind)}任务 #${parsed.id}` : "任务详情"}</p>
            <h2 id="task-detail-title" className="mt-1 truncate text-lg font-display font-semibold text-snow">
              {summary?.title || (parsed ? taskKindLabel(parsed.kind) : "无法识别的任务")}
            </h2>
          </div>
          <button type="button" onClick={onClose} className="icon-btn h-9 w-9 shrink-0" aria-label="关闭任务详情" title="关闭">
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-8 sm:px-6">
          {loading && (
            <div className="flex min-h-64 items-center justify-center gap-2 text-sm text-mist" role="status">
              <LoaderCircle className="animate-spin" size={18} aria-hidden="true" /> 加载任务详情
            </div>
          )}
          {error && (
            <div className="mt-5 flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/10 px-3 py-3 text-sm text-bad" role="alert">
              <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 flex-1">{error}</span>
              {parsed && !loading && <button type="button" className="btn-ghost btn-sm shrink-0" onClick={() => window.location.reload()}><RefreshCw size={13} />重试</button>}
            </div>
          )}

          {(detail || summary) && !loading && (
            <>
              <section className="py-5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`badge ${taskStatusClass(group)}`}>{taskStatusLabel(status, group)}</span>
                  {(detail?.phase || summary?.phase) && <span className="text-xs text-fog">阶段：{detail?.phase || summary?.phase}</span>}
                  {["active", "needs_attention"].includes(group) && Number(summary?.progress ?? detail?.progress) > 0 && (
                    <span className="text-xs text-fog">进度 {Math.min(100, Number(summary?.progress ?? detail?.progress) || 0)}%</span>
                  )}
                </div>
                <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
                  <MetaItem label="模型">{detail?.model_name || summary?.model_name || detail?.model_id || summary?.model_id}</MetaItem>
                  <MetaItem label="创作类型">{detail?.category || summary?.category}</MetaItem>
                  <MetaItem label="任务阶段">{detail?.stage || summary?.stage}</MetaItem>
                  <MetaItem label="创建时间">{formatLocalDateTime(detail?.created_at || summary?.created_at)}</MetaItem>
                  <MetaItem label="完成时间">{formatLocalDateTime(detail?.finished_at || summary?.finished_at)}</MetaItem>
                  <MetaItem label="业务版本">{detail?.workflow_schema_version || detail?.result_schema_version}</MetaItem>
                </dl>
              </section>

              {(frozen > 0 || settled > 0 || parsed?.kind !== "parse") && (
                <section className="border-t border-line py-5">
                  <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-snow"><Coins size={15} />费用</h3>
                  <dl className="mt-3 grid grid-cols-3 gap-3">
                    <MetaItem label="冻结">{frozen} 积分</MetaItem>
                    <MetaItem label="结算">{settled} 积分</MetaItem>
                    <MetaItem label="退回/释放">{refunded} 积分</MetaItem>
                  </dl>
                </section>
              )}

              {parsed && <DetailJson title="输入快照" icon={FileInput} value={detailInput(parsed, detail || {})} emptyText="当前任务没有可展示的输入快照。" />}
              {parsed && <DetailJson title="结果" icon={FileOutput} value={detailResult(parsed, detail || {})} emptyText="任务尚未产生结果。" />}

              {downloadTargets.length > 0 && (
                <section className="border-t border-line py-5">
                  <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-snow"><Download size={15} />结果下载</h3>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {downloadTargets.map((target) => (
                      <button key={target.key} type="button" className="btn-secondary btn-sm" onClick={() => downloadAsset(target)} disabled={Boolean(downloading)} title={target.label}>
                        {downloading === target.key ? <LoaderCircle className="animate-spin" size={14} /> : <Download size={14} />}
                        下载 {target.label}
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {(failureSuggestion || compatibleRetryModels.length > 0) && (
                <section className="border-t border-line py-5">
                  <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-warn"><AlertTriangle size={15} />失败建议</h3>
                  {failureSuggestion && (
                    <div className="mt-3 border-l-2 border-warn bg-warn/10 px-3 py-3">
                      <p className="text-sm font-medium text-warn">{failureSuggestion.title}</p>
                      <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-mist">{failureSuggestion.message}</p>
                      <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        <MetaItem label="失败分类">{failureSuggestion.error_type}</MetaItem>
                        <MetaItem label="建议操作">{recommendedActionLabel(failureSuggestion.recommended_action)}</MetaItem>
                      </dl>
                    </div>
                  )}
                  {compatibleRetryModels.length > 0 && (
                    <div className="mt-4">
                      <p className="text-xs font-display font-semibold text-mist">已验证的兼容模型</p>
                      <ul className="mt-2 divide-y divide-line border-y border-line">
                        {compatibleRetryModels.map((candidate) => (
                          <li key={candidate.model_config_id} className="py-3">
                            <div className="flex min-w-0 items-start justify-between gap-3">
                              <div className="min-w-0">
                                <p className="break-words text-sm font-medium text-snow">{candidate.display_name}</p>
                                <p className="mt-0.5 break-all text-xs text-fog">{candidate.provider} · {candidate.model_id}</p>
                              </div>
                              <span className="shrink-0 text-sm font-display font-semibold text-aqua">{Number(candidate.estimated_credits).toLocaleString("zh-CN")} 积分</span>
                            </div>
                            <p className="mt-2 text-xs leading-5 text-mist">{candidate.reason}</p>
                            <p className="mt-1 text-[11px] text-fog">路由状态：{candidate.route_status}；提交时会自动校验模型与账户状态。</p>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </section>
              )}

              {(errors.length > 0 || detail?.error_code || summary?.error_type) && (
                <section className="border-t border-line py-5">
                  <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-bad"><AlertTriangle size={15} />错误信息</h3>
                  <p className="mt-2 text-xs text-fog">{detail?.error_code || summary?.error_type}</p>
                  {errors.map((message, index) => <p key={`${message}-${index}`} className="mt-2 whitespace-pre-wrap text-sm text-bad">{message}</p>)}
                </section>
              )}

              {(sourceGenerationId || sourceReverseId || detail?.parent_task_id) && (
                <section className="border-t border-line py-5">
                  <h3 className="flex items-center gap-2 text-sm font-display font-semibold text-snow"><GitBranch size={15} />任务链路</h3>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {sourceGenerationId && <Link className="btn-secondary btn-sm" href={`/history?task=generation%3A${sourceGenerationId}`}>来源生成 #{sourceGenerationId}</Link>}
                    {detail?.parent_task_id && <Link className="btn-secondary btn-sm" href={`/history?task=generation%3A${detail.parent_task_id}`}>父任务 #{detail.parent_task_id}</Link>}
                    {sourceReverseId && <Link className="btn-secondary btn-sm" href={`/history?task=reverse%3A${sourceReverseId}`}>来源反推 #{sourceReverseId}</Link>}
                  </div>
                </section>
              )}

              <section className="border-t border-line py-5">
                <h3 className="text-sm font-display font-semibold text-snow">相关业务</h3>
                <div className="mt-3 flex flex-wrap gap-2">
                  {(summary?.project_ids || []).map((projectId) => <Link key={projectId} href={`/projects?project=${encodeURIComponent(projectId)}`} onClick={onClose} className="btn-secondary btn-sm"><ExternalLink size={13} />项目 #{projectId}</Link>)}
                  {detail?.creation_recipe_id && <Link href="/prompts" onClick={onClose} className="btn-secondary btn-sm"><ExternalLink size={13} />配方 #{detail.creation_recipe_id}</Link>}
                  {detail?.reverse_operation_id && <Link href={`/history?task=reverse%3A${detail.reverse_operation_id}`} className="btn-secondary btn-sm"><ExternalLink size={13} />反推 #{detail.reverse_operation_id}</Link>}
                  {parsed?.kind === "workflow" && summary?.workflow_entry_path && <Link href={summary.workflow_entry_path} onClick={onClose} className="btn-secondary btn-sm"><ExternalLink size={13} />打开工作流</Link>}
                  {!summary?.project_ids?.length && !detail?.creation_recipe_id && !detail?.reverse_operation_id && !(parsed?.kind === "workflow" && summary?.workflow_entry_path) && <span className="text-sm text-fog">暂无关联项目、配方或上游任务。</span>}
                </div>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
