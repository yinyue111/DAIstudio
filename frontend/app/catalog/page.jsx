"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Check,
  CircleHelp,
  CircleX,
  Eye,
  Film,
  GitCompareArrows,
  Image as ImageIcon,
  Search,
  Sparkles,
  WandSparkles,
  Workflow,
  Wrench,
} from "lucide-react";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";
import { errorMessage, redirectOnAuthError } from "../../lib/errorHandling";
import { catalogModelStudioHref } from "../studio/workflowPreset";

const VIEWS = [
  ["models", "模型"],
  ["tools", "工具与工作流"],
];
const MODEL_FILTERS = [
  ["all", "全部"],
  ["image", "图片"],
  ["video", "视频"],
  ["vision", "视觉反推"],
  ["prompt", "提示词"],
];
const TOOL_FILTERS = [
  ["all", "全部"],
  ["image", "图片"],
  ["video", "视频"],
  ["workflow", "工作流"],
  ["utility", "实用工具"],
];

const MODEL_META = {
  image: { label: "图片", icon: ImageIcon },
  video: { label: "视频", icon: Film },
  vision: { label: "视觉反推", icon: Eye },
  prompt: { label: "提示词", icon: Sparkles },
};
const TOOL_META = {
  image: { label: "图片", icon: ImageIcon },
  video: { label: "视频", icon: Film },
  workflow: { label: "工作流", icon: Workflow },
  utility: { label: "实用工具", icon: Wrench },
};

const CAPABILITY_LABELS = {
  text_to_image: "文生图",
  image_to_image: "图片编辑",
  mask_edit: "蒙版编辑",
  reference_image: "独立参考图",
  multi_reference: "多图参考",
  text_to_video: "文生视频",
  image_to_video: "首帧图生视频",
  video_reference: "视频参考",
  video_edit: "视频编辑",
  audio_reference: "音频参考输入",
  generated_audio: "生成同步音频",
  first_last_frame: "首尾帧",
  product_profile: "商品档案识别",
  portrait_profile: "人物档案识别",
  prompt_optimization: "提示词优化",
  image_analysis: "图片反推",
  video_analysis: "视频反推（平台抽帧分析）",
};

function capabilityLabels(capabilities) {
  return Object.entries(capabilities || {}).flatMap(([key, value]) => {
    if (value !== true || !CAPABILITY_LABELS[key]) return [];
    return [CAPABILITY_LABELS[key]];
  });
}

