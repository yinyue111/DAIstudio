"use client";

import { useEffect, useState } from "react";
import { RefreshCw, Save, ThumbsDown, ThumbsUp, Undo2, X } from "lucide-react";
import {
  ReverseDraftView,
  ReverseEvidenceView,
  ReverseStoryboardView,
  ReverseStructureView,
  ReverseVersionsView,
} from "./StudioReverseResultViews";
import { normalizeImageEvidence } from "./imageEvidence";
import { normalizePendingReverseResult } from "./reverseResultApplication";

const BASE_TABS = [
  ["draft", "生成稿"],
  ["structure", "结构参数"],
  ["evidence", "分析证据"],
  ["versions", "版本"],
];

const FEEDBACK_ISSUES = [
  ["subject_error", "主体识别错误"],
  ["style_error", "风格判断错误"],
  ["action_missing", "动作缺失"],
  ["shot_missing", "分镜缺失"],
  ["camera_error", "运镜错误"],
  ["text_error", "文字识别错误"],
  ["audio_error", "音频分析错误"],
  ["hallucination", "存在臆测"],
  ["other", "其他"],
];

function resultPayload(pending) {
  if (!pending || typeof pending !== "object") return null;
  if (pending.payload && typeof pending.payload === "object") return pending.payload;
  if (pending.result && typeof pending.result === "object") return pending.result;
  return pending;
}

const PARAMETER_FIELDS = [
  ["ratio", "画幅"],
  ["vDuration", "时长"],
  ["vResolution", "清晰度"],
];

function applicationFields(normalized, isVideo) {
  const structuredKeys = Object.keys(normalized.structured).filter(
    (key) => !["final_text", "shots", "负向", "negative"].includes(key),
  );
  const fields = [];
  if (normalized.prompt) fields.push({ token: "prompt", label: "生成稿", group: "base" });
  if (normalized.negative) fields.push({ token: "negative", label: "负向词", group: "base" });
  for (const key of structuredKeys) {
    fields.push({ token: `structured:${key}`, label: key, group: "structured", key });
  }
  for (const [key, label] of PARAMETER_FIELDS) {
    if (normalized.parameters[key] != null) {
      fields.push({ token: `parameter:${key}`, label, group: "parameter", key });
    }
  }
  if (!isVideo && normalized.imageEvidence.length) {
    const confirmedCount = normalized.imageEvidence.filter(
      (item) => item.review_status === "confirmed",
    ).length;
    fields.push({
      token: "imageEvidence",
      label: `已确认图片证据 ${confirmedCount}/${normalized.imageEvidence.length}`,
      group: "evidence",
      disabled: confirmedCount === 0,
    });
  }
  if (isVideo && normalized.videoAnalysis) {
    fields.push({ token: "videoAnalysis", label: "视频证据", group: "base" });
  }
  return fields;
}

