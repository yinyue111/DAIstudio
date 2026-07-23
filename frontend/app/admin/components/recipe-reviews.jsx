"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  FileText,
  ImageIcon,
  RefreshCw,
  ShieldCheck,
  Video,
  X,
} from "lucide-react";
import { api } from "../../../lib/api";
import { formatLocalDateTime } from "../../../lib/datetime";
import AssetMedia from "../../../components/AssetMedia";
import { Card } from "./admin-ui";

const FILTERS = [
  ["pending", "待审核"],
  ["approved", "已通过"],
  ["rejected", "已驳回"],
  ["all", "全部"],
];

const STATUS = {
  draft: ["草稿", "text-fog bg-white/5"],
  pending: ["待审核", "text-warn bg-warn/10"],
  approved: ["已通过", "text-ok bg-ok/10"],
  rejected: ["已驳回", "text-bad bg-bad/10"],
};

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function recipeSummary(recipe) {
  const payload = objectValue(recipe?.version?.payload);
  const snapshot = objectValue(payload.reverse_snapshot_v3);
  const reverseResult = objectValue(payload.reverse_result);
  return {
    prompt: String(payload.prompt || snapshot.final_text || reverseResult.final_text || ""),
    negative: String(payload.negative || ""),
    structured: objectValue(payload.structured),
    payload,
  };
}

function RecipeReviewRow({ recipe, busy, onDecision }) {
  const [note, setNote] = useState("");
  const [localError, setLocalError] = useState("");
  const summary = useMemo(() => recipeSummary(recipe), [recipe]);
  const pending = recipe.moderation_status === "pending";
  const CategoryIcon = recipe.category === "video" ? Video : ImageIcon;
  const [statusLabel, statusClass] = STATUS[recipe.moderation_status] || STATUS.draft;
  const coverAsset = recipe.cover_asset_url
    ? {
        type: "image",
        origin: "uploaded",
        url: recipe.cover_asset_url,
        preview_url: recipe.cover_asset_url,
        available: true,
      }
    : null;

  async function decide(action) {
    const normalizedNote = note.trim();
    if (action === "reject" && !normalizedNote) {
      setLocalError("驳回时必须填写审核意见");
      return;
    }
    setLocalError("");
    await onDecision(recipe, action, normalizedNote || null);
  }

  return (
    <article className="border-t border-line py-4 first:border-t-0 first:pt-0 last:pb-0">
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex h-10 w-10 flex-none items-center justify-center rounded-xl border border-line bg-white/5 text-mist">
          <CategoryIcon size={18} aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 className="break-words text-sm font-semibold text-snow">{recipe.title}</h3>
              <p className="mt-1 text-[11px] text-fog">
                #{recipe.id} · v{recipe.current_version} · {recipe.category === "video" ? "视频" : "图片"}
                {recipe.submitted_at ? ` · ${formatLocalDateTime(recipe.submitted_at)} 提交` : ""}
              </p>
            </div>
            <span className={`inline-flex min-h-6 items-center rounded px-2 text-[11px] ${statusClass}`}>{statusLabel}</span>
          </div>

          <p className="mt-3 line-clamp-4 whitespace-pre-wrap break-words text-xs leading-6 text-mist">
            {summary.prompt || "该配方以结构化内容为主，没有单独提示词。"}
          </p>

          <details className="group mt-2">
            <summary className="flex min-h-9 cursor-pointer list-none items-center gap-1.5 text-xs text-fog hover:text-mist">
              <ChevronDown size={13} className="transition group-open:rotate-180" aria-hidden="true" />
              查看审核内容
            </summary>
            <div className="mt-2 grid gap-3 border-y border-line py-3 text-xs">
              {coverAsset && (
                <div>
                  <h4 className="font-medium text-fog">公开封面</h4>
                  <AssetMedia
                    asset={coverAsset}
                    className="mt-2 aspect-video max-h-64 w-full border border-line bg-black/20 object-contain"
                    fallbackClassName="mt-2 flex min-h-24 items-center justify-center border border-line bg-black/20 px-3 text-fog"
                  />
                  <p className="mt-1 break-all text-[11px] text-fog">{recipe.cover_asset_url}</p>
                </div>
              )}
              {summary.negative && (
                <div>
                  <h4 className="font-medium text-fog">负向约束</h4>
                  <p className="mt-1 whitespace-pre-wrap break-words leading-5 text-mist">{summary.negative}</p>
                </div>
              )}
              {Object.keys(summary.structured).length > 0 && (
                <div>
                  <h4 className="font-medium text-fog">结构化参数</h4>
                  <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words border border-line bg-black/15 p-3 text-[11px] leading-5 text-mist">
                    {JSON.stringify(summary.structured, null, 2)}
                  </pre>
                </div>
              )}
              <div>
                <h4 className="font-medium text-fog">完整待公开配方数据</h4>
                <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words border border-line bg-black/15 p-3 text-[11px] leading-5 text-mist">
                  {JSON.stringify(summary.payload, null, 2)}
                </pre>
              </div>
            </div>
          </details>

          {recipe.review_note && !pending && (
            <p className={`mt-2 text-xs ${recipe.moderation_status === "rejected" ? "text-bad" : "text-fog"}`}>
              审核意见：{recipe.review_note}
            </p>
          )}

          {pending && (
            <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              <label className="grid gap-1 text-xs text-fog">
                <span>审核意见（驳回必填）</span>
                <textarea
                  className="input min-h-20 resize-y py-2 text-xs"
                  value={note}
                  maxLength={500}
                  placeholder="说明风险点或修改要求"
                  onChange={(event) => setNote(event.target.value)}
                  disabled={Boolean(busy)}
                />
              </label>
              <div className="grid grid-cols-2 gap-2 sm:flex">
                <button type="button" className="btn-secondary btn-sm min-h-10 text-bad" onClick={() => decide("reject")} disabled={Boolean(busy)}>
                  <X size={14} aria-hidden="true" /> 驳回
                </button>
                <button type="button" className="btn-primary btn-sm min-h-10" onClick={() => decide("approve")} disabled={Boolean(busy)}>
                  <Check size={14} aria-hidden="true" /> 通过
                </button>
              </div>
            </div>
          )}
          {localError && <p className="mt-2 text-xs text-bad" role="alert">{localError}</p>}
        </div>
      </div>
    </article>
  );
}