function FilterBar({ items, value, onChange }) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label="目录筛选">
      {items.map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
            value === key ? "bg-brand text-white" : "text-mist hover:bg-white/5 hover:text-snow"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

const CONSTRAINT_ROWS = [
  ["aspect_ratios", "画幅比例"],
  ["resolutions", "分辨率"],
  ["durations", "可选时长"],
  ["min_duration_seconds", "最短时长"],
  ["max_duration_seconds", "最长时长"],
  ["max_reference_duration_seconds", "参考图模式最长时长"],
  ["max_reference_images", "平台最多参考图"],
  ["max_reference_videos", "平台最多视频输入"],
  ["max_reference_audio", "平台最多音频输入"],
];

function hasCapability(capabilities, key) {
  return Boolean(capabilities && Object.prototype.hasOwnProperty.call(capabilities, key));
}

function CapabilityState({ value, declared }) {
  if (!declared) {
    return <span className="inline-flex items-center gap-1 text-fog"><CircleHelp size={13} aria-hidden="true" />未知</span>;
  }
  if (value === true) {
    return <span className="inline-flex items-center gap-1 text-ok"><Check size={13} aria-hidden="true" />支持</span>;
  }
  return <span className="inline-flex items-center gap-1 text-bad"><CircleX size={13} aria-hidden="true" />不支持</span>;
}

function constraintValue(model, key) {
  const capabilities = model.capabilities || {};
  if (!hasCapability(capabilities, key)) return "未知";
  const value = capabilities[key];
  if (Array.isArray(value)) return value.length ? value.join(" / ") : "未声明";
  if (key === "max_reference_images") return `${value} 张`;
  if (["max_reference_videos", "max_reference_audio"].includes(key)) {
    return `${value} 个`;
  }
  if (["min_duration_seconds", "max_duration_seconds", "max_reference_duration_seconds"].includes(key)) {
    return `${value} 秒`;
  }
  return String(value);
}

function routeAvailabilityLabel(model) {
  const route = model.route_availability;
  if (!route || typeof route !== "object") return "未知";
  const configured = Number(route.configured);
  const available = Number(route.available);
  if (!Number.isFinite(configured) || !Number.isFinite(available)) return "未知";
  return route.status === "available"
    ? `可用 ${available}/${configured}`
    : `不可用 ${available}/${configured}`;
}

function ModelStudioAction({ model, label = "使用" }) {
  const entry = catalogModelStudioHref(model);
  if (entry.status !== "ready" || !entry.href) {
    return (
      <button
        type="button"
        className="btn-secondary btn-sm"
        disabled
        title={entry.message}
        aria-label={`${label}不可用：${entry.message}`}
      >
        不可直达
      </button>
    );
  }
  return <Link href={entry.href} className="btn-secondary btn-sm">{label}</Link>;
}

function ModelCard({ model, compared, onToggleCompare }) {
  const meta = MODEL_META[model.use] || MODEL_META.image;
  const Icon = meta.icon;
  const capabilities = capabilityLabels(model.capabilities);
  return (
    <article className="flex min-h-52 flex-col rounded-lg border border-line bg-white/[0.025] p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line bg-base2 text-iris-400">
          <Icon size={18} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h2 className="truncate text-sm font-display font-semibold text-snow">{model.name}</h2>
            {model.is_default && <span className="rounded-full bg-good/10 px-2 py-0.5 text-[10px] text-good">默认</span>}
          </div>
          <p className="mt-0.5 truncate text-xs text-fog">{model.provider_label} · {model.model_id}</p>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-1.5">
        {(capabilities.length ? capabilities : [meta.label]).map((label) => (
          <span key={label} className="rounded-full border border-line px-2 py-1 text-[11px] text-mist">{label}</span>
        ))}
      </div>
      <div className="mt-auto flex items-end justify-between gap-3 border-t border-line pt-4">
        <div>
          <p className="text-[10px] uppercase text-fog">当前价格版本</p>
          <p className="mt-1 text-sm font-semibold text-snow">
            {model.cost_credits} 积分起
            <span className="ml-1 text-[10px] font-normal text-fog">
              {model.price_version?.version ? `v${model.price_version.version}` : "未发布"}
            </span>
          </p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            className={`icon-btn h-9 w-9 ${compared ? "border-aqua/50 bg-aqua/10 text-aqua" : ""}`}
            onClick={() => onToggleCompare(model.id)}
            title={compared ? "移出对比" : "加入对比"}
            aria-label={compared ? `将 ${model.name} 移出对比` : `将 ${model.name} 加入对比`}
            aria-pressed={compared}
          >
            {compared ? <Check size={15} aria-hidden="true" /> : <GitCompareArrows size={15} aria-hidden="true" />}
          </button>
          <ModelStudioAction model={model} />
        </div>
      </div>
    </article>
  );
}

function ModelComparison({ models, onRemove, onClear }) {
  if (!models.length) return null;
  const capabilityKeys = Object.keys(CAPABILITY_LABELS).filter((key) => (
    models.some((model) => hasCapability(model.capabilities, key))
  ));
  return (
    <section className="mb-4 border-y border-line bg-white/[0.02] py-4" aria-labelledby="model-comparison-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 id="model-comparison-title" className="flex items-center gap-2 text-sm font-display font-semibold text-snow"><GitCompareArrows size={16} />模型对比</h2>
          {models.map((model) => (
            <button key={model.id} type="button" className="chip chip-active" onClick={() => onRemove(model.id)} title="移出对比">
              {model.name} <span aria-hidden="true">×</span>
            </button>
          ))}
        </div>
        <button type="button" className="btn-ghost btn-sm" onClick={onClear}>清空</button>
      </div>
      {models.length > 1 ? (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[42rem] table-fixed text-left text-xs">
            <thead><tr className="border-b border-line text-fog"><th className="w-36 py-2 pr-3 font-medium">对比项</th>{models.map((model) => <th key={model.id} className="py-2 pr-3 font-medium text-mist">{model.name}</th>)}</tr></thead>
            <tbody className="divide-y divide-line/70">
              <tr><th className="py-2 pr-3 font-medium text-fog">提供商</th>{models.map((model) => <td key={model.id} className="py-2 pr-3 text-mist">{model.provider_label}</td>)}</tr>
              <tr><th className="py-2 pr-3 font-medium text-fog">调用价格</th>{models.map((model) => <td key={model.id} className="py-2 pr-3 font-semibold text-snow">{model.cost_credits} 积分</td>)}</tr>
              <tr><th className="py-2 pr-3 font-medium text-fog">价格版本</th>{models.map((model) => <td key={model.id} className="py-2 pr-3 text-mist">{model.price_version?.version ? `v${model.price_version.version}` : "未知"}</td>)}</tr>
              <tr><th className="py-2 pr-3 font-medium text-fog">能力版本</th>{models.map((model) => <td key={model.id} className="py-2 pr-3 text-mist">{model.capability_version?.version ? `v${model.capability_version.version}` : "未知"}</td>)}</tr>
              <tr><th className="py-2 pr-3 font-medium text-fog">路由状态</th>{models.map((model) => <td key={model.id} className={`py-2 pr-3 ${model.route_availability?.status === "unavailable" ? "text-bad" : "text-mist"}`}>{routeAvailabilityLabel(model)}</td>)}</tr>
              {capabilityKeys.map((key) => (
                <tr key={key}><th className="py-2 pr-3 font-medium text-fog">{CAPABILITY_LABELS[key] || key}</th>{models.map((model) => <td key={model.id} className="py-2 pr-3"><CapabilityState value={model.capabilities?.[key]} declared={hasCapability(model.capabilities, key)} /></td>)}</tr>
              ))}
              {CONSTRAINT_ROWS.map(([key, label]) => (
                <tr key={key}><th className="py-2 pr-3 font-medium text-fog">{label}</th>{models.map((model) => <td key={model.id} className="py-2 pr-3 text-mist">{constraintValue(model, key)}</td>)}</tr>
              ))}
              <tr><th className="py-2 pr-3" />{models.map((model) => <td key={model.id} className="py-2 pr-3"><ModelStudioAction model={model} label="在创作中使用" /></td>)}</tr>
            </tbody>
          </table>
        </div>
      ) : <p className="mt-3 text-xs text-fog">再选择一个模型即可比较能力和价格。</p>}
    </section>
  );
}

function ToolCard({ tool }) {
  const meta = TOOL_META[tool.category] || TOOL_META.workflow;
  const Icon = tool.featured ? WandSparkles : meta.icon;
  const hasActiveVersion = Boolean(tool.active_version?.is_active && tool.active_version?.version);
  const studioRenderer = String(tool.renderer || "").toLowerCase() === "studio";
  const canOpen = tool.enabled !== false && hasActiveVersion && studioRenderer;
  const unavailableReason = !studioRenderer
    ? `当前客户端不支持渲染器 ${tool.renderer || "unknown"}`
    : !hasActiveVersion
      ? "该工具没有可用的活动版本"
      : "该工具已禁用";
  return (
    <article className="flex min-h-52 flex-col rounded-lg border border-line bg-white/[0.025] p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line bg-base2 text-brand">
          <Icon size={18} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-display font-semibold text-snow">{tool.name}</h2>
          <p className="mt-0.5 text-xs text-fog">{meta.label} · {hasActiveVersion ? `v${tool.active_version.version}` : "未发布"}</p>
        </div>
      </div>
      <p className="mt-4 line-clamp-3 text-sm leading-6 text-mist">{tool.description || "进入统一创作工作台继续配置。"}</p>
      <div className="mt-auto flex items-center justify-between gap-3 border-t border-line pt-4">
        <span className="text-xs text-fog">按使用量自动计费</span>
        {canOpen
          ? <Link href={tool.entry_path} className="btn-secondary btn-sm">打开</Link>
          : <button type="button" className="btn-secondary btn-sm" disabled title={unavailableReason}>不可用</button>}
      </div>
    </article>
  );
}

export default function CatalogPage() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [models, setModels] = useState([]);
  const [tools, setTools] = useState([]);
  const [view, setView] = useState("models");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [comparisonIds, setComparisonIds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([api.me(), api.modelCatalog(), api.toolCatalog()])
      .then(([user, modelPage, toolPage]) => {
        if (!active) return;
        setMe(user);
        setModels(modelPage.items || []);
        setTools(toolPage.items || []);
      })
      .catch((loadError) => {
        if (!active || redirectOnAuthError(loadError, router, setError, "catalog session probe")) return;
        setError(errorMessage(loadError, "加载能力目录失败"));
      })
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [router]);

  const visibleItems = useMemo(() => {
    const source = view === "models" ? models : tools;
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    return source.filter((item) => {
      const category = view === "models" ? item.use : item.category;
      if (filter !== "all" && category !== filter) return false;
      if (!normalized) return true;
      return [item.name, item.model_id, item.provider_label, item.slug, item.description]
        .filter(Boolean)
        .some((value) => String(value).toLocaleLowerCase("zh-CN").includes(normalized));
    });
  }, [filter, models, query, tools, view]);

  function changeView(next) {
    setView(next);
    setFilter("all");
  }

  function toggleComparison(id) {
    setComparisonIds((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      return [...current.slice(-2), id];
    });
  }

  const comparedModels = comparisonIds
    .map((id) => models.find((model) => model.id === id))
    .filter(Boolean);

  return (
    <div className="min-h-screen">
      <Nav me={me} active="catalog" />
      <main className="mx-auto max-w-7xl px-4 py-7 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-5">
          <div>
            <h1 className="text-2xl font-display font-bold text-snow">模型与工具</h1>
            <p className="mt-1 text-sm text-fog">查看可用能力、当前价格版本，并进入统一创作工作台。</p>
          </div>
          <label className="relative block w-full sm:w-72">
            <Search size={15} className="pointer-events-none absolute left-3 top-2.5 text-fog" aria-hidden="true" />
            <span className="sr-only">搜索目录</span>
            <input className="input h-9 w-full pl-9 text-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型或工具" />
          </label>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 py-4">
          <div className="inline-flex rounded-full border border-line bg-white/5 p-1">
            {VIEWS.map(([key, label]) => (
              <button key={key} type="button" onClick={() => changeView(key)} className={`rounded-full px-4 py-1.5 text-sm ${view === key ? "bg-brand text-white" : "text-mist"}`}>{label}</button>
            ))}
          </div>
          <FilterBar items={view === "models" ? MODEL_FILTERS : TOOL_FILTERS} value={filter} onChange={setFilter} />
        </div>

        {view === "models" ? (
          <ModelComparison models={comparedModels} onRemove={toggleComparison} onClear={() => setComparisonIds([])} />
        ) : null}

        {error && <div className="mb-4 border-y border-bad/30 bg-bad/5 px-3 py-2 text-sm text-bad">{error}</div>}
        {loading ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="目录加载中">
            {[0, 1, 2, 3, 4, 5].map((item) => <div key={item} className="skeleton h-52 rounded-lg" />)}
          </div>
        ) : visibleItems.length ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {view === "models"
              ? visibleItems.map((item) => <ModelCard key={item.id} model={item} compared={comparisonIds.includes(item.id)} onToggleCompare={toggleComparison} />)
              : visibleItems.map((item) => <ToolCard key={item.id} tool={item} />)}
          </div>
        ) : (
          <div className="border-y border-line py-16 text-center text-sm text-fog">没有符合条件的能力。</div>
        )}
      </main>
    </div>
  );
}
