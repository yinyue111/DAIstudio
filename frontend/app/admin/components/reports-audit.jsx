"use client";

import { useEffect, useRef, useState } from "react";
import { Download, RefreshCw, RotateCcw } from "lucide-react";
import { api, downloadBlob } from "../../../lib/api";
import { formatLocalDateTime } from "../../../lib/datetime";
import { Card, Th } from "./admin-ui";
import { createLatestRequestGate } from "./latest-request";
import {
  buildReverseReportQuery,
  updateReverseReportFilters,
} from "./reverse-report-filters";

export function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [costs, setCosts] = useState(null);
  const [reverseUsage, setReverseUsage] = useState(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [mediaType, setMediaType] = useState("");
  const [model, setModel] = useState("");
  const [focus, setFocus] = useState("");
  const [qualityDimension, setQualityDimension] = useState("overall");
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [msg, setMsg] = useState("");
  const requestGateRef = useRef(null);
  if (!requestGateRef.current) requestGateRef.current = createLatestRequestGate();

  function dateQs() {
    const p = [];
    if (start) p.push(`start=${encodeURIComponent(start)}`);
    if (end) p.push(`end=${encodeURIComponent(end)}`);
    return p.length ? `?${p.join("&")}` : "";
  }

  function reverseQs(overrides = null, format = "json") {
    const selected = overrides || { mediaType, model, focus };
    return buildReverseReportQuery({ start, end, ...selected }, format);
  }

  function load(reverseOverrides = null) {
    const requestGeneration = requestGateRef.current.begin();
    setLoading(true);
    setMsg("");
    const suffix = dateQs();
    Promise.all([
      api.adminUsageDashboard(suffix),
      api.adminModelCosts(suffix),
      api.adminReverseUsage(reverseQs(reverseOverrides)),
    ])
      .then(([dashboard, modelCosts, nextReverseUsage]) => {
        if (!requestGateRef.current.isCurrent(requestGeneration)) return;
        setSummary(dashboard);
        setCosts(modelCosts);
        setReverseUsage(nextReverseUsage);
      })
      .catch((e) => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setMsg(e.message);
      })
      .finally(() => {
        if (requestGateRef.current.isCurrent(requestGeneration)) setLoading(false);
      });
  }

  useEffect(() => {
    load();
    return () => requestGateRef.current.invalidate();
  }, []);

  const s = summary?.summary || {};
  const modelRows = costs?.models || [];
  const reverseQuality = reverseUsage?.quality || {};
  const economics = reverseUsage?.economics || {};
  const filterOptions = reverseUsage?.filters?.options || {};
  const qualityConfig = qualityDimensionConfig(qualityDimension);
  const qualityRows = reverseQualityRows(reverseUsage, qualityDimension);
  const hasReverseFilters = Boolean(mediaType || model || focus);

  function clearReverseFilters() {
    const cleared = { mediaType: "", model: "", focus: "" };
    setMediaType("");
    setModel("");
    setFocus("");
    load(cleared);
  }

  function changeReverseFilter(key, value) {
    const next = updateReverseReportFilters({ mediaType, model, focus }, key, value);
    setMediaType(next.mediaType);
    setModel(next.model);
    setFocus(next.focus);
    load(next);
  }

  async function downloadReverseCsv() {
    setDownloading(true);
    setMsg("");
    try {
      await downloadBlob(
        `/api/admin/usage/reverse-operations${reverseQs(null, "csv")}`,
        "reverse_quality_report.csv",
      );
    } catch (e) {
      setMsg(e.message);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <FilterField label="开始日期">
            <input type="date" className="input min-w-0" value={start} onChange={(e) => setStart(e.target.value)} />
          </FilterField>
          <FilterField label="结束日期">
            <input type="date" className="input min-w-0" value={end} onChange={(e) => setEnd(e.target.value)} />
          </FilterField>
          <FilterField label="媒体类型">
            <select className="select min-w-0" value={mediaType} onChange={(e) => changeReverseFilter("mediaType", e.target.value)}>
              <option value="">全部媒体</option>
              {(filterOptions.media_types || []).map((value) => (
                <option key={value} value={value}>{mediaTypeLabel(value)}</option>
              ))}
            </select>
          </FilterField>
          <FilterField label="反推模型">
            <select className="select min-w-0" value={model} onChange={(e) => changeReverseFilter("model", e.target.value)}>
              <option value="">全部模型</option>
              {(filterOptions.models || []).map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </FilterField>
          <FilterField label="分析目标">
            <select className="select min-w-0" value={focus} onChange={(e) => changeReverseFilter("focus", e.target.value)}>
              <option value="">全部目标</option>
              {(filterOptions.focuses || []).map((value) => (
                <option key={value} value={value}>{focusLabel(value)}</option>
              ))}
            </select>
          </FilterField>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => load()} disabled={loading} className="btn-secondary">
            <RefreshCw size={15} aria-hidden="true" className={loading ? "animate-spin" : ""} />
            {loading ? "查询中…" : "刷新概览"}
          </button>
          <button type="button" onClick={clearReverseFilters} disabled={loading || !hasReverseFilters} className="btn-ghost">
            <RotateCcw size={15} aria-hidden="true" />
            清除反推筛选
          </button>
          <button type="button" onClick={downloadReverseCsv} disabled={downloading} className="btn-primary sm:ml-auto">
            <Download size={15} aria-hidden="true" />
            {downloading ? "导出中…" : "导出反推质量 CSV"}
          </button>
        </div>
        {msg && <p className="mt-3 text-sm text-bad" role="alert">{msg}</p>}
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
          <div className="flex flex-wrap items-end justify-between gap-3 pt-2">
            <div>
              <h2 className="font-display text-lg font-semibold text-snow">反推质量与转化</h2>
              <p className="mt-1 text-xs text-fog">当前筛选共 {reverseUsage?.summary?.operation_count || 0} 个任务</p>
            </div>
            <div className="flex max-w-full overflow-x-auto border border-line bg-base/35 p-1" role="tablist" aria-label="反推质量分组维度">
              {QUALITY_DIMENSIONS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="tab"
                  aria-selected={qualityDimension === item.key}
                  className={`min-h-9 shrink-0 px-3 text-xs transition-colors ${qualityDimension === item.key ? "bg-brand text-white" : "text-mist hover:bg-white/5 hover:text-snow"}`}
                  onClick={() => setQualityDimension(item.key)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="成功率" value={formatRate(reverseUsage?.summary?.success_rate)} detail={`${reverseUsage?.summary?.succeeded || 0} 个成功`} />
            <Metric label="证据覆盖率" value={formatRate(reverseQuality.avg_evidence_coverage)} />
            <Metric label="结果采用率" value={formatRate(reverseQuality.adoption_rate)} />
            <Metric label="用户编辑率" value={formatRate(reverseQuality.edit_rate)} />
            <Metric label="平均编辑幅度" value={formatRate(reverseQuality.avg_edit_ratio)} />
            <Metric label="生成转化率" value={formatRate(reverseQuality.generation_conversion_rate)} />
            <Metric label="配方沉淀率" value={formatRate(reverseQuality.recipe_conversion_rate)} />
            <Metric label="反馈有用率" value={formatRate(reverseQuality.useful_rate)} />
          </div>
          <Card>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-display font-semibold text-snow">{qualityConfig.label}明细</h3>
              <span className="text-xs text-fog">收入与成本单位：积分</span>
            </div>
            <ReverseQualityTable rows={qualityRows} dimension={qualityConfig.field} dimensionLabel={qualityConfig.label} />
          </Card>
          <div>
            <h2 className="font-display text-lg font-semibold text-snow">反推成本与毛利</h2>
            <p className="mt-1 text-xs text-fog">成本记录覆盖 {formatRate(economics.gateway_cost_coverage_rate)}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Metric label="收入（积分）" value={formatCredits(economics.revenue_credits)} />
            <Metric
              label="供应商成本（积分）"
              value={providerCostDisplay(economics)}
              detail={economics.cost_status === "partial" ? "仅统计已记录成本" : ""}
            />
            <Metric label="成本状态" value={<CostStatusBadge status={economics.cost_status} />} valueClassName="text-base" />
            <Metric label="毛利（积分）" value={completeCost(economics) ? formatCredits(economics.gross_profit_credits) : "不可用"} />
            <Metric label="毛利率" value={completeCost(economics) ? formatRate(economics.gross_margin_rate) : "不可用"} />
            <Metric label="成本归属覆盖" value={formatRate(economics.operation_cost_coverage_rate)} />
          </div>
          <Card>
            <div className="mb-3 text-sm font-display font-semibold text-snow">反馈问题分布</div>
            <IssueDistribution issues={reverseQuality.issue_types || {}} />
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

const QUALITY_DIMENSIONS = [
  { key: "overall", label: "总体", field: "overall" },
  { key: "media", label: "媒体", field: "media_type" },
  { key: "model", label: "模型", field: "model" },
  { key: "focus", label: "目标", field: "focus" },
];

function FilterField({ label, children }) {
  return (
    <label className="grid min-w-0 gap-1 text-xs text-fog">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Metric({ label, value, detail = "", valueClassName = "text-2xl" }) {
  return (
    <Card>
      <div className="text-xs text-fog">{label}</div>
      <div className={`mt-1 min-h-8 font-display font-bold text-snow ${valueClassName}`}>{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-fog">{detail}</div> : null}
    </Card>
  );
}

function formatRate(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

function formatCredits(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function completeCost(value) {
  return value?.cost_status === "complete";
}

function providerCostDisplay(value) {
  if (!value?.cost_status || value.cost_status === "unavailable") return "不可用";
  return formatCredits(value.provider_cost_credits);
}

function CostStatusBadge({ status }) {
  const config = {
    complete: ["成本完整", "border-ok/35 bg-ok/10 text-ok"],
    partial: ["部分成本", "border-warn/35 bg-warn/10 text-warn"],
    unavailable: ["成本不可用", "border-line bg-white/5 text-fog"],
  }[status] || ["成本不可用", "border-line bg-white/5 text-fog"];
  return <span className={`badge border ${config[1]}`}>{config[0]}</span>;
}

function qualityDimensionConfig(dimension) {
  return QUALITY_DIMENSIONS.find((item) => item.key === dimension) || QUALITY_DIMENSIONS[0];
}

function reverseQualityRows(data, dimension) {
  if (!data) return [];
  if (dimension === "media") return data.by_media_type || [];
  if (dimension === "model") return data.by_model_quality || [];
  if (dimension === "focus") return data.by_focus || [];
  return [{
    overall: "all",
    operation_count: data.summary?.operation_count || 0,
    succeeded: data.summary?.succeeded || 0,
    success_rate: data.summary?.success_rate,
    avg_evidence_coverage: data.quality?.avg_evidence_coverage,
    adoption_rate: data.quality?.adoption_rate,
    edit_rate: data.quality?.edit_rate,
    avg_edit_ratio: data.quality?.avg_edit_ratio,
    generation_conversion_rate: data.quality?.generation_conversion_rate,
    recipe_conversion_rate: data.quality?.recipe_conversion_rate,
    useful_rate: data.quality?.useful_rate,
    settled_credits: data.economics?.revenue_credits,
    provider_cost_credits: data.economics?.provider_cost_credits,
    cost_status: data.economics?.cost_status,
    gross_profit_credits: data.economics?.gross_profit_credits,
    gross_margin_rate: data.economics?.gross_margin_rate,
  }];
}

function mediaTypeLabel(value) {
  return { image: "图片", video: "视频", unknown: "未知" }[value] || value;
}

function focusLabel(value) {
  return {
    comprehensive: "全面拆解",
    replica: "同款复刻",
    style: "风格提取",
    product_ad: "商品广告",
    portrait: "人像摄影",
    composition_lighting: "构图光影",
    poster_layout: "海报版式",
    camera_motion: "镜头运动",
    subject_action: "主体动作",
    storyboard: "分镜脚本",
    editing_rhythm: "剪辑节奏",
    audio_script: "音频文案",
  }[value] || value;
}

function qualityValueLabel(value, dimension) {
  if (dimension === "overall") return "全部反推";
  if (dimension === "media_type") return mediaTypeLabel(value);
  if (dimension === "focus") return focusLabel(value);
  return value === "unknown" ? "未知" : value;
}

function ReverseQualityTable({ rows, dimension, dimensionLabel }) {
  if (!rows?.length) return <p className="py-8 text-center text-sm text-fog">暂无反推质量数据。</p>;
  return (
    <div className="max-w-full overflow-x-auto">
      <table className="w-full min-w-[94rem] text-left text-xs">
        <thead><tr className="border-b border-line">
          <Th>{dimensionLabel}</Th><Th>任务</Th><Th>成功率</Th><Th>证据覆盖</Th><Th>采用率</Th><Th>编辑率</Th><Th>编辑幅度</Th><Th>生成转化</Th><Th>Recipe 转化</Th><Th>反馈有用</Th><Th>收入</Th><Th>供应商成本</Th><Th>成本状态</Th><Th>毛利</Th><Th>毛利率</Th>
        </tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row[dimension]} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
              <td className="whitespace-nowrap py-2.5 pr-3 font-medium text-snow">{qualityValueLabel(row[dimension], dimension)}</td>
              <td className="whitespace-nowrap pr-3">{row.operation_count || 0}</td>
              <td className="whitespace-nowrap pr-3 font-display text-snow">{formatRate(row.success_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.avg_evidence_coverage)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.adoption_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.edit_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.avg_edit_ratio)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.generation_conversion_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.recipe_conversion_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatRate(row.useful_rate)}</td>
              <td className="whitespace-nowrap pr-3">{formatCredits(row.settled_credits)}</td>
              <td className="whitespace-nowrap pr-3">{providerCostDisplay(row)}</td>
              <td className="whitespace-nowrap pr-3"><CostStatusBadge status={row.cost_status} /></td>
              <td className="whitespace-nowrap pr-3">{completeCost(row) ? formatCredits(row.gross_profit_credits) : "不可用"}</td>
              <td className="whitespace-nowrap">{completeCost(row) ? formatRate(row.gross_margin_rate) : "不可用"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function IssueDistribution({ issues }) {
  const labels = {
    subject_error: "主体错误",
    style_error: "风格错误",
    action_missing: "动作遗漏",
    shot_missing: "镜头遗漏",
    camera_error: "运镜错误",
    text_error: "文字错误",
    audio_error: "音频错误",
    hallucination: "无证据推测",
    other: "其他",
  };
  const rows = Object.entries(issues).sort((a, b) => Number(b[1]) - Number(a[1]));
  if (!rows.length) return <p className="py-8 text-center text-sm text-fog">暂无质量问题反馈。</p>;
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {rows.map(([key, value]) => (
        <div key={key} className="rounded-xl border border-line bg-white/[0.03] px-3 py-2">
          <div className="text-[11px] text-fog">{labels[key] || key}</div>
          <div className="font-display text-xl font-bold text-snow">{value}</div>
        </div>
      ))}
    </div>
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
