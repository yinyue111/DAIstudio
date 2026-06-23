"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "../../../lib/api";
import {
  confirmReviewTaskAction,
  formatMinutes,
  modelUseLabel,
  reportReasonLabel,
  reportStatusLabel,
  promptAdminPassword,
} from "./admin-helpers";
import { Card, Th } from "./admin-ui";

export function Models() {
  const [rows, setRows] = useState([]);
  const [providers, setProviders] = useState({});
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState("ok");
  const [probing, setProbing] = useState({});
  const probeSeqRef = useRef(new Map());
  const PROBE_KEYS = new Set(["provider", "base_url", "gateway_format", "api_key", "api_key_clear"]);
  const load = () => api.adminModels()
    .then((payload) => {
      const list = Array.isArray(payload) ? payload : (payload.models || []);
      setProviders(payload.providers || {});
      setRows(list.map((r) => ({
        ...r,
        provider: r.provider || "",
        base_url: r.base_url || "",
        gateway_format: r.gateway_format || (r.use === "video" ? "ark" : "openai"),
        api_key: "",
        api_key_clear: false,
        probeModels: [],
        probeSignature: "",
        extraText: r.extra ? JSON.stringify(r.extra, null, 2) : "",
      })));
    })
    .catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  function set(i, key, val) {
    setRows((prev) => {
      const copy = [...prev];
      const next = { ...copy[i], [key]: val };
      if (PROBE_KEYS.has(key)) {
        next.probeModels = [];
        next.probeSignature = "";
        probeSeqRef.current.delete(next.use);
      }
      copy[i] = next;
      return copy;
    });
  }

  function probeSignature(r) {
    return JSON.stringify({
      use: r.use || "",
      provider: r.provider || "",
      base_url: r.base_url || "",
      gateway_format: r.gateway_format || "",
      api_key: r.api_key || "",
      api_key_configured: !!r.api_key_configured,
      api_key_clear: !!r.api_key_clear,
    });
  }

  function setProvider(i, provider) {
    const preset = providers[provider] || {};
    setRows((prev) => {
      const copy = [...prev];
      const current = copy[i];
      const hasPresetBaseUrl = Object.prototype.hasOwnProperty.call(preset, "base_url");
      copy[i] = {
        ...current,
        provider,
        base_url: hasPresetBaseUrl ? (preset.base_url || "") : (current.base_url || ""),
        gateway_format: preset.gateway_format || current.gateway_format || "openai",
        probeModels: [],
        probeSignature: "",
      };
      probeSeqRef.current.delete(current.use);
      return copy;
    });
  }

  async function save(r) {
    setMsg("");
    setMsgType("ok");
    try {
      let extra = null;
      if ((r.extraText || "").trim()) {
        extra = JSON.parse(r.extraText);
      }
      if (probing[r.use]) {
        setMsgType("bad");
        setMsg("模型探测仍在进行，请等待探测完成后再保存。");
        return;
      }
      const cost = Number(r.cost_credits);
      const unlockCost = Number(r.unlock_cost);
      const summary = [
        `用途：${modelUseLabel(r.use)}`,
        `提供商：${r.provider || "环境变量兜底"}`,
        `模型：${r.model_id || "-"}`,
        `Base URL：${r.base_url || "-"}`,
        `调用消耗：${Number.isFinite(cost) ? cost : "-"} 积分`,
        `解锁消耗：${Number.isFinite(unlockCost) ? unlockCost : "-"} 积分`,
        `状态：${r.enabled ? "启用" : "停用"}`,
      ];
      if (r.api_key || r.api_key_clear) {
        summary.push(r.api_key_clear ? "API Key：将清空" : "API Key：将更新");
      }
      if (!window.confirm(`确认保存模型配置？\n${summary.join("\n")}\n保存后新任务会使用该配置。`)) return;
      await api.adminSaveModel({
        use: r.use, model_id: r.model_id,
        provider: r.provider || null,
        base_url: r.base_url || null,
        gateway_format: r.gateway_format || null,
        api_key: r.api_key || null,
        api_key_clear: !!r.api_key_clear,
        cost_credits: cost, unlock_cost: unlockCost,
        enabled: !!r.enabled, extra,
      });
      setMsgType("ok");
      setMsg(`${r.use} 已保存`);
      load();
    } catch (e) {
      setMsgType("bad");
      setMsg(e instanceof SyntaxError ? `${r.use} 的 extra 不是合法 JSON` : e.message);
    }
  }

  async function probe(r, i) {
    setMsg("");
    setMsgType("ok");
    const signature = probeSignature(r);
    probeSeqRef.current.set(r.use, signature);
    setProbing((prev) => ({ ...prev, [r.use]: signature }));
    try {
      const res = await api.adminProbeModels({
        use: r.use,
        provider: r.provider || null,
        base_url: r.base_url || null,
        gateway_format: r.gateway_format || null,
        api_key: r.api_key || null,
      });
      if (probeSeqRef.current.get(r.use) !== signature) {
        setMsgType("bad");
        setMsg("探测结果已过期，请按当前配置重新探测。");
        return;
      }
      setRows((prev) => {
        const copy = [...prev];
        const current = copy[i];
        if (!current || current.use !== r.use || probeSignature(current) !== signature) {
          return prev;
        }
        copy[i] = { ...current, probeModels: res.models || [], probeSignature: signature };
        return copy;
      });
      setMsgType("ok");
      setMsg(`已读取 ${res.models?.length || 0} 个模型`);
    } catch (e) {
      setMsgType("bad");
      setMsg(e.message);
    } finally {
      setProbing((prev) => {
        if (prev[r.use] !== signature) return prev;
        const next = { ...prev };
        delete next[r.use];
        return next;
      });
    }
  }

  const providerEntries = Object.entries(providers);

  return (
    <Card>
      {msg && <p className={`mb-2 text-sm ${msgType === "bad" ? "text-bad" : "text-ok"}`}>{msg}</p>}
      <div className="space-y-4">
        {rows.map((r, i) => (
          <div key={r.use} className="rounded-xl border border-line bg-white/[0.03] p-4">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="font-display text-sm font-semibold text-snow">{modelUseLabel(r.use)}</div>
                <div className="mt-1 flex flex-wrap items-center gap-1 text-xs">
                  <span className={`badge ${r.enabled ? "bg-ok/15 text-ok" : "bg-white/10 text-fog"}`}>
                    {r.enabled ? "已启用" : "已停用"}
                  </span>
                  {r.api_key_configured ? <span className="badge bg-aqua/15 text-aqua">API Key 已配置</span> : null}
                  <span className="badge bg-white/10 text-mist">{r.gateway_format || "openai"}</span>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button onClick={() => probe(r, i)} disabled={!!probing[r.use]} className="btn-secondary btn-sm">
                  {probing[r.use] ? "探测中" : "探测模型"}
                </button>
                <button onClick={() => save(r)} disabled={!!probing[r.use]} className="btn-primary btn-sm">保存配置</button>
              </div>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <label className="grid gap-1 text-xs text-fog">
                <span>模型提供商</span>
                <select className="select w-full" value={r.provider || ""}
                  onChange={(e) => setProvider(i, e.target.value)}>
                  <option value="">使用环境变量兜底</option>
                  {providerEntries.map(([key, p]) => (
                    <option key={key} value={key}>{p.label || key}</option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>网关格式</span>
                <select className="select w-full" value={r.gateway_format || "openai"}
                  onChange={(e) => set(i, "gateway_format", e.target.value)}>
                  <option value="openai">OpenAI-Compatible</option>
                  <option value="ark">火山方舟 Ark</option>
                </select>
              </label>
              <label className="grid gap-1 text-xs text-fog md:col-span-2">
                <span>Base URL</span>
                <input className="input w-full" value={r.base_url || ""}
                  placeholder="例如 https://api.openai.com/v1 或 https://ark.cn-beijing.volces.com/api/v3"
                  onChange={(e) => set(i, "base_url", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog md:col-span-2">
                <span className="flex items-center justify-between gap-2">
                  <span>API Key{r.api_key_configured ? <b className="ml-2 font-normal text-ok">已配置</b> : null}</span>
                  {r.api_key_configured ? (
                    <button type="button" className="text-warn hover:text-snow"
                      onClick={() => {
                        if (window.confirm(`确认清空 ${modelUseLabel(r.use)} API Key？保存后生效。`)) {
                          set(i, "api_key_clear", true);
                          set(i, "api_key", "");
                        }
                      }}>
                      清空
                    </button>
                  ) : null}
                </span>
                <input type="password" className="input w-full" value={r.api_key || ""}
                  placeholder={r.api_key_clear ? "保存后清空 API Key" : "留空表示不修改"}
                  onChange={(e) => {
                    set(i, "api_key", e.target.value);
                    if (e.target.value) set(i, "api_key_clear", false);
                  }} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>模型 ID</span>
                <input className="input w-full" value={r.model_id || ""}
                  onChange={(e) => set(i, "model_id", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>从探测结果选择</span>
                <select className="select w-full" value=""
                  onChange={(e) => e.target.value && set(i, "model_id", e.target.value)}>
                  <option value="">选择模型 ID</option>
                  {(r.probeModels || []).map((m) => (
                    <option key={m.id} value={m.id}>{m.id}{m.owned_by ? ` · ${m.owned_by}` : ""}</option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>调用消耗积分</span>
                <input className="input w-full" type="number" min="0" value={r.cost_credits}
                  onChange={(e) => set(i, "cost_credits", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>解锁消耗积分</span>
                <input className="input w-full" type="number" min="0" value={r.unlock_cost}
                  onChange={(e) => set(i, "unlock_cost", e.target.value)} />
              </label>
              <label className="flex items-center gap-2 text-xs text-fog">
                <input type="checkbox" className="accent-iris" checked={!!r.enabled}
                  onChange={(e) => set(i, "enabled", e.target.checked)} />
                启用该模型
              </label>
              <label className="grid gap-1 text-xs text-fog md:col-span-2">
                <span>extra(JSON)</span>
                <textarea
                  className="textarea h-24 font-mono text-xs"
                  value={r.extraText || ""}
                  placeholder='{"preview_cost":5,"edit_path":"/v1/images/edits","submit_path":"/v1/videos/generations","poll_path":"/v1/videos/{id}","id_field":"id"}'
                  onChange={(e) => set(i, "extraText", e.target.value)}
                />
              </label>
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-fog">提示:API Key 只写入后端加密存储，不会回显；Base URL 建议填写平台 OpenAI-Compatible 地址，火山方舟视频选择 Ark 格式。</p>
    </Card>
  );
}

export function ReviewTasks() {
  const [rows, setRows] = useState([]);
  const [msg, setMsg] = useState("");
  const [busyId, setBusyId] = useState(null);

  const load = () => {
    setMsg("");
    api.adminReviewTasks().then(setRows).catch((e) => setMsg(e.message));
  };
  useEffect(() => { load(); }, []);

  async function refund(task) {
    const note = window.prompt("退款关闭原因", task.error || "上游状态未知,人工退款");
    if (note == null) return;
    if (!confirmReviewTaskAction(task, "退款关闭", note)) return;
    const adminPassword = await promptAdminPassword("退款关闭待对账任务");
    if (!adminPassword) return;
    setBusyId(task.id);
    setMsg("");
    try {
      await api.adminRefundReviewTask(task.id, { note, admin_password: adminPassword });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusyId(null);
    }
  }

  async function settle(task) {
    const resultUrl = window.prompt(
      task.category === "image" ? "填入上游已生成的图片结果 URL" : "填入上游已生成的视频结果 URL"
    );
    if (!resultUrl && task.category !== "image") return;
    const externalTaskId = window.prompt("外部任务 ID(可选)", task.external_task_id || "") || null;
    const note = window.prompt("结算备注(可选)", "人工补结果结算") || "";
    if (!confirmReviewTaskAction(task, "补结果结算", note, resultUrl)) return;
    const adminPassword = await promptAdminPassword("补结果结算待对账任务");
    if (!adminPassword) return;
    setBusyId(task.id);
    setMsg("");
    try {
      await api.adminSettleReviewTask(task.id, {
        result_url: resultUrl,
        external_task_id: externalTaskId,
        note,
        admin_password: adminPassword,
      });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-display font-semibold text-snow">待人工对账任务</div>
          <div className="mt-1 text-xs text-fog">视频提交状态未知时会保留冻结额度，超过 SLA 的任务会高亮显示。</div>
        </div>
        <button onClick={load} className="btn-secondary btn-sm">刷新</button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
            <Th>任务</Th><Th>用户</Th><Th>类型</Th><Th>冻结</Th><Th>等待</Th><Th>外部信息</Th><Th>错误</Th><Th>操作</Th>
          </tr></thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={8} className="py-6 text-center text-sm text-fog">暂无待对账任务</td></tr>
            ) : rows.map((t) => (
              <tr key={t.id} className={`border-b border-line/60 align-top text-mist transition-colors hover:bg-white/5 ${t.review_overdue ? "bg-warn/5" : ""}`}>
                <td className="py-2 pr-3 text-snow">#{t.id}</td>
                <td className="pr-3">{t.user_id || "-"}</td>
                <td className="pr-3">{t.category} / {t.stage}</td>
                <td className="pr-3 font-display text-snow">{t.cost_frozen}</td>
                <td className="pr-3">
                  <span className={`badge ${t.review_overdue ? "bg-warn/15 text-warn" : "bg-white/10 text-fog"}`}>
                    {formatMinutes(t.age_minutes)}{t.review_overdue ? " 超时" : ""}
                  </span>
                  {t.review_sla_minutes ? <div className="mt-1 text-[11px] text-fog">SLA {t.review_sla_minutes} 分钟</div> : null}
                </td>
                <td className="max-w-xs pr-3 text-xs text-fog">
                  {t.model_use ? <div>model: {t.model_use}</div> : null}
                  {t.parent_task_id ? <div>parent: {t.parent_task_id}</div> : null}
                  {t.phase ? <div>phase: {t.phase}</div> : null}
                  {t.video_request_id ? <div>request: {t.video_request_id}</div> : null}
                  {t.external_task_id ? <div>external: {t.external_task_id}</div> : null}
                </td>
                <td className="max-w-xs pr-3 text-xs text-warn">{t.error || "-"}</td>
                <td className="whitespace-nowrap py-1">
                  <button onClick={() => settle(t)} disabled={busyId === t.id} className="btn-primary btn-sm">补结果</button>
                  <button onClick={() => refund(t)} disabled={busyId === t.id} className="btn-ghost btn-sm ml-1">退款关闭</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function AssetReports() {
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("open");
  const [msg, setMsg] = useState("");
  const [busyId, setBusyId] = useState(null);

  const load = () => {
    setMsg("");
    api.adminAssetReports({ status }).then(setRows).catch((e) => setMsg(e.message));
  };
  useEffect(() => { load(); }, [status]);

  async function handle(row, action) {
    const label = action === "takedown" ? "下架素材" : "驳回举报";
    const note = window.prompt(`${label}说明`, action === "takedown" ? "确认违规下架" : "未发现违规");
    if (note == null) return;
    const lines = [
      `确认${label}？`,
      `举报：#${row.id}`,
      `素材：${row.asset_id ? `#${row.asset_id}` : "已删除"}`,
      `举报人：${row.reporter_user_id}`,
      `原因：${reportReasonLabel(row.reason)}`,
    ];
    if (row.note) lines.push(`用户说明：${row.note}`);
    if (note) lines.push(`处理说明：${note}`);
    if (action === "takedown") lines.push("下架后该素材将不能继续预览、解锁或下载。");
    if (!window.confirm(lines.join("\n"))) return;
    const adminPassword = await promptAdminPassword(label);
    if (!adminPassword) return;
    setBusyId(row.id);
    setMsg("");
    try {
      await api.adminHandleAssetReport(row.id, { action, note, admin_password: adminPassword });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-display font-semibold text-snow">素材举报处理</div>
          <div className="mt-1 text-xs text-fog">处理版权、敏感、违法、隐私等素材举报；下架会保留审计记录并禁止继续预览、解锁或下载。</div>
        </div>
        <div className="flex gap-2">
          <select className="input w-28 py-1.5 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="open">待处理</option>
            <option value="all">全部</option>
            <option value="takedown">已下架</option>
            <option value="dismissed">已驳回</option>
          </select>
          <button onClick={load} className="btn-secondary btn-sm">刷新</button>
        </div>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
            <Th>举报</Th><Th>素材</Th><Th>举报人</Th><Th>原因</Th><Th>说明</Th><Th>状态</Th><Th>操作</Th>
          </tr></thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={7} className="py-6 text-center text-sm text-fog">暂无举报</td></tr>
            ) : rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60 align-top text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">#{r.id}</td>
                <td className="pr-3">{r.asset_id ? `#${r.asset_id}` : "已删除"}</td>
                <td className="pr-3">{r.reporter_user_id}</td>
                <td className="pr-3">{reportReasonLabel(r.reason)}</td>
                <td className="max-w-xs pr-3 text-xs text-fog">{r.note || r.handle_note || "-"}</td>
                <td className="pr-3">{reportStatusLabel(r.status)}</td>
                <td className="whitespace-nowrap py-1">
                  {r.status === "open" ? (
                    <>
                      <button onClick={() => handle(r, "takedown")} disabled={busyId === r.id} className="btn-primary btn-sm">下架</button>
                      <button onClick={() => handle(r, "dismiss")} disabled={busyId === r.id} className="btn-ghost btn-sm ml-1">驳回</button>
                    </>
                  ) : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
