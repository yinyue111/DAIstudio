"use client";

import { useEffect, useState } from "react";
import { api, downloadBlob } from "../../../lib/api";
import { Card, Th } from "./admin-ui";

export function Report() {
  const [data, setData] = useState(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [msg, setMsg] = useState("");

  function qs() {
    const p = [];
    if (start) p.push(`start=${encodeURIComponent(start)}`);
    if (end) p.push(`end=${encodeURIComponent(end)}`);
    return p.length ? `?${p.join("&")}` : "";
  }
  const load = () => {
    setMsg("");
    api.adminReport(qs()).then(setData).catch((e) => setMsg(e.message));
  };
  useEffect(() => { load(); }, []);

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

export function Audit() {
  const [rows, setRows] = useState([]);
  const [action, setAction] = useState("");
  const [userId, setUserId] = useState("");
  const [msg, setMsg] = useState("");

  function load() {
    const p = [];
    if (action) p.push(`action=${encodeURIComponent(action)}`);
    if (userId) p.push(`user_id=${userId}`);
    const qs = p.length ? `?${p.join("&")}` : "";
    setMsg("");
    api.adminAudit(qs).then(setRows).catch((e) => setMsg(e.message));
  }
  useEffect(() => { load(); }, []);

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
                <td className="whitespace-nowrap py-2 pr-3 text-xs">{(r.created_at || "").replace("T", " ").slice(0, 19)}</td>
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
