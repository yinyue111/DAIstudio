"use client";

import { useEffect, useState } from "react";
import { api } from "../../../lib/api";
import { formatLocalDateTime } from "../../../lib/datetime";
import { Card, Th } from "./admin-ui";

const RUN_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  waiting_review: "待人工审核",
  waiting_external: "待外部结果",
  succeeded: "已成功",
  failed: "已失败",
  canceled: "已取消",
  compensating: "补偿中",
};

const COMPENSATION_LABELS = {
  pending: "待补偿",
  running: "补偿中",
  succeeded: "补偿成功",
  failed: "补偿失败",
};

function statusLabel(status) {
  return RUN_STATUS_LABELS[status] || status;
}

function statusBadgeClass(status) {
  if (status === "succeeded") return "bg-ok/15 text-ok";
  if (status === "failed" || status === "compensating") return "bg-bad/15 text-bad";
  if (status === "canceled") return "bg-white/10 text-fog";
  return "bg-warn/15 text-warn";
}

function CountBadges({ title, counts, labels }) {
  const entries = Object.entries(counts || {});
  return (
    <div className="rounded-xl2 border border-line bg-black/15 p-3">
      <p className="mb-2 text-xs font-display font-medium text-fog">{title}</p>
      {entries.length === 0 ? (
        <p className="text-xs text-fog">暂无数据</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {entries.map(([status, count]) => (
            <span key={status} className={`badge ${statusBadgeClass(status)}`}>
              {(labels || RUN_STATUS_LABELS)[status] || status} · {count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function WorkflowOps() {
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");
  const [okMsg, setOkMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({
    runId: "",
    nodeKey: "",
    status: "succeeded",
    externalKind: "",
    externalId: "",
    output: "",
    errorCode: "",
    error: "",
  });

  const load = () => {
    setLoading(true);
    return api.adminWorkflowSummary()
      .then(setSummary)
      .catch((e) => setMsg(e.message))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);

  function setField(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  // 从最近运行里挑出待外部结果的节点，一键填入补录表单。
  function prefill(run, node) {
    setForm({
      runId: String(run.id),
      nodeKey: node.key,
      status: "succeeded",
      externalKind: node.external_kind || "",
      externalId: node.external_id || "",
      output: "",
      errorCode: "",
      error: "",
    });
    setOkMsg("");
    setMsg(`已填入运行 #${run.id} 节点 ${node.key}，请核对外部任务信息后提交补录`);
  }

  async function submit() {
    if (submitting) return;
    setMsg("");
    setOkMsg("");
    const runId = Number(form.runId);
    const nodeKey = form.nodeKey.trim();
    const externalKind = form.externalKind.trim();
    const externalId = form.externalId.trim();
    if (!runId || runId <= 0) return setMsg("请填写工作流运行 ID");
    if (!nodeKey) return setMsg("请填写节点 key");
    if (!externalKind || !externalId) return setMsg("请填写节点绑定的外部任务类型与 ID，两者必须与节点登记一致");
    let output = {};
    if (form.output.trim()) {
      try {
        output = JSON.parse(form.output);
      } catch (e) {
        return setMsg("输出必须是合法的 JSON 对象");
      }
      if (!output || typeof output !== "object" || Array.isArray(output)) {
        return setMsg("输出必须是合法的 JSON 对象");
      }
    }
    if (form.status === "failed" && !form.errorCode.trim() && !form.error.trim()) {
      return setMsg("补录失败结果时请填写错误码或错误信息");
    }
    const action = form.status === "succeeded" ? "成功" : "失败";
    if (!window.confirm(`确认将运行 #${runId} 节点 ${nodeKey} 人工补录为「${action}」？\nexternal：${externalKind} / ${externalId}\n该操作会驱动工作流继续推进并记录审计。`)) return;
    setSubmitting(true);
    try {
      await api.adminCompleteWorkflowNode(runId, nodeKey, {
        status: form.status,
        output,
        error_code: form.errorCode.trim() || null,
        error: form.error.trim() || null,
        external_kind: externalKind,
        external_id: externalId,
      });
      setOkMsg(`已补录运行 #${runId} 节点 ${nodeKey} 为${action}`);
      setForm({ runId: "", nodeKey: "", status: "succeeded", externalKind: "", externalId: "", output: "", errorCode: "", error: "" });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  const recent = summary?.recent || [];

  return (
    <div className="grid gap-4">
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-display font-semibold text-snow">工作流运行概览</h2>
          <button onClick={load} disabled={loading} className="btn-secondary btn-sm disabled:opacity-50">
            {loading ? "刷新中" : "刷新"}
          </button>
        </div>
        {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
        {okMsg && <p className="mb-2 text-sm text-ok">{okMsg}</p>}
        <div className="grid gap-3 sm:grid-cols-3">
          <CountBadges title="运行状态" counts={summary?.runs_by_status} />
          <CountBadges title="节点状态" counts={summary?.nodes_by_status} />
          <CountBadges title="补偿状态" counts={summary?.compensations_by_status} labels={COMPENSATION_LABELS} />
        </div>
        <p className="mt-3 text-xs text-fog">
          正常情况下异常运行由每分钟对账任务自动恢复；仅当外部结果长期未回传时才需要人工补录。
        </p>
      </Card>

      <Card>
        <h2 className="mb-3 text-sm font-display font-semibold text-snow">最近运行</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead><tr className="border-b border-line">
              <Th>ID</Th><Th>工具运行</Th><Th>状态</Th><Th>当前节点</Th><Th>错误</Th><Th>创建时间</Th><Th>待外部节点</Th>
            </tr></thead>
            <tbody>
              {recent.length === 0 && (
                <tr><td colSpan={7} className="py-3 text-center text-xs text-fog">暂无工作流运行</td></tr>
              )}
              {recent.map((run) => {
                const waitingNodes = (run.nodes || []).filter((node) => node.status === "waiting_external");
                return (
                  <tr key={run.id} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                    <td className="py-2 pr-3 text-snow">#{run.id}</td>
                    <td className="pr-3">{run.tool_run_id ?? "-"}</td>
                    <td className="pr-3">
                      <span className={`badge ${statusBadgeClass(run.status)}`}>{statusLabel(run.status)}</span>
                    </td>
                    <td className="pr-3">{run.current_node_key || "-"}</td>
                    <td className="max-w-48 truncate pr-3" title={run.error || ""}>{run.error_code || run.error || "-"}</td>
                    <td className="pr-3 text-xs">{run.created_at ? formatLocalDateTime(run.created_at) : "-"}</td>
                    <td className="py-1">
                      {waitingNodes.length === 0 ? (
                        <span className="text-xs text-fog">-</span>
                      ) : waitingNodes.map((node) => (
                        <button
                          key={node.key}
                          onClick={() => prefill(run, node)}
                          className="btn-ghost btn-sm mr-1"
                          title={`external：${node.external_kind || "-"} / ${node.external_id || "-"}`}
                        >
                          补录 {node.key}
                        </button>
                      ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <h2 className="mb-1 text-sm font-display font-semibold text-snow">外部结果人工补录</h2>
        <p className="mb-3 text-xs text-fog">
          仅用于外部任务已有结论但回调丢失的场景。外部任务类型 / ID 必须与节点登记完全一致，否则会被服务端拒绝。
        </p>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <input className="input" placeholder="运行 ID" value={form.runId} onChange={(e) => setField("runId", e.target.value)} />
          <input className="input" placeholder="节点 key" value={form.nodeKey} onChange={(e) => setField("nodeKey", e.target.value)} />
          <select className="input" value={form.status} onChange={(e) => setField("status", e.target.value)}>
            <option value="succeeded">补录为成功</option>
            <option value="failed">补录为失败</option>
          </select>
          <input className="input" placeholder="外部任务类型 (external_kind)" value={form.externalKind} onChange={(e) => setField("externalKind", e.target.value)} />
          <input className="input" placeholder="外部任务 ID (external_id)" value={form.externalId} onChange={(e) => setField("externalId", e.target.value)} />
          {form.status === "failed" && (
            <>
              <input className="input" placeholder="错误码（可选）" value={form.errorCode} onChange={(e) => setField("errorCode", e.target.value)} />
              <input className="input" placeholder="错误信息（可选）" value={form.error} onChange={(e) => setField("error", e.target.value)} />
            </>
          )}
        </div>
        <textarea
          className="input mt-2 min-h-24 w-full resize-y py-2 font-mono text-xs"
          placeholder='节点输出 JSON（可选），如 {"video_url": "https://..."}'
          value={form.output}
          onChange={(e) => setField("output", e.target.value)}
        />
        <div className="mt-3">
          <button onClick={submit} disabled={submitting} className="btn-primary btn-sm disabled:opacity-50">
            {submitting ? "补录中" : "提交补录"}
          </button>
        </div>
      </Card>
    </div>
  );
}
