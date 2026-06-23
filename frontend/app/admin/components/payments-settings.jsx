"use client";

import { useEffect, useState } from "react";
import { api } from "../../../lib/api";
import { paymentPackageDiff, providerLabel } from "./admin-helpers";
import { Card, PayInput, PaySecret, Th } from "./admin-ui";

const MAX_PAYMENT_AMOUNT_CENTS = 100000000;
const MAX_PAYMENT_PACKAGE_CREDITS = 100000000;
const MAX_PAYMENT_CREDITS_PER_CENT = 10000;
const PACKAGE_ID_RE = /^[A-Za-z0-9_-]+$/;

export function Payments() {
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
  const [editingPackageId, setEditingPackageId] = useState("");
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

  function validatePackageForm(item) {
    const id = String(item.id || "").trim();
    const title = String(item.title || "").trim();
    if (!id) return "套餐 ID 不能为空";
    if (!PACKAGE_ID_RE.test(id)) return "套餐 ID 只能包含字母、数字、下划线和短横线";
    if (id.length > 32) return "套餐 ID 不能超过 32 个字符";
    if (!title) return "套餐名称不能为空";
    if (title.length > 64) return "套餐名称不能超过 64 个字符";
    if (!Number.isInteger(item.amount_cents) || item.amount_cents <= 0) return "金额必须是大于 0 的整数分";
    if (item.amount_cents > MAX_PAYMENT_AMOUNT_CENTS) return "金额不能超过 1,000,000 元";
    if (!Number.isInteger(item.credits) || item.credits <= 0) return "积分必须是大于 0 的整数";
    if (item.credits > MAX_PAYMENT_PACKAGE_CREDITS) return "积分不能超过 100,000,000";
    if (item.credits > item.amount_cents * MAX_PAYMENT_CREDITS_PER_CENT) return "套餐积分/价格比例异常，请核对金额和积分";
    if (!Number.isInteger(item.sort_order)) return "排序必须是整数";
    if (String(item.badge || "").length > 32) return "角标不能超过 32 个字符";
    return "";
  }

  async function savePackage() {
    const existing = editingPackageId ? packages.find((p) => p.id === editingPackageId) : null;
    const normalizedPackage = {
      ...pkg,
      id: String(pkg.id || "").trim(),
      title: String(pkg.title || "").trim(),
      amount_cents: Number(pkg.amount_cents),
      credits: Number(pkg.credits),
      sort_order: Number(pkg.sort_order || 0),
      badge: String(pkg.badge || "").trim() || null,
      enabled: !!pkg.enabled,
    };
    const validationError = validatePackageForm(normalizedPackage);
    if (validationError) {
      setMsg(validationError);
      setMsgKind("bad");
      return;
    }
    if (existing) {
      const diff = paymentPackageDiff(existing, normalizedPackage);
      if (diff.length && !window.confirm(`确认修改套餐 ${existing.id}？\n${diff.join("\n")}\n历史订单不受影响，新订单会使用新配置。`)) {
        return;
      }
    } else if (normalizedPackage.enabled) {
      const ratio = normalizedPackage.amount_cents > 0
        ? (normalizedPackage.credits / (normalizedPackage.amount_cents / 100)).toFixed(2)
        : "-";
      if (!window.confirm(
        `确认新建并启用套餐 ${normalizedPackage.id || "-"}？\n` +
        `金额：￥${(normalizedPackage.amount_cents / 100).toFixed(2)}\n` +
        `积分：${normalizedPackage.credits}\n` +
        `折算：${ratio} 积分/元\n新订单会立即可购买该套餐。`,
      )) {
        return;
      }
    }
    setMsg("");
    setMsgKind("ok");
    setSaving("package");
    try {
      await api.adminSavePaymentPackage(normalizedPackage);
      setPkg(emptyPackage);
      setEditingPackageId("");
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
    setMsg("");
    setMsgKind("ok");
    try {
      await api.adminDisablePaymentPackage(id, {});
      load();
      setMsg("套餐已停用");
      setMsgKind("ok");
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    }
  }

  function editPackage(item) {
    setPkg({ ...item, badge: item.badge || "" });
    setEditingPackageId(item.id);
  }

  async function saveProvider(provider) {
    const body = forms[provider];
    if (!body) return;
    const current = providers.find((p) => p.provider === provider);
    if (current?.source === "env") {
      setMsg(`${providerLabel(provider)} 当前由环境变量管理，不能在后台直接覆盖。`);
      setMsgKind("bad");
      return;
    }
    if (body.mode === "live" || body.enabled) {
      const notifyUrl = body.public_config?.notify_url || "默认 PUBLIC_BASE_URL 回调";
      if (!window.confirm(`${providerLabel(provider)} 将保存为 ${body.enabled ? "启用" : "停用"} / ${body.mode}。\n回调地址：${notifyUrl}\n该配置会影响真实收款，确认继续？`)) return;
    }
    const cleaned = {
      ...body,
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

  async function disableEnvProvider(provider) {
    const current = providers.find((p) => p.provider === provider);
    if (!current || current.source !== "env") return;
    if (!window.confirm(`${providerLabel(provider)} 当前由环境变量启用。\n确认写入后台停用覆盖吗？该操作会立即停止前台使用该支付渠道。`)) return;
    setMsg("");
    setMsgKind("ok");
    setSaving(provider);
    try {
      await api.adminSavePaymentProvider(provider, {
        provider,
        enabled: false,
        mode: "mock",
        public_config: {},
        secret_config: {},
      });
      load();
      setMsg(`${providerLabel(provider)} 已写入后台停用覆盖`);
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
          <button onClick={() => { setPkg(emptyPackage); setEditingPackageId(""); }} className="btn-secondary btn-sm">新增套餐</button>
        </div>
        {msg && <p className={`mb-3 text-sm ${msgKind === "bad" ? "text-bad" : "text-ok"}`}>{msg}</p>}
        <div className="mb-4 grid gap-2 md:grid-cols-7">
          <input className="input" placeholder="ID" value={pkg.id} disabled={!!editingPackageId}
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
              {editingPackageId ? "保存编辑" : "保存"}
            </button>
          </div>
        </div>
        {editingPackageId && (
          <div className="mb-4 rounded-xl border border-aqua/30 bg-aqua/10 px-3 py-2 text-xs text-aqua">
            正在编辑套餐 {editingPackageId}，ID 已锁定。价格或积分变更只影响新订单。
          </div>
        )}
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
                    <button onClick={() => editPackage(p)} className="btn-secondary btn-sm">编辑</button>
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
          const envManaged = p.source === "env";
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
                {envManaged ? (
                  <button onClick={() => disableEnvProvider(p.provider)} disabled={saving === p.provider} className="btn-secondary btn-sm text-warn">
                    停用覆盖
                  </button>
                ) : (
                  <button onClick={() => saveProvider(p.provider)} disabled={saving === p.provider} className="btn-primary btn-sm">
                    保存渠道
                  </button>
                )}
              </div>
              {envManaged && (
                <div className="mb-3 rounded-xl border border-aqua/30 bg-aqua/10 px-3 py-2 text-xs text-aqua">
                  当前渠道由环境变量生效。需要改成后台数据库配置时，先移除对应支付环境变量并重启后端，再在这里保存。
                </div>
              )}
              {p.issues?.length > 0 && (
                <div className="mb-3 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
                  {p.issues.join("；")}
                </div>
              )}
              <div className="mb-3 flex flex-wrap items-center gap-3 text-sm text-mist">
                <label className="flex items-center gap-2">
                  启用
                  <input type="checkbox" className="accent-iris" checked={!!form.enabled}
                    disabled={envManaged}
                    onChange={(e) => setProviderRoot(p.provider, "enabled", e.target.checked)} />
                </label>
                <select className="select w-auto" value={form.mode || "mock"} disabled={envManaged}
                  onChange={(e) => setProviderRoot(p.provider, "mode", e.target.value)}>
                  <option value="mock">mock</option>
                  <option value="live">live</option>
                </select>
              </div>
              {p.provider === "alipay" ? (
                <div className="space-y-2">
                  <PayInput label="APP_ID" value={form.public_config?.app_id || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "app_id", v)} />
                  <PayInput label="商户 PID / seller_id" value={form.public_config?.seller_id || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "seller_id", v)} />
                  <PayInput label="网关地址" value={form.public_config?.gateway_url || ""} disabled={envManaged}
                    placeholder="默认 https://openapi.alipay.com/gateway.do"
                    onChange={(v) => setProviderField(p.provider, "public_config", "gateway_url", v)} />
                  <PayInput label="通知地址" value={form.public_config?.notify_url || ""} disabled={envManaged}
                    placeholder="默认 PUBLIC_BASE_URL/api/payments/alipay/notify"
                    onChange={(v) => setProviderField(p.provider, "public_config", "notify_url", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:private_key`} label="应用私钥" configured={p.secret_config_masked?.private_key} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "private_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:public_key`} label="支付宝公钥" configured={p.secret_config_masked?.public_key} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "public_key", v)} />
                </div>
              ) : (
                <div className="space-y-2">
                  <PayInput label="APPID" value={form.public_config?.appid || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "appid", v)} />
                  <PayInput label="商户号" value={form.public_config?.mchid || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "mchid", v)} />
                  <PayInput label="商户证书序列号" value={form.public_config?.serial_no || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "serial_no", v)} />
                  <PayInput label="平台证书序列号" value={form.public_config?.platform_serial_no || ""} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "public_config", "platform_serial_no", v)} />
                  <PayInput label="网关地址" value={form.public_config?.gateway_url || ""} disabled={envManaged}
                    placeholder="默认 https://api.mch.weixin.qq.com"
                    onChange={(v) => setProviderField(p.provider, "public_config", "gateway_url", v)} />
                  <PayInput label="通知地址" value={form.public_config?.notify_url || ""} disabled={envManaged}
                    placeholder="默认 PUBLIC_BASE_URL/api/payments/wechat/notify"
                    onChange={(v) => setProviderField(p.provider, "public_config", "notify_url", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:private_key`} label="商户私钥" configured={p.secret_config_masked?.private_key} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "private_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:api_v3_key`} label="APIv3 Key" configured={p.secret_config_masked?.api_v3_key} disabled={envManaged}
                    onChange={(v) => setProviderField(p.provider, "secret_config", "api_v3_key", v)} />
                  <PaySecret key={`${secretNonce}:${p.provider}:platform_cert_pem`} label="平台证书 PEM" configured={p.secret_config_masked?.platform_cert_pem} disabled={envManaged}
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

export function Settings() {
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
    setMsg("");
    setMsgKind("ok");
    try {
      const r = await api.adminSaveSettings({
        reverse_prompt_enabled: !!s.reverse_prompt_enabled,
        sms_auth_enabled: !!s.sms_auth_enabled,
        payment_enabled: !!s.payment_enabled,
        content_safety_enabled: !!s.content_safety_enabled,
        content_safety_banned_terms: s.content_safety_banned_terms || "",
        image_n: Number(s.image_n),
        image_size: s.image_size,
        asset_retention_days: Number(s.asset_retention_days),
        audit_retention_days: Number(s.audit_retention_days),
        admin_api_rate_per_hour: Number(s.admin_api_rate_per_hour),
        admin_quota_grant_single_limit: Number(s.admin_quota_grant_single_limit),
        admin_quota_grant_daily_limit: Number(s.admin_quota_grant_daily_limit),
        review_task_sla_minutes: Number(s.review_task_sla_minutes),
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
          <label className="flex items-center gap-2">
            管理请求/小时
            <input type="number" min="10" max="100000" className="input w-24"
              value={s.admin_api_rate_per_hour ?? 600}
              onChange={(e) => setS({ ...s, admin_api_rate_per_hour: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            单次发放上限
            <input type="number" min="1" max="100000000" className="input w-28"
              value={s.admin_quota_grant_single_limit ?? 100000}
              onChange={(e) => setS({ ...s, admin_quota_grant_single_limit: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            每日发放上限
            <input type="number" min="1" max="1000000000" className="input w-28"
              value={s.admin_quota_grant_daily_limit ?? 500000}
              onChange={(e) => setS({ ...s, admin_quota_grant_daily_limit: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            待对账 SLA(分钟)
            <input type="number" min="1" max="10080" className="input w-24"
              value={s.review_task_sla_minutes ?? 30}
              onChange={(e) => setS({ ...s, review_task_sla_minutes: e.target.value })} />
          </label>
          <button onClick={save} className="btn-primary">保存</button>
          {msg && <span className={msgKind === "ok" ? "text-ok" : "text-bad"}>{msg}</span>}
        </div>
      </Card>
      <Card>
        <div className="mb-3 text-sm font-display font-semibold text-snow">内容安全</div>
        <div className="space-y-3 text-sm text-mist">
          <label className="flex items-center gap-2">
            启用提示词禁止词拦截
            <input
              type="checkbox"
              className="accent-iris"
              checked={!!s.content_safety_enabled}
              onChange={(e) => setS({ ...s, content_safety_enabled: e.target.checked })}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-fog">禁止词，一行一个或用逗号分隔</span>
            <textarea
              className="textarea min-h-[96px] w-full font-mono text-xs"
              value={s.content_safety_banned_terms || ""}
              onChange={(e) => setS({ ...s, content_safety_banned_terms: e.target.value })}
              placeholder="例如：违规词1&#10;违规词2"
            />
          </label>
          <p className="text-xs text-fog">
            开启后，生成和反推请求会在调用模型前检查用户输入文本；结果图/视频审核仍需接入独立内容审核服务。
          </p>
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
              <li>重启后端服务，打开“短信验证码注册”，用新手机号测试发送验证码和注册。</li>
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
