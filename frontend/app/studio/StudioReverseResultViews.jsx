"use client";

import { Eye } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import StudioImageEvidence from "./StudioImageEvidence";
import StudioReverseStoryboard from "./StudioReverseStoryboard";
import StudioVideoComposition from "./StudioVideoComposition";
import StudioVideoEvidenceTimeline from "./StudioVideoEvidenceTimeline";
import { replaceReverseResultImageEvidence } from "./imageEvidenceReview";
import {
  compareReverseResultRevisions,
  formatReverseRevisionValue,
} from "./reverseResultRevisionDiff";
import {
  storyboardResultShots,
  storyboardResultWithShots,
  verifiedAppliedStoryboardRevision,
} from "./storyboard";

const REVISION_SOURCE_LABELS = {
  provider_raw: "供应商原稿",
  normalized: "标准化稿",
  user_edit: "用户修改稿",
  applied: "应用稿",
  generation: "生成稿",
};

const DIFF_GROUP_LABELS = {
  prompt: "生成稿",
  negative: "负向词",
  structured: "结构字段",
  parameters: "生成参数",
};

const DIFF_STATUS_LABELS = {
  added: "新增",
  removed: "删除",
  changed: "修改",
};

const VIDEO_ANALYSIS_MODE_LABELS = {
  keyframes: "多帧分析",
  multi_frame: "多帧分析",
  cover_fallback: "封面单帧",
  cover: "封面单帧",
  image_motion: "单图运动设计",
  unavailable: "分析不可用",
};

function analysisGapLabel(gap) {
  if (typeof gap === "string") return gap;
  if (!gap || typeof gap !== "object") return "未标注的证据缺口";
  const explicit = String(gap.message || gap.label || gap.field || "").trim();
  if (explicit) return explicit;
  const start = Number(gap.start_seconds);
  const end = Number(gap.end_seconds);
  if (Number.isFinite(start) && Number.isFinite(end)) {
    return `${start.toFixed(2)}-${end.toFixed(2)}s 未覆盖`;
  }
  if (Number.isFinite(start)) return `${start.toFixed(2)}s 之后未覆盖`;
  if (Number.isFinite(end)) return `${end.toFixed(2)}s 之前未覆盖`;
  return "未标注的证据缺口";
}

function revisionKey(revision) {
  return String(revision?.id || `version-${revision?.version || 0}`);
}

function sortedRevisions(revisions) {
  return [...revisions].sort((left, right) => Number(left.version || 0) - Number(right.version || 0));
}

function defaultComparisonKeys(revisions) {
  const ordered = sortedRevisions(revisions);
  const latest = ordered[ordered.length - 1];
  const previous = ordered[ordered.length - 2] || latest;
  return {
    before: previous ? revisionKey(previous) : "",
    after: latest ? revisionKey(latest) : "",
  };
}

function revisionOptionLabel(revision) {
  const source = REVISION_SOURCE_LABELS[revision.source] || revision.source || "未知来源";
  return `v${revision.version} · ${source}`;
}

function RevisionSelector({ side, value, revisions, onChange, onRestore }) {
  const revision = revisions.find((item) => revisionKey(item) === value) || null;
  return (
    <div className="min-w-0 border-b border-line pb-3 sm:border-b-0 sm:pb-0">
      <label className="block">
        <span className="label mb-1">{side} 版本</span>
        <select
          className="input min-h-11 w-full px-2.5 py-2 text-xs"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        >
          {revisions.map((item) => (
            <option key={revisionKey(item)} value={revisionKey(item)}>
              {revisionOptionLabel(item)}
            </option>
          ))}
        </select>
      </label>
      {revision && (
        <div className="mt-2 flex min-w-0 items-center justify-between gap-2">
          <p className="min-w-0 truncate text-[11px] text-fog">
            {revision.created_at ? new Date(revision.created_at).toLocaleString("zh-CN") : "未记录时间"}
          </p>
          <button type="button" className="btn-secondary btn-sm shrink-0" onClick={() => onRestore?.(revision)}>
            恢复 {side}
          </button>
        </div>
      )}
    </div>
  );
}

