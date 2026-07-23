"use client";

import {
  Check,
  CircleCheck,
  CircleX,
  Clock3,
  Columns3,
  Crosshair,
  Eye,
  FileText,
  LockKeyhole,
  Merge,
  Package,
  PencilLine,
  Plus,
  Rows3,
  Save,
  ScanText,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { safeAssetMediaSrc } from "../../components/AssetMedia";
import { authenticatedObjectUrl } from "../../lib/api";
import { reportBackgroundError } from "../../lib/errorHandling";
import {
  imageEvidenceConflictGroups,
  imageEvidenceMaskReadiness,
  imageEvidenceSources,
  normalizeImageEvidence,
} from "./imageEvidence";
import {
  addManualImageEvidenceItem,
  bboxFromNormalizedDrag,
  mergeImageEvidenceItems,
  removeImageEvidenceItem,
  reviewImageEvidenceItem,
  setImageEvidenceRegionMode,
  splitImageEvidenceItem,
  updateImageEvidenceItem,
} from "./imageEvidenceReview";

const TYPE_META = {
  visual_field: [Eye, "画面事实"],
  ocr: [ScanText, "OCR 文字"],
  logo: [FileText, "Logo"],
  packaging: [Package, "包装"],
  subject_protection: [LockKeyhole, "主体保护"],
};

const REVIEW_META = {
  pending: [Clock3, "待确认", "border-warn/35 bg-warn/10 text-warn"],
  confirmed: [CircleCheck, "已确认", "border-ok/35 bg-ok/10 text-ok"],
  rejected: [CircleX, "已驳回", "border-bad/35 bg-bad/10 text-bad"],
};

const ANALYSIS_META = {
  ready: ["已完成", "border-aqua/30 bg-aqua/10 text-aqua"],
  pending: ["分析中", "border-line bg-white/[0.035] text-fog"],
  unsupported: ["未支持", "border-line bg-white/[0.035] text-fog"],
  degraded: ["已降级", "border-warn/35 bg-warn/10 text-warn"],
};

const MASK_STATUS_CLASS = {
  ready: "border-ok/35 bg-ok/10 text-ok",
  pending: "border-line bg-white/[0.035] text-fog",
  unsupported: "border-line bg-white/[0.035] text-fog",
  degraded: "border-warn/35 bg-warn/10 text-warn",
  source_expired: "border-bad/35 bg-bad/10 text-bad",
  source_hash_changed: "border-bad/35 bg-bad/10 text-bad",
};

function analyzerLabel(item) {
  if (item.analyzer) return item.analyzer;
  if (item.evidence_type === "ocr") return "OCR";
  if (item.evidence_type === "subject_protection") return "分割/检测";
  return "VLM";
}

function regionClass(item, active) {
  if (item.protected) return active ? "border-warn bg-warn/20" : "border-warn/80 bg-warn/10";
  if (item.editable) return active ? "border-aqua bg-aqua/20" : "border-aqua/75 bg-aqua/10";
  return active ? "border-iris bg-iris/20" : "border-iris/70 bg-iris/10";
}

function polygonPalette(item, active) {
  if (item.protected) return { fill: active ? "rgba(255, 193, 92, .24)" : "rgba(255, 193, 92, .12)", stroke: "#ffc15c" };
  if (item.editable) return { fill: active ? "rgba(74, 222, 204, .24)" : "rgba(74, 222, 204, .12)", stroke: "#4adecc" };
  return { fill: active ? "rgba(139, 124, 246, .24)" : "rgba(139, 124, 246, .12)", stroke: "#8b7cf6" };
}

function isProtectedUpload(src) {
  return String(src || "").includes("/api/uploads/");
}

function useEvidenceImageSrc(rawSrc) {
  const [state, setState] = useState({ src: "", loading: false, error: "" });
  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    const safeSrc = safeAssetMediaSrc(rawSrc);
    if (!safeSrc) {
      setState({ src: "", loading: false, error: rawSrc ? "素材地址不在允许范围内" : "原图已过期" });
      return undefined;
    }
    if (!isProtectedUpload(safeSrc)) {
      setState({ src: safeSrc, loading: false, error: "" });
      return undefined;
    }
    setState({ src: "", loading: true, error: "" });
    authenticatedObjectUrl(safeSrc)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setState({ src: url, loading: false, error: "" });
      })
      .catch((error) => {
        reportBackgroundError(error, "load reverse image evidence source");
        if (!cancelled) setState({ src: "", loading: false, error: "原图加载失败" });
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rawSrc]);
  return state;
}

