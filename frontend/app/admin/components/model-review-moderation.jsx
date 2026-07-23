"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Plus, Search, Trash2 } from "lucide-react";
import { api } from "../../../lib/api";
import {
  confirmReviewTaskAction,
  formatMinutes,
  modelUseLabel,
  reportReasonLabel,
  reportStatusLabel,
} from "./admin-helpers";
import { Card, Th } from "./admin-ui";
import { ModelCatalogImporter } from "./model-catalog-importer";

function validateExternalResultUrl(value, category) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  let url;
  try {
    url = new URL(trimmed);
  } catch (e) {
    throw new Error("结果 URL 格式不正确");
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("结果 URL 仅支持 http/https");
  }
  const path = `${url.pathname}${url.search}`.toLowerCase();
  const imageLike = /\.(png|jpe?g|webp|gif)(\?|$|&)/i.test(path);
  const videoLike = /\.(mp4|webm|mov|m4v)(\?|$|&)/i.test(path);
  if (category === "image" && !imageLike && !window.confirm("URL 看起来不像图片文件，仍继续交给后端校验？")) {
    throw new Error("已取消补结果");
  }
  if (category === "video" && !videoLike && !window.confirm("URL 看起来不像视频文件，仍继续交给后端校验？")) {
    throw new Error("已取消补结果");
  }
  return url.toString();
}