function DiffValue({ side, value }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-[10px] font-display font-medium text-fog">{side}</p>
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-mist">
        {formatReverseRevisionValue(value)}
      </pre>
    </div>
  );
}

export function ReverseDraftView({ result, onChange }) {
  return (
    <div className="space-y-3">
      <label className="block">
        <span className="label mb-1">可直接生成的提示词</span>
        <textarea
          className="textarea min-h-40 resize-y border-line bg-base2/60 px-3 py-2.5 text-sm leading-relaxed"
          value={String(result.final_text || "")}
          onChange={(event) => onChange?.({ ...result, final_text: event.target.value })}
        />
      </label>
      <label className="block">
        <span className="label mb-1">负向提示词</span>
        <textarea
          className="textarea min-h-20 resize-y border-line bg-base2/60 px-3 py-2.5 text-xs leading-relaxed"
          value={String(result.negative || result.structured?.["负向"] || "")}
          onChange={(event) => onChange?.({
            ...result,
            negative: event.target.value,
            structured: { ...(result.structured || {}), "负向": event.target.value },
          })}
        />
      </label>
    </div>
  );
}

export function ReverseStructureView({
  result,
  onChange,
  evidenceFieldKeys = [],
  selectedField = "",
  onSelectField,
  onInspectEvidence,
}) {
  const structured = result.structured && typeof result.structured === "object" ? result.structured : {};
  const entries = Object.entries(structured).filter(([key]) => key !== "final_text" && key !== "shots");
  const evidenceFields = new Set(evidenceFieldKeys);
  if (entries.length === 0) {
    return <p className="py-8 text-center text-sm text-fog" role="status">当前结果没有结构化字段。</p>;
  }
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {entries.map(([key, value], index) => (
        <div
          key={key}
          className={`${key.includes("观察") || key.includes("时序") ? "sm:col-span-2" : ""} border-l-2 pl-2 transition ${
            selectedField === key ? "border-aqua" : "border-transparent"
          }`}
        >
          <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
            <label className="label min-w-0 truncate normal-case" htmlFor={`reverse-structure-field-${index}`}>{key}</label>
            {evidenceFields.has(key) && (
              <button
                type="button"
                className="icon-btn h-8 w-8 shrink-0"
                aria-label={`查看 ${key} 的图片证据`}
                title="查看图片区域证据"
                onClick={() => onInspectEvidence?.(key)}
              >
                <Eye size={13} aria-hidden="true" />
              </button>
            )}
          </div>
          <textarea
            id={`reverse-structure-field-${index}`}
            className="textarea min-h-20 resize-y border-line bg-base2/60 px-2.5 py-2 text-xs"
            value={typeof value === "string" ? value : JSON.stringify(value ?? "", null, 2)}
            onFocus={() => onSelectField?.(key)}
            onChange={(event) => onChange?.({
              ...result,
              structured: { ...structured, [key]: event.target.value },
            })}
          />
        </div>
      ))}
    </div>
  );
}

export function ReverseStoryboardView({
  result,
  operation,
  revisions = [],
  appliedVersion = null,
  appliedRevisionId = null,
  onChange,
  selectedShotIndex = -1,
  busyAction = "",
  targetModelName = "当前视频模型",
  onSelectShot,
  onCompileShot,
  onApplyShot,
  onGenerationSubmitted,
  onPickCompositionAsset,
  requestQuoteConfirmation,
  videoCompositionEnabled = true,
}) {
  const analysis = result.video_analysis && typeof result.video_analysis === "object"
    ? result.video_analysis
    : {};
  const shots = storyboardResultShots(result);
  const appliedRevision = verifiedAppliedStoryboardRevision(revisions, {
    ...(operation && typeof operation === "object" ? operation : {}),
    ...(appliedVersion ? { applied_result_version: appliedVersion } : {}),
    ...(appliedRevisionId ? { applied_result_revision_id: appliedRevisionId } : {}),
  });
  return (
    <div>
      <StudioReverseStoryboard
        shots={shots}
        result={result}
        operation={operation}
        appliedRevision={appliedRevision}
        frames={Array.isArray(analysis.sampled_frames) ? analysis.sampled_frames : []}
        selectedShotIndex={selectedShotIndex}
        busyAction={busyAction}
        targetModelName={targetModelName}
        onChange={(nextShots) => onChange?.(storyboardResultWithShots(result, nextShots))}
        onSelectShot={onSelectShot}
        onCompileShot={onCompileShot}
        onApplyShot={onApplyShot}
        onGenerationSubmitted={onGenerationSubmitted}
        requestQuoteConfirmation={requestQuoteConfirmation}
      />
      {videoCompositionEnabled && (
        <StudioVideoComposition
          result={result}
          storyboardShots={shots}
          operation={operation}
          busyAction={busyAction}
          onChange={onChange}
          onPickAsset={onPickCompositionAsset}
          requestQuoteConfirmation={requestQuoteConfirmation}
        />
      )}
    </div>
  );
}