function percentValue(value) {
  return value == null ? "" : String(Math.round(Number(value) * 10000) / 100);
}

function normalizedPointer(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  if (!(rect.width > 0) || !(rect.height > 0)) return null;
  return {
    x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
  };
}

export default function StudioImageEvidence({
  result,
  operation,
  selectedField = "",
  onSelectField,
  onChange,
}) {
  const externalEvidence = useMemo(() => normalizeImageEvidence(result?.image_evidence), [result?.image_evidence]);
  const externalIdentity = JSON.stringify(externalEvidence);
  const [draftEvidence, setDraftEvidence] = useState(externalEvidence);
  const editable = typeof onChange === "function";
  const evidence = editable ? draftEvidence : externalEvidence;
  const evidenceIdentity = JSON.stringify(evidence);
  const sources = useMemo(() => imageEvidenceSources(operation), [operation]);
  const conflictGroups = useMemo(() => imageEvidenceConflictGroups(evidence), [evidenceIdentity]);
  const maskReadiness = imageEvidenceMaskReadiness(result, operation);
  const conflictingIds = useMemo(
    () => new Set(conflictGroups.flatMap((group) => group.evidence_ids)),
    [conflictGroups],
  );
  const operationIdentity = String(operation?.id || operation?.operation_id || operation?.created_at || "");
  const [sourceIndex, setSourceIndex] = useState(evidence[0]?.source_index || 1);
  const [activeEvidenceId, setActiveEvidenceId] = useState(evidence[0]?.evidence_id || "");
  const [selectedIds, setSelectedIds] = useState([]);
  const [fieldKeyDraft, setFieldKeyDraft] = useState("");
  const [textDraft, setTextDraft] = useState("");
  const [bboxDraft, setBboxDraft] = useState({ x: "", y: "", width: "", height: "" });
  const [drawMode, setDrawMode] = useState(false);
  const [drawState, setDrawState] = useState(null);
  const [editError, setEditError] = useState("");

  const activeItem = evidence.find((item) => item.evidence_id === activeEvidenceId) || evidence[0];
  const activeIndex = activeItem ? evidence.indexOf(activeItem) : -1;
  const source = sources[sourceIndex - 1];
  const imageState = useEvidenceImageSrc(source?.asset_url || "");

  useEffect(() => {
    setDraftEvidence(externalEvidence);
  }, [externalIdentity]);

  useEffect(() => {
    const first = evidence[0];
    setActiveEvidenceId((current) => evidence.some((item) => item.evidence_id === current)
      ? current
      : first?.evidence_id || "");
    setSelectedIds((current) => current.filter((id) => evidence.some((item) => item.evidence_id === id)));
    if (!evidence.some((item) => item.source_index === sourceIndex)) setSourceIndex(first?.source_index || 1);
  }, [operationIdentity, evidenceIdentity]);

  useEffect(() => {
    if (!selectedField) return;
    const item = evidence.find((candidate) => candidate.field_key === selectedField);
    if (!item) return;
    setActiveEvidenceId(item.evidence_id);
    setSourceIndex(item.source_index);
  }, [selectedField, evidenceIdentity]);

  useEffect(() => {
    setFieldKeyDraft(activeItem?.field_key || "");
    setTextDraft(activeItem?.evidence_text || "");
    setBboxDraft({
      x: percentValue(activeItem?.bbox?.x),
      y: percentValue(activeItem?.bbox?.y),
      width: percentValue(activeItem?.bbox?.width),
      height: percentValue(activeItem?.bbox?.height),
    });
    setDrawMode(false);
    setDrawState(null);
    setEditError("");
  }, [activeItem?.evidence_id, activeItem?.field_key, activeItem?.evidence_text, JSON.stringify(activeItem?.bbox)]);

  function addManualEvidence() {
    try {
      const nextEvidence = addManualImageEvidenceItem(evidence, { sourceIndex });
      const created = nextEvidence[nextEvidence.length - 1];
      setDraftEvidence(nextEvidence);
      onChange(nextEvidence);
      setActiveEvidenceId(created.evidence_id);
      setSelectedIds([created.evidence_id]);
      onSelectField?.(created.field_key);
      setEditError("");
    } catch (error) {
      setEditError(error instanceof Error ? error.message : "人工证据创建失败。");
    }
  }

  if (!evidence.length) {
    const rawStatus = String(result?.image_evidence_status || result?.image_evidence_capability?.status || "unsupported");
    const status = ["pending", "unsupported", "degraded"].includes(rawStatus) ? rawStatus : "unsupported";
    const [label, className] = ANALYSIS_META[status];
    const reason = String(
      result?.image_evidence_reason
      || result?.image_evidence_capability?.reason
      || (status === "pending" ? "图片证据仍在分析，暂不可审阅。" : status === "degraded" ? "本次分析未返回可验证区域。" : "当前分析器未提供图片区域证据。"),
    );
    return (
      <section aria-labelledby="image-evidence-title-empty">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p id="image-evidence-title-empty" className="text-xs font-display font-medium text-mist">图片区域证据</p>
          {editable && status !== "pending" && (
            <button
              type="button"
              className="btn-secondary btn-sm min-h-10"
              disabled={!sources.length}
              title={sources.length ? "新增人工区域证据" : "源素材不可用"}
              onClick={addManualEvidence}
            >
              <Plus size={14} aria-hidden="true" /> 人工标注
            </button>
          )}
        </div>
        {editable && sources.length > 1 && status !== "pending" && (
          <div className="mt-2 flex flex-wrap gap-1" role="tablist" aria-label="选择人工证据来源图">
            {sources.map((item, index) => (
              <button
                key={`${item.asset_url}-${index}`}
                type="button"
                role="tab"
                aria-selected={sourceIndex === index + 1}
                className={`chip min-h-9 ${sourceIndex === index + 1 ? "chip-active" : ""}`}
                onClick={() => setSourceIndex(index + 1)}
              >
                {item.label || `参考图 ${index + 1}`}
              </button>
            ))}
          </div>
        )}
        <div className={`mt-2 min-h-12 border px-3 py-2 text-xs ${className}`} role="status">
          <span className="font-display font-medium">{label}</span> · {reason}
        </div>
        {editError && (
          <p className="mt-2 rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">{editError}</p>
        )}
      </section>
    );
  }

  const sourceEvidence = evidence.filter((item) => item.source_index === sourceIndex);
  const previewBox = drawState ? bboxFromNormalizedDrag(drawState.start, drawState.current, 0) : null;

  function selectEvidence(item) {
    setSourceIndex(item.source_index);
    setActiveEvidenceId(item.evidence_id);
    onSelectField?.(item.field_key);
  }

  function selectSource(nextSourceIndex) {
    setSourceIndex(nextSourceIndex);
    const nextItem = evidence.find((item) => item.source_index === nextSourceIndex);
    if (!nextItem) return;
    setActiveEvidenceId(nextItem.evidence_id);
    onSelectField?.(nextItem.field_key);
  }

  function commitEvidence(nextEvidence, preferredId = activeItem?.evidence_id || "") {
    const normalized = normalizeImageEvidence(nextEvidence);
    if (normalized.length !== nextEvidence.length) throw new Error("证据修改结果无效，请检查文字和区域坐标。");
    setDraftEvidence(normalized);
    onChange(normalized);
    if (preferredId && normalized.some((item) => item.evidence_id === preferredId)) {
      setActiveEvidenceId(preferredId);
    } else {
      setActiveEvidenceId(normalized[0]?.evidence_id || "");
      setSourceIndex(normalized[0]?.source_index || 1);
    }
    setSelectedIds((current) => current.filter((id) => normalized.some((item) => item.evidence_id === id)));
    setEditError("");
  }

  function runEdit(change, preferredId) {
    try {
      commitEvidence(change(), preferredId);
    } catch (error) {
      setEditError(error instanceof Error ? error.message : "证据修改失败。");
    }
  }

  function commitText() {
    if (!activeItem || textDraft.trim() === activeItem.evidence_text) return;
    runEdit(
      () => updateImageEvidenceItem(evidence, activeItem.evidence_id, { evidence_text: textDraft.trim() }),
      activeItem.evidence_id,
    );
  }

  function commitBbox() {
    if (!activeItem) return;
    const entries = Object.entries(bboxDraft);
    if (entries.some(([, value]) => !String(value).trim() || !Number.isFinite(Number(value)))) {
      setEditError("请完整填写 X、Y、宽度和高度百分比。");
      return;
    }
    const bbox = Object.fromEntries(entries.map(([key, value]) => [key, Number(value) / 100]));
    runEdit(
      () => updateImageEvidenceItem(evidence, activeItem.evidence_id, { bbox, polygon: undefined }),
      activeItem.evidence_id,
    );
  }

  function finishDraw(event) {
    if (!drawState || !activeItem) return;
    const end = normalizedPointer(event) || drawState.current;
    const bbox = bboxFromNormalizedDrag(drawState.start, end);
    setDrawState(null);
    setDrawMode(false);
    if (!bbox) {
      setEditError("重画区域过小，请拖出一个更大的矩形。");
      return;
    }
    runEdit(
      () => updateImageEvidenceItem(evidence, activeItem.evidence_id, { bbox, polygon: undefined }),
      activeItem.evidence_id,
    );
  }

  function mergeSelected() {
    const first = evidence.find((item) => selectedIds.includes(item.evidence_id));
    runEdit(() => mergeImageEvidenceItems(evidence, selectedIds), first?.evidence_id);
    setSelectedIds(first ? [first.evidence_id] : []);
  }

  return (
    <section className="space-y-3" aria-labelledby="image-evidence-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p id="image-evidence-title" className="text-xs font-display font-medium text-mist">图片区域证据</p>
          <p className="mt-0.5 text-[11px] text-fog">
            {editable ? "审阅可见事实后，确认的保护区与编辑区才能进入服务端蒙版；不会自动应用" : "仅高亮供应商返回且通过校验的可见区域"}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1">
          {editable && (
            <button
              type="button"
              className="icon-btn h-9 w-9"
              disabled={!sources.length}
              aria-label="新增人工证据"
              title="新增人工区域证据"
              onClick={addManualEvidence}
            >
              <Plus size={15} aria-hidden="true" />
            </button>
          )}
          {sources.length > 1 && (
            <div className="flex flex-wrap gap-1" role="tablist" aria-label="参考图证据">
              {sources.map((item, index) => (
                <button
                  key={`${item.asset_url}-${index}`}
                  type="button"
                  role="tab"
                  aria-selected={sourceIndex === index + 1}
                  className={`chip min-h-9 ${sourceIndex === index + 1 ? "chip-active" : ""}`}
                  onClick={() => selectSource(index + 1)}
                >
                  {item.label || `参考图 ${index + 1}`}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className={`flex min-h-10 items-start gap-2 border px-2.5 py-2 text-[11px] ${MASK_STATUS_CLASS[maskReadiness.status]}`} role="status">
        {!["ready", "pending", "unsupported"].includes(maskReadiness.status) && (
          <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
        )}
        <span><strong className="font-display font-medium">{maskReadiness.label}</strong> · {maskReadiness.reason}</span>
      </div>

      {conflictGroups.length > 0 && (
        <div className="border-l-2 border-warn/70 bg-warn/[0.06] px-3 py-2" role="alert">
          <p className="text-[11px] font-display font-medium text-warn">检测到 {conflictGroups.length} 组分析冲突</p>
          <ul className="mt-1 space-y-1 text-[10px] text-fog">
            {conflictGroups.map((group) => (
              <li key={group.id}>
                参考图 {group.source_index} · {group.field_key} · {group.analyzers.join(" / ")}，需人工确认后再用于蒙版。
              </li>
            ))}
          </ul>
        </div>
      )}

      {imageState.loading ? (
        <div className="rounded-lg border border-line px-3 py-10 text-center text-xs text-fog" role="status">
          正在加载受保护原图…
        </div>
      ) : imageState.src ? (
        <div className="overflow-auto rounded-lg border border-line bg-black/35 p-2 text-center">
          <div className="relative inline-block max-w-full align-top">
            {/* The shrink-wrapped wrapper keeps normalized boxes aligned when the image is letterboxed. */}
            <img src={imageState.src} alt="反推证据来源" className="block max-h-96 max-w-full" />
            {sourceEvidence.map((item) => {
              const evidenceIndex = evidence.indexOf(item);
              const active = evidenceIndex === activeIndex || item.field_key === selectedField;
              if (item.bbox) {
                return (
                  <button
                    key={item.evidence_id}
                    type="button"
                    className={`absolute border-2 transition ${regionClass(item, active)} ${active ? "z-20" : "z-10"} ${drawMode ? "pointer-events-none" : ""}`}
                    style={{
                      left: `${item.bbox.x * 100}%`,
                      top: `${item.bbox.y * 100}%`,
                      width: `${item.bbox.width * 100}%`,
                      height: `${item.bbox.height * 100}%`,
                    }}
                    aria-label={`查看 ${item.field_key} 区域证据`}
                    onClick={() => selectEvidence(item)}
                  >
                    <span className="absolute left-0 top-0 max-w-full -translate-y-full truncate bg-black/80 px-1 py-0.5 text-[9px] text-white">
                      {item.field_key}
                    </span>
                  </button>
                );
              }
              if (!item.polygon?.length) return null;
              const palette = polygonPalette(item, active);
              const clipPath = `polygon(${item.polygon.map((point) => `${point.x * 100}% ${point.y * 100}%`).join(",")})`;
              return (
                <span key={item.evidence_id} className={`absolute inset-0 ${active ? "z-20" : "z-10"}`}>
                  <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                    <polygon
                      points={item.polygon.map((point) => `${point.x * 100},${point.y * 100}`).join(" ")}
                      fill={palette.fill}
                      stroke={palette.stroke}
                      strokeWidth={active ? 1.2 : 0.8}
                      vectorEffect="non-scaling-stroke"
                    />
                  </svg>
                  <button
                    type="button"
                    className={`absolute inset-0 ${drawMode ? "pointer-events-none" : ""}`}
                    style={{ clipPath }}
                    aria-label={`查看 ${item.field_key} 多边形区域证据`}
                    onClick={() => selectEvidence(item)}
                  />
                </span>
              );
            })}
            {previewBox && (
              <span
                className="pointer-events-none absolute z-40 border-2 border-dashed border-snow bg-white/10"
                style={{
                  left: `${previewBox.x * 100}%`,
                  top: `${previewBox.y * 100}%`,
                  width: `${previewBox.width * 100}%`,
                  height: `${previewBox.height * 100}%`,
                }}
              />
            )}
            {editable && drawMode && activeItem?.source_index === sourceIndex && (
              <div
                className="absolute inset-0 z-30 cursor-crosshair touch-none bg-white/[0.015]"
                role="application"
                aria-label={`拖拽重画 ${activeItem.field_key} 区域`}
                onPointerDown={(event) => {
                  const point = normalizedPointer(event);
                  if (!point) return;
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setDrawState({ start: point, current: point });
                }}
                onPointerMove={(event) => {
                  if (!drawState) return;
                  const point = normalizedPointer(event);
                  if (point) setDrawState((current) => current ? { ...current, current: point } : current);
                }}
                onPointerUp={finishDraw}
                onPointerCancel={() => {
                  setDrawState(null);
                  setDrawMode(false);
                }}
              />
            )}
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-dashed border-line px-3 py-6 text-center text-xs text-fog">
          {imageState.error || "原图已过期或当前不可预览"}，区域坐标仍保留在证据列表中。
        </div>
      )}

      {editable && (
        <div className="flex min-h-9 flex-wrap items-center gap-2 rounded-lg border border-line bg-white/[0.025] px-2.5 py-2">
          <span className="text-[11px] text-fog">已选 {selectedIds.length} 条</span>
          <button type="button" className="chip min-h-8 px-2.5" disabled={selectedIds.length < 2} onClick={mergeSelected}>
            <Merge size={13} aria-hidden="true" /> 合并
          </button>
          {selectedIds.length > 0 && (
            <button type="button" className="icon-btn h-8 w-8" aria-label="清除证据选择" title="清除选择" onClick={() => setSelectedIds([])}>
              <X size={14} aria-hidden="true" />
            </button>
          )}
        </div>
      )}

      <ul className="grid gap-2 sm:grid-cols-2" aria-label="图片证据列表">
        {evidence.map((item, index) => {
          const [Icon, label] = TYPE_META[item.evidence_type] || [Crosshair, item.evidence_type];
          const [ReviewIcon, reviewLabel, reviewClass] = REVIEW_META[item.review_status];
          const [analysisLabel, analysisClass] = ANALYSIS_META[item.analysis_status] || ANALYSIS_META.degraded;
          const active = index === activeIndex || item.field_key === selectedField;
          return (
            <li key={item.evidence_id} className="relative">
              {editable && (
                <label className="absolute left-2.5 top-2.5 z-10 flex h-5 w-5 cursor-pointer items-center justify-center" title="选择证据">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--aqua)]"
                    checked={selectedIds.includes(item.evidence_id)}
                    aria-label={`选择 ${item.field_key} 证据`}
                    onChange={(event) => setSelectedIds((current) => event.target.checked
                      ? [...new Set([...current, item.evidence_id])]
                      : current.filter((id) => id !== item.evidence_id))}
                  />
                </label>
              )}
              <button
                type="button"
                className={`h-full w-full rounded-lg border py-2 pr-3 text-left transition ${editable ? "pl-9" : "px-3"} ${
                  active ? "border-aqua/60 bg-aqua/10" : "border-line bg-white/[0.025] hover:bg-white/[0.05]"
                }`}
                onClick={() => selectEvidence(item)}
              >
                <span className="flex items-start justify-between gap-2">
                  <span className="flex min-w-0 items-center gap-1.5 text-[11px] font-display font-medium text-mist">
                    <Icon size={13} className="shrink-0" aria-hidden="true" />
                    <span className="truncate">{label} · {item.field_key}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    {conflictingIds.has(item.evidence_id) && <span className="badge border border-warn/35 bg-warn/10 text-warn">冲突</span>}
                    <span className={`badge gap-1 border ${reviewClass}`}><ReviewIcon size={10} aria-hidden="true" /> {reviewLabel}</span>
                    <span className="text-[10px] text-fog">{Math.round(item.confidence * 100)}%</span>
                  </span>
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-snow">{item.evidence_text}</span>
                <span className="mt-1 flex flex-wrap gap-1 text-[10px] text-fog">
                  <span className={`border px-1.5 py-0.5 ${analysisClass}`}>{analyzerLabel(item)} · {analysisLabel}</span>
                  <span>{item.evidence_source || `参考图 ${item.source_index}`}</span>
                  <span>{item.fact_status === "visible" ? "可见事实" : item.fact_status === "inferred" ? "模型推断" : "未知"}</span>
                  {item.protected && <span className="text-warn">必须保留</span>}
                  {item.editable && <span className="text-aqua">允许修改</span>}
                  {!item.protected && !item.editable && <span>无区域约束</span>}
                </span>
                {item.degraded_reason && <span className="mt-1 block text-[10px] text-warn">{item.degraded_reason}</span>}
              </button>
            </li>
          );
        })}
      </ul>

      {editable && activeItem && (
        <div className="space-y-3 rounded-lg border border-line bg-white/[0.025] p-3" aria-label={`编辑 ${activeItem.field_key} 证据`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-display text-xs font-medium text-mist">编辑证据 · {activeItem.field_key}</p>
            <div className="flex items-center gap-1">
              <button
                type="button"
                className="icon-btn h-9 w-9"
                disabled={!imageState.src || activeItem.source_index !== sourceIndex}
                aria-label={drawMode ? "取消重画区域" : "重画证据区域"}
                title={drawMode ? "取消重画" : "在原图拖拽重画"}
                onClick={() => {
                  setDrawMode((current) => !current);
                  setDrawState(null);
                  setEditError("");
                }}
              >
                {drawMode ? <X size={15} aria-hidden="true" /> : <PencilLine size={15} aria-hidden="true" />}
              </button>
              <button
                type="button"
                className="icon-btn h-9 w-9 text-fog hover:text-bad"
                aria-label="删除当前证据"
                title="删除证据"
                onClick={() => runEdit(() => removeImageEvidenceItem(evidence, activeItem.evidence_id))}
              >
                <Trash2 size={15} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div>
            <p className="label mb-1">证据分类</p>
            <div className="grid gap-2 sm:grid-cols-[12rem_minmax(0,1fr)_2.5rem]">
              <label>
                <span className="sr-only">证据类型</span>
                <select
                  className="select min-h-10 py-2 text-xs"
                  value={activeItem.evidence_type}
                  onChange={(event) => runEdit(
                    () => updateImageEvidenceItem(evidence, activeItem.evidence_id, {
                      evidence_type: event.target.value,
                    }),
                    activeItem.evidence_id,
                  )}
                >
                  {Object.entries(TYPE_META).map(([value, [, label]]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              <label>
                <span className="sr-only">关联字段</span>
                <input
                  type="text"
                  className="input min-h-10 px-2.5 py-2 text-xs"
                  maxLength={64}
                  value={fieldKeyDraft}
                  placeholder="关联字段"
                  onChange={(event) => setFieldKeyDraft(event.target.value)}
                />
              </label>
              <button
                type="button"
                className="icon-btn h-10 w-10 text-ok"
                disabled={!fieldKeyDraft.trim() || fieldKeyDraft.trim() === activeItem.field_key}
                aria-label="保存关联字段"
                title="保存字段"
                onClick={() => runEdit(
                  () => updateImageEvidenceItem(evidence, activeItem.evidence_id, {
                    field_key: fieldKeyDraft.trim(),
                  }),
                  activeItem.evidence_id,
                )}
              >
                <Save size={15} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div>
            <label className="label mb-1" htmlFor={`evidence-text-${activeItem.evidence_id}`}>证据文字</label>
            <div className="flex items-start gap-2">
              <textarea
                id={`evidence-text-${activeItem.evidence_id}`}
                className="textarea min-h-20 resize-y px-2.5 py-2 text-xs"
                maxLength={2000}
                value={textDraft}
                onChange={(event) => setTextDraft(event.target.value)}
              />
              <button
                type="button"
                className="icon-btn h-10 w-10 shrink-0 text-ok"
                disabled={!textDraft.trim() || textDraft.trim() === activeItem.evidence_text}
                aria-label="保存证据文字"
                title="保存文字"
                onClick={commitText}
              >
                <Save size={15} aria-hidden="true" />
              </button>
            </div>
          </div>

          <div>
            <p className="label mb-1">矩形区域（%）</p>
            <div className="grid grid-cols-[repeat(4,minmax(0,1fr))_2.5rem] gap-2">
              {[['x', 'X'], ['y', 'Y'], ['width', '宽'], ['height', '高']].map(([key, label]) => (
                <label key={key} className="min-w-0 text-[10px] text-fog">
                  <span className="mb-1 block">{label}</span>
                  <input
                    type="number"
                    className="input min-w-0 px-2 py-1.5 text-xs"
                    min="0"
                    max="100"
                    step="0.1"
                    value={bboxDraft[key]}
                    placeholder="-"
                    onChange={(event) => setBboxDraft((current) => ({ ...current, [key]: event.target.value }))}
                  />
                </label>
              ))}
              <button
                type="button"
                className="icon-btn mt-5 h-9 w-9 text-ok"
                aria-label="保存矩形区域"
                title="保存区域"
                onClick={commitBbox}
              >
                <Save size={15} aria-hidden="true" />
              </button>
            </div>
            {activeItem.polygon?.length > 0 && !activeItem.bbox && (
              <p className="mt-1 text-[10px] text-fog">当前为多边形区域；保存数值或拖拽重画后将转换为矩形。</p>
            )}
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <div>
              <p className="label mb-1">审阅状态</p>
              <div className="flex flex-wrap gap-1" role="group" aria-label="证据审阅状态">
                {[
                  ["pending", Clock3, "待确认"],
                  ["confirmed", Check, "确认"],
                  ["rejected", X, "驳回"],
                ].map(([status, Icon, label]) => (
                  <button
                    key={status}
                    type="button"
                    className={`chip min-h-9 ${activeItem.review_status === status ? "chip-active" : ""}`}
                    aria-pressed={activeItem.review_status === status}
                    onClick={() => runEdit(
                      () => reviewImageEvidenceItem(evidence, activeItem.evidence_id, status),
                      activeItem.evidence_id,
                    )}
                  >
                    <Icon size={13} aria-hidden="true" /> {label}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <p className="label mb-1">区域用途</p>
              <div className="flex flex-wrap gap-1" role="group" aria-label="证据区域用途">
                {[
                  ["protected", LockKeyhole, "必须保留"],
                  ["editable", PencilLine, "允许修改"],
                  ["none", X, "无约束"],
                ].map(([mode, Icon, label]) => (
                  <button
                    key={mode}
                    type="button"
                    className={`chip min-h-9 ${
                      (mode === "protected" && activeItem.protected)
                      || (mode === "editable" && activeItem.editable)
                      || (mode === "none" && !activeItem.protected && !activeItem.editable)
                        ? "chip-active" : ""
                    }`}
                    disabled={mode !== "none" && (activeItem.fact_status !== "visible" || (!activeItem.bbox && !activeItem.polygon?.length))}
                    onClick={() => runEdit(
                      () => setImageEvidenceRegionMode(evidence, activeItem.evidence_id, mode),
                      activeItem.evidence_id,
                    )}
                  >
                    <Icon size={13} aria-hidden="true" /> {label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div>
            <p className="label mb-1">区域拆分</p>
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                className="chip min-h-9"
                disabled={!activeItem.bbox && !activeItem.polygon?.length}
                onClick={() => runEdit(
                  () => splitImageEvidenceItem(evidence, activeItem.evidence_id, "vertical"),
                  `${activeItem.evidence_id}-a`,
                )}
              >
                <Columns3 size={13} aria-hidden="true" /> 左右拆分
              </button>
              <button
                type="button"
                className="chip min-h-9"
                disabled={!activeItem.bbox && !activeItem.polygon?.length}
                onClick={() => runEdit(
                  () => splitImageEvidenceItem(evidence, activeItem.evidence_id, "horizontal"),
                  `${activeItem.evidence_id}-a`,
                )}
              >
                <Rows3 size={13} aria-hidden="true" /> 上下拆分
              </button>
            </div>
          </div>

          {editError && (
            <p className="rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">{editError}</p>
          )}
        </div>
      )}

      {activeItem?.fact_status !== "visible" && (
        <p className="rounded-lg border border-warn/25 bg-warn/10 px-3 py-2 text-xs text-warn">
          当前条目是{activeItem.fact_status === "unknown" ? "未知结论" : "模型推断"}，不作为可锁定的图片区域。
        </p>
      )}
    </section>
  );
}
