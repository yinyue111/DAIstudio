"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, downloadBlob, getToken } from "../../lib/api";
import Nav from "../../components/Nav";

const TABS = [
  ["whitelist", "白名单"],
  ["users", "用户 / 额度"],
  ["models", "模型配置"],
  ["review", "待对账"],
  ["report", "用量报表"],
  ["payments", "支付设置"],
  ["settings", "平台设置"],
  ["audit", "审计日志"],
];

export default function AdminPage() {
  const router = useRouter();
  const [tab, setTab] = useState("whitelist");
  const [me, setMe] = useState(null);

  useEffect(() => {
    if (!getToken()) return router.push("/login");
    api.me().then((u) => {
      if (!u.is_admin) router.push("/");
      else setMe(u);
    }).catch(() => router.push("/login"));
  }, []);

  if (!me) return <div className="p-10 text-sm text-mist">加载中…</div>;

  return (
    <div className="min-h-screen">
      <Nav me={me} active="admin" />
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        <h1 className="mb-1 text-2xl font-bold sm:text-3xl">
          管理<span className="text-grad">后台</span>
        </h1>
        <p className="mb-6 text-sm text-fog">白名单、用户额度、模型与平台设置、用量与审计一站式管控。</p>

        <div className="mb-6 inline-flex flex-wrap gap-1 rounded-full border border-line bg-white/5 p-1 backdrop-blur-xl">
          {TABS.map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`rounded-full px-4 py-1.5 text-sm font-display font-medium transition-all ${
                tab === k ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:bg-white/5 hover:text-snow"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "whitelist" && <Whitelist />}
        {tab === "users" && <Users />}
        {tab === "models" && <Models />}
        {tab === "review" && <ReviewTasks />}
        {tab === "report" && <Report />}
        {tab === "payments" && <Payments />}
        {tab === "settings" && <Settings />}
        {tab === "audit" && <Audit />}
      </main>
    </div>
  );
}

function Card({ children, className = "" }) {
  return <div className={`card p-5 animate-fadeup ${className}`}>{children}</div>;
}

function Th({ children }) {
  return <th className="py-2 pr-3 text-xs font-display font-medium text-fog">{children}</th>;
}

function Whitelist() {
  const [list, setList] = useState([]);
  const [phone, setPhone] = useState("");
  const [note, setNote] = useState("");
  const [department, setDept] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api.adminWhitelist().then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function add() {
    const adminPassword = await promptAdminPassword("添加白名单");
    if (!adminPassword) return;
    setMsg("");
    try {
      await api.adminAddWhitelist({ phone, note, department, admin_password: adminPassword });
      setPhone(""); setNote(""); setDept("");
      load();
    } catch (e) { setMsg(e.message); }
  }

  async function remove(phone) {
    if (!window.confirm(`确认移除白名单 ${phone}？`)) return;
    const adminPassword = await promptAdminPassword("移除白名单");
    if (!adminPassword) return;
    setMsg("");
    try {
      await api.adminRemoveWhitelist(phone, { admin_password: adminPassword });
      load();
    } catch (e) { setMsg(e.message); }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className="input w-40" placeholder="手机号" value={phone} onChange={(e) => setPhone(e.target.value)} />
        <input className="input w-40" placeholder="备注 / 姓名" value={note} onChange={(e) => setNote(e.target.value)} />
        <input className="input w-32" placeholder="部门" value={department} onChange={(e) => setDept(e.target.value)} />
        <button onClick={add} className="btn-primary">添加</button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line"><Th>手机号</Th><Th>备注</Th><Th>部门</Th><Th>操作</Th></tr></thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.phone} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">{r.phone}</td><td className="pr-3">{r.note}</td><td className="pr-3">{r.department}</td>
                <td><button onClick={() => remove(r.phone)} className="btn-ghost btn-sm">移除</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Users() {
  const [list, setList] = useState([]);
  const [amounts, setAmounts] = useState({});
  const [notes, setNotes] = useState({});
  const [grantKeys, setGrantKeys] = useState({});
  const [msg, setMsg] = useState("");
  const [granting, setGranting] = useState(null);
  const load = () => api.adminUsers().then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function grant(uid) {
    if (granting === uid) return;
    const amount = Number(amounts[uid] || 0);
    const note = (notes[uid] || "").trim();
    if (!amount) return;
    if (!note) {
      setMsg("请填写额度发放原因");
      return;
    }
    setGranting(uid);
    if (!window.confirm(`确认给用户 ${uid} 发放 ${amount} 积分？\n原因：${note}`)) {
      setGranting(null);
      return;
    }
    const adminPassword = await promptAdminPassword("发放积分");
    if (!adminPassword) {
      setGranting(null);
      return;
    }
    const signature = `${uid}:${amount}:${note}`;
    const existing = grantKeys[uid];
    const idempotencyKey = existing?.signature === signature ? existing.key : crypto.randomUUID();
    setGrantKeys((prev) => ({ ...prev, [uid]: { signature, key: idempotencyKey } }));
    setMsg("");
    try {
      await api.adminGrant({
        user_id: uid,
        amount,
        note,
        admin_password: adminPassword,
        idempotency_key: idempotencyKey,
      });
      setAmounts({ ...amounts, [uid]: "" });
      setNotes({ ...notes, [uid]: "" });
      setGrantKeys((prev) => {
        const next = { ...prev };
        delete next[uid];
        return next;
      });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setGranting(null);
    }
  }

  async function resetPw(uid) {
    const pw = await promptPassword("为该用户设置新密码(≥6 位),将强制其重新登录", "新密码");
    if (!pw) return;
    if (pw.length < 6) {
      window.alert("新密码至少 6 位");
      return;
    }
    const adminPassword = await promptAdminPassword("重置用户密码");
    if (!adminPassword) return;
    try {
      await api.adminResetPassword(uid, { password: pw, admin_password: adminPassword });
      window.alert("已重置");
    } catch (e) {
      window.alert(e.message);
    }
  }

  async function setStatus(user, status) {
    const action = status === "disabled" ? "禁用" : status === "active" ? "启用" : "设为待审核";
    if (!window.confirm(`确认${action}用户 ${user.phone}？该操作会使其已登录令牌失效。`)) return;
    const adminPassword = await promptAdminPassword(`${action}用户`);
    if (!adminPassword) return;
    setMsg("");
    try {
      await api.adminSetUserStatus(user.id, { status, admin_password: adminPassword });
      load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <Card>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
              <Th>ID</Th><Th>手机号</Th><Th>部门</Th><Th>状态</Th><Th>余额</Th><Th>发放</Th><Th>账号</Th>
          </tr></thead>
          <tbody>
            {list.map((u) => (
              <tr key={u.id} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">{u.id}</td>
                <td className="pr-3 text-snow">
                  {u.phone}
                  {u.is_admin && <span className="badge ml-1 bg-iris/15 text-iris-400">admin</span>}
                </td>
                <td className="pr-3">{u.department}</td>
                <td className="pr-3">
                  <span className={`badge ${u.status === "active" ? "bg-ok/15 text-ok" : u.status === "disabled" ? "bg-bad/15 text-bad" : "bg-warn/15 text-warn"}`}>
                    {u.status}
                  </span>
                </td>
                <td className="pr-3 font-display text-snow">{u.balance_credits}</td>
                <td className="py-1">
                  <input
                    className="input inline-block w-20"
                    value={amounts[u.id] || ""} placeholder="额度"
                    onChange={(e) => {
                      setAmounts({ ...amounts, [u.id]: e.target.value });
                      setGrantKeys((prev) => {
                        const next = { ...prev };
                        delete next[u.id];
                        return next;
                      });
                    }}
                  />
                  <input
                    className="input ml-1.5 inline-block w-36"
                    value={notes[u.id] || ""} placeholder="原因"
                    onChange={(e) => {
                      setNotes({ ...notes, [u.id]: e.target.value });
                      setGrantKeys((prev) => {
                        const next = { ...prev };
                        delete next[u.id];
                        return next;
                      });
                    }}
                  />
                  <button onClick={() => grant(u.id)} disabled={granting === u.id}
                    className="btn-secondary btn-sm ml-1.5">
                    {granting === u.id ? "发放中" : "发放"}
                  </button>
                  <button onClick={() => resetPw(u.id)} className="btn-ghost btn-sm ml-1">重置密码</button>
                </td>
                <td className="py-1">
                  {u.status === "disabled" ? (
                    <button onClick={() => setStatus(u, "active")} className="btn-secondary btn-sm">启用</button>
                  ) : (
                    <button onClick={() => setStatus(u, "disabled")} className="btn-ghost btn-sm">禁用</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Models() {
  const [rows, setRows] = useState([]);
  const [providers, setProviders] = useState({});
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState("ok");
  const [probing, setProbing] = useState(null);
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
        extraText: r.extra ? JSON.stringify(r.extra, null, 2) : "",
      })));
    })
    .catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  function set(i, key, val) {
    setRows((prev) => {
      const copy = [...prev];
      copy[i] = { ...copy[i], [key]: val };
      return copy;
    });
  }

  function setProvider(i, provider) {
    const preset = providers[provider] || {};
    setRows((prev) => {
      const copy = [...prev];
      const current = copy[i];
      copy[i] = {
        ...current,
        provider,
        base_url: preset.base_url || current.base_url || "",
        gateway_format: preset.gateway_format || current.gateway_format || "openai",
        probeModels: [],
      };
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
      const adminPassword = await promptAdminPassword(`保存 ${r.use} 模型配置`);
      if (!adminPassword) return;
      await api.adminSaveModel({
        use: r.use, model_id: r.model_id,
        provider: r.provider || null,
        base_url: r.base_url || null,
        gateway_format: r.gateway_format || null,
        api_key: r.api_key || null,
        api_key_clear: !!r.api_key_clear,
        cost_credits: Number(r.cost_credits), unlock_cost: Number(r.unlock_cost),
        enabled: !!r.enabled, extra, admin_password: adminPassword,
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
    setProbing(r.use);
    try {
      let adminPassword = null;
      if (r.api_key_configured && !(r.api_key || "").trim()) {
        adminPassword = await promptAdminPassword(`探测 ${modelUseLabel(r.use)} 已保存模型密钥`);
        if (!adminPassword) return;
      }
      const res = await api.adminProbeModels({
        use: r.use,
        provider: r.provider || null,
        base_url: r.base_url || null,
        gateway_format: r.gateway_format || null,
        api_key: r.api_key || null,
        admin_password: adminPassword,
      });
      setRows((prev) => {
        const copy = [...prev];
        copy[i] = { ...copy[i], probeModels: res.models || [] };
        return copy;
      });
      setMsgType("ok");
      setMsg(`已读取 ${res.models?.length || 0} 个模型`);
    } catch (e) {
      setMsgType("bad");
      setMsg(e.message);
    } finally {
      setProbing(null);
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
                <button onClick={() => probe(r, i)} disabled={probing === r.use} className="btn-secondary btn-sm">
                  {probing === r.use ? "探测中" : "探测模型"}
                </button>
                <button onClick={() => save(r)} className="btn-primary btn-sm">保存配置</button>
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

function modelUseLabel(use) {
  return use === "vision" ? "反推 / 视觉理解" : use === "image" ? "图片生成" : "视频生成";
}

function ReviewTasks() {
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
    const resultUrl = window.prompt("填入上游已生成的视频结果 URL");
    if (!resultUrl) return;
    const externalTaskId = window.prompt("外部任务 ID(可选)", task.external_task_id || "") || null;
    const note = window.prompt("结算备注(可选)", "人工补结果结算") || "";
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
          <div className="mt-1 text-xs text-fog">视频提交状态未知时会保留冻结额度，需要确认上游后退款关闭或补结果结算。</div>
        </div>
        <button onClick={load} className="btn-secondary btn-sm">刷新</button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
            <Th>任务</Th><Th>用户</Th><Th>类型</Th><Th>冻结</Th><Th>外部信息</Th><Th>错误</Th><Th>操作</Th>
          </tr></thead>
          <tbody>
            {rows.length === 0 ? (
              <tr><td colSpan={7} className="py-6 text-center text-sm text-fog">暂无待对账任务</td></tr>
            ) : rows.map((t) => (
              <tr key={t.id} className="border-b border-line/60 align-top text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">#{t.id}</td>
                <td className="pr-3">{t.user_id || "-"}</td>
                <td className="pr-3">{t.category} / {t.stage}</td>
                <td className="pr-3 font-display text-snow">{t.cost_frozen}</td>
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

function Report() {
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

function Payments() {
  const emptyPackage = {
    id: "",
    title: "",
    amount_cents: 990,
    credits: 100,
    badge: "",
    enabled: true,
    sort_order: 0,
  };
  const [packages, setPackages] = useState([]);
  const [providers, setProviders] = useState([]);
  const [pkg, setPkg] = useState(emptyPackage);
  const [forms, setForms] = useState({});
  const [msg, setMsg] = useState("");
  const [msgKind, setMsgKind] = useState("ok");
  const [saving, setSaving] = useState("");
  const [secretNonce, setSecretNonce] = useState(0);

  function load() {
    setMsg("");
    setMsgKind("ok");
    Promise.all([api.adminPaymentPackages(), api.adminPaymentProviders()])
      .then(([pkgs, ps]) => {
        setPackages(pkgs);
        setProviders(ps);
        const next = {};
        ps.forEach((p) => {
          next[p.provider] = {
            provider: p.provider,
            enabled: !!p.enabled,
            mode: p.mode || "mock",
            public_config: p.public_config || {},
            secret_config: {},
          };
        });
        setForms(next);
        setSecretNonce((n) => n + 1);
      })
      .catch((e) => {
        setMsg(e.message);
        setMsgKind("bad");
      });
  }
  useEffect(() => { load(); }, []);

  async function savePackage() {
    const adminPassword = await promptAdminPassword("保存支付套餐");
    if (!adminPassword) return;
    setMsg("");
    setMsgKind("ok");
    setSaving("package");
    try {
      await api.adminSavePaymentPackage({
        ...pkg,
        amount_cents: Number(pkg.amount_cents),
        credits: Number(pkg.credits),
        sort_order: Number(pkg.sort_order || 0),
        badge: pkg.badge || null,
        enabled: !!pkg.enabled,
        admin_password: adminPassword,
      });
      setPkg(emptyPackage);
      load();
      setMsg("套餐已保存");
      setMsgKind("ok");
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    } finally {
      setSaving("");
    }
  }

  async function disablePackage(id) {
    if (!window.confirm("确认停用该套餐？历史订单不受影响。")) return;
    const adminPassword = await promptAdminPassword("停用支付套餐");
    if (!adminPassword) return;
    setMsg("");
    setMsgKind("ok");
    try {
      await api.adminDisablePaymentPackage(id, { admin_password: adminPassword });
      load();
      setMsg("套餐已停用");
      setMsgKind("ok");
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    }
  }

  async function saveProvider(provider) {
    const body = forms[provider];
    if (!body) return;
    if (body.mode === "live" || body.enabled) {
      const notifyUrl = body.public_config?.notify_url || "默认 PUBLIC_BASE_URL 回调";
      if (!window.confirm(`${providerLabel(provider)} 将保存为 ${body.enabled ? "启用" : "停用"} / ${body.mode}。\n回调地址：${notifyUrl}\n该配置会影响真实收款，确认继续？`)) return;
    }
    const adminPassword = await promptAdminPassword(`保存${providerLabel(provider)}配置`);
    if (!adminPassword) return;
    const cleaned = {
      ...body,
      admin_password: adminPassword,
      secret_config: Object.fromEntries(
        Object.entries(body.secret_config || {}).filter(([, value]) => String(value || "").trim()),
      ),
    };
    setMsg("");
    setMsgKind("ok");
    setSaving(provider);
    try {
      await api.adminSavePaymentProvider(provider, cleaned);
      load();
      setSecretNonce((n) => n + 1);
      setMsg(`${providerLabel(provider)} 已保存`);
      setMsgKind("ok");
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    } finally {
      setSaving("");
    }
  }

  function setProviderField(provider, section, key, value) {
    const current = forms[provider] || { provider, public_config: {}, secret_config: {} };
    setForms({
      ...forms,
      [provider]: {
        ...current,
        [section]: { ...(current[section] || {}), [key]: value },
      },
    });
  }

  function setProviderRoot(provider, key, value) {
    const current = forms[provider] || { provider, public_config: {}, secret_config: {} };
    setForms({ ...forms, [provider]: { ...current, [key]: value } });
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-sm font-display font-semibold text-snow">充值套餐</div>
            <div className="mt-1 text-xs text-fog">前台只展示启用套餐，订单会保存下单时的价格和积分快照。</div>
          </div>
          <button onClick={() => setPkg(emptyPackage)} className="btn-secondary btn-sm">新增套餐</button>
        </div>
        {msg && <p className={`mb-3 text-sm ${msgKind === "bad" ? "text-bad" : "text-ok"}`}>{msg}</p>}
        <div className="mb-4 grid gap-2 md:grid-cols-7">
          <input className="input" placeholder="ID" value={pkg.id}
            onChange={(e) => setPkg({ ...pkg, id: e.target.value })} />
          <input className="input" placeholder="名称" value={pkg.title}
            onChange={(e) => setPkg({ ...pkg, title: e.target.value })} />
          <input className="input" type="number" min="1" placeholder="金额(分)" value={pkg.amount_cents}
            onChange={(e) => setPkg({ ...pkg, amount_cents: e.target.value })} />
          <input className="input" type="number" min="1" placeholder="积分" value={pkg.credits}
            onChange={(e) => setPkg({ ...pkg, credits: e.target.value })} />
          <input className="input" placeholder="角标" value={pkg.badge || ""}
            onChange={(e) => setPkg({ ...pkg, badge: e.target.value })} />
          <label className="flex items-center gap-2 rounded-xl border border-line bg-base2/60 px-3 text-xs text-mist">
            <input type="checkbox" className="accent-iris" checked={!!pkg.enabled}
              onChange={(e) => setPkg({ ...pkg, enabled: e.target.checked })} />
            启用
          </label>
          <div className="flex gap-2">
            <input className="input w-20" type="number" placeholder="排序" value={pkg.sort_order}
              onChange={(e) => setPkg({ ...pkg, sort_order: e.target.value })} />
            <button onClick={savePackage} disabled={saving === "package"} className="btn-primary whitespace-nowrap">
              保存
            </button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead><tr className="border-b border-line">
              <Th>ID</Th><Th>名称</Th><Th>金额</Th><Th>积分</Th><Th>角标</Th><Th>状态</Th><Th>操作</Th>
            </tr></thead>
            <tbody>
              {packages.map((p) => (
                <tr key={p.id} className="border-b border-line/60 text-mist hover:bg-white/5">
                  <td className="py-2 pr-3 font-mono text-xs text-snow">{p.id}</td>
                  <td className="pr-3">{p.title}</td>
                  <td className="pr-3">￥{(p.amount_cents / 100).toFixed(2)}</td>
                  <td className="pr-3 font-display text-snow">{p.credits}</td>
                  <td className="pr-3">{p.badge || "-"}</td>
                  <td className="pr-3">
                    <span className={`badge ${p.enabled ? "bg-ok/15 text-ok" : "bg-white/10 text-fog"}`}>
                      {p.enabled ? "启用" : "停用"}
                    </span>
                  </td>
                  <td className="py-1">
                    <button onClick={() => setPkg({ ...p, badge: p.badge || "" })} className="btn-secondary btn-sm">编辑</button>
                    {p.enabled && <button onClick={() => disablePackage(p.id)} className="btn-ghost btn-sm ml-1">停用</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {providers.map((p) => {
          const form = forms[p.provider] || { provider: p.provider, public_config: {}, secret_config: {} };
          return (
            <Card key={p.provider}>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-sm font-display font-semibold text-snow">{providerLabel(p.provider)}</div>
                  <div className="mt-1 flex flex-wrap gap-1 text-xs">
                    <span className={`badge ${p.ready ? "bg-ok/15 text-ok" : "bg-warn/15 text-warn"}`}>
                      {p.ready ? "可用" : "未就绪"}
                    </span>
                    <span className="badge bg-white/10 text-mist">{p.mode}</span>
                    {p.source === "env" && <span className="badge bg-aqua/15 text-aqua">env</span>}
                  </div>
                </div>
                <button onClick={() => saveProvider(p.provider)} disabled={saving === p.provider} className="btn-primary btn-sm">
                  保存渠道
                </button>
              </div>
              {p.issues?.length > 0 && (
                <div className="mb-3 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
                  {p.issues.join("；")}
                </div>
              )}
              <div className="mb-3 flex flex-wrap items-center gap-3 text-sm text-mist">
                <label className="flex items-center gap-2">
                  启用
                  <input type="checkbox" className="accent-iris" checked={!!form.enabled}
                    onChange={(e) => setProviderRoot(p.provider, "enabled", e.target.checked)} />
                </label>
                <select className="select w-auto" value={form.mode || "mock"}
                  onChange={(e) => setProviderRoot(p.provider, "mode", e.target.value)}>
                  <option value="mock">mock</option>
                  <option value="live">live</option>
                </select>
              </div>
              {p.provider === "alipay" ? (
                <div className="space-y-2">
                  <PayInput label="APP_ID" value={form.public_config?.app_id || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "app_id", v)} />
                  <PayInput label="商户 PID / seller_id" value={form.public_config?.seller_id || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "seller_id", v)} />
                  <PayInput label="网关地址" value={form.public_config?.gateway_url || ""}
                    placeholder="默认 https://openapi.alipay.com/gateway.do"
                    onChange={(v) => setProviderField(p.provider, "public_config", "gateway_url", v)} />
                  <PayInput label="通知地址" value={form.public_config?.notify_url || ""}
                    placeholder="默认 PUBLIC_BASE_URL/api/payments/alipay/notify"
                    onChange={(v) => setProviderField(p.provider, "public_config", "notify_url", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:private_key`} label="应用私钥" configured={p.secret_config_masked?.private_key}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "private_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:public_key`} label="支付宝公钥" configured={p.secret_config_masked?.public_key}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "public_key", v)} />
                </div>
              ) : (
                <div className="space-y-2">
                  <PayInput label="APPID" value={form.public_config?.appid || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "appid", v)} />
                  <PayInput label="商户号" value={form.public_config?.mchid || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "mchid", v)} />
                  <PayInput label="商户证书序列号" value={form.public_config?.serial_no || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "serial_no", v)} />
                  <PayInput label="平台证书序列号" value={form.public_config?.platform_serial_no || ""}
                    onChange={(v) => setProviderField(p.provider, "public_config", "platform_serial_no", v)} />
                  <PayInput label="网关地址" value={form.public_config?.gateway_url || ""}
                    placeholder="默认 https://api.mch.weixin.qq.com"
                    onChange={(v) => setProviderField(p.provider, "public_config", "gateway_url", v)} />
                  <PayInput label="通知地址" value={form.public_config?.notify_url || ""}
                    placeholder="默认 PUBLIC_BASE_URL/api/payments/wechat/notify"
                    onChange={(v) => setProviderField(p.provider, "public_config", "notify_url", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:private_key`} label="商户私钥" configured={p.secret_config_masked?.private_key}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "private_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:api_v3_key`} label="APIv3 Key" configured={p.secret_config_masked?.api_v3_key}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "api_v3_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:platform_cert_pem`} label="平台证书 PEM" configured={p.secret_config_masked?.platform_cert_pem}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "platform_cert_pem", v)} />
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}

