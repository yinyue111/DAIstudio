"use client";

import { useMemo, useState } from "react";
import {
  Check,
  Image as ImageIcon,
  LoaderCircle,
  MessageSquareText,
  PlugZap,
  Search,
  Video,
} from "lucide-react";
import { api } from "../../../lib/api";
import { modelUseLabel } from "./admin-helpers";
import { Card } from "./admin-ui";

const USE_ORDER = ["image", "video", "prompt"];
const USE_ICON = {
  image: ImageIcon,
  video: Video,
  prompt: MessageSquareText,
};

function defaultCost(rows, use) {
  const exact = rows.find((row) => row.use === use && row.is_default)
    || rows.find((row) => row.use === use);
  return Number(exact?.cost_credits || (use === "image" ? 15 : use === "video" ? 16 : 1));
}

function modelImportKey(model) {
  return `${model.id}:${model.recommended_uses?.[0] || "unsupported"}`;
}

export function ModelCatalogImporter({ providers, rows, onImported }) {
  const [connection, setConnection] = useState({
    provider: "",
    base_url: "",
    gateway_format: "openai",
    api_key: "",
  });
  const [models, setModels] = useState([]);
  const [selected, setSelected] = useState({});
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [probing, setProbing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [message, setMessage] = useState(null);

  const existing = useMemo(
    () => new Set(rows.map((row) => `${row.use}:${row.model_id}`)),
    [rows],
  );
  const visibleModels = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return models.filter((model) => {
      const use = model.recommended_uses?.[0] || "unsupported";
      if (filter !== "all" && use !== filter) return false;
      if (!needle) return true;
      return `${model.id} ${model.owned_by || ""} ${model.capability_label || ""}`
        .toLowerCase()
        .includes(needle);
    });
  }, [filter, models, query]);
  const selectedModels = Object.values(selected);
  const counts = Object.fromEntries(USE_ORDER.map((use) => [
    use,
    models.filter((model) => model.recommended_uses?.[0] === use).length,
  ]));

  function changeConnection(key, value) {
    setConnection((current) => ({ ...current, [key]: value }));
    setModels([]);
    setSelected({});
    setMessage(null);
  }

  function chooseProvider(provider) {
    const preset = providers[provider] || {};
    setConnection({
      provider,
      base_url: preset.base_url || "",
      gateway_format: preset.gateway_format || "openai",
      api_key: "",
    });
    setModels([]);
    setSelected({});
    setMessage(null);
  }

  async function probe() {
    if (!connection.provider) {
      setMessage({ type: "bad", text: "请先选择模型提供商。" });
      return;
    }
    if (!connection.base_url.trim() || !connection.api_key.trim()) {
      setMessage({ type: "bad", text: "探测前请填写 Base URL 和 API Key。" });
      return;
    }
    setProbing(true);
    setMessage(null);
    try {
      const result = await api.adminProbeModels(connection);
      setModels(result.models || []);
      setSelected({});
      setMessage({
        type: "ok",
        text: `连接成功，读取到 ${result.models?.length || 0} 个模型。`,
      });
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setProbing(false);
    }
  }

  function toggleModel(model) {
    const use = model.recommended_uses?.[0];
    if (!use) return;
    const key = modelImportKey(model);
    setSelected((current) => {
      if (current[key]) {
        const next = { ...current };
        delete next[key];
        return next;
      }
      return {
        ...current,
        [key]: {
          use,
          model_id: model.id,
          display_name: model.id,
          cost_credits: defaultCost(rows, use),
          unlock_cost: 0,
          enabled: true,
          sort_order: rows.filter((row) => row.use === use).length * 10 + 10,
          extra: model.default_extra || null,
        },
      };
    });
  }

  function updateSelection(model, key, value) {
    const selectionKey = modelImportKey(model);
    setSelected((current) => ({
      ...current,
      [selectionKey]: { ...current[selectionKey], [key]: value },
    }));
  }

  async function importSelected() {
    if (!selectedModels.length) return;
    const summary = selectedModels
      .map((model) => `${modelUseLabel(model.use)} · ${model.model_id} · ${model.cost_credits} 积分`)
      .join("\n");
    if (!window.confirm(`确认批量导入 ${selectedModels.length} 个模型？\n${summary}\n导入后立即启用，不会改变已有默认模型。`)) return;
    setImporting(true);
    setMessage(null);
    try {
      await api.adminImportModels({ ...connection, models: selectedModels });
      setMessage({ type: "ok", text: `已导入并启用 ${selectedModels.length} 个模型。` });
      setConnection((current) => ({ ...current, api_key: "" }));
      setSelected({});
      setModels([]);
      await onImported?.();
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setImporting(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 font-display text-sm font-semibold text-snow">
            <PlugZap className="h-4 w-4 text-aqua" aria-hidden="true" />
            提供商接入向导
          </div>
          <p className="mt-1 text-xs leading-relaxed text-fog">一次填写网关与密钥，探测后按图片、视频、对话能力批量导入。</p>
        </div>
        {message ? (
          <div role="status" className={`max-w-md text-xs ${message.type === "bad" ? "text-bad" : "text-ok"}`}>
            {message.text}
          </div>
        ) : null}
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <label className="grid gap-1 text-xs text-fog">
          <span>模型提供商</span>
          <select className="select w-full" value={connection.provider} onChange={(event) => chooseProvider(event.target.value)}>
            <option value="">选择提供商</option>
            {Object.entries(providers).map(([key, provider]) => (
              <option key={key} value={key}>{provider.label || key}</option>
            ))}
          </select>
          {connection.provider && providers[connection.provider]?.description ? (
            <span className="text-[11px] leading-relaxed text-fog">{providers[connection.provider].description}</span>
          ) : null}
        </label>
        <label className="grid gap-1 text-xs text-fog">
          <span>网关协议</span>
          <select className="select w-full" value={connection.gateway_format} onChange={(event) => changeConnection("gateway_format", event.target.value)}>
            <option value="openai">OpenAI-Compatible</option>
            <option value="anthropic">Anthropic Messages</option>
            <option value="ark">火山方舟 Ark</option>
          </select>
        </label>
        <label className="grid gap-1 text-xs text-fog">
          <span>Base URL</span>
          <input className="input w-full" value={connection.base_url} onChange={(event) => changeConnection("base_url", event.target.value)} placeholder="https://gateway.example.com/v1" />
        </label>
        <label className="grid gap-1 text-xs text-fog">
          <span>API Key</span>
          <input type="password" className="input w-full" autoComplete="new-password" value={connection.api_key} onChange={(event) => changeConnection("api_key", event.target.value)} placeholder="仅用于探测和加密入库" />
        </label>
      </div>
      <div className="mt-3 flex justify-end">
        <button type="button" className="btn-secondary btn-sm inline-flex items-center gap-1.5" onClick={probe} disabled={probing || importing}>
          {probing ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <PlugZap className="h-3.5 w-3.5" aria-hidden="true" />}
          {probing ? "正在探测" : "探测模型与能力"}
        </button>
      </div>

      {models.length > 0 ? (
        <div className="mt-5 border-t border-line pt-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap gap-1.5 text-xs">
              <button type="button" onClick={() => setFilter("all")} className={`badge ${filter === "all" ? "bg-iris/20 text-iris" : "bg-white/10 text-mist"}`}>全部 {models.length}</button>
              {USE_ORDER.map((use) => {
                const Icon = USE_ICON[use];
                return (
                  <button key={use} type="button" onClick={() => setFilter(use)} className={`badge inline-flex items-center gap-1 ${filter === use ? "bg-iris/20 text-iris" : "bg-white/10 text-mist"}`}>
                    <Icon className="h-3 w-3" aria-hidden="true" />
                    {modelUseLabel(use)} {counts[use]}
                  </button>
                );
              })}
            </div>
            <label className="relative block min-w-52 flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fog" aria-hidden="true" />
              <input className="input w-full pl-9 text-xs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型 ID" aria-label="搜索探测模型" />
            </label>
          </div>

          <div className="mt-3 max-h-[28rem] divide-y divide-line overflow-y-auto border-y border-line">
            {visibleModels.map((model) => {
              const use = model.recommended_uses?.[0];
              const key = modelImportKey(model);
              const choice = selected[key];
              const duplicate = use ? existing.has(`${use}:${model.id}`) : false;
              return (
                <div key={key} className="grid gap-2 py-3 sm:grid-cols-[minmax(0,1fr)_8rem_6.5rem] sm:items-center">
                  <label className={`flex min-w-0 items-start gap-2 ${!use || duplicate ? "opacity-50" : "cursor-pointer"}`}>
                    <input type="checkbox" className="mt-0.5 accent-iris" checked={!!choice} disabled={!use || duplicate} onChange={() => toggleModel(model)} />
                    <span className="min-w-0">
                      <span className="block truncate text-sm text-snow">{model.id}</span>
                      <span className="mt-0.5 block text-[11px] text-fog">
                        {duplicate ? "已在模型目录中" : `${model.capability_label} · ${model.transport}`}
                      </span>
                    </span>
                  </label>
                  <span className="text-xs text-mist">{use ? modelUseLabel(use) : "需手动配置"}</span>
                  {choice ? (
                    <label className="flex items-center gap-1 text-[11px] text-fog">
                      <input className="input w-16 py-1 text-xs" type="number" min="1" value={choice.cost_credits} onChange={(event) => updateSelection(model, "cost_credits", Number(event.target.value))} aria-label={`${model.id} 调用积分`} />
                      积分
                    </label>
                  ) : <span />}
                </div>
              );
            })}
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[11px] text-fog">已选 {selectedModels.length} 个；密钥将分别加密保存，页面不会回显。</p>
            <button type="button" className="btn-primary btn-sm inline-flex items-center gap-1.5" disabled={!selectedModels.length || importing} onClick={importSelected}>
              {importing ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Check className="h-3.5 w-3.5" aria-hidden="true" />}
              {importing ? "正在导入" : `批量导入 ${selectedModels.length || ""}`}
            </button>
          </div>
        </div>
      ) : null}
    </Card>
  );
}