export function RecipeReviews() {
  const [status, setStatus] = useState("pending");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState({ type: "", text: "" });
  const latestLoadRef = useRef(0);

  async function load(nextStatus = status) {
    const requestId = ++latestLoadRef.current;
    setLoading(true);
    setMessage({ type: "", text: "" });
    try {
      const nextRows = await api.adminRecipeReviews({ status: nextStatus, limit: 100 });
      if (requestId !== latestLoadRef.current) return;
      setRows(nextRows);
    } catch (error) {
      if (requestId !== latestLoadRef.current) return;
      setMessage({ type: "bad", text: error.message || "配方审核队列加载失败" });
    } finally {
      if (requestId === latestLoadRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    load(status);
    return () => { latestLoadRef.current += 1; };
  }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  async function review(recipe, action, note) {
    const actionKey = `${action}-${recipe.id}`;
    setBusy(actionKey);
    setMessage({ type: "", text: "" });
    try {
      const updated = await api.adminReviewRecipe(recipe.id, { action, note });
      setRows((current) => status === "pending"
        ? current.filter((item) => item.id !== recipe.id)
        : current.map((item) => item.id === recipe.id ? updated : item));
      setMessage({ type: "ok", text: action === "approve" ? "配方已审核通过" : "配方已驳回" });
    } catch (error) {
      setMessage({ type: "bad", text: error.message || "配方审核失败" });
    } finally {
      setBusy("");
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-bold text-snow"><ShieldCheck size={19} aria-hidden="true" />创作配方审核</h2>
          <p className="mt-1 text-xs text-fog">核对公开配方的封面、提示词、引用数据和完整参数后再决定是否发布。</p>
        </div>
        <button type="button" className="icon-btn h-10 w-10" aria-label="刷新配方审核队列" onClick={() => load()} disabled={loading}>
          <RefreshCw size={16} className={loading ? "animate-spin" : ""} aria-hidden="true" />
        </button>
      </div>

      <div className="my-4 flex flex-wrap gap-1 border-y border-line py-3" role="group" aria-label="筛选配方审核状态">
        {FILTERS.map(([value, label]) => (
          <button key={value} type="button" className={`chip min-h-10 px-3 ${status === value ? "chip-active" : ""}`} aria-pressed={status === value} onClick={() => setStatus(value)}>
            {label}
          </button>
        ))}
      </div>

      {message.text && (
        <p className={`mb-3 flex items-center gap-1.5 text-xs ${message.type === "bad" ? "text-bad" : "text-ok"}`} role={message.type === "bad" ? "alert" : "status"}>
          {message.type === "bad" ? <AlertTriangle size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
          {message.text}
        </p>
      )}

      {loading && rows.length === 0 ? (
        <div className="space-y-3" aria-busy="true" aria-label="正在加载配方审核队列">
          {[0, 1, 2].map((key) => <div key={key} className="skeleton h-36" />)}
        </div>
      ) : rows.length > 0 ? (
        <div>
          {rows.map((recipe) => (
            <RecipeReviewRow key={recipe.id} recipe={recipe} busy={busy === `approve-${recipe.id}` || busy === `reject-${recipe.id}`} onDecision={review} />
          ))}
        </div>
      ) : (
        <div className="border border-dashed border-line py-10 text-center" role="status">
          <FileText className="mx-auto text-fog" size={28} aria-hidden="true" />
          <p className="mt-2 text-sm text-mist">当前筛选下没有创作配方</p>
        </div>
      )}
    </Card>
  );
}
