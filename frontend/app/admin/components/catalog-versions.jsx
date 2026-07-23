"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Archive,
  ChevronDown,
  CirclePause,
  FilePenLine,
  History,
  Plus,
  RefreshCw,
  Radio,
  RotateCcw,
  Rocket,
  Save,
  Settings2,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import { api } from "../../../lib/api";
import { Card } from "./admin-ui";
import {
  EMPTY_TOOL_VERSION,
  jsonObjectText,
  modelRouteDraft,
  modelRoutePayload,
  modelVersionDraft,
  modelVersionPayload,
  toolVersionDraft,
  toolVersionPayload,
} from "./catalog-version-utils";

const TOOL_CATEGORIES = [
  ["image", "图片"],
  ["video", "视频"],
  ["workflow", "工作流"],
  ["utility", "通用工具"],
];

const EMPTY_TOOL = {
  slug: "",
  name: "",
  description: "",
  category: "workflow",
  renderer: "studio",
  entry_path: "/?workflow=",
  icon: "",
  sort_order: 0,
  enabled: true,
  featured: false,
};

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

function JsonPreview({ value, label }) {
  return (
    <details className="group">
      <summary className="flex min-h-9 cursor-pointer list-none items-center gap-1 text-xs text-mist">
        <ChevronDown size={13} className="transition group-open:rotate-180" aria-hidden="true" />
        {label}
      </summary>
      <pre className="max-h-64 overflow-auto border border-line bg-black/20 p-3 text-[11px] leading-relaxed text-fog">
        {jsonObjectText(value)}
      </pre>
    </details>
  );
}

const MODEL_VERSION_STATUS = {
  draft: { label: "草稿", className: "bg-warn/15 text-warn" },
  published: { label: "当前", className: "bg-ok/15 text-ok" },
  disabled: { label: "已停用", className: "bg-white/5 text-fog" },
  retired: { label: "已退役", className: "bg-bad/15 text-bad" },
};

function ModelVersionStatus({ version }) {
  const status = version?.status || (version?.is_active ? "published" : "disabled");
  const item = MODEL_VERSION_STATUS[status] || MODEL_VERSION_STATUS.disabled;
  return <span className={`inline-flex min-h-6 items-center rounded px-2 text-[11px] ${item.className}`}>{item.label}</span>;
}

const METADATA_ORIGIN_LABELS = {
  recorded: "当时记录",
  legacy_backfill: "历史回填",
};

function metadataOriginLabel(origin) {
  return METADATA_ORIGIN_LABELS[origin] || origin || "未标记";
}

function ModelMetadataSnapshotSummary({ snapshot }) {
  if (!snapshot || typeof snapshot !== "object") {
    return <p className="mt-2 border-t border-line/70 pt-2 text-[11px] text-fog">无目录元数据快照</p>;
  }
  return (
    <div className="mt-2 border-t border-line/70 pt-2 text-[11px] leading-relaxed text-fog">
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span>目录快照</span>
        <strong className="font-medium text-mist">{snapshot.display_name || snapshot.model_id || "未命名模型"}</strong>
        <span className="break-all font-mono">{snapshot.model_id || "-"}</span>
      </p>
      <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        <span>{snapshot.enabled ? "启用" : "停用"}</span>
        <span>{snapshot.is_default ? "默认模型" : "非默认"}</span>
        <span>排序 {Number.isInteger(snapshot.sort_order) ? snapshot.sort_order : "-"}</span>
        <span>来源 {metadataOriginLabel(snapshot.origin)}</span>
      </p>
    </div>
  );
}

function ToolMetadataSnapshotSummary({ snapshot }) {
  if (!snapshot || typeof snapshot !== "object") {
    return <p className="mt-2 border-t border-line/70 pt-2 text-[11px] text-fog">无目录元数据快照</p>;
  }
  return (
    <div className="mt-2 border-t border-line/70 pt-2 text-[11px] leading-relaxed text-fog">
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span>目录快照</span>
        <strong className="font-medium text-mist">{snapshot.name || snapshot.slug || "未命名工具"}</strong>
        <span className="break-all font-mono">{snapshot.slug || "-"}</span>
      </p>
      <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        <span>{snapshot.enabled ? "启用" : "停用"}</span>
        <span>分类 {snapshot.category || "-"}</span>
        <span>Renderer {snapshot.renderer || "-"}</span>
        <span>排序 {Number.isInteger(snapshot.sort_order) ? snapshot.sort_order : "-"}</span>
        <span>{snapshot.featured ? "已推荐" : "未推荐"}</span>
        <span>来源 {metadataOriginLabel(snapshot.origin)}</span>
      </p>
    </div>
  );
}