export function ReverseEvidenceView({
  result,
  operation,
  selectedField = "",
  activeShotIndex = -1,
  onChange,
  onSelectField,
  onSelectShot,
}) {
  const analysis = result.video_analysis && typeof result.video_analysis === "object"
    ? result.video_analysis
    : {};
  const source = analysis.source && typeof analysis.source === "object" ? analysis.source : {};
  const frames = Array.isArray(analysis.sampled_frames) ? analysis.sampled_frames : [];
  const gaps = Array.isArray(result.analysis_gaps)
    ? result.analysis_gaps
    : Array.isArray(analysis.analysis_gaps) ? analysis.analysis_gaps : [];
  const isVideo = String(operation?.target || "") === "video";
  const analysisMode = String(analysis.analysis_mode || "unavailable");
  const degradedReason = String(analysis.degraded_reason || analysis.fallback_reason || "").trim();

  return (
    <div className="space-y-4 text-xs">
      <StudioImageEvidence
        result={result}
        operation={operation}
        selectedField={selectedField}
        onSelectField={onSelectField}
        onChange={(nextEvidence) => onChange?.(
          replaceReverseResultImageEvidence(result, nextEvidence),
        )}
      />
      {isVideo && (
        <>
          <div className="border-l-2 border-aqua pl-2" aria-label="视频分析状态">
            <p className="text-mist">
              <span className="text-fog">分析模式 </span>
              {VIDEO_ANALYSIS_MODE_LABELS[analysisMode] || analysisMode}
            </p>
            {degradedReason && <p className="mt-1 text-warn">降级原因：{degradedReason}</p>}
          </div>
          <StudioVideoEvidenceTimeline
            analysis={analysis}
            shots={storyboardResultShots(result)}
            gaps={gaps}
            activeShotIndex={activeShotIndex}
            onSelectShot={onSelectShot}
          />
        </>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <div><dt className="text-fog">素材类型</dt><dd className="mt-0.5 text-mist">{source.source_type || "图片"}</dd></div>
        <div><dt className="text-fog">分析时长</dt><dd className="mt-0.5 text-mist">{source.duration_seconds != null ? `${Number(source.duration_seconds).toFixed(2)}s` : "-"}</dd></div>
        <div><dt className="text-fog">证据帧</dt><dd className="mt-0.5 text-mist">{frames.length || "-"}</dd></div>
        <div><dt className="text-fog">音频</dt><dd className="mt-0.5 text-mist">{source.audio_analyzed ? "已分析" : source.has_audio ? "未分析" : "未检测到"}</dd></div>
      </dl>
      {frames.length > 0 && (
        <div>
          <p className="font-display font-medium text-mist">采样时间</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {frames.map((frame, index) => (
              <span key={`${frame.index}-${index}`} className="badge border border-line bg-white/[0.04] text-fog">
                {Number(frame.absolute_timestamp_seconds ?? frame.timestamp_seconds ?? 0).toFixed(2)}s
              </span>
            ))}
          </div>
        </div>
      )}
      <div>
        <p className="font-display font-medium text-mist">证据缺口</p>
        {gaps.length ? (
          <ul className="mt-2 space-y-1.5 text-fog">
            {gaps.map((gap, index) => <li key={index}>- {analysisGapLabel(gap)}</li>)}
          </ul>
        ) : <p className="mt-2 text-fog">未发现需要提示的证据缺口。</p>}
      </div>
    </div>
  );
}

export function ReverseVersionsView({ revisions = [], onRestore }) {
  const identity = revisions.map((revision) => revisionKey(revision)).join("|");
  const defaults = defaultComparisonKeys(revisions);
  const [beforeKey, setBeforeKey] = useState(defaults.before);
  const [afterKey, setAfterKey] = useState(defaults.after);
  useEffect(() => {
    const keys = new Set(revisions.map((revision) => revisionKey(revision)));
    const nextDefaults = defaultComparisonKeys(revisions);
    setBeforeKey((current) => keys.has(current) ? current : nextDefaults.before);
    setAfterKey((current) => keys.has(current) ? current : nextDefaults.after);
  }, [identity]);

  const ordered = useMemo(() => sortedRevisions(revisions), [revisions]);
  const beforeRevision = ordered.find((revision) => revisionKey(revision) === beforeKey) || null;
  const afterRevision = ordered.find((revision) => revisionKey(revision) === afterKey) || null;
  const comparison = useMemo(
    () => beforeRevision && afterRevision
      ? compareReverseResultRevisions(beforeRevision, afterRevision)
      : null,
    [beforeRevision, afterRevision],
  );

  if (revisions.length === 0) {
    return <p className="py-8 text-center text-sm text-fog" role="status">尚未保存结果版本。</p>;
  }
  return (
    <div className="space-y-4" aria-label="反推结果版本对比">
      <div className="grid gap-3 border-b border-line pb-4 sm:grid-cols-2 sm:divide-x sm:divide-line">
        <RevisionSelector
          side="A"
          value={beforeKey}
          revisions={ordered}
          onChange={setBeforeKey}
          onRestore={onRestore}
        />
        <div className="sm:pl-3">
          <RevisionSelector
            side="B"
            value={afterKey}
            revisions={ordered}
            onChange={setAfterKey}
            onRestore={onRestore}
          />
        </div>
      </div>

      {comparison && (
        <div aria-live="polite">
          <div className="flex flex-wrap items-center gap-1.5">
            <p className="mr-1 text-xs font-display font-medium text-mist">变更摘要</p>
            <span className="badge border border-line bg-white/[0.04] text-fog">
              {comparison.summary.total} 项变更
            </span>
            {comparison.summary.added > 0 && (
              <span className="badge border border-line bg-white/[0.04] text-fog">
                新增 {comparison.summary.added}
              </span>
            )}
            {comparison.summary.removed > 0 && (
              <span className="badge border border-line bg-white/[0.04] text-fog">
                删除 {comparison.summary.removed}
              </span>
            )}
            {comparison.summary.changed > 0 && (
              <span className="badge border border-line bg-white/[0.04] text-fog">
                修改 {comparison.summary.changed}
              </span>
            )}
            {Object.entries(comparison.summary.groups).map(([group, count]) => count > 0 && (
              <span key={group} className="badge border border-line bg-white/[0.04] text-fog">
                {DIFF_GROUP_LABELS[group]} {count}
              </span>
            ))}
          </div>

          {comparison.diffs.length === 0 ? (
            <p className="py-8 text-center text-sm text-fog" role="status">两个版本的提示词、结构和参数一致。</p>
          ) : (
            <ol className="mt-3 divide-y divide-line border-y border-line" aria-label="版本字段变更">
              {comparison.diffs.map((diff) => (
                <li key={diff.key} className="py-3">
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    <p className="text-xs font-display font-medium text-snow">{diff.label}</p>
                    <span className="text-[10px] text-fog">{DIFF_GROUP_LABELS[diff.group]}</span>
                    <span className="badge border border-aqua/25 bg-aqua/[0.06] text-aqua">
                      {DIFF_STATUS_LABELS[diff.status]}
                    </span>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2 sm:divide-x sm:divide-line">
                    <DiffValue side="A" value={diff.before} />
                    <div className="sm:pl-3"><DiffValue side="B" value={diff.after} /></div>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