export default function StudioReverseResultPanel({
  pending,
  operation,
  activeTab = "draft",
  revisions = [],
  appliedVersion = null,
  appliedRevisionId = null,
  feedback = null,
  loadingRevisions = false,
  canUndo = false,
  busyAction = "",
  generationModelName = "当前视频模型",
  onTabChange,
  onResultChange,
  onApply,
  onUndo,
  onClose,
  onRetry,
  onSavePrompt,
  onSaveVersion,
  onSaveRecipe,
  onFeedback,
  onRestoreRevision,
  onCompileStoryboardShot,
  onApplyStoryboardShot,
  onGenerationSubmitted,
  onPickCompositionAsset,
  requestQuoteConfirmation,
  videoCompositionEnabled = true,
  recipesEnabled = true,
}) {
  const result = resultPayload(pending);
  const isVideo = String(pending?.category || pending?.target || operation?.target || "") === "video";
  const normalizedPending = normalizePendingReverseResult(pending, {
    mediaType: isVideo ? "video" : "image",
  });
  const selectableFields = applicationFields(normalizedPending, isVideo);
  const enabledFields = selectableFields.filter((field) => !field.disabled);
  const enabledFieldIdentity = enabledFields.map((field) => field.token).join("|");
  const imageEvidenceCount = normalizedPending.imageEvidence.length;
  const confirmedImageEvidenceCount = normalizedPending.imageEvidence.filter(
    (item) => item.review_status === "confirmed",
  ).length;
  // A running operation exists before its review envelope. Prefer the envelope
  // timestamp so the first completed result gets a fresh default selection,
  // while later field edits and an intentional "clear all" keep their state.
  const selectionIdentity = String(
    pending?.received_at || pending?.operation_id || pending?.operationId || operation?.id || "empty",
  );
  const [feedbackRating, setFeedbackRating] = useState(feedback?.rating || "");
  const [feedbackIssues, setFeedbackIssues] = useState(feedback?.issue_types || []);
  const [feedbackNote, setFeedbackNote] = useState(feedback?.note || "");
  const [selectedFields, setSelectedFields] = useState(() => new Set(enabledFields.map((field) => field.token)));
  const [selectedEvidenceField, setSelectedEvidenceField] = useState("");
  const [selectedShotIndex, setSelectedShotIndex] = useState(-1);
  const evidenceFieldKeys = [...new Set(normalizeImageEvidence(result?.image_evidence).map((item) => item.field_key))];
  useEffect(() => {
    setFeedbackRating(feedback?.rating || "");
    setFeedbackIssues(Array.isArray(feedback?.issue_types) ? feedback.issue_types : []);
    setFeedbackNote(feedback?.note || "");
  }, [feedback?.operation_id, feedback?.rating, feedback?.updated_at]);
  useEffect(() => {
    setSelectedFields(new Set(enabledFields.map((field) => field.token)));
    setSelectedEvidenceField("");
    setSelectedShotIndex(-1);
  }, [selectionIdentity]);
  useEffect(() => {
    const enabledTokens = new Set(enabledFields.map((field) => field.token));
    setSelectedFields((current) => new Set(
      [...current].filter((token) => enabledTokens.has(token)),
    ));
  }, [enabledFieldIdentity]);
  if (!result) return null;
  const tabs = isVideo
    ? [BASE_TABS[0], BASE_TABS[1], ["storyboard", "分镜"], BASE_TABS[2], BASE_TABS[3]]
    : BASE_TABS;

  function changeResult(nextResult) {
    if (pending?.payload && typeof pending.payload === "object") {
      onResultChange?.({ ...pending, payload: nextResult, dirty: true });
    } else if (pending?.result && typeof pending.result === "object") {
      onResultChange?.({ ...pending, result: nextResult, dirty: true });
    } else {
      onResultChange?.({ ...nextResult, dirty: true });
    }
  }

  function toggleFeedbackIssue(issue) {
    setFeedbackIssues((current) => (
      current.includes(issue)
        ? current.filter((item) => item !== issue)
        : current.length >= 8 ? current : [...current, issue]
    ));
  }

  function toggleApplicationField(token) {
    setSelectedFields((current) => {
      const next = new Set(current);
      if (next.has(token)) next.delete(token);
      else next.add(token);
      return next;
    });
  }

  function applicationSelection() {
    const structured = selectableFields.filter((field) => field.group === "structured");
    const parameters = selectableFields.filter((field) => field.group === "parameter");
    let selectedStructured = structured
      .filter((field) => selectedFields.has(field.token))
      .map((field) => field.key);
    if (enabledFields.every((field) => selectedFields.has(field.token))) {
      selectedStructured = Object.keys(normalizedPending.structured);
    }
    const selectedParameters = parameters
      .filter((field) => selectedFields.has(field.token))
      .map((field) => field.key);
    return {
      prompt: selectedFields.has("prompt"),
      negative: selectedFields.has("negative"),
      structuredKeys: selectedStructured,
      parameterFields: selectedParameters,
      imageEvidence: selectedFields.has("imageEvidence"),
      videoAnalysis: selectedFields.has("videoAnalysis"),
    };
  }

  function inspectFieldEvidence(fieldKey) {
    setSelectedEvidenceField(fieldKey);
    onTabChange?.("evidence");
  }

  function inspectStoryboardShot(index) {
    setSelectedShotIndex(index);
    onTabChange?.("storyboard");
  }

  const selectedFieldCount = enabledFields.filter((field) => selectedFields.has(field.token)).length;

  return (
    <section className="mt-3 overflow-hidden rounded-xl3 border border-aqua/35 bg-base2/70 shadow-glow-sm" aria-labelledby="reverse-result-title">
      <header className="flex items-start justify-between gap-3 border-b border-line px-3 py-3">
        <div>
          <p className="text-[11px] font-display font-medium text-aqua">待应用结果</p>
          <h3 id="reverse-result-title" className="mt-0.5 text-base font-display font-semibold text-snow">
            {isVideo ? "视频反推完成" : "图片反推完成"}
          </h3>
          <p className="mt-1 text-xs text-fog">
            {operation?.model_name || operation?.model_id || "视觉模型"}
            {operation?.cost_settled != null ? ` · 已结算 ${operation.cost_settled} 积分` : ""}
          </p>
        </div>
        <button type="button" className="icon-btn h-10 w-10" aria-label="关闭反推结果" onClick={onClose}>
          <X size={17} aria-hidden="true" />
        </button>
      </header>

      <div className="overflow-x-auto border-b border-line px-2" role="tablist" aria-label="反推结果视图">
        <div className="flex min-w-max">
          {tabs.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={activeTab === key}
              className={`min-h-11 border-b-2 px-3 text-xs font-display font-medium transition ${
                activeTab === key ? "border-aqua text-snow" : "border-transparent text-fog hover:text-mist"
              }`}
              onClick={() => onTabChange?.(key)}
            >
              {label}{key === "versions" && revisions.length ? ` ${revisions.length}` : ""}
            </button>
          ))}
        </div>
      </div>

      <div className="max-h-[32rem] overflow-y-auto p-3" role="tabpanel">
        {activeTab === "draft" && <ReverseDraftView result={result} onChange={changeResult} />}
        {activeTab === "structure" && (
          <ReverseStructureView
            result={result}
            onChange={changeResult}
            evidenceFieldKeys={evidenceFieldKeys}
            selectedField={selectedEvidenceField}
            onSelectField={setSelectedEvidenceField}
            onInspectEvidence={inspectFieldEvidence}
          />
        )}
        {activeTab === "storyboard" && (
          <ReverseStoryboardView
            result={result}
            operation={operation}
            revisions={revisions}
            appliedVersion={appliedVersion}
            appliedRevisionId={appliedRevisionId}
            onChange={changeResult}
            selectedShotIndex={selectedShotIndex}
            busyAction={busyAction}
            targetModelName={generationModelName}
            onSelectShot={setSelectedShotIndex}
            onCompileShot={onCompileStoryboardShot}
            onApplyShot={onApplyStoryboardShot}
            onGenerationSubmitted={onGenerationSubmitted}
            onPickCompositionAsset={onPickCompositionAsset}
            requestQuoteConfirmation={requestQuoteConfirmation}
            videoCompositionEnabled={videoCompositionEnabled}
          />
        )}
        {activeTab === "evidence" && (
          <ReverseEvidenceView
            result={result}
            operation={operation}
            selectedField={selectedEvidenceField}
            activeShotIndex={selectedShotIndex}
            onChange={changeResult}
            onSelectField={setSelectedEvidenceField}
            onSelectShot={inspectStoryboardShot}
          />
        )}
        {activeTab === "versions" && (loadingRevisions
          ? <p className="py-8 text-center text-sm text-fog" role="status">正在加载版本…</p>
          : <ReverseVersionsView revisions={revisions} onRestore={onRestoreRevision} />)}
      </div>

      <footer className="border-t border-line p-3">
        <fieldset className="mb-3" aria-label="选择要应用的反推字段">
          <legend className="sr-only">应用范围</legend>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-display font-medium text-mist" aria-hidden="true">应用范围</span>
            <div className="flex items-center gap-1.5 text-[11px]">
              <span className="text-fog">{selectedFieldCount}/{enabledFields.length}</span>
              <button type="button" className="text-aqua hover:text-snow" onClick={() => setSelectedFields(new Set(enabledFields.map((field) => field.token)))}>全选</button>
              <button type="button" className="text-fog hover:text-snow" onClick={() => setSelectedFields(new Set())}>清空</button>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {selectableFields.filter((field) => field.group !== "structured").map((field) => (
              <label
                key={field.token}
                className={`chip min-h-9 gap-1.5 px-2.5 ${field.disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"} ${selectedFields.has(field.token) ? "chip-active" : ""}`}
                title={field.disabled ? "请先在分析证据中确认需要应用的图片证据" : undefined}
              >
                <input type="checkbox" className="h-3.5 w-3.5 accent-brand" disabled={field.disabled} checked={!field.disabled && selectedFields.has(field.token)} onChange={() => toggleApplicationField(field.token)} />
                {field.label}
              </label>
            ))}
          </div>
          {selectableFields.some((field) => field.group === "structured") && (
            <details className="mt-2 border-t border-line pt-2">
              <summary className="cursor-pointer text-xs text-fog hover:text-mist">
                选择结构字段 · {selectableFields.filter((field) => field.group === "structured" && selectedFields.has(field.token)).length}/{selectableFields.filter((field) => field.group === "structured").length}
              </summary>
              <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
                {selectableFields.filter((field) => field.group === "structured").map((field) => (
                  <label key={field.token} className="flex min-h-9 cursor-pointer items-center gap-2 text-xs text-mist">
                    <input type="checkbox" className="h-3.5 w-3.5 accent-brand" checked={selectedFields.has(field.token)} onChange={() => toggleApplicationField(field.token)} />
                    <span className="truncate" title={field.label}>{field.label}</span>
                  </label>
                ))}
              </div>
            </details>
          )}
          {!isVideo && imageEvidenceCount > 0 && (
            <p className="mt-2 text-[11px] text-fog">
              图片证据：{confirmedImageEvidenceCount} 条已确认·{imageEvidenceCount - confirmedImageEvidenceCount} 条待审，应用版本只携带已确认证据。
            </p>
          )}
        </fieldset>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <button type="button" disabled={Boolean(busyAction) || selectedFieldCount === 0} className="btn-primary btn-sm min-h-11 justify-center" onClick={() => onApply?.("replace", applicationSelection())}>
            替换当前
          </button>
          <button type="button" disabled={Boolean(busyAction) || selectedFieldCount === 0} className="btn-secondary btn-sm min-h-11 justify-center" onClick={() => onApply?.("append", applicationSelection())}>
            追加内容
          </button>
          <button type="button" disabled={Boolean(busyAction) || selectedFieldCount === 0} className="btn-secondary btn-sm min-h-11 justify-center" onClick={() => onApply?.("structure_only", applicationSelection())}>
            仅结构
          </button>
          <button type="button" disabled={Boolean(busyAction) || selectedFieldCount === 0} className="btn-secondary btn-sm min-h-11 justify-center" onClick={() => onApply?.("parameters_only", applicationSelection())}>
            仅参数
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" disabled={!canUndo || Boolean(busyAction)} className="chip min-h-10 disabled:cursor-not-allowed disabled:opacity-45" onClick={onUndo}>
            <Undo2 size={14} aria-hidden="true" /> 撤销应用
          </button>
          <button type="button" disabled={Boolean(busyAction)} className="chip min-h-10" onClick={onSavePrompt}>
            <Save size={14} aria-hidden="true" /> 保存提示词
          </button>
          <button type="button" disabled={Boolean(busyAction)} className="chip min-h-10" onClick={onSaveVersion}>
            <Save size={14} aria-hidden="true" /> 保存当前版本
          </button>
          {recipesEnabled && (
            <button type="button" disabled={Boolean(busyAction)} className="chip min-h-10" onClick={onSaveRecipe}>
              <Save size={14} aria-hidden="true" /> 保存配方
            </button>
          )}
          <button type="button" disabled={Boolean(busyAction)} className="chip min-h-10" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" /> 再次反推
          </button>
          <span className="ml-auto text-[11px] text-fog">结果是否有用？</span>
          <button
            type="button"
            className={`icon-btn h-10 w-10 ${feedbackRating === "useful" ? "border-ok/50 bg-ok/10 text-ok" : ""}`}
            aria-label="结果有用"
            aria-pressed={feedbackRating === "useful"}
            disabled={Boolean(busyAction)}
            onClick={() => {
              setFeedbackRating("useful");
              setFeedbackIssues([]);
              setFeedbackNote("");
              onFeedback?.({ rating: "useful", issue_types: [], note: null });
            }}
          >
            <ThumbsUp size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`icon-btn h-10 w-10 ${feedbackRating === "not_useful" ? "border-bad/50 bg-bad/10 text-bad" : ""}`}
            aria-label="结果无用"
            aria-pressed={feedbackRating === "not_useful"}
            disabled={Boolean(busyAction)}
            onClick={() => setFeedbackRating("not_useful")}
          >
            <ThumbsDown size={15} aria-hidden="true" />
          </button>
        </div>
        {feedbackRating === "not_useful" && (
          <fieldset className="mt-3 rounded-xl border border-line bg-black/15 p-3">
            <legend className="px-1 text-xs font-display font-medium text-mist">哪些地方需要改进</legend>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {FEEDBACK_ISSUES.map(([value, label]) => (
                <label key={value} className={`chip min-h-9 cursor-pointer gap-1.5 px-2.5 ${feedbackIssues.includes(value) ? "chip-active" : ""}`}>
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 accent-brand"
                    checked={feedbackIssues.includes(value)}
                    onChange={() => toggleFeedbackIssue(value)}
                  />
                  {label}
                </label>
              ))}
            </div>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end">
              <label className="min-w-0 flex-1">
                <span className="sr-only">补充反馈</span>
                <textarea
                  className="textarea min-h-20 resize-y border-line bg-base2/60 px-3 py-2 text-xs"
                  maxLength={500}
                  placeholder="补充说明（可选）"
                  value={feedbackNote}
                  onChange={(event) => setFeedbackNote(event.target.value)}
                />
              </label>
              <button
                type="button"
                className="btn-secondary btn-sm min-h-10 justify-center"
                disabled={Boolean(busyAction)}
                onClick={() => onFeedback?.({
                  rating: "not_useful",
                  issue_types: feedbackIssues,
                  note: feedbackNote.trim() || null,
                })}
              >
                保存反馈
              </button>
            </div>
          </fieldset>
        )}
      </footer>
    </section>
  );
}