export function Models() {
  const [rows, setRows] = useState([]);
  const [providers, setProviders] = useState({});
  const [providerConnections, setProviderConnections] = useState([]);
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState("ok");
  const [probing, setProbing] = useState({});
  const [busy, setBusy] = useState({});
  const [expanded, setExpanded] = useState({});
  const [useFilter, setUseFilter] = useState("all");
  const [query, setQuery] = useState("");
  const probeSeqRef = useRef(new Map());
  const draftSeqRef = useRef(0);
  const PROBE_KEYS = new Set(["use", "provider_config_id", "provider", "base_url", "gateway_format", "api_key", "api_key_clear"]);

  function rowKey(row) {
    return row.id == null ? row.draftKey : String(row.id);
  }

  function normalizeRow(row) {
    const { api_key: _apiKey, api_key_encrypted: _encryptedKey, ...safeRow } = row || {};
    return {
      ...safeRow,
      provider_config_id: row.provider_config_id || null,
      display_name: row.display_name || row.model_id || "",
      provider: row.provider === "env" ? "" : (row.provider || ""),
      base_url: row.base_url || "",
      gateway_format: row.gateway_format || (row.use === "prompt" ? "anthropic" : row.use === "video" ? "ark" : "openai"),
      api_key: "",
      api_key_clear: false,
      is_default: !!row.is_default,
      sort_order: Number(row.sort_order || 0),
      probeModels: [],
      probeSignature: "",
      extraText: row.extra ? JSON.stringify(row.extra, null, 2) : "",
    };
  }

  const load = () => api.adminModels()
    .then((payload) => {
      const list = Array.isArray(payload) ? payload : (payload.models || []);
      setProviders(payload.providers || {});
      setProviderConnections(payload.provider_connections || []);
      setRows(list.map(normalizeRow));
    })
    .catch((e) => {
      setMsgType("bad");
      setMsg(e.message);
    });
  useEffect(() => { load(); }, []);

  function addDraft() {
    const draftKey = `draft-${++draftSeqRef.current}`;
    setExpanded((current) => ({ ...current, [draftKey]: true }));
    setRows((prev) => [
      normalizeRow({
        id: null,
        draftKey,
        use: "vision",
        display_name: "",
        model_id: "",
        cost_credits: 2,
        unlock_cost: 0,
        enabled: true,
        is_default: false,
        sort_order: prev.length * 10 + 10,
        extra: null,
      }),
      ...prev,
    ]);
    setMsg("");
  }

  function removeDraft(row) {
    setRows((prev) => prev.filter((item) => rowKey(item) !== rowKey(row)));
  }

  function set(i, key, val) {
    setRows((prev) => {
      const copy = [...prev];
      const next = { ...copy[i], [key]: val };
      if (PROBE_KEYS.has(key)) {
        next.probeModels = [];
        next.probeSignature = "";
        probeSeqRef.current.delete(rowKey(next));
      }
      copy[i] = next;
      return copy;
    });
  }

  function probeSignature(r) {
    return JSON.stringify({
      use: r.use || "",
      provider_config_id: r.provider_config_id || null,
      provider: r.provider || "",
      base_url: r.base_url || "",
      gateway_format: r.gateway_format || "",
      api_key_present: Boolean(r.api_key),
      api_key_length: r.api_key ? String(r.api_key).length : 0,
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
        base_url: !provider ? "" : hasPresetBaseUrl ? (preset.base_url || "") : (current.base_url || ""),
        gateway_format: preset.gateway_format || current.gateway_format || "openai",
        api_key_clear: !provider && current.api_key_configured ? true : current.api_key_clear,
        probeModels: [],
        probeSignature: "",
      };
      probeSeqRef.current.delete(rowKey(current));
      return copy;
    });
  }

  function setProviderConnection(i, value) {
    const providerConfigId = Number(value) || null;
    const connection = providerConnections.find((item) => item.id === providerConfigId) || null;
    setRows((prev) => {
      const copy = [...prev];
      const current = copy[i];
      copy[i] = {
        ...current,
        provider_config_id: providerConfigId,
        provider: connection?.provider || "",
        gateway_format: connection?.gateway_format || "openai",
        base_url: "",
        api_key: "",
        api_key_clear: false,
        display_name: current.display_name === current.model_id ? "" : current.display_name,
        model_id: "",
        extraText: "",
        probeModels: [],
        probeSignature: "",
      };
      probeSeqRef.current.delete(rowKey(current));
      return copy;
    });
    setMsg("");
  }

  function chooseProbedModel(i, modelId) {
    setRows((prev) => {
      const copy = [...prev];
      const current = copy[i];
      const model = (current.probeModels || []).find((item) => item.id === modelId);
      if (!model) return prev;
      copy[i] = {
        ...current,
        model_id: model.id,
        display_name: current.display_name || model.id,
        extraText: model.default_extra ? JSON.stringify(model.default_extra, null, 2) : "",
      };
      return copy;
    });
  }

  function parseAndValidate(row) {
    const displayName = String(row.display_name || "").trim();
    const modelId = String(row.model_id || "").trim();
    if (!displayName) throw new Error("请填写前端显示名称。");
    if (!modelId) throw new Error("请填写模型 ID。");
    let extra = null;
    if ((row.extraText || "").trim()) {
      extra = JSON.parse(row.extraText);
      if (!extra || Array.isArray(extra) || typeof extra !== "object") {
        throw new Error("extra 必须是 JSON 对象。");
      }
    }
    const cost = Number(row.cost_credits);
    const unlockCost = Number(row.unlock_cost);
    const sortOrder = Number(row.sort_order);
    if (!Number.isFinite(cost) || cost < 1) throw new Error("调用消耗积分必须大于等于 1。");
    if (!Number.isFinite(unlockCost) || unlockCost < 0) throw new Error("解锁消耗积分不能小于 0。");
    if (!Number.isInteger(sortOrder)) throw new Error("排序值必须是整数。");
    const gatewayPayload = row.id == null
      ? { provider_config_id: Number(row.provider_config_id) || null }
      : {
          provider: row.provider || null,
          base_url: row.base_url || null,
          gateway_format: row.gateway_format || null,
          api_key: row.api_key || null,
          api_key_clear: !!row.api_key_clear,
        };
    if (row.id == null && !gatewayPayload.provider_config_id) {
      throw new Error("请选择已有供应商并完成模型探测。");
    }
    return {
      use: row.use,
      display_name: displayName,
      model_id: modelId,
      ...gatewayPayload,
      cost_credits: cost,
      unlock_cost: unlockCost,
      enabled: !!row.enabled,
      is_default: !!row.is_default,
      sort_order: sortOrder,
      extra,
    };
  }

  async function save(r) {
    setMsg("");
    setMsgType("ok");
    const key = rowKey(r);
    try {
      if (probing[key]) throw new Error("模型探测仍在进行，请等待探测完成后再保存。");
      const payload = parseAndValidate(r);
      const providerConnection = providerConnections.find((item) => item.id === payload.provider_config_id);
      const summary = [
        `名称：${payload.display_name}`,
        `用途：${modelUseLabel(payload.use)}`,
        `提供商：${providerConnection?.label || payload.provider || "环境变量兜底"}`,
        `模型：${payload.model_id}`,
        `调用消耗：${payload.cost_credits} 积分`,
        `解锁消耗：${payload.unlock_cost} 积分`,
        `排序：${payload.sort_order}`,
        `状态：${payload.enabled ? "启用" : "停用"}${payload.is_default ? " / 默认" : ""}`,
      ];
      if (r.id != null) summary.splice(4, 0, `Base URL：${payload.base_url || "-"}`);
      if (r.api_key || r.api_key_clear) {
        summary.push(r.api_key_clear ? "API Key：将清空" : "API Key：将更新");
      }
      const action = r.id == null ? "新增" : "保存";
      if (!window.confirm(`确认${action}模型配置？\n${summary.join("\n")}\n变更只影响后续新任务。`)) return;
      setBusy((prev) => ({ ...prev, [key]: "save" }));
      if (r.id == null) await api.adminCreateModel(payload);
      else await api.adminUpdateModel(r.id, payload);
      setMsgType("ok");
      setMsg(
        r.id == null && payload.enabled
          ? `${payload.display_name} 已新增，可在创作页对应模型下拉框选择`
          : `${payload.display_name} 已${r.id == null ? "新增，启用后将进入创作页模型下拉框" : "保存"}`,
      );
      await load();
    } catch (e) {
      setMsgType("bad");
      setMsg(e instanceof SyntaxError ? `${r.display_name || modelUseLabel(r.use)} 的 extra 不是合法 JSON` : e.message);
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }

  async function removeModel(row) {
    const key = rowKey(row);
    const name = row.display_name || row.model_id;
    if (!window.confirm(`确认删除「${name}」？\n删除后将不再出现在模型配置和创作页中，历史任务记录会保留。`)) return;
    setBusy((prev) => ({ ...prev, [key]: "delete" }));
    setMsg("");
    try {
      await api.adminDeleteModel(row.id);
      setMsgType("ok");
      setMsg(`${name} 已删除`);
      await load();
    } catch (e) {
      setMsgType("bad");
      setMsg(e.message);
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }

  async function updateState(row, patch, actionLabel) {
    const key = rowKey(row);
    setMsg("");
    setMsgType("ok");
    if (row.id == null) {
      setRows((prev) => prev.map((item) => rowKey(item) === key ? { ...item, ...patch } : item));
      return;
    }
    if (patch.enabled === false && row.is_default) {
      setMsgType("bad");
      setMsg("默认模型不能直接停用，请先把同用途的其他模型设为默认。");
      return;
    }
    const targetState = patch.is_default ? "设为默认并启用" : patch.enabled ? "启用" : "停用";
    if (!window.confirm(`确认${targetState}「${row.display_name || row.model_id}」？\n变更只影响后续新任务。`)) return;
    setBusy((prev) => ({ ...prev, [key]: actionLabel }));
    try {
      await api.adminUpdateModel(row.id, patch);
      setMsgType("ok");
      setMsg(
        patch.enabled === true
          ? `${row.display_name || row.model_id} 已${targetState}，可在创作页对应模型下拉框选择`
          : `${row.display_name || row.model_id} 已${targetState}`,
      );
      await load();
    } catch (e) {
      setMsgType("bad");
      setMsg(e.message);
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }

  async function probe(r, i) {
    setMsg("");
    setMsgType("ok");
    const key = rowKey(r);
    const signature = probeSignature(r);
    probeSeqRef.current.set(key, signature);
    setProbing((prev) => ({ ...prev, [key]: signature }));
    try {
      if (r.id == null && !r.provider_config_id) {
        throw new Error("请先选择已有供应商。");
      }
      const probePayload = r.id == null
        ? { provider_config_id: r.provider_config_id, use: r.use }
        : {
            model_config_id: r.id,
            use: r.use,
            provider: r.provider || null,
            base_url: r.base_url || null,
            gateway_format: r.gateway_format || null,
            api_key: r.api_key || null,
          };
      const res = await api.adminProbeModels(probePayload);
      if (probeSeqRef.current.get(key) !== signature) {
        setMsgType("bad");
        setMsg("探测结果已过期，请按当前配置重新探测。");
        return;
      }
      setRows((prev) => {
        const copy = [...prev];
        const currentIndex = copy.findIndex((item) => rowKey(item) === key);
        const current = copy[currentIndex];
        if (!current || rowKey(current) !== key || probeSignature(current) !== signature) {
          return prev;
        }
        copy[currentIndex] = { ...current, probeModels: res.models || [], probeSignature: signature };
        return copy;
      });
      setMsgType("ok");
      setMsg(`已读取 ${res.models?.length || 0} 个模型`);
    } catch (e) {
      setMsgType("bad");
      setMsg(e.message);
    } finally {
      setProbing((prev) => {
        if (prev[key] !== signature) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  }

  const providerEntries = Object.entries(providers);
  const normalizedQuery = query.trim().toLowerCase();
  const filteredRows = rows
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => (
      (useFilter === "all" || row.use === useFilter)
      && (!normalizedQuery || `${row.display_name || ""} ${row.model_id || ""}`.toLowerCase().includes(normalizedQuery))
    ));
  const enabledCount = rows.filter((row) => row.enabled).length;

  return (
    <div className="space-y-4">
      <ModelCatalogImporter providers={providers} rows={rows} onImported={load} />
      <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="font-display text-sm font-semibold text-snow">模型目录</div>
          <div className="mt-1 text-xs text-fog">后台启用后会自动出现在对应前端模型下拉框；默认项供旧客户端和未主动选择的任务使用。</div>
        </div>
        <button type="button" className="btn-primary btn-sm inline-flex items-center gap-1.5" onClick={addDraft}>
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          手动新增
        </button>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5 text-xs">
        <span className="badge bg-white/10 text-mist">全部 {rows.length}</span>
        <span className="badge bg-ok/15 text-ok">已启用 {enabledCount}</span>
        {[
          ["vision", "视觉"],
          ["image", "图片"],
          ["video", "视频"],
          ["prompt", "对话"],
        ].map(([use, label]) => (
          <span key={use} className="badge bg-white/10 text-mist">{label} {rows.filter((row) => row.use === use).length}</span>
        ))}
      </div>
      <div className="mt-4 grid gap-2 sm:grid-cols-[10rem_minmax(0,1fr)]">
        <select className="select w-full text-xs" value={useFilter} onChange={(event) => setUseFilter(event.target.value)} aria-label="按用途筛选模型">
          <option value="all">全部用途</option>
          <option value="vision">反推 / 视觉理解</option>
          <option value="image">图片生成</option>
          <option value="video">视频生成</option>
          <option value="prompt">对话 / 提示词</option>
        </select>
        <label className="relative block">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fog" aria-hidden="true" />
          <input className="input w-full pl-9 text-xs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型名称或 ID" aria-label="搜索模型目录" />
        </label>
      </div>
      {msg && <p className={`mb-2 text-sm ${msgType === "bad" ? "text-bad" : "text-ok"}`}>{msg}</p>}
      <div className="mt-4 space-y-3">
        {filteredRows.map(({ row: r, index: i }) => {
          const key = rowKey(r);
          const isExpanded = r.id == null || !!expanded[key];
          return (
          <div key={rowKey(r)} className="rounded-lg border border-line bg-white/[0.03] p-4">
            <div className={`flex flex-wrap items-center justify-between gap-2 ${isExpanded ? "mb-4" : ""}`}>
              <div>
                <div className="font-display text-sm font-semibold text-snow">
                  {r.display_name || (r.id == null ? "新模型" : r.model_id)}
                  {r.id != null ? <span className="ml-2 font-normal text-fog">#{r.id}</span> : null}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1 text-xs">
                  <span className={`badge ${r.enabled ? "bg-ok/15 text-ok" : "bg-white/10 text-fog"}`}>
                    {r.enabled ? "已启用" : "已停用"}
                  </span>
                  {r.is_default ? <span className="badge bg-iris/15 text-iris">默认</span> : null}
                  <span className="badge bg-white/10 text-mist">{modelUseLabel(r.use)}</span>
                  {r.api_key_configured ? <span className="badge bg-aqua/15 text-aqua">API Key 已配置</span> : null}
                  <span className="badge bg-white/10 text-mist">{r.gateway_format || "openai"}</span>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                {r.id == null ? (
                  <button type="button" onClick={() => removeDraft(r)} disabled={!!busy[rowKey(r)]} className="btn-ghost btn-sm">取消</button>
                ) : (
                  <>
                    {!r.is_default ? (
                      <button type="button" onClick={() => updateState(r, { is_default: true, enabled: true }, "default")}
                        disabled={!!busy[rowKey(r)]} className="btn-secondary btn-sm">设为默认</button>
                    ) : null}
                    <button type="button" onClick={() => updateState(r, { enabled: !r.enabled }, "toggle")}
                      disabled={!!busy[rowKey(r)] || (r.is_default && r.enabled)} className="btn-ghost btn-sm">
                      {r.enabled ? "停用" : "启用"}
                    </button>
                  </>
                )}
                <button
                  type="button"
                  onClick={() => probe(r, i)}
                  disabled={!!probing[rowKey(r)] || !!busy[rowKey(r)] || (r.id == null && !r.provider_config_id)}
                  className="btn-secondary btn-sm"
                >
                  {probing[rowKey(r)] ? "探测中" : "探测模型"}
                </button>
                <button
                  type="button"
                  onClick={() => save(r)}
                  disabled={
                    !!probing[rowKey(r)]
                    || !!busy[rowKey(r)]
                    || (r.id == null && (!r.provider_config_id || !r.model_id))
                  }
                  className="btn-primary btn-sm"
                >
                  {busy[rowKey(r)] === "save" ? "保存中" : r.id == null ? "新增并保存" : "保存配置"}
                </button>
                {r.id != null ? (
                  <button
                    type="button"
                    className="btn-ghost btn-sm inline-flex items-center gap-1"
                    aria-expanded={isExpanded}
                    onClick={() => setExpanded((current) => ({ ...current, [key]: !current[key] }))}
                  >
                    {isExpanded ? "收起" : "编辑"}
                    <ChevronDown className={`h-3.5 w-3.5 transition-transform ${isExpanded ? "rotate-180" : ""}`} aria-hidden="true" />
                  </button>
                ) : null}
                {r.id != null ? (
                  <button
                    type="button"
                    onClick={() => removeModel(r)}
                    disabled={!!busy[key]}
                    className="btn-ghost btn-sm px-2 text-bad"
                    title="删除模型"
                    aria-label={`删除模型 ${r.display_name || r.model_id}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                ) : null}
              </div>
            </div>

            {isExpanded ? (
            <div className="grid gap-3 md:grid-cols-2">
              <label className="grid gap-1 text-xs text-fog">
                <span>前端显示名称</span>
                <input className="input w-full" value={r.display_name || ""} placeholder="例如 GPT Image 2 高清"
                  onChange={(e) => set(i, "display_name", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>用途</span>
                <select className="select w-full" value={r.use || "vision"}
                  onChange={(e) => set(i, "use", e.target.value)}>
                  <option value="vision">反推 / 视觉理解</option>
                  <option value="image">图片生成</option>
                  <option value="video">视频生成</option>
                  <option value="prompt">对话 / 提示词</option>
                </select>
                {r.id != null ? <span className="text-[11px] text-fog">修改后会从原用途移到新用途，仅影响后续任务。</span> : null}
              </label>
              {r.id == null ? (
                <label className="grid gap-1 text-xs text-fog md:col-span-2">
                  <span>已有供应商</span>
                  <select
                    className="select w-full"
                    value={r.provider_config_id || ""}
                    onChange={(event) => setProviderConnection(i, event.target.value)}
                    disabled={providerConnections.length === 0}
                  >
                    <option value="">选择已保存的供应商</option>
                    {providerConnections.map((connection) => (
                      <option key={connection.id} value={connection.id}>
                        {connection.label} · 凭据来源：{connection.source_display_name}
                      </option>
                    ))}
                  </select>
                  <span className="text-[11px] leading-relaxed text-fog">
                    {providerConnections.length
                      ? "复用已加密保存的供应商连接，无需再次填写 Base URL 和 API Key。"
                      : "暂无可复用供应商，请先使用上方“提供商接入向导”完成一次接入。"}
                  </span>
                </label>
              ) : (
                <>
                  <label className="grid gap-1 text-xs text-fog">
                    <span>模型提供商</span>
                    <select className="select w-full" value={r.provider || ""}
                      onChange={(e) => setProvider(i, e.target.value)}>
                      <option value="">使用环境变量兜底</option>
                      {providerEntries.map(([providerKey, p]) => (
                        <option key={providerKey} value={providerKey}>{p.label || providerKey}</option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1 text-xs text-fog">
                    <span>网关格式</span>
                    <select className="select w-full" value={r.gateway_format || "openai"}
                      onChange={(e) => set(i, "gateway_format", e.target.value)}>
                      <option value="openai">OpenAI-Compatible</option>
                      <option value="ark">火山方舟 Ark</option>
                      <option value="anthropic">Anthropic Messages</option>
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
                </>
              )}
              {r.id == null ? (
                <label className="grid gap-1 text-xs text-fog md:col-span-2">
                  <span>从探测结果选择模型</span>
                  <select
                    className="select w-full"
                    value={(r.probeModels || []).some((model) => model.id === r.model_id) ? r.model_id : ""}
                    onChange={(event) => event.target.value && chooseProbedModel(i, event.target.value)}
                    disabled={!r.provider_config_id || (r.probeModels || []).length === 0}
                  >
                    <option value="">{r.probeModels?.length ? "选择模型" : "请先探测模型"}</option>
                    {(r.probeModels || []).map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.id}{model.capability_label ? ` · ${model.capability_label}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <>
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
                      {(r.probeModels || []).map((model) => (
                        <option key={model.id} value={model.id}>{model.id}{model.owned_by ? ` · ${model.owned_by}` : ""}</option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              <label className="grid gap-1 text-xs text-fog">
                <span>调用消耗积分</span>
                <input className="input w-full" type="number" min="1" value={r.cost_credits}
                  onChange={(e) => set(i, "cost_credits", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>解锁消耗积分</span>
                <input className="input w-full" type="number" min="0" value={r.unlock_cost}
                  onChange={(e) => set(i, "unlock_cost", e.target.value)} />
              </label>
              <label className="grid gap-1 text-xs text-fog">
                <span>排序</span>
                <input className="input w-full" type="number" step="1" value={r.sort_order}
                  onChange={(e) => set(i, "sort_order", e.target.value)} />
              </label>
              {r.id == null ? (
                <div className="flex flex-wrap items-end gap-5 pb-2 text-xs text-fog">
                  <label className="flex items-center gap-2">
                    <input type="checkbox" className="accent-iris" checked={!!r.enabled} disabled={!!r.is_default}
                      onChange={(e) => set(i, "enabled", e.target.checked)} />
                    创建后启用
                  </label>
                  <label className="flex items-center gap-2">
                    <input type="checkbox" className="accent-iris" checked={!!r.is_default}
                      onChange={(e) => {
                        set(i, "is_default", e.target.checked);
                        if (e.target.checked) set(i, "enabled", true);
                      }} />
                    设为该用途默认模型
                  </label>
                </div>
              ) : null}
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
            ) : null}
          </div>
          );
        })}
      </div>
      {filteredRows.length === 0 ? <p className="py-8 text-center text-sm text-fog">{rows.length ? "没有匹配的模型。" : "暂无模型配置，请先新增模型。"}</p> : null}
      <p className="mt-3 text-xs text-fog">提示：API Key 只写入后端加密存储，页面永不回显；留空表示不修改。默认模型不能直接停用，请先设置同用途的其他默认项。</p>
    </Card>
    </div>
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
    setBusyId(task.id);
    setMsg("");
    try {
      await api.adminRefundReviewTask(task.id, { note });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusyId(null);
    }
  }

  async function settle(task) {
    const canUseLocalImageResults = task.category === "image" && task.has_local_results;
    const resultUrl = window.prompt(
      task.category === "image"
        ? (canUseLocalImageResults
            ? "填入上游已生成的图片结果 URL；留空则使用本地已保存结果"
            : "填入上游已生成的图片结果 URL")
        : "填入上游已生成的视频结果 URL"
    );
    if (resultUrl == null) return;
    if (!resultUrl.trim() && !canUseLocalImageResults) {
      setMsg("该任务没有可用的本地图片结果，请填写上游结果 URL 后再结算。");
      return;
    }
    let cleanResultUrl = "";
    try {
      cleanResultUrl = validateExternalResultUrl(resultUrl, task.category);
    } catch (e) {
      if (e.message !== "已取消补结果") setMsg(e.message);
      return;
    }
    const externalTaskId = window.prompt("外部任务 ID(可选)", task.external_task_id || "") || null;
    const note = window.prompt("结算备注(可选)", "人工补结果结算") || "";
    if (!confirmReviewTaskAction(task, "补结果结算", note, cleanResultUrl)) return;
    setBusyId(task.id);
    setMsg("");
    try {
      await api.adminSettleReviewTask(task.id, {
        result_url: cleanResultUrl,
        external_task_id: externalTaskId,
        note,
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
          <div className="text-sm font-display font-semibold text-snow">异常任务处理</div>
          <div className="mt-1 text-xs text-fog">查看历史异常任务，必要时补结果或退款。</div>
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
  const loadSeqRef = useRef(0);

  const load = () => {
    const seq = ++loadSeqRef.current;
    setMsg("");
    api.adminAssetReports({ status })
      .then((nextRows) => {
        if (seq === loadSeqRef.current) setRows(nextRows);
      })
      .catch((e) => {
        if (seq === loadSeqRef.current) setMsg(e.message);
      });
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
    setBusyId(row.id);
    setMsg("");
    try {
      await api.adminHandleAssetReport(row.id, { action, note });
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