function providerLabel(provider) {
  return provider === "wechat" ? "微信支付" : "支付宝";
}

function promptAdminPassword(action) {
  return promptPassword(`${action}需要管理员密码确认`, "管理员密码");
}

function promptPassword(title, placeholder = "密码") {
  if (typeof document === "undefined") return Promise.resolve("");
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm";
    const panel = document.createElement("form");
    panel.className = "panel w-full max-w-sm p-5";
    const h = document.createElement("div");
    h.className = "mb-3 text-sm font-display font-semibold text-snow";
    h.textContent = title;
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "current-password";
    input.placeholder = placeholder;
    input.className = "input w-full";
    const actions = document.createElement("div");
    actions.className = "mt-4 flex justify-end gap-2";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn-secondary btn-sm";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.type = "submit";
    ok.className = "btn-primary btn-sm";
    ok.textContent = "确认";
    actions.append(cancel, ok);
    panel.append(h, input, actions);
    wrap.append(panel);
    const cleanup = (value) => {
      input.value = "";
      wrap.remove();
      resolve(value);
    };
    cancel.addEventListener("click", () => cleanup(""));
    wrap.addEventListener("click", (e) => { if (e.target === wrap) cleanup(""); });
    panel.addEventListener("submit", (e) => {
      e.preventDefault();
      cleanup(input.value);
    });
    document.body.append(wrap);
    input.focus();
  });
}

