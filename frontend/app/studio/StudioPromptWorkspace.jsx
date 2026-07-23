"use client";

import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  History,
  LoaderCircle,
  RotateCw,
  Undo2,
  WandSparkles,
  X,
} from "lucide-react";
import { api } from "../../lib/api";
import {
  isSupportedPromptOptimizationSegment,
  PROMPT_OPTIMIZATION_DIRECTIONS,
  promptOptimizationDisplayProposal,
} from "./promptOptimization";
import StudioModelSelector from "./StudioModelSelector";
import StudioProductVideoStrategy from "./StudioProductVideoStrategy";

const DIRECTION_LABELS = Object.fromEntries(
  PROMPT_OPTIMIZATION_DIRECTIONS.map((item) => [item.key, item.label]),
);

const HISTORY_PAGE_SIZE = 8;
const HISTORY_STATUS_LABELS = {
  proposed: "待处理",
  accepted: "已接受",
  partially_accepted: "部分接受",
  rejected: "已保留原文",
  expired: "已过期",
};

const WARNING_ACTION_META = {
  dropped: { label: "已丢弃", className: "border-bad/35 bg-bad/10 text-bad" },
  transformed: { label: "已转换", className: "border-aqua/35 bg-aqua/10 text-aqua" },
  unverified: { label: "未验证", className: "border-warn/35 bg-warn/10 text-warn" },
  info: { label: "说明", className: "border-line bg-white/[0.04] text-fog" },
};

function warningDisplayValue(value) {
  if (!value || typeof value !== "object") {
    return { action: "info", field: "", message: String(value || "") };
  }
  return {
    action: WARNING_ACTION_META[value.action] ? value.action : "info",
    field: String(value.field || "").trim(),
    message: String(value.message || "").trim(),
  };
}

function historyTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function PromptOptimizationProposal({
  proposal,
  onAccept,
  onReject,
  readOnly = false,
  title = "提示词优化建议",
}) {
  const [selectedSegments, setSelectedSegments] = useState([]);
  const supportedSegments = Array.isArray(proposal?.segments)
    ? proposal.segments.filter((item) => (
      item.changed && isSupportedPromptOptimizationSegment(item.field_path)
    ))
    : [];
  const supportedChangeSummary = supportedSegments.map((item) => item.label || item.field_path);
  useEffect(() => {
    setSelectedSegments(supportedSegments.map((item) => item.id));
  }, [proposal?.proposal_id]);
  if (!proposal) return null;
  const titleId = `prompt-optimization-proposal-title-${readOnly ? "history" : "current"}-${proposal.proposal_id || "new"}`;
  const rawText = proposal.raw_text ?? proposal.original?.final_text ?? "";
  const optimizedText = proposal.optimized_text ?? proposal.suggestion?.final_text ?? "";
  const direction = proposal.direction || proposal.mode;
  return (
    <section className="rounded-xl border border-iris/35 bg-iris/10 p-3" aria-labelledby={titleId}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="flex items-center gap-1.5 text-xs font-display font-semibold text-snow" id={titleId}>
            <WandSparkles size={14} aria-hidden="true" /> {title}
          </p>
          <p className="mt-0.5 text-[11px] text-fog">
            {proposal.model_name || "提示词模型"} · {DIRECTION_LABELS[direction] || "忠实整理"}
            {proposal.charged_credits ? ` · ${proposal.charged_credits} 积分` : ""}
            {readOnly
              ? ` · ${HISTORY_STATUS_LABELS[proposal.status] || "历史记录"}`
              : " · 接受前不会修改当前内容"}
          </p>
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div className="min-w-0 rounded-lg border border-line bg-base/35 p-2.5">
          <p className="text-[10px] font-display font-medium text-fog">当前原文</p>
          <p className="mt-1 max-h-36 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-mist">{rawText}</p>
        </div>
        <div className="min-w-0 rounded-lg border border-aqua/30 bg-aqua/10 p-2.5">
          <p className="text-[10px] font-display font-medium text-aqua">优化建议</p>
          <p className="mt-1 max-h-36 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-snow">{optimizedText}</p>
        </div>
      </div>
      {supportedChangeSummary.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5" aria-label="优化变更摘要">
          {supportedChangeSummary.map((item) => (
            <span key={item} className="badge border border-line bg-white/[0.04] text-fog">{item}</span>
          ))}
        </div>
      )}
      {Array.isArray(proposal.warnings) && proposal.warnings.length > 0 && (
        <ul className="mt-2 divide-y divide-warn/20 rounded-lg border border-warn/30 bg-warn/10 px-3 text-xs" aria-label="优化风险提示">
          {proposal.warnings.map((item, index) => {
            const warning = warningDisplayValue(item);
            const action = WARNING_ACTION_META[warning.action];
            return (
              <li
                key={`${item?.code || "warning"}-${warning.field || warning.message}-${index}`}
                className="grid min-w-0 gap-1 py-2"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <span className={`inline-flex border px-1.5 py-0.5 text-[10px] font-medium ${action.className}`}>
                    {action.label}
                  </span>
                  {warning.field && (
                    <code className="min-w-0 break-all text-[10px] text-mist" title={warning.field}>
                      字段：{warning.field}
                    </code>
                  )}
                </div>
                <p className="break-words leading-relaxed text-warn">{warning.message || "模型适配需要人工确认。"}</p>
              </li>
            );
          })}
        </ul>
      )}
      {Array.isArray(proposal.constraint_coverage) && proposal.constraint_coverage.length > 0 && (
        <details className="mt-2 border-t border-line pt-2 text-xs">
          <summary className="cursor-pointer text-fog hover:text-mist">约束覆盖证据</summary>
          <ul className="mt-2 space-y-1.5 text-fog">
            {proposal.constraint_coverage.map((item) => (
              <li key={item.constraint_id} className="flex items-start justify-between gap-3">
                <span className="min-w-0 break-words">{item.source_field}</span>
                <span className={item.status === "verified" ? "text-aqua" : "text-warn"}>
                  {item.status === "verified" ? "已精确验证" : item.status === "mapped" ? "已映射·待确认" : "未验证"}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {!readOnly && supportedSegments.length > 1 && (
        <fieldset className="mt-2 border-t border-line pt-2 text-xs">
          <legend className="font-display font-medium text-mist">选择要应用的字段</legend>
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
            {supportedSegments.map((item) => (
              <label key={item.id} className="flex min-w-0 items-center gap-2 text-fog">
                <input
                  type="checkbox"
                  checked={selectedSegments.includes(item.id)}
                  onChange={(event) => setSelectedSegments((current) => (
                    event.target.checked
                      ? [...current, item.id]
                      : current.filter((id) => id !== item.id)
                  ))}
                />
                <span className="truncate">{item.label || item.field_path}</span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {readOnly ? (
        <p className="mt-3 border-t border-line pt-2 text-[11px] leading-relaxed text-fog">
          历史建议仅供对比，重新打开不会覆盖当前工作区。
        </p>
      ) : (
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" className="btn-secondary btn-sm min-h-10" onClick={onReject}>
            <X size={14} aria-hidden="true" /> 保留原文
          </button>
          <button
            type="button"
            className="btn-primary btn-sm min-h-10"
            disabled={selectedSegments.length === 0}
            onClick={() => onAccept?.(selectedSegments)}
          >
            <Check size={14} aria-hidden="true" /> {proposal.optimization_kind === "model_compile" ? "应用编译稿" : "接受优化"}
          </button>
        </div>
      )}
    </section>
  );
}

function PromptOptimizationHistory() {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [page, setPage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const detailRequestRef = useRef(0);

  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setLoading(true);
    setError("");
    api.studioPromptOptimizations({ limit: HISTORY_PAGE_SIZE, offset })
      .then((result) => {
        if (active) setPage(result);
      })
      .catch((requestError) => {
        if (active) setError(requestError?.message || "优化历史加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, offset, reloadVersion]);

  async function reopenProposal(proposalId) {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setDetailLoading(true);
    setError("");
    try {
      const result = await api.studioPromptOptimization(proposalId);
      if (detailRequestRef.current !== requestId) return;
      setDetail(promptOptimizationDisplayProposal(result));
    } catch (requestError) {
      if (detailRequestRef.current === requestId) {
        setError(requestError?.message || "历史建议打开失败");
      }
    } finally {
      if (detailRequestRef.current === requestId) setDetailLoading(false);
    }
  }

  function closeHistory() {
    detailRequestRef.current += 1;
    setDetail(null);
    setDetailLoading(false);
    setOpen(false);
  }

  const items = Array.isArray(page?.items) ? page.items : [];
  return (
    <section className="rounded-xl border border-line bg-base/30" aria-labelledby="prompt-optimization-history-title">
      <button
        type="button"
        className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs text-mist hover:text-snow"
        aria-expanded={open}
        aria-controls="prompt-optimization-history-content"
        onClick={() => {
          if (open) closeHistory();
          else setOpen(true);
        }}
      >
        <span className="flex items-center gap-2 font-display font-medium" id="prompt-optimization-history-title">
          <History size={14} aria-hidden="true" /> 优化历史
        </span>
        <span className="text-[11px] text-fog">{open ? "收起" : "查看与重新打开"}</span>
      </button>
      {open && (
        <div className="border-t border-line p-3" id="prompt-optimization-history-content">
          {detail ? (
            <div className="grid gap-2">
              <button
                type="button"
                className="btn-secondary btn-sm min-h-10 justify-self-start"
                onClick={() => setDetail(null)}
              >
                <ChevronLeft size={14} aria-hidden="true" /> 返回历史列表
              </button>
              <PromptOptimizationProposal
                proposal={detail}
                readOnly
                title="已重新打开的历史建议"
              />
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] text-fog">最近的提示词优化和模型编译记录</p>
                <button
                  type="button"
                  className="icon-btn h-9 w-9"
                  aria-label="刷新优化历史"
                  title="刷新优化历史"
                  onClick={() => setReloadVersion((value) => value + 1)}
                  disabled={loading}
                >
                  <RotateCw size={14} aria-hidden="true" />
                </button>
              </div>
              {loading && (
                <div className="mt-2 flex min-h-20 items-center justify-center gap-2 text-xs text-fog" role="status">
                  <LoaderCircle className="animate-spin" size={15} aria-hidden="true" /> 正在加载优化历史
                </div>
              )}
              {!loading && error && (
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad" role="alert">
                  <span>{error}</span>
                  <button type="button" className="btn-secondary btn-sm" onClick={() => setReloadVersion((value) => value + 1)}>
                    重试
                  </button>
                </div>
              )}
              {!loading && !error && items.length === 0 && (
                <p className="mt-2 rounded-lg border border-dashed border-line px-3 py-5 text-center text-xs text-fog">
                  还没有优化历史
                </p>
              )}
              {!loading && !error && items.length > 0 && (
                <ul className="mt-2 divide-y divide-line border-y border-line" aria-label="提示词优化历史">
                  {items.map((item) => (
                    <li key={item.proposal_id} className="py-2.5">
                      <button
                        type="button"
                        className="grid min-h-10 w-full min-w-0 gap-1 text-left hover:text-snow"
                        onClick={() => reopenProposal(item.proposal_id)}
                        disabled={detailLoading}
                      >
                        <span className="flex min-w-0 items-center justify-between gap-3">
                          <span className="truncate text-xs font-display font-medium text-mist">
                            {item.suggestion_preview || item.original_preview || "未命名优化建议"}
                          </span>
                          <span className="shrink-0 text-[10px] text-aqua">重新打开</span>
                        </span>
                        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-fog">
                          <span>{HISTORY_STATUS_LABELS[item.status] || item.status}</span>
                          <span>{item.category === "video" ? "视频" : "图片"}</span>
                          <span>{DIRECTION_LABELS[item.mode] || "模型适配"}</span>
                          {item.target_model_id && <span>{item.target_model_id}</span>}
                          <time dateTime={item.created_at}>{historyTime(item.created_at)}</time>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {detailLoading && (
                <p className="mt-2 flex items-center gap-2 text-xs text-fog" role="status">
                  <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> 正在打开历史建议
                </p>
              )}
              {!loading && !error && (offset > 0 || page?.has_more) && (
                <div className="mt-3 flex items-center justify-between gap-3">
                  <button
                    type="button"
                    className="btn-secondary btn-sm min-h-10"
                    disabled={offset === 0}
                    onClick={() => setOffset((value) => Math.max(0, value - HISTORY_PAGE_SIZE))}
                  >
                    <ChevronLeft size={14} aria-hidden="true" /> 上一页
                  </button>
                  <span className="text-[10px] text-fog">第 {Math.floor(offset / HISTORY_PAGE_SIZE) + 1} 页</span>
                  <button
                    type="button"
                    className="btn-secondary btn-sm min-h-10"
                    disabled={!page?.has_more}
                    onClick={() => setOffset((value) => value + HISTORY_PAGE_SIZE)}
                  >
                    下一页 <ChevronRight size={14} aria-hidden="true" />
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}

function PromptOptimizationControls({
  direction,
  targetLanguage,
  estimatedCredits,
  onSettingsChange,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  promptModelOptions,
  selectedPromptModelConfigId,
  onPromptModelChange,
}) {
  const selectedDirection = PROMPT_OPTIMIZATION_DIRECTIONS.find((item) => item.key === direction)
    || PROMPT_OPTIMIZATION_DIRECTIONS[0];
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      <StudioModelSelector
        use="prompt"
        options={promptModelOptions}
        value={selectedPromptModelConfigId}
        onChange={onPromptModelChange}
        compact
      />
      <label className="min-w-0">
        <span className="sr-only">提示词优化方向</span>
        <select
          className="input min-h-10 max-w-[10rem] px-2.5 py-1.5 text-xs"
          value={selectedDirection.key}
          disabled={optimizingPrompt}
          onChange={(event) => onSettingsChange?.({ direction: event.target.value })}
          title={selectedDirection.description}
        >
          {PROMPT_OPTIMIZATION_DIRECTIONS.map((item) => (
            <option key={item.key} value={item.key}>{item.label}</option>
          ))}
        </select>
      </label>
      {selectedDirection.key === "translate" && (
        <label>
          <span className="sr-only">翻译目标语言</span>
          <select
            className="input min-h-10 px-2.5 py-1.5 text-xs"
            value={targetLanguage || "en"}
            disabled={optimizingPrompt}
            onChange={(event) => onSettingsChange?.({ targetLanguage: event.target.value })}
          >
            <option value="en">转为英文</option>
            <option value="zh-CN">转为中文</option>
          </select>
        </label>
      )}
      <button
        type="button"
        onClick={onOptimizePrompt}
        disabled={!canOptimizePrompt || optimizingPrompt}
        className="chip disabled:cursor-not-allowed disabled:opacity-45"
        title={selectedDirection.description}
      >
        <WandSparkles size={14} aria-hidden="true" />
        {optimizingPrompt
          ? "处理中…"
          : selectedDirection.key === "target_model_adaptation" ? "编译到当前模型" : "生成优化建议"}
      </button>
      <span className="text-[10px] text-fog">预计 {Math.max(0, Number(estimatedCredits || 0))} 积分</span>
    </div>
  );
}

function transferRulesForEditMode({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
}) {
  if (!isImageEditMode) {
    return portraitGenerationMode
      ? [
          ["人物重构目标", "上传人像作为唯一身份参考"],
          ["迁移视频风格", "参考视频只提供动作、镜头、场景和节奏"],
          ["能力边界", "当前为人物参考驱动重构，非逐帧换脸"],
        ]
      : [
          ["保留产品身份", "Logo、包装、颜色和形状不漂移"],
          ["迁移参考气质", "只迁移场景、构图、光线和广告质感"],
          ["输出商业素材", "产品清晰，边缘自然融入新场景"],
        ];
  }
  if (portraitGenerationMode) {
    return [
      ["锁定人物身份", "五官、脸型、发型、肤色和年龄感不变"],
      ["迁移参考风格", "只迁移场景、构图、光线、妆造和画面质感"],
      ["高保真人像", "脸部清晰自然，避免变成参考图里的人"],
    ];
  }
  if (productGenerationMode) {
    return [
      ["锁定产品身份", "SKU、Logo、包装结构、品牌色和表面文字不改"],
      ["迁移商业场景", "只生成背景、道具、构图、光线和广告质感"],
      ["高保真输出", "产品清晰锐利，包装文字逐字保留"],
    ];
  }
  return [
    ["按提示词编辑", "只改用户明确要求修改的部分"],
    ["保留源图细节", "主体、Logo、文字和比例默认保持"],
    ["可选参考增强", "可用右侧参考图迁移光线、构图和质感"],
  ];
}

function modeTitle({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
  creationModeLabel,
}) {
  if (isImageEditMode) {
    if (portraitGenerationMode) return "上传人像生成同款风格写真";
    if (productGenerationMode) return "上传产品图生成商业素材";
    return "上传图片后按提示词编辑";
  }
  return portraitGenerationMode ? "上传人物生成目标视频风格" : `${creationModeLabel}素材重构`;
}

function modeBadge({
  category,
  portraitGenerationMode,
  productGenerationMode,
}) {
  if (category === "video") return portraitGenerationMode ? "人物重构" : "图生视频源";
  if (portraitGenerationMode) return "人像高保真";
  if (productGenerationMode) return "产品高保真";
  return "图片编辑源";
}

function EditSubjectSelector({
  isImageEditMode,
  subjectMode,
  onSubjectModeChange,
}) {
  const options = isImageEditMode
    ? [
        { key: "general", label: "普通编辑", desc: "按提示修图" },
        { key: "product", label: "产品生产", desc: "强保护Logo和包装" },
        { key: "portrait", label: "人像写真", desc: "强保护五官和身份" },
      ]
    : [
        { key: "product", label: "产品视频", desc: "上传产品图做主体" },
        { key: "portrait", label: "人物重构", desc: "人像参考生成同款视频" },
      ];

  return (
    <div className={`mt-4 grid gap-2 rounded-xl border border-line bg-base/35 p-1 ${isImageEditMode ? "grid-cols-1 sm:grid-cols-3" : "grid-cols-2"}`}>
      {options.map((item) => {
        const active = item.key === subjectMode;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onSubjectModeChange(item.key)}
            className={`rounded-lg px-3 py-2 text-left transition ${
              active
                ? "border border-aqua/50 bg-aqua/15 text-snow shadow-glow-sm"
                : "border border-transparent text-fog hover:bg-white/[0.04] hover:text-mist"
            }`}
          >
            <span className="block text-sm font-display font-semibold">{item.label}</span>
            <span className="mt-0.5 block text-[11px] leading-snug">{item.desc}</span>
          </button>
        );
      })}
    </div>
  );
}

function EditBrief({
  category,
  isImageEditMode,
  subjectMode,
  portraitGenerationMode,
  productGenerationMode,
  readySteps,
  creationModeLabel,
  onSubjectModeChange,
}) {
  return (
    <div className="rounded-2xl border border-line2 bg-gradient-to-br from-iris/15 via-white/[0.055] to-aqua/10 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-display text-fog">编辑 Brief</p>
          <h2 className="mt-1 text-xl font-display font-semibold text-snow">
            {modeTitle({ isImageEditMode, portraitGenerationMode, productGenerationMode, creationModeLabel })}
          </h2>
        </div>
        <span className="badge bg-brand-soft text-snow">
          {modeBadge({ category, portraitGenerationMode, productGenerationMode })}
        </span>
      </div>

      <EditSubjectSelector
        isImageEditMode={isImageEditMode}
        subjectMode={subjectMode}
        onSubjectModeChange={onSubjectModeChange}
      />

      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        {readySteps.map((step) => (
          <div
            key={step.label}
            className={`rounded-xl border px-3 py-2 ${
              step.ready ? "border-aqua/35 bg-aqua/10" : "border-line bg-base/40"
            }`}
          >
            <p className="text-[11px] text-fog">{step.label}</p>
            <p className={`mt-1 text-sm font-display font-medium ${step.ready ? "text-aqua" : "text-mist"}`}>
              {step.ready ? step.readyText : step.pendingText}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

function EditPromptPanel({
  prompt,
  placeholder,
  onPromptChange,
  onPromptDirty,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  optimizationDirection,
  optimizationTargetLanguage,
  optimizationEstimatedCredits,
  onOptimizationSettingsChange,
  promptModelOptions,
  selectedPromptModelConfigId,
  onPromptModelChange,
  onClearWorkspace,
  onClearAllWorkspaces,
  canClearWorkspace,
  onSubmitPreview,
}) {
  return (
    <div className="rounded-2xl border border-line bg-base/35 p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <label className="label m-0">补充要求 / 卖点 / 必须保留</label>
        <span className="text-[11px] text-fog">⌘/Ctrl + Enter</span>
      </div>
      <textarea
        className="textarea h-28 resize-none border-line bg-base2/60 text-[14px]"
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => {
          onPromptChange(e.target.value);
          onPromptDirty(true);
        }}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSubmitPreview();
          }
        }}
      />
      <div className="mt-2 flex flex-wrap gap-1.5">
        <PromptOptimizationControls
          direction={optimizationDirection}
          targetLanguage={optimizationTargetLanguage}
          estimatedCredits={optimizationEstimatedCredits}
          onSettingsChange={onOptimizationSettingsChange}
          onOptimizePrompt={onOptimizePrompt}
          canOptimizePrompt={canOptimizePrompt}
          optimizingPrompt={optimizingPrompt}
          promptModelOptions={promptModelOptions}
          selectedPromptModelConfigId={selectedPromptModelConfigId}
          onPromptModelChange={onPromptModelChange}
        />
        <button
          type="button"
          onClick={onClearWorkspace}
          disabled={!canClearWorkspace}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="清空提示词、上传素材和反推结果"
        >
          ✕ 清空
        </button>
        <button
          type="button"
          onClick={onClearAllWorkspaces}
          className="chip text-fog hover:text-bad"
          title="二次确认后清空全部创作模式"
        >
          清空全部模式
        </button>
      </div>
    </div>
  );
}

function EditAssistPanels({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
  editStyleKeys,
  onRecompose,
}) {
  const rules = transferRulesForEditMode({ isImageEditMode, portraitGenerationMode, productGenerationMode });

  return (
    <div className="grid min-w-0 gap-3 xl:grid-cols-[1fr_1.15fr]">
      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
        <p className="text-xs font-display font-medium text-mist">迁移规则</p>
        <div className="mt-3 space-y-2">
          {rules.map(([title, desc]) => (
            <div key={title} className="rounded-xl border border-line bg-base/35 px-3 py-2">
              <p className="text-sm font-display font-medium text-snow">{title}</p>
              <p className="mt-0.5 text-xs text-fog">{desc}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs font-display font-medium text-mist">已提取风格维度</p>
          {editStyleKeys.length > 0 && (
            <button type="button" onClick={onRecompose} className="chip px-2 py-0.5">
              重组提示词
            </button>
          )}
        </div>
        {editStyleKeys.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {editStyleKeys.slice(0, 12).map((key) => (
              <span key={key} className="rounded-full border border-iris/30 bg-iris/10 px-2.5 py-1 text-xs text-mist">
                {key}
              </span>
            ))}
            {editStyleKeys.length > 12 && (
              <span className="rounded-full border border-line bg-white/5 px-2.5 py-1 text-xs text-fog">
                +{editStyleKeys.length - 12}
              </span>
            )}
          </div>
        ) : (
          <div className="mt-3 rounded-xl border border-dashed border-line2 bg-base/25 px-3 py-4">
            <p className="text-sm text-mist">右侧反推后会显示可迁移的风格维度。</p>
            <p className="mt-1 text-xs text-fog">也可以直接在上方补充产品卖点后生成。</p>
          </div>
        )}
      </div>
    </div>
  );
}

function DefaultPromptPanel({
  prompt,
  placeholder,
  onPromptChange,
  onPromptDirty,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  optimizationDirection,
  optimizationTargetLanguage,
  optimizationEstimatedCredits,
  onOptimizationSettingsChange,
  promptModelOptions,
  selectedPromptModelConfigId,
  onPromptModelChange,
  onClearWorkspace,
  onClearAllWorkspaces,
  canClearWorkspace,
  onSubmitPreview,
}) {
  return (
    <>
      <textarea
        className="textarea h-36 resize-none border-0 bg-transparent px-1 text-[15px] focus:ring-0 lg:h-40"
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => {
          onPromptChange(e.target.value);
          onPromptDirty(true);
        }}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSubmitPreview();
          }
        }}
      />
      <div className="mt-1.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
        <PromptOptimizationControls
          direction={optimizationDirection}
          targetLanguage={optimizationTargetLanguage}
          estimatedCredits={optimizationEstimatedCredits}
          onSettingsChange={onOptimizationSettingsChange}
          onOptimizePrompt={onOptimizePrompt}
          canOptimizePrompt={canOptimizePrompt}
          optimizingPrompt={optimizingPrompt}
          promptModelOptions={promptModelOptions}
          selectedPromptModelConfigId={selectedPromptModelConfigId}
          onPromptModelChange={onPromptModelChange}
        />
        <button
          type="button"
          onClick={onClearWorkspace}
          disabled={!canClearWorkspace}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="清空提示词、上传素材和反推结果"
        >
          ✕ 清空
        </button>
        <button
          type="button"
          onClick={onClearAllWorkspaces}
          className="chip text-fog hover:text-bad"
          title="二次确认后清空全部创作模式"
        >
          清空全部模式
        </button>
      </div>
    </>
  );
}

export default function StudioPromptWorkspace({
  category,
  creationModeLabel,
  isEditMode,
  isImageEditMode,
  subjectMode,
  portraitGenerationMode,
  productGenerationMode,
  prompt,
  placeholder,
  readySteps,
  editStyleKeys,
  onPromptChange,
  onPromptDirty,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  optimizationProposal = null,
  optimizationDirection = "faithful",
  optimizationTargetLanguage = "en",
  optimizationEstimatedCredits = 0,
  onOptimizationSettingsChange,
  onAcceptOptimization,
  onRejectOptimization,
  optimizationUndoAvailable = false,
  onUndoOptimization,
  generationModelOptions = [],
  selectedGenerationModelConfigId = null,
  onGenerationModelChange,
  productVideoStrategyOptions = [],
  productVideoTemplate = null,
  productVideoStrategySupported = true,
  onProductVideoTemplateChange,
  promptModelOptions = [],
  selectedPromptModelConfigId = null,
  onPromptModelChange,
  onClearWorkspace,
  onClearAllWorkspaces,
  canClearWorkspace,
  onSubjectModeChange,
  onRecompose,
  onSubmitPreview,
}) {
  const generationModelSelector = (
    <div className="flex min-w-0 justify-end border-b border-line pb-2">
      <StudioModelSelector
        use={category}
        options={generationModelOptions}
        value={selectedGenerationModelConfigId}
        onChange={onGenerationModelChange}
      />
    </div>
  );
  const productVideoStrategySelector = category === "video" && productGenerationMode ? (
    <StudioProductVideoStrategy
      options={productVideoStrategyOptions}
      value={productVideoTemplate}
      supported={productVideoStrategySupported}
      onChange={onProductVideoTemplateChange}
    />
  ) : null;
  if (!isEditMode) {
    return (
      <div className="grid gap-2">
        {generationModelSelector}
        {productVideoStrategySelector}
        <DefaultPromptPanel
          prompt={prompt}
          placeholder={placeholder}
          onPromptChange={onPromptChange}
          onPromptDirty={onPromptDirty}
          onOptimizePrompt={onOptimizePrompt}
          canOptimizePrompt={canOptimizePrompt}
          optimizingPrompt={optimizingPrompt}
          optimizationDirection={optimizationDirection}
          optimizationTargetLanguage={optimizationTargetLanguage}
          optimizationEstimatedCredits={optimizationEstimatedCredits}
          onOptimizationSettingsChange={onOptimizationSettingsChange}
          promptModelOptions={promptModelOptions}
          selectedPromptModelConfigId={selectedPromptModelConfigId}
          onPromptModelChange={onPromptModelChange}
          onClearWorkspace={onClearWorkspace}
          onClearAllWorkspaces={onClearAllWorkspaces}
          canClearWorkspace={canClearWorkspace}
          onSubmitPreview={onSubmitPreview}
        />
        <PromptOptimizationProposal
          proposal={optimizationProposal}
          onAccept={onAcceptOptimization}
          onReject={onRejectOptimization}
        />
        {optimizationUndoAvailable && (
          <button type="button" className="btn-secondary btn-sm justify-self-start" onClick={onUndoOptimization}>
            <Undo2 size={14} aria-hidden="true" /> 撤销上次优化
          </button>
        )}
        <PromptOptimizationHistory />
      </div>
    );
  }

  return (
    <div className="grid gap-3">
      {generationModelSelector}
      {productVideoStrategySelector}
      <EditBrief
        category={category}
        isImageEditMode={isImageEditMode}
        subjectMode={subjectMode}
        portraitGenerationMode={portraitGenerationMode}
        productGenerationMode={productGenerationMode}
        readySteps={readySteps}
        creationModeLabel={creationModeLabel}
        onSubjectModeChange={onSubjectModeChange}
      />

      <EditPromptPanel
        prompt={prompt}
        placeholder={placeholder}
        onPromptChange={onPromptChange}
        onPromptDirty={onPromptDirty}
        onOptimizePrompt={onOptimizePrompt}
        canOptimizePrompt={canOptimizePrompt}
        optimizingPrompt={optimizingPrompt}
        optimizationDirection={optimizationDirection}
        optimizationTargetLanguage={optimizationTargetLanguage}
        optimizationEstimatedCredits={optimizationEstimatedCredits}
        onOptimizationSettingsChange={onOptimizationSettingsChange}
        promptModelOptions={promptModelOptions}
        selectedPromptModelConfigId={selectedPromptModelConfigId}
        onPromptModelChange={onPromptModelChange}
        onClearWorkspace={onClearWorkspace}
        onClearAllWorkspaces={onClearAllWorkspaces}
        canClearWorkspace={canClearWorkspace}
        onSubmitPreview={onSubmitPreview}
      />

      <PromptOptimizationProposal
        proposal={optimizationProposal}
        onAccept={onAcceptOptimization}
        onReject={onRejectOptimization}
      />
      {optimizationUndoAvailable && (
        <button type="button" className="btn-secondary btn-sm justify-self-start" onClick={onUndoOptimization}>
          <Undo2 size={14} aria-hidden="true" /> 撤销上次优化
        </button>
      )}
      <PromptOptimizationHistory />

      <EditAssistPanels
        isImageEditMode={isImageEditMode}
        portraitGenerationMode={portraitGenerationMode}
        productGenerationMode={productGenerationMode}
        editStyleKeys={editStyleKeys}
        onRecompose={onRecompose}
      />
    </div>
  );
}