function ModelVersionsPanel({ models }) {
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState({ type: "", text: "" });
  const [draftEditor, setDraftEditor] = useState(null);
  const loadRequestRef = useRef(0);

  useEffect(() => {
    if (!selectedId && models.length) setSelectedId(String(models[0].id));
  }, [models, selectedId]);

  async function load(id = selectedId) {
    if (!id) return;
    const requestId = ++loadRequestRef.current;
    setBusy("load");
    setDetail(null);
    setMessage({ type: "", text: "" });
    try {
      const next = await api.adminModelVersions(id);
      if (requestId !== loadRequestRef.current) return;
      setDetail(next);
    } catch (error) {
      if (requestId !== loadRequestRef.current) return;
      setMessage({ type: "bad", text: error.message });
    } finally {
      if (requestId === loadRequestRef.current) setBusy("");
    }
  }

  useEffect(() => {
    setDraftEditor(null);
    load(selectedId);
  }, [selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  function openDraft(kind, version = null) {
    const current = version || (kind === "capability" ? detail?.capability_version : detail?.price_version) || (
      kind === "capability"
        ? { capabilities: detail?.capabilities || {} }
        : {
            base_cost_credits: detail?.cost_credits || 0,
            unlock_cost_credits: detail?.unlock_cost || 0,
            pricing: {},
          }
    );
    setDraftEditor({
      kind,
      version: version?.version || null,
      values: modelVersionDraft(kind, current),
    });
    setMessage({ type: "", text: "" });
  }

  function patchDraft(key, value) {
    setDraftEditor((current) => ({
      ...current,
      values: { ...current.values, [key]: value },
    }));
  }

  async function saveDraft() {
    if (!draftEditor) return;
    setMessage({ type: "", text: "" });
    try {
      const editing = Number.isInteger(draftEditor.version);
      const payload = modelVersionPayload(draftEditor.values, { includeKind: !editing });
      setBusy("save-draft");
      const next = editing
        ? await api.adminUpdateModelVersion(selectedId, draftEditor.kind, draftEditor.version, payload)
        : await api.adminCreateModelVersion(selectedId, payload);
      setDetail(next);
      setDraftEditor(null);
      setMessage({ type: "ok", text: `${draftEditor.kind === "capability" ? "能力" : "价格"}版本草稿已保存` });
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function runLifecycle(kind, version, action) {
    const kindLabel = kind === "capability" ? "能力" : "价格";
    const actionLabel = { publish: "发布", disable: "停用", retire: "退役", rollback: "回滚" }[action];
    const warning = action === "retire"
      ? "退役不可恢复，历史任务仍会保留该版本引用。"
      : "变更只影响后续新任务，历史任务继续引用原快照。";
    if (!window.confirm(`确认${actionLabel}${kindLabel}版本 v${version}？${warning}`)) return;
    setBusy(`${action}-${kind}-${version}`);
    setMessage({ type: "", text: "" });
    try {
      const operation = {
        publish: api.adminPublishModelVersion,
        disable: api.adminDisableModelVersion,
        retire: api.adminRetireModelVersion,
        rollback: api.adminRollbackModelVersion,
      }[action];
      const next = await operation(selectedId, kind, version);
      setDetail(next);
      setDraftEditor(null);
      const active = kind === "capability" ? next.capability_version : next.price_version;
      const suffix = action === "rollback" && active ? `，已生成 v${active.version}` : "";
      setMessage({ type: "ok", text: `${kindLabel}版本 v${version} 已${actionLabel}${suffix}` });
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  const capabilityVersions = detail?.capability_versions || [];
  const priceVersions = detail?.price_versions || [];

  function sourceVersionLabel(versions, sourceId) {
    if (!sourceId) return null;
    const source = versions.find((item) => Number(item.id) === Number(sourceId));
    return source ? `回滚自 v${source.version}` : `回滚来源 #${sourceId}`;
  }

  function versionActions(kind, version) {
    const status = version.status || (version.is_active ? "published" : "disabled");
    if (status === "draft") {
      return (
        <div className="flex flex-wrap justify-end gap-1">
          <button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => openDraft(kind, version)}><FilePenLine size={13} /> 编辑</button>
          <button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => runLifecycle(kind, version.version, "publish")}><Rocket size={13} /> 发布</button>
          <button type="button" className="btn-ghost btn-sm text-bad" disabled={Boolean(busy)} onClick={() => runLifecycle(kind, version.version, "retire")}><Archive size={13} /> 退役</button>
        </div>
      );
    }
    if (status === "disabled") {
      return (
        <div className="flex flex-wrap justify-end gap-1">
          <button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => runLifecycle(kind, version.version, "rollback")}><RotateCcw size={13} /> 回滚</button>
          <button type="button" className="btn-ghost btn-sm text-bad" disabled={Boolean(busy)} onClick={() => runLifecycle(kind, version.version, "retire")}><Archive size={13} /> 退役</button>
        </div>
      );
    }
    if (status === "published" && version.is_active && detail?.enabled === false) {
      return <button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => runLifecycle(kind, version.version, "disable")}><CirclePause size={13} /> 停用版本</button>;
    }
    return null;
  }

  function versionCard(kind, version, versions) {
    const sourceLabel = sourceVersionLabel(versions, version.source_version_id);
    return (
      <article key={version.id} className="border border-line bg-base/25 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-mist">
            <b className="text-snow">v{version.version}</b>
            <span>{version.schema_version}</span>
            <ModelVersionStatus version={version} />
            {sourceLabel ? <span className="text-aqua">{sourceLabel}</span> : null}
          </div>
          {versionActions(kind, version)}
        </div>
        {kind === "price" ? <p className="mt-2 text-xs text-mist">调用 {version.base_cost_credits} 积分 · 解锁 {version.unlock_cost_credits} 积分</p> : null}
        <p className="mt-2 text-[11px] text-fog">
          创建 {formatTime(version.created_at)}
          {version.activated_at ? ` · 发布 ${formatTime(version.activated_at)}` : ""}
          {version.disabled_at ? ` · 停用 ${formatTime(version.disabled_at)}` : ""}
          {version.retired_at ? ` · 退役 ${formatTime(version.retired_at)}` : ""}
        </p>
        {kind === "capability" ? <ModelMetadataSnapshotSummary snapshot={version.metadata_snapshot} /> : null}
        <JsonPreview value={kind === "capability" ? version.capabilities : version.pricing} label={kind === "capability" ? "能力 JSON" : "计价 JSON"} />
      </article>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <label className="grid flex-1 gap-1 text-xs text-fog">
          <span>模型</span>
          <select className="input h-10 py-1" value={selectedId} disabled={Boolean(busy)} onChange={(event) => setSelectedId(event.target.value)}>
            {models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.display_name || model.name || model.model_id} · {model.use} · {model.provider || "env"}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-ghost btn-sm h-10" onClick={() => load()} disabled={!selectedId || busy === "load"}>
          <RefreshCw size={15} className={busy === "load" ? "animate-spin" : ""} aria-hidden="true" /> 刷新
        </button>
      </div>

      {message.text ? <p className={`text-xs ${message.type === "bad" ? "text-bad" : "text-ok"}`}>{message.text}</p> : null}
      {!models.length ? <p className="border border-dashed border-line p-6 text-center text-sm text-fog">暂无模型配置</p> : null}

      {draftEditor ? (
        <section className="border border-line bg-white/[0.025] p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="font-display text-sm font-semibold text-snow">{draftEditor.version ? `编辑 v${draftEditor.version}` : "新建"}{draftEditor.kind === "capability" ? "能力" : "价格"}草稿</h3>
              <p className="mt-1 text-[11px] text-fog">草稿不会用于新生成任务；发布后快照不可再编辑。</p>
            </div>
            <button type="button" className="icon-btn" title="关闭草稿编辑" onClick={() => setDraftEditor(null)}><X size={16} /></button>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1 text-xs text-fog">
              <span>Schema 版本</span>
              <input className="input h-10" value={draftEditor.values.schema_version} onChange={(event) => patchDraft("schema_version", event.target.value)} />
            </label>
            {draftEditor.kind === "price" ? (
              <div className="grid grid-cols-2 gap-2">
                <label className="grid gap-1 text-xs text-fog"><span>调用积分</span><input className="input h-10" type="number" min="0" max="1000000" value={draftEditor.values.base_cost_credits} onChange={(event) => patchDraft("base_cost_credits", event.target.value)} /></label>
                <label className="grid gap-1 text-xs text-fog"><span>解锁积分</span><input className="input h-10" type="number" min="0" max="1000000" value={draftEditor.values.unlock_cost_credits} onChange={(event) => patchDraft("unlock_cost_credits", event.target.value)} /></label>
              </div>
            ) : null}
          </div>
          <label className="mt-3 grid gap-1 text-xs text-fog">
            <span>{draftEditor.kind === "capability" ? "能力 JSON" : "计价 JSON"}</span>
            <textarea className="input min-h-52 resize-y font-mono text-xs leading-relaxed" value={draftEditor.kind === "capability" ? draftEditor.values.capabilities : draftEditor.values.pricing} onChange={(event) => patchDraft(draftEditor.kind === "capability" ? "capabilities" : "pricing", event.target.value)} />
          </label>
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" className="btn-ghost btn-sm" onClick={() => setDraftEditor(null)}>取消</button>
            <button type="button" className="btn-primary btn-sm" disabled={Boolean(busy)} onClick={saveDraft}><Save size={14} /> 保存草稿</button>
          </div>
        </section>
      ) : null}

      {detail ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <section className="border border-line bg-white/[0.025] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="flex items-center gap-2 font-display text-sm font-semibold text-snow"><Settings2 size={16} />能力版本</h3>
              <div className="flex items-center gap-2"><span className="text-xs text-fog">{capabilityVersions.length} 个版本</span><button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => openDraft("capability")}><Plus size={13} /> 草稿</button></div>
            </div>
            <div className="space-y-2">
              {capabilityVersions.map((version) => versionCard("capability", version, capabilityVersions))}
              {!capabilityVersions.length ? <p className="py-6 text-center text-xs text-fog">暂无能力版本</p> : null}
            </div>
          </section>

          <section className="border border-line bg-white/[0.025] p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="flex items-center gap-2 font-display text-sm font-semibold text-snow"><History size={16} />价格版本</h3>
              <div className="flex items-center gap-2"><span className="text-xs text-fog">{priceVersions.length} 个版本</span><button type="button" className="btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => openDraft("price")}><Plus size={13} /> 草稿</button></div>
            </div>
            <div className="space-y-2">
              {priceVersions.map((version) => versionCard("price", version, priceVersions))}
              {!priceVersions.length ? <p className="py-6 text-center text-xs text-fog">暂无价格版本</p> : null}
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function RouteHealthBadge({ status }) {
  const styles = {
    closed: "bg-ok/15 text-ok",
    open: "bg-bad/15 text-bad",
    half_open: "bg-warn/15 text-warn",
  };
  const labels = { closed: "正常", open: "熔断", half_open: "半开探测" };
  return (
    <span className={`inline-flex min-h-6 items-center rounded px-2 text-[11px] ${styles[status] || "bg-white/5 text-fog"}`}>
      {labels[status] || status || "未知"}
    </span>
  );
}

function RouteField({ label, children, hint = "" }) {
  return (
    <label className="grid gap-1 text-xs text-fog">
      <span>{label}</span>
      {children}
      {hint ? <span className="text-[10px] leading-relaxed text-fog/80">{hint}</span> : null}
    </label>
  );
}

function ModelRoutesPanel({ models }) {
  const [modelId, setModelId] = useState("");
  const [routes, setRoutes] = useState([]);
  const [selectedRouteId, setSelectedRouteId] = useState("");
  const [draft, setDraft] = useState(modelRouteDraft());
  const [creating, setCreating] = useState(false);
  const [events, setEvents] = useState([]);
  const [eventsOpen, setEventsOpen] = useState(false);
  const [routeVersions, setRouteVersions] = useState([]);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState({ type: "", text: "" });

  useEffect(() => {
    if (!modelId && models.length) setModelId(String(models[0].id));
  }, [modelId, models]);

  function selectRoute(route) {
    setCreating(false);
    setSelectedRouteId(String(route.id));
    setDraft(modelRouteDraft(route));
    setEvents([]);
    setEventsOpen(false);
    setRouteVersions([]);
    setVersionsOpen(false);
  }

  async function loadRoutes(nextModelId = modelId, preferredRouteId = selectedRouteId) {
    if (!nextModelId) return;
    setBusy("load");
    try {
      const payload = await api.adminModelRoutes(nextModelId);
      const items = payload?.items || [];
      setRoutes(items);
      const selected = items.find((item) => String(item.id) === String(preferredRouteId)) || items[0];
      if (selected) selectRoute(selected);
      else {
        setSelectedRouteId("");
        setDraft(modelRouteDraft());
      }
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    setCreating(false);
    setSelectedRouteId("");
    setEvents([]);
    setEventsOpen(false);
    setRouteVersions([]);
    setVersionsOpen(false);
    setMessage({ type: "", text: "" });
    if (modelId) loadRoutes(modelId, "");
  }, [modelId]); // eslint-disable-line react-hooks/exhaustive-deps

  function patchDraft(key, value) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function beginCreate() {
    setCreating(true);
    setSelectedRouteId("");
    setDraft(modelRouteDraft());
    setEvents([]);
    setEventsOpen(false);
    setRouteVersions([]);
    setVersionsOpen(false);
    setMessage({ type: "", text: "" });
  }

  function cancelCreate() {
    const first = routes[0];
    if (first) selectRoute(first);
    else setCreating(false);
  }

  async function saveRoute() {
    setMessage({ type: "", text: "" });
    try {
      const payload = modelRoutePayload(draft, { creating });
      setBusy("save");
      const saved = creating
        ? await api.adminCreateModelRoute(modelId, payload)
        : await api.adminUpdateModelRoute(modelId, selectedRouteId, payload);
      setMessage({ type: "ok", text: `路由「${saved.name}」已${creating ? "创建" : "保存"}` });
      await loadRoutes(modelId, saved.id);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function probeRoute() {
    if (!selectedRouteId) return;
    setBusy("probe");
    setMessage({ type: "", text: "" });
    try {
      const result = await api.adminProbeModelRoute(modelId, selectedRouteId);
      setMessage({ type: "ok", text: `探测成功，供应商返回 ${result.models?.length || 0} 个模型` });
      await loadRoutes(modelId, selectedRouteId);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
      await loadRoutes(modelId, selectedRouteId);
    } finally {
      setBusy("");
    }
  }

  async function resetHealth() {
    if (!selectedRouteId || !window.confirm("确认清空该路由的熔断状态和失败窗口？")) return;
    setBusy("reset");
    try {
      const saved = await api.adminResetModelRouteHealth(modelId, selectedRouteId);
      setMessage({ type: "ok", text: `路由「${saved.name}」健康状态已重置` });
      await loadRoutes(modelId, selectedRouteId);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function loadEvents() {
    if (!selectedRouteId) return;
    setBusy("events");
    try {
      const payload = await api.adminModelRouteHealthEvents(modelId, selectedRouteId, 50);
      setEvents(payload?.items || []);
      setEventsOpen(true);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function loadRouteVersions(nextModelId = modelId, nextRouteId = selectedRouteId) {
    if (!nextModelId || !nextRouteId) return;
    setBusy("versions");
    try {
      const payload = await api.adminModelRouteVersions(nextModelId, nextRouteId);
      setRouteVersions(payload?.versions || []);
      setVersionsOpen(true);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function runRouteVersionLifecycle(version, action) {
    const actionLabel = action === "rollback" ? "回滚" : "退役";
    const warning = action === "rollback"
      ? "系统会复制历史配置为新版本，现有 API Key 不会被历史版本覆盖。"
      : "退役后不能再回滚到该版本，历史任务引用不受影响。";
    if (!window.confirm(`确认${actionLabel}路由版本 v${version}？${warning}`)) return;
    setBusy(`${action}-route-${version}`);
    setMessage({ type: "", text: "" });
    try {
      if (action === "rollback") {
        const saved = await api.adminRollbackModelRouteVersion(modelId, selectedRouteId, version);
        setMessage({ type: "ok", text: `已从路由 v${version} 回滚并发布为 v${saved.active_version?.version || saved.config_revision}` });
        await loadRoutes(modelId, selectedRouteId);
      } else {
        await api.adminRetireModelRouteVersion(modelId, selectedRouteId, version);
        setMessage({ type: "ok", text: `路由版本 v${version} 已退役` });
      }
      await loadRouteVersions(modelId, selectedRouteId);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  const selectedRoute = routes.find((item) => String(item.id) === selectedRouteId) || null;
  const gatewayDisabled = Boolean(draft.managed_by_model_config);
  const failureRate = selectedRoute?.window_requests
    ? Math.round((selectedRoute.window_failures / selectedRoute.window_requests) * 100)
    : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <RouteField label="模型">
          <select className="input h-10 py-1" value={modelId} onChange={(event) => setModelId(event.target.value)}>
            {models.map((model) => <option key={model.id} value={model.id}>{model.display_name || model.name || model.model_id} · {model.use}</option>)}
          </select>
        </RouteField>
        <button type="button" className="btn-ghost btn-sm h-10" onClick={() => loadRoutes()} disabled={!modelId || busy === "load"}>
          <RefreshCw size={15} className={busy === "load" ? "animate-spin" : ""} />刷新
        </button>
        <button type="button" className="btn-primary btn-sm h-10" onClick={beginCreate} disabled={!modelId || Boolean(busy)}>
          <Plus size={15} />新建路由
        </button>
      </div>

      {message.text ? <p className={`text-xs ${message.type === "bad" ? "text-bad" : "text-ok"}`}>{message.text}</p> : null}
      {!models.length ? <p className="border border-dashed border-line p-6 text-center text-sm text-fog">暂无模型配置</p> : null}

      <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <aside className="border border-line bg-white/[0.025] p-3">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="font-display text-sm font-semibold text-snow">供应商路由</h3>
            <span className="text-[10px] text-fog">{routes.length} 条</span>
          </div>
          <div className="space-y-1">
            {routes.map((route) => (
              <button key={route.id} type="button" className={`w-full border px-3 py-2 text-left transition ${String(route.id) === selectedRouteId ? "border-brand bg-brand/15" : "border-transparent hover:border-line hover:bg-white/5"}`} onClick={() => selectRoute(route)}>
                <span className="flex min-w-0 items-center justify-between gap-2"><b className="truncate text-xs text-snow">{route.name}</b><RouteHealthBadge status={route.health_status} /></span>
                <span className="mt-1 block truncate text-[10px] text-fog">优先级 {route.priority} · {route.enabled ? "启用" : "停用"} · {route.latency_ema_ms == null ? "暂无延迟" : `${route.latency_ema_ms} ms`}</span>
              </button>
            ))}
            {!routes.length ? <p className="py-8 text-center text-xs text-fog">暂无路由</p> : null}
          </div>
        </aside>

        <section className="min-w-0 border border-line bg-white/[0.025] p-4">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="flex items-center gap-2 font-display text-sm font-semibold text-snow"><Radio size={16} />{creating ? "新建模型路由" : selectedRoute?.name || "模型路由"}</h3>
              {!creating && selectedRoute ? <p className="mt-1 text-[11px] text-fog">{selectedRoute.route_key} · 配置修订 v{selectedRoute.config_revision}</p> : null}
            </div>
            <div className="flex flex-wrap gap-2">
              {creating ? <button type="button" className="btn-ghost btn-sm" onClick={cancelCreate}><X size={14} />取消</button> : null}
              {!creating && selectedRoute ? <button type="button" className="btn-ghost btn-sm" onClick={probeRoute} disabled={Boolean(busy)}><Activity size={14} />探测</button> : null}
              {!creating && selectedRoute ? <button type="button" className="btn-ghost btn-sm" onClick={resetHealth} disabled={Boolean(busy)}><ShieldCheck size={14} />重置健康</button> : null}
              <button type="button" className="btn-primary btn-sm" onClick={saveRoute} disabled={Boolean(busy) || (!creating && !selectedRoute)}><Save size={14} />保存</button>
            </div>
          </div>

          {!creating && selectedRoute ? (
            <div className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <div className="border border-line bg-base/30 p-3"><span className="text-[10px] text-fog">健康状态</span><div className="mt-1"><RouteHealthBadge status={selectedRoute.health_status} /></div></div>
              <div className="border border-line bg-base/30 p-3"><span className="text-[10px] text-fog">失败窗口</span><p className="mt-1 text-sm text-snow">{selectedRoute.window_failures}/{selectedRoute.window_requests} · {failureRate}%</p></div>
              <div className="border border-line bg-base/30 p-3"><span className="text-[10px] text-fog">连续失败</span><p className="mt-1 text-sm text-snow">{selectedRoute.consecutive_failures}</p></div>
              <div className="border border-line bg-base/30 p-3"><span className="text-[10px] text-fog">平均延迟</span><p className="mt-1 text-sm text-snow">{selectedRoute.latency_ema_ms == null ? "-" : `${selectedRoute.latency_ema_ms} ms`}</p></div>
            </div>
          ) : null}

          {(creating || selectedRoute) ? (
            <div className="space-y-4">
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <RouteField label="路由标识" hint="创建后不可修改"><input className="input h-10 py-1" value={draft.route_key} disabled={!creating} onChange={(event) => patchDraft("route_key", event.target.value)} placeholder="provider-primary" /></RouteField>
                <RouteField label="路由名称"><input className="input h-10 py-1" value={draft.name} onChange={(event) => patchDraft("name", event.target.value)} /></RouteField>
                <RouteField label="优先级" hint="数值越小越优先"><input className="input h-10 py-1" type="number" min="0" max="100000" value={draft.priority} onChange={(event) => patchDraft("priority", event.target.value)} /></RouteField>
              </div>

              {!creating && selectedRoute?.route_key === "legacy-default" ? (
                <label className="flex items-center gap-2 text-xs text-mist"><input type="checkbox" className="accent-iris" checked={draft.managed_by_model_config} onChange={(event) => patchDraft("managed_by_model_config", event.target.checked)} />跟随模型配置中的供应商、地址、Key 和协议</label>
              ) : null}

              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <RouteField label="供应商"><input className="input h-10 py-1" value={draft.provider} disabled={gatewayDisabled} onChange={(event) => patchDraft("provider", event.target.value)} placeholder="openai" /></RouteField>
                <RouteField label="供应商模型 ID"><input className="input h-10 py-1" value={draft.model_id} disabled={gatewayDisabled} onChange={(event) => patchDraft("model_id", event.target.value)} placeholder="留空沿用目录模型 ID" /></RouteField>
                <RouteField label="网关协议"><select className="input h-10 py-1" value={draft.gateway_format} disabled={gatewayDisabled} onChange={(event) => patchDraft("gateway_format", event.target.value)}><option value="openai">OpenAI</option><option value="ark">火山方舟</option><option value="anthropic">Anthropic</option></select></RouteField>
                <div className="md:col-span-2 xl:col-span-3"><RouteField label="Base URL"><input className="input h-10 py-1" value={draft.base_url} disabled={gatewayDisabled} onChange={(event) => patchDraft("base_url", event.target.value)} placeholder="https://gateway.example.com/v1" /></RouteField></div>
                <div className="md:col-span-2 xl:col-span-3">
                  <RouteField label={`API Key${selectedRoute?.api_key_configured ? "（已配置）" : ""}`} hint="留空表示不修改；密钥写入后不会回显">
                    <input type="password" autoComplete="new-password" className="input h-10 py-1" value={draft.api_key} disabled={gatewayDisabled || draft.api_key_clear} onChange={(event) => patchDraft("api_key", event.target.value)} placeholder={selectedRoute?.api_key_configured ? "已配置，输入新值可轮换" : "输入供应商密钥"} />
                  </RouteField>
                  {!creating && selectedRoute?.api_key_configured && !gatewayDisabled ? <label className="mt-2 flex items-center gap-2 text-xs text-warn"><input type="checkbox" className="accent-iris" checked={draft.api_key_clear} onChange={(event) => patchDraft("api_key_clear", event.target.checked)} />保存时清空现有 API Key</label> : null}
                </div>
              </div>

              <RouteField label="路由扩展配置" hint="不能包含密钥、Authorization、能力或价格字段"><textarea className="input min-h-28 resize-y py-2 font-mono text-[11px]" value={draft.extra} disabled={gatewayDisabled} onChange={(event) => patchDraft("extra", event.target.value)} spellCheck={false} /></RouteField>

              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <RouteField label="失败阈值"><input className="input h-10 py-1" type="number" min="1" max="100" value={draft.failure_threshold} onChange={(event) => patchDraft("failure_threshold", event.target.value)} /></RouteField>
                <RouteField label="统计窗口（秒）"><input className="input h-10 py-1" type="number" min="1" max="86400" value={draft.window_seconds} onChange={(event) => patchDraft("window_seconds", event.target.value)} /></RouteField>
                <RouteField label="熔断冷却（秒）"><input className="input h-10 py-1" type="number" min="1" max="86400" value={draft.cooldown_seconds} onChange={(event) => patchDraft("cooldown_seconds", event.target.value)} /></RouteField>
                <label className="flex min-h-10 items-center gap-2 self-end text-xs text-mist"><input type="checkbox" className="accent-iris" checked={draft.enabled} onChange={(event) => patchDraft("enabled", event.target.checked)} />参与新任务路由选择</label>
              </div>

              {!creating && selectedRoute ? (
                <div className="border-t border-line pt-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div><h4 className="text-xs font-semibold text-snow">配置版本</h4><p className="mt-1 text-[10px] text-fog">保存路由会发布新版本；回滚复制历史配置，但继续使用当前密钥。</p></div>
                    <button type="button" className="btn-ghost btn-sm" onClick={() => loadRouteVersions()} disabled={Boolean(busy)}><History size={14} />{versionsOpen ? "刷新版本" : "查看版本"}</button>
                  </div>
                  {versionsOpen ? <div className="mt-3 space-y-2">
                    {routeVersions.map((version) => {
                      const source = routeVersions.find((item) => Number(item.id) === Number(version.source_version_id));
                      const status = version.status || (version.is_active ? "published" : "disabled");
                      return <article key={version.id} className="border border-line bg-base/25 p-3">
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div className="flex flex-wrap items-center gap-2 text-xs text-mist">
                            <b className="text-snow">v{version.version}</b>
                            <span>{version.schema_version}</span>
                            <ModelVersionStatus version={version} />
                            {version.source_version_id ? <span className="text-aqua">{source ? `回滚自 v${source.version}` : `回滚来源 #${version.source_version_id}`}</span> : null}
                            {version.api_key_configured ? <span className="text-fog">密钥已配置</span> : null}
                          </div>
                          {status === "disabled" ? <div className="flex flex-wrap justify-end gap-1">
                            <button type="button" className="btn-ghost btn-sm" onClick={() => runRouteVersionLifecycle(version.version, "rollback")} disabled={Boolean(busy)}><RotateCcw size={13} />回滚</button>
                            <button type="button" className="btn-ghost btn-sm text-bad" onClick={() => runRouteVersionLifecycle(version.version, "retire")} disabled={Boolean(busy)}><Archive size={13} />退役</button>
                          </div> : null}
                        </div>
                        <p className="mt-2 text-[11px] text-fog">创建 {formatTime(version.created_at)}{version.activated_at ? ` · 发布 ${formatTime(version.activated_at)}` : ""}{version.disabled_at ? ` · 停用 ${formatTime(version.disabled_at)}` : ""}{version.retired_at ? ` · 退役 ${formatTime(version.retired_at)}` : ""}</p>
                        <JsonPreview value={version.config} label="路由配置快照" />
                      </article>;
                    })}
                    {!routeVersions.length ? <p className="border border-dashed border-line p-5 text-center text-xs text-fog">暂无配置版本</p> : null}
                  </div> : null}
                </div>
              ) : null}

              {!creating && selectedRoute ? (
                <div className="border-t border-line pt-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div><h4 className="text-xs font-semibold text-snow">健康事件</h4><p className="mt-1 text-[10px] text-fog">只展示运行结果、延迟和错误码，不包含请求内容或密钥。</p></div>
                    <button type="button" className="btn-ghost btn-sm" onClick={loadEvents} disabled={Boolean(busy)}><History size={14} />{eventsOpen ? "刷新事件" : "查看事件"}</button>
                  </div>
                  {eventsOpen ? <div className="mt-3 max-h-64 overflow-auto border border-line">
                    {events.map((event) => <div key={event.id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-line px-3 py-2 text-[11px] last:border-b-0"><div className="min-w-0"><p className={event.outcome === "success" ? "text-ok" : event.outcome === "failure" ? "text-bad" : "text-fog"}>{event.operation} · {event.outcome}{event.error_code ? ` · ${event.error_code}` : ""}</p><p className="mt-1 text-fog">{formatTime(event.created_at)}{event.counts_toward_circuit ? " · 计入熔断" : ""}</p></div><span className="text-fog">{event.latency_ms == null ? "-" : `${event.latency_ms} ms`}</span></div>)}
                    {!events.length ? <p className="p-5 text-center text-xs text-fog">暂无健康事件</p> : null}
                  </div> : null}
                </div>
              ) : null}
            </div>
          ) : <p className="py-12 text-center text-sm text-fog">请选择或新建路由</p>}
        </section>
      </div>
    </div>
  );
}

function ToolField({ label, children }) {
  return <label className="grid gap-1 text-xs text-fog"><span>{label}</span>{children}</label>;
}

function VersionEditor({ draft, setDraft }) {
  function update(key, value) { setDraft((current) => ({ ...current, [key]: value })); }
  return (
    <div className="grid gap-3">
      <ToolField label="Schema 版本">
        <input className="input h-10 py-1" value={draft.schema_version} onChange={(event) => update("schema_version", event.target.value)} />
      </ToolField>
      <div className="grid gap-3 xl:grid-cols-2">
        {[
          ["input_schema", "输入 Schema"],
          ["workflow", "工作流"],
          ["pricing_policy", "计价策略"],
          ["capabilities", "能力声明"],
        ].map(([key, label]) => (
          <ToolField key={key} label={label}>
            <textarea className="input min-h-36 resize-y py-2 font-mono text-[11px] leading-relaxed" value={draft[key]} onChange={(event) => update(key, event.target.value)} spellCheck={false} />
          </ToolField>
        ))}
      </div>
    </div>
  );
}

function toolMetadata(item) {
  return {
    slug: item?.slug || "",
    name: item?.name || "",
    description: item?.description || "",
    category: item?.category || "workflow",
    renderer: item?.renderer || "studio",
    entry_path: item?.entry_path || "/?workflow=",
    icon: item?.icon || "",
    sort_order: Number(item?.sort_order || 0),
    enabled: item?.enabled !== false,
    featured: Boolean(item?.featured),
  };
}

function ToolCatalogPanel() {
  const [tools, setTools] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [metadata, setMetadata] = useState(EMPTY_TOOL);
  const [versionDraft, setVersionDraft] = useState(toolVersionDraft());
  const [creating, setCreating] = useState(false);
  const [versionEditor, setVersionEditor] = useState(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState({ type: "", text: "" });

  async function loadTools(preferredId = null) {
    setBusy("load");
    try {
      const payload = await api.adminTools();
      const items = payload?.items || [];
      setTools(items);
      const next = preferredId || selectedId || items[0]?.id || "";
      setSelectedId(next ? String(next) : "");
      if (!next) {
        setDetail(null);
        setMetadata(EMPTY_TOOL);
      }
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function loadDetail(id = selectedId) {
    if (!id || creating) return;
    setBusy("detail");
    try {
      const next = await api.adminToolVersions(id);
      setDetail(next);
      setMetadata(toolMetadata(next));
      setVersionDraft(toolVersionDraft(next.active_version || EMPTY_TOOL_VERSION));
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  useEffect(() => { loadTools(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (selectedId && !creating) loadDetail(selectedId); }, [selectedId, creating]); // eslint-disable-line react-hooks/exhaustive-deps

  function beginCreate() {
    setCreating(true);
    setSelectedId("");
    setDetail(null);
    setMetadata({ ...EMPTY_TOOL });
    setVersionDraft(toolVersionDraft());
    setVersionEditor(null);
    setMessage({ type: "", text: "" });
  }

  function cancelCreate() {
    setCreating(false);
    const first = tools[0]?.id || "";
    setSelectedId(first ? String(first) : "");
    setVersionEditor(null);
  }

  function patchMetadata(key, value) { setMetadata((current) => ({ ...current, [key]: value })); }

  function validatedMetadata() {
    const next = {
      ...metadata,
      slug: String(metadata.slug || "").trim(),
      name: String(metadata.name || "").trim(),
      description: String(metadata.description || "").trim() || null,
      renderer: String(metadata.renderer || "").trim(),
      entry_path: String(metadata.entry_path || "").trim(),
      icon: String(metadata.icon || "").trim() || null,
      sort_order: Number(metadata.sort_order || 0),
    };
    if (!next.slug) throw new Error("请填写工具标识");
    if (!next.name) throw new Error("请填写工具名称");
    if (!next.renderer) throw new Error("请填写渲染器");
    if (!next.entry_path.startsWith("/") || next.entry_path.startsWith("//")) throw new Error("工具入口必须是站内绝对路径");
    if (!Number.isInteger(next.sort_order)) throw new Error("排序必须是整数");
    return next;
  }

  async function saveMetadata() {
    setMessage({ type: "", text: "" });
    try {
      const payload = validatedMetadata();
      setBusy("save");
      if (creating) {
        const created = await api.adminCreateTool({ ...payload, initial_version: toolVersionPayload(versionDraft) });
        setCreating(false);
        setMessage({ type: "ok", text: `工具「${created.name}」已创建` });
        await loadTools(created.id);
      } else {
        const updated = await api.adminUpdateTool(selectedId, payload);
        setMessage({ type: "ok", text: `工具「${updated.name}」已保存` });
        await loadTools(updated.id);
        await loadDetail(updated.id);
      }
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  function openVersionEditor(version = null) {
    setVersionDraft(toolVersionDraft(version || detail?.active_version || EMPTY_TOOL_VERSION));
    setVersionEditor({ version: version?.version || null });
    setMessage({ type: "", text: "" });
  }

  async function saveVersionDraft() {
    if (!versionEditor) return;
    setMessage({ type: "", text: "" });
    try {
      setBusy("version");
      const editing = Number.isInteger(versionEditor.version);
      const updated = editing
        ? await api.adminUpdateToolVersion(selectedId, versionEditor.version, toolVersionPayload(versionDraft))
        : await api.adminCreateToolVersion(selectedId, toolVersionPayload(versionDraft));
      setDetail(updated);
      const saved = editing
        ? updated.versions?.find((item) => Number(item.version) === Number(versionEditor.version))
        : updated.versions?.find((item) => item.status === "draft");
      setVersionDraft(toolVersionDraft(saved || updated.active_version || EMPTY_TOOL_VERSION));
      setVersionEditor(null);
      setMessage({ type: "ok", text: `工具版本 v${saved?.version || versionEditor.version || "-"} 草稿已${editing ? "更新" : "创建"}` });
      await loadTools(updated.id);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function runToolVersionLifecycle(version, action) {
    const actionLabel = { publish: "发布", disable: "停用", retire: "退役", rollback: "回滚" }[action];
    const warning = action === "rollback"
      ? "系统会复制该历史版本并发布为新版本。"
      : action === "retire"
        ? "退役后不能再回滚到该版本，历史任务引用不受影响。"
        : "变更只影响后续新任务，历史任务继续引用原快照。";
    if (!window.confirm(`确认${actionLabel}工具版本 v${version}？${warning}`)) return;
    setBusy(`${action}-${version}`);
    setMessage({ type: "", text: "" });
    try {
      const operation = {
        publish: api.adminPublishToolVersion,
        disable: api.adminDisableToolVersion,
        retire: api.adminRetireToolVersion,
        rollback: api.adminRollbackToolVersion,
      }[action];
      const updated = await operation(selectedId, version);
      setDetail(updated);
      setVersionEditor(null);
      const suffix = action === "rollback" && updated.active_version
        ? `，已生成 v${updated.active_version.version}`
        : "";
      setMessage({ type: "ok", text: `工具版本 v${version} 已${actionLabel}${suffix}` });
      await loadTools(updated.id);
    } catch (error) {
      setMessage({ type: "bad", text: error.message });
    } finally {
      setBusy("");
    }
  }

  const versions = detail?.versions || [];

  function toolVersionActions(version) {
    const status = version.status || (version.is_active ? "published" : "disabled");
    if (status === "draft") {
      return <div className="flex flex-wrap justify-end gap-1">
        <button type="button" className="btn-ghost btn-sm" onClick={() => openVersionEditor(version)} disabled={Boolean(busy)}><FilePenLine size={13} />编辑</button>
        <button type="button" className="btn-ghost btn-sm" onClick={() => runToolVersionLifecycle(version.version, "publish")} disabled={Boolean(busy)}><Rocket size={13} />发布</button>
        <button type="button" className="btn-ghost btn-sm text-bad" onClick={() => runToolVersionLifecycle(version.version, "retire")} disabled={Boolean(busy)}><Archive size={13} />退役</button>
      </div>;
    }
    if (status === "disabled") {
      return <div className="flex flex-wrap justify-end gap-1">
        <button type="button" className="btn-ghost btn-sm" onClick={() => runToolVersionLifecycle(version.version, "rollback")} disabled={Boolean(busy)}><RotateCcw size={13} />回滚</button>
        <button type="button" className="btn-ghost btn-sm text-bad" onClick={() => runToolVersionLifecycle(version.version, "retire")} disabled={Boolean(busy)}><Archive size={13} />退役</button>
      </div>;
    }
    if (status === "published" && version.is_active) {
      return <button type="button" className="btn-ghost btn-sm" title={detail?.enabled ? "请先停用工具" : "停用当前版本"} onClick={() => runToolVersionLifecycle(version.version, "disable")} disabled={Boolean(busy) || detail?.enabled}><CirclePause size={13} />停用版本</button>;
    }
    return null;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="border border-line bg-white/[0.025] p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h3 className="font-display text-sm font-semibold text-snow">工具目录</h3>
          <button type="button" className="btn-ghost btn-sm h-8 w-8 p-0" onClick={beginCreate} title="新建工具" aria-label="新建工具"><Plus size={15} /></button>
        </div>
        <div className="space-y-1">
          {tools.map((tool) => (
            <button key={tool.id} type="button" className={`w-full border px-3 py-2 text-left text-xs transition ${String(tool.id) === selectedId ? "border-brand bg-brand/15 text-snow" : "border-transparent text-mist hover:border-line hover:bg-white/5"}`} onClick={() => { setCreating(false); setSelectedId(String(tool.id)); setVersionEditor(null); }}>
              <span className="flex items-center justify-between gap-2"><b className="truncate font-display font-medium">{tool.name}</b><span className={tool.enabled ? "text-ok" : "text-fog"}>{tool.enabled ? "启用" : "停用"}</span></span>
              <span className="mt-1 block truncate text-[10px] text-fog">{tool.slug} · {tool.active_version ? `v${tool.active_version.version}` : "无版本"}</span>
            </button>
          ))}
          {!tools.length && !creating ? <p className="py-8 text-center text-xs text-fog">暂无工具</p> : null}
        </div>
      </aside>

      <section className="min-w-0 border border-line bg-white/[0.025] p-4">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 font-display text-sm font-semibold text-snow"><Wrench size={16} />{creating ? "新建工具" : detail?.name || "工具配置"}</h3>
            {!creating && detail ? <p className="mt-1 text-[11px] text-fog">{detail.slug} · 当前 v{detail.active_version?.version || "-"}</p> : null}
          </div>
          <div className="flex gap-2">
            {creating ? <button type="button" className="btn-ghost btn-sm" onClick={cancelCreate}><X size={14} />取消</button> : null}
            <button type="button" className="btn-primary btn-sm" onClick={saveMetadata} disabled={Boolean(busy)}><Save size={14} />{creating ? "创建" : "保存"}</button>
          </div>
        </div>

        {message.text ? <p className={`mb-3 text-xs ${message.type === "bad" ? "text-bad" : "text-ok"}`}>{message.text}</p> : null}

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <ToolField label="工具标识"><input className="input h-10 py-1" value={metadata.slug} onChange={(event) => patchMetadata("slug", event.target.value)} /></ToolField>
          <ToolField label="名称"><input className="input h-10 py-1" value={metadata.name} onChange={(event) => patchMetadata("name", event.target.value)} /></ToolField>
          <ToolField label="分类"><select className="input h-10 py-1" value={metadata.category} onChange={(event) => patchMetadata("category", event.target.value)}>{TOOL_CATEGORIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></ToolField>
          <ToolField label="渲染器"><input className="input h-10 py-1" value={metadata.renderer} onChange={(event) => patchMetadata("renderer", event.target.value)} /></ToolField>
          <ToolField label="站内入口"><input className="input h-10 py-1" value={metadata.entry_path} onChange={(event) => patchMetadata("entry_path", event.target.value)} /></ToolField>
          <ToolField label="排序"><input className="input h-10 py-1" type="number" value={metadata.sort_order} onChange={(event) => patchMetadata("sort_order", event.target.value)} /></ToolField>
          <ToolField label="图标"><input className="input h-10 py-1" value={metadata.icon} onChange={(event) => patchMetadata("icon", event.target.value)} /></ToolField>
          <ToolField label="描述"><input className="input h-10 py-1" value={metadata.description} onChange={(event) => patchMetadata("description", event.target.value)} /></ToolField>
          <div className="flex min-h-10 items-end gap-4 pb-2 text-xs text-mist">
            <label className="flex items-center gap-2"><input type="checkbox" checked={metadata.enabled} onChange={(event) => patchMetadata("enabled", event.target.checked)} />启用</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={metadata.featured} onChange={(event) => patchMetadata("featured", event.target.checked)} />推荐</label>
          </div>
        </div>

        {creating ? <div className="mt-5 border-t border-line pt-4"><VersionEditor draft={versionDraft} setDraft={setVersionDraft} /></div> : null}

        {!creating && detail ? (
          <div className="mt-5 border-t border-line pt-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h4 className="font-display text-sm font-semibold text-snow">版本历史</h4>
              <button type="button" className="btn-ghost btn-sm" onClick={() => openVersionEditor()}><Plus size={14} />新建草稿</button>
            </div>
            {versionEditor ? (
              <div className="mb-4 border border-line bg-base/25 p-4">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <div><h5 className="text-xs font-semibold text-snow">{versionEditor.version ? `编辑 v${versionEditor.version} 草稿` : "新建版本草稿"}</h5><p className="mt-1 text-[10px] text-fog">草稿不会进入目录或执行；发布后配置不可直接编辑。</p></div>
                  <button type="button" className="icon-btn" title="关闭版本编辑" onClick={() => setVersionEditor(null)}><X size={15} /></button>
                </div>
                <VersionEditor draft={versionDraft} setDraft={setVersionDraft} />
                <div className="mt-3 flex justify-end gap-2"><button type="button" className="btn-ghost btn-sm" onClick={() => setVersionEditor(null)}>取消</button><button type="button" className="btn-primary btn-sm" onClick={saveVersionDraft} disabled={Boolean(busy)}><Save size={14} />{versionEditor.version ? "保存草稿" : "创建草稿"}</button></div>
              </div>
            ) : null}
            <div className="space-y-2">
              {versions.map((version) => {
                const source = versions.find((item) => Number(item.id) === Number(version.source_version_id));
                return <article key={version.id} className="border border-line bg-base/25 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-mist"><b className="text-snow">v{version.version}</b><span>{version.schema_version}</span><ModelVersionStatus version={version} />{version.source_version_id ? <span className="text-aqua">{source ? `回滚自 v${source.version}` : `回滚来源 #${version.source_version_id}`}</span> : null}</div>
                    {toolVersionActions(version)}
                  </div>
                  <p className="mt-2 text-[11px] text-fog">创建 {formatTime(version.created_at)}{version.activated_at ? ` · 发布 ${formatTime(version.activated_at)}` : ""}{version.disabled_at ? ` · 停用 ${formatTime(version.disabled_at)}` : ""}{version.retired_at ? ` · 退役 ${formatTime(version.retired_at)}` : ""}</p>
                  <ToolMetadataSnapshotSummary snapshot={version.metadata_snapshot} />
                  <div className="mt-2 grid gap-x-4 sm:grid-cols-2"><JsonPreview value={version.input_schema} label="输入 Schema" /><JsonPreview value={version.workflow} label="工作流" /><JsonPreview value={version.pricing_policy} label="计价策略" /><JsonPreview value={version.capabilities} label="能力声明" /></div>
                </article>;
              })}
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}

export function CatalogVersions() {
  const [view, setView] = useState("models");
  const [models, setModels] = useState([]);
  const [modelError, setModelError] = useState("");

  useEffect(() => {
    api.adminModels()
      .then((payload) => setModels(Array.isArray(payload) ? payload : payload.models || []))
      .catch((error) => setModelError(error.message));
  }, []);

  const counts = useMemo(() => ({ models: models.length }), [models]);

  return (
    <Card className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-lg font-semibold text-snow">能力与工具版本</h2>
          <p className="mt-1 text-xs text-fog">{counts.models} 个模型配置</p>
        </div>
        <div className="inline-flex flex-wrap border border-line bg-base/35 p-1" role="tablist" aria-label="能力与工具版本视图">
          <button type="button" role="tab" aria-selected={view === "models"} className={`min-h-9 px-3 text-xs ${view === "models" ? "bg-brand text-white" : "text-mist"}`} onClick={() => setView("models")}><Settings2 size={14} className="mr-1 inline" />模型版本</button>
          <button type="button" role="tab" aria-selected={view === "routes"} className={`min-h-9 px-3 text-xs ${view === "routes" ? "bg-brand text-white" : "text-mist"}`} onClick={() => setView("routes")}><Activity size={14} className="mr-1 inline" />模型路由</button>
          <button type="button" role="tab" aria-selected={view === "tools"} className={`min-h-9 px-3 text-xs ${view === "tools" ? "bg-brand text-white" : "text-mist"}`} onClick={() => setView("tools")}><Wrench size={14} className="mr-1 inline" />工具目录</button>
        </div>
      </div>
      {modelError ? <p className="text-xs text-bad">{modelError}</p> : null}
      {view === "models" ? <ModelVersionsPanel models={models} /> : view === "routes" ? <ModelRoutesPanel models={models} /> : <ToolCatalogPanel />}
    </Card>
  );
}
