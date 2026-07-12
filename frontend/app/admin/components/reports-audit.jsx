"use client";

import { useEffect, useRef, useState } from "react";
import { api, downloadBlob } from "../../../lib/api";
import { formatLocalDateTime } from "../../../lib/datetime";
import { Card, Th } from "./admin-ui";
import { createLatestRequestGate } from "./latest-request";

export function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [costs, setCosts] = useState(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [msg, setMsg] = useState("");
  const requestGateRef = useRef(null);
  if (!requestGateRef.current) requestGateRef.current = createLatestRequestGate();

  function qs() {
    const p = [];
    if (start) p.push(`start=${encodeURIComponent(start)}`);
    if (end) p.push(`end=${encodeURIComponent(end)}`);
    return p.length ? `?${p.join("&")}` : "";
  }

  function load() {
    const requestGeneration = requestGateRef.current.begin();
    setMsg("");
    const suffix = qs();
    Promise.all([api.adminUsageDashboard(suffix), api.adminModelCosts(suffix)])
      .then(([dashboard, modelCosts]) => {
        if (!requestGateRef.current.isCurrent(requestGeneration)) return;
        setSummary(dashboard);
        setCosts(modelCosts);
      })
      .catch((e) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setMsg(e.message);
      });
  }

  useEffect(() => {
    load();
    return () => requestGateRef.current.invalidate();
  }, []);

  const s = summary?.summary || {};
  const modelRows = costs?.models || [];

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-fog">日期范围</span>
          <input type="date" className="input w-auto" value={start} onChange={(e) => setStart(e.target.value)} />
          <span className="text-fog">—</span>
          <input type="date" className="input w-auto" value={end} onChange={(e) => setEnd(e.target.value)} />
          <button onClick={load} className="btn-secondary">刷新概览</button>
        </div>
        {msg && <p className="mt-2 text-sm text-bad">{msg}</p>}
      </Card>

      {!summary ? <Card><span className="text-mist">加载中…</span></Card> : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Metric label="活跃用户" value={s.dau || 0} />
            <Metric label="生成任务" value={s.task_count || 0} />
            <Metric label="成功率" value={`${Math.round((s.success_rate || 0) * 100)}%`} />
            <Metric label="待审核" value={s.needs_review_count || 0} />
            <Metric label="平均耗时" value={`${Math.round(summary.avg_duration_seconds || 0)}s`} />
          </div>
          <Card>
            <div className="mb-3 text-sm font-display font-semibold text-snow">失败原因分布</div>
            <div className="grid gap-2 sm:grid-cols-5">
              {Object.entries(summary.failure_reasons || {}).map(([key, value]) => (
                <div key={key} className="rounded-xl border border-line bg-white/[0.03] px-3 py-2">
                  <div className="text-[11px] text-fog">{failureLabel(key)}</div>
                  <div className="font-display text-xl font-bold text-snow">{value}</div>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <div className="mb-3 text-sm font-display font-semibold text-snow">模型成本与失败率</div>
            <ModelCostTable rows={modelRows} />
          </Card>
        </>
      )}
    </div>
  );
}