function PayInput({ label, value, onChange, placeholder = "" }) {
  return (
    <label className="grid gap-1 text-xs text-fog">
      <span>{label}</span>
      <input className="input w-full" value={value || ""} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function PaySecret({ label, configured, onChange }) {
  const [value, setValue] = useState("");
  function update(next) {
    setValue(next);
    onChange(next);
  }
  return (
    <label className="grid gap-1 text-xs text-fog">
      <span className="flex items-center justify-between gap-2">
        <span>{label}{configured ? <b className="ml-2 font-normal text-ok">已配置</b> : null}</span>
        {configured ? (
          <button type="button" className="text-warn hover:text-snow"
            onClick={() => {
              if (window.confirm(`确认清空${label}？保存渠道后生效。`)) update("__clear__");
            }}>
            清空
          </button>
        ) : null}
      </span>
      <textarea className="input min-h-20 w-full resize-y py-2" placeholder="留空表示不修改"
        value={value}
        onChange={(e) => update(e.target.value)} />
    </label>
  );
}

function Settings() {
  const [s, setS] = useState(null);
  const [cfg, setCfg] = useState(null);
  const [gw, setGw] = useState(null);
  const [msg, setMsg] = useState("");
  const [msgKind, setMsgKind] = useState("ok");

  useEffect(() => {
    api.adminSettings().then(setS).catch((e) => {
      setMsg(e.message);
      setMsgKind("bad");
    });
    api.config().then(setCfg).catch(() => {});
    api.adminGateway().then(setGw).catch((e) => {
      setMsg(e.message);
      setMsgKind("bad");
    });
  }, []);

  async function save() {
    const adminPassword = await promptAdminPassword("保存平台设置");
    if (!adminPassword) return;
    setMsg("");
    setMsgKind("ok");
    try {
      const r = await api.adminSaveSettings({
        reverse_prompt_enabled: !!s.reverse_prompt_enabled,
        sms_auth_enabled: !!s.sms_auth_enabled,
        payment_enabled: !!s.payment_enabled,
        image_n: Number(s.image_n),
        image_size: s.image_size,
        asset_retention_days: Number(s.asset_retention_days),
        audit_retention_days: Number(s.audit_retention_days),
        admin_password: adminPassword,
      });
      setS(r);
      setMsg("已保存");
      setMsgKind("ok");
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    }
  }

  if (!s) {
    return (
      <Card>
        <span className="text-mist">加载中…</span>
        {msg && <span className="ml-3 text-sm text-bad">{msg}</span>}
      </Card>
    );
  }
  const sizes = cfg?.image_sizes?.length ? cfg.image_sizes : [s.image_size || "1024x1024"];
  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 text-sm font-display font-semibold text-snow">平台默认</div>
        <div className="flex flex-wrap items-center gap-4 text-sm text-mist">
          <label className="flex items-center gap-2">
            启用反推功能
            <input type="checkbox" className="accent-iris" checked={!!s.reverse_prompt_enabled}
              onChange={(e) => setS({ ...s, reverse_prompt_enabled: e.target.checked })} />
          </label>
          <label className="flex items-center gap-2">
            短信验证码注册
            <input type="checkbox" className="accent-iris" checked={!!s.sms_auth_enabled}
              onChange={(e) => setS({ ...s, sms_auth_enabled: e.target.checked })} />
          </label>
          <label className="flex items-center gap-2">
            支付充值功能
            <input type="checkbox" className="accent-iris" checked={!!s.payment_enabled}
              onChange={(e) => setS({ ...s, payment_enabled: e.target.checked })} />
          </label>
          <label className="flex items-center gap-2">
            出图数量
            <input type="number" min="1" max="8" className="input w-16"
              value={s.image_n} onChange={(e) => setS({ ...s, image_n: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            默认尺寸
            <select className="select w-auto" value={s.image_size} onChange={(e) => setS({ ...s, image_size: e.target.value })}>
              {sizes.map((sz) => <option key={sz}>{sz}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-2">
            素材保留天数
            <input type="number" min="1" max="365" className="input w-20"
              value={s.asset_retention_days ?? 30}
              onChange={(e) => setS({ ...s, asset_retention_days: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            审计保留天数
            <input type="number" min="1" max="3650" className="input w-20"
              value={s.audit_retention_days ?? 90}
              onChange={(e) => setS({ ...s, audit_retention_days: e.target.value })} />
          </label>
          <button onClick={save} className="btn-primary">保存</button>
          {msg && <span className={msgKind === "ok" ? "text-ok" : "text-bad"}>{msg}</span>}
        </div>
      </Card>
      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-display font-semibold text-snow">短信认证与支付开启配置指引</div>
            <div className="mt-1 text-xs text-fog">两个功能默认关闭。先完成环境变量和商户/短信渠道配置，再打开上方开关。</div>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className={`badge ${s.sms_auth_enabled ? "bg-ok/15 text-ok" : "bg-white/10 text-fog"}`}>
              短信{s.sms_auth_enabled ? "已开启" : "默认关闭"}
            </span>
            <span className={`badge ${s.payment_enabled ? "bg-ok/15 text-ok" : "bg-white/10 text-fog"}`}>
              支付{s.payment_enabled ? "已开启" : "默认关闭"}
            </span>
          </div>
        </div>
        <div className="grid gap-5 lg:grid-cols-2">
          <div className="space-y-3 text-sm text-mist">
            <div className="font-display text-xs font-semibold uppercase tracking-wide text-fog">短信验证码</div>
            <ol className="list-decimal space-y-2 pl-5">
              <li>准备真实短信服务，推荐用内部 HTTP 网关封装云厂商短信能力。</li>
              <li>后端环境配置 <code className="font-mono text-snow">SMS_PROVIDER=http</code>、<code className="font-mono text-snow">SMS_HTTP_URL</code>、可选 <code className="font-mono text-snow">SMS_HTTP_API_KEY</code>。</li>
              <li>按供应商模板配置 <code className="font-mono text-snow">SMS_SIGN_NAME</code> 和 <code className="font-mono text-snow">SMS_TEMPLATE_CODE</code>。</li>
              <li>短信 HTTP 网关需接收 <code className="font-mono text-snow">phone</code>、<code className="font-mono text-snow">code</code>、<code className="font-mono text-snow">sign_name</code>、<code className="font-mono text-snow">template_code</code> 并返回 2xx。</li>
              <li>重启后端服务，先添加手机号白名单，再打开“短信验证码注册”，用白名单手机号测试发送和注册。</li>
            </ol>
          </div>
          <div className="space-y-3 text-sm text-mist">
            <div className="font-display text-xs font-semibold uppercase tracking-wide text-fog">支付宝 / 微信支付</div>
            <ol className="list-decimal space-y-2 pl-5">
              <li>域名和回调先配置为你的 HTTPS 生产域名，例如 <code className="font-mono text-snow">PUBLIC_BASE_URL=https://dream.aiwuq.cn</code>、<code className="font-mono text-snow">PAYMENT_FRONTEND_BASE_URL=https://dream.aiwuq.cn</code>。</li>
              <li>设置长度至少 32 位的 <code className="font-mono text-snow">PAYMENT_CONFIG_SECRET</code>，用于加密后台保存的商户密钥。</li>
              <li>在“支付设置”里配置套餐价格、积分数量、支付宝 APP_ID/商户 PID/应用私钥/支付宝公钥，或微信 APPID/商户号/证书序列号/APIv3 Key/平台证书。</li>
              <li>支付平台后台配置回调：支付宝 <code className="font-mono text-snow">/api/payments/alipay/notify</code>，微信 <code className="font-mono text-snow">/api/payments/wechat/notify</code>。</li>
              <li>支付渠道显示“可用”后，打开“支付充值功能”，用小额套餐测试二维码、回调入账和订单状态。</li>
            </ol>
          </div>
        </div>
      </Card>
      {gw && (
        <Card>
          <div className="mb-2 text-sm font-display font-semibold text-snow">网关状态(只读)</div>
          <div className="space-y-1.5 text-sm text-mist">
            <div>base_url:<span className="ml-2 font-mono text-snow">{gw.base_url || "(未配置)"}</span></div>
            <div>api_key:<span className="ml-2 font-mono text-snow">{gw.api_key_masked || "(未配置)"}</span></div>
            <div className="flex items-center gap-2">
              模式:
              <span className={`badge ${gw.mock_mode ? "bg-warn/15 text-warn" : "bg-ok/15 text-ok"}`}>
                {gw.mock_mode ? "MOCK(占位)" : "真实网关"}
              </span>
            </div>
            <div className="text-xs text-fog">通用超时 {gw.timeout_seconds}s · 重试 {gw.max_retries} 次</div>
            {gw.image && (
              <div className="text-xs text-fog">
                图片生成 {gw.image.timeout_seconds}s · 图片下载 {gw.image.download_timeout_seconds}s
              </div>
            )}
            {gw.video && (
              <div className="mt-3 border-t border-line pt-3">
                <div className="mb-1 font-display text-xs font-medium text-fog">视频网关</div>
                <div>base_url:<span className="ml-2 font-mono text-snow">{gw.video.base_url || "(未配置)"}</span></div>
                <div>api_key:<span className="ml-2 font-mono text-snow">{gw.video.api_key_masked || "(未配置)"}</span></div>
                <div className="flex items-center gap-2">
                  格式 <span className="badge bg-white/10 text-mist">{gw.video.format}</span>
                  <span className={`badge ${gw.video.mock_mode ? "bg-warn/15 text-warn" : "bg-ok/15 text-ok"}`}>
                    {gw.video.mock_mode ? "MOCK" : "真实网关"}
                  </span>
                </div>
                <div className="text-xs text-fog">
                  提交超时 {gw.video.submit_timeout_seconds}s · 轮询窗口 {gw.video.poll_max_seconds}s · 间隔 {gw.video.poll_interval_seconds}s
                </div>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}

function Audit() {
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