export function Report() {
  const [data, setData] = useState(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [msg, setMsg] = useState("");
  const requestGateRef = useRef(null);
  if (!requestGateRef.current) requestGateRef.current = createLatestRequestGate();

  function qs() {
    const p = [];
    if (start) p.push(`start=${encodeURIComponent(start)}`);
    if (end) p.push(`end=${encodeURIComponent(end)}`);
    return p.length ? `?${p.join("&")}` : "";
  }
  const load = () => {
    const requestGeneration = requestGateRef.current.begin();
    const suffix = qs();
    setMsg("");
    api.adminReport(suffix)
      .then((nextData) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setData(nextData);
      })
      .catch((e) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setMsg(e.message);
      });
  };
  useEffect(() => {
    load();
    return () => requestGateRef.current.invalidate();
  }, []);

  async function downloadCsv() {
    const suffix = qs() ? qs() + "&format=csv" : "?format=csv";
    try {
      await downloadBlob(`/api/admin/usage/report${suffix}`, "usage_report.csv");
    } catch (e) { setMsg(e.message); }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-fog">日期范围</span>
          <input type="date" className="input w-auto" value={start} onChange={(e) => setStart(e.target.value)} />
          <span className="text-fog">—</span>
          <input type="date" className="input w-auto" value={end} onChange={(e) => setEnd(e.target.value)} />
          <button onClick={load} className="btn-secondary">查询</button>
          <button onClick={downloadCsv} className="btn-primary">导出 CSV</button>
        </div>
        {msg && <p className="mt-2 text-sm text-bad">{msg}</p>}
      </Card>
      {!data ? <Card><span className="text-mist">加载中…</span></Card> : (
        <>
          {data.daily && data.daily.length > 0 && (
            <Card>
              <div className="mb-3 text-sm font-display font-semibold text-snow">每日消耗趋势</div>
              <Trend daily={data.daily} />
            </Card>
          )}
          <Card>
            <div className="mb-3 text-sm font-display font-semibold text-snow">按部门成本</div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead><tr className="border-b border-line"><Th>部门</Th><Th>消耗额度</Th></tr></thead>
                <tbody>
                  {data.per_department.map((d) => (
                    <tr key={d.department} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                      <td className="py-2 pr-3 text-snow">{d.department}</td><td className="font-display text-snow">{d.spend_credits}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Card>
            <div className="mb-3 text-sm font-display font-semibold text-snow">按人用量</div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead><tr className="border-b border-line">
                  <Th>手机号</Th><Th>部门</Th><Th>消耗</Th><Th>余额</Th><Th>任务</Th>
                </tr></thead>
                <tbody>
                  {data.per_user.map((u) => (
                    <tr key={u.user_id} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                      <td className="py-2 pr-3 text-snow">{u.phone}</td><td className="pr-3">{u.department}</td>
                      <td className="pr-3 font-display text-snow">{u.spend_credits}</td><td className="pr-3">{u.balance}</td>
                      <td className="text-xs text-fog">{JSON.stringify(u.tasks)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <Card>
      <div className="text-xs text-fog">{label}</div>
      <div className="mt-1 font-display text-2xl font-bold text-snow">{value}</div>
    </Card>
  );
}

function failureLabel(key) {
  return {
    moderation: "审核/待确认",
    user_input: "用户可修正",
    provider_timeout: "网关超时",
    provider_error: "网关异常",
    system_error: "系统异常",
  }[key] || key;
}

function ModelCostTable({ rows }) {
  if (!rows?.length) return <p className="py-8 text-center text-sm text-fog">暂无模型调用记录。</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead><tr className="border-b border-line">
          <Th>模型</Th><Th>类型</Th><Th>调用</Th><Th>失败</Th><Th>失败率</Th><Th>Token</Th><Th>估算积分</Th><Th>均耗时</Th>
        </tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.model_id}:${row.kind}`} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
              <td className="py-2 pr-3 text-snow">{row.model_id}</td>
              <td className="pr-3">{row.kind}</td>
              <td className="pr-3 font-display text-snow">{row.call_count}</td>
              <td className="pr-3">{row.failed_count}</td>
              <td className="pr-3">{Math.round((row.failure_rate || 0) * 100)}%</td>
              <td className="pr-3">{row.total_tokens || 0}</td>
              <td className="pr-3">{row.estimated_cost_credits || 0}</td>
              <td>{Math.round(row.avg_latency_ms || 0)}ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Audit() {
  const [rows, setRows] = useState([]);
  const [action, setAction] = useState("");
  const [userId, setUserId] = useState("");
  const [msg, setMsg] = useState("");
  const requestGateRef = useRef(null);
  if (!requestGateRef.current) requestGateRef.current = createLatestRequestGate();

  function load() {
    const requestGeneration = requestGateRef.current.begin();
    const p = [];
    if (action) p.push(`action=${encodeURIComponent(action)}`);
    if (userId) p.push(`user_id=${encodeURIComponent(userId)}`);
    const qs = p.length ? `?${p.join("&")}` : "";
    setMsg("");
    api.adminAudit(qs)
      .then((nextRows) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setRows(nextRows);
      })
      .catch((e) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setMsg(e.message);
      });
  }
  useEffect(() => {
    load();
    return () => requestGateRef.current.invalidate();
  }, []);

  return (
    <Card>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className="input w-64" placeholder="action(如 login/generate/unlock)"
          value={action} onChange={(e) => setAction(e.target.value)} />
        <input className="input w-28" placeholder="user_id" value={userId} onChange={(e) => setUserId(e.target.value)} />
        <button onClick={load} className="btn-secondary">查询</button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
            <Th>时间</Th><Th>用户</Th><Th>动作</Th><Th>对象</Th><Th>IP</Th><Th>详情</Th>
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60 align-top text-mist transition-colors hover:bg-white/5">
                <td className="whitespace-nowrap py-2 pr-3 text-xs">
                  {formatLocalDateTime(r.created_at, { includeSeconds: true })}
                </td>
                <td className="pr-3 text-snow">{r.user_id ?? "-"}</td>
                <td className="pr-3 font-display text-snow">{r.action}</td>
                <td className="pr-3 text-xs">{r.biz_type ? `${r.biz_type}#${r.biz_id ?? ""}` : "-"}</td>
                <td className="pr-3 text-xs">{r.ip || "-"}</td>
                <td className="text-xs text-fog">{r.detail ? JSON.stringify(r.detail) : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Trend({ daily }) {
  const rows = daily.slice(-30);
  const max = Math.max(1, ...rows.map((d) => d.spend_credits));
  return (
    <div className="flex items-end gap-1 overflow-x-auto" style={{ height: 120 }}>
      {rows.map((d) => (
        <div key={d.date} className="group flex min-w-[14px] flex-1 flex-col items-center justify-end"
          title={`${d.date}: ${d.spend_credits}`}>
          <div className="w-full rounded-t bg-brand transition-all group-hover:bg-iris"
            style={{ height: `${Math.round((d.spend_credits / max) * 100)}%`, minHeight: d.spend_credits > 0 ? 3 : 0 }} />
          <span className="mt-1 text-[9px] text-fog">{d.date.slice(5)}</span>
        </div>
      ))}
    </div>
  );
}
