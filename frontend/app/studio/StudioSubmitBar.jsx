"use client";

import { formatDuration } from "./helpers";
import { generationSubmitDisabled } from "./taskConcurrency";

export default function StudioSubmitBar({
  variant = "desktop",
  category,
  videoFinalCost,
  estCost,
  imageCount,
  videoDuration,
  vResolution,
  submit,
  missingRequiredSource,
  missingRequiredSourceLabel,
  videoModelSwitchRequired,
  videoModelSwitchMessage,
  onVideoModelSwitchRequired,
  structuredDirty,
  submitting,
  parsing,
  uploadBlocked,
  reversing,
  productProfiling,
  task,
  currentModelEnabled,
  running,
  submitLabel,
}) {
  const isMobile = variant === "mobile";
  const estimatedCredits = category === "video" ? videoFinalCost : estCost;
  const imageUnitCredits = category === "image" && imageCount > 0
    ? estCost / imageCount
    : 0;
  const submitDisabled = missingRequiredSource || structuredDirty || generationSubmitDisabled({
    submitting,
    busy: parsing || uploadBlocked || reversing || productProfiling,
    currentTask: task,
    nextCategory: category,
    currentModelEnabled,
  });

  const handleSubmit = () => {
    if (videoModelSwitchRequired) {
      onVideoModelSwitchRequired?.();
      return;
    }
    submit(category === "video" ? "final" : "preview");
  };

  return (
    <div
      className={isMobile
        ? "fixed inset-x-0 bottom-0 z-30 flex min-w-0 items-center justify-between gap-3 border-t border-line bg-base/90 px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-pop backdrop-blur-xl lg:hidden"
        : "mt-3 hidden min-w-0 items-center justify-between gap-3 px-1 lg:flex"
      }
    >
      <div className="min-w-0" aria-live="polite">
        {estCost ? (
          <>
            <div className="flex min-w-0 items-baseline gap-1 whitespace-nowrap">
              <span className="text-[11px] text-fog">预计消耗</span>
              <strong className="font-display text-base font-semibold text-snow">{estimatedCredits}</strong>
              <span className="text-xs font-medium text-mist">积分</span>
            </div>
            <p className="mt-0.5 truncate text-[10px] leading-snug text-fog">
              {category === "video"
                ? `${formatDuration(videoDuration)} · ${vResolution}`
                : `${imageCount} 张 × ${imageUnitCredits} 积分/张 · 可连续提交`}
            </p>
          </>
        ) : (
          <p className="text-xs leading-snug text-fog">费用将在提交时按当前参数预估并冻结</p>
        )}
      </div>
      <button
        onClick={handleSubmit}
        disabled={submitDisabled}
        title={missingRequiredSource
          ? `${missingRequiredSourceLabel}后再生成`
          : videoModelSwitchRequired
            ? videoModelSwitchMessage
            : structuredDirty
              ? "请先应用结构修改或撤销结构修改"
              : uploadBlocked
                ? "本次生成需要的素材仍在上传中"
                : undefined}
        className="btn-primary btn-lg min-w-28 shrink-0 px-4 sm:min-w-32 sm:px-6"
      >
        {(submitting || running || uploadBlocked) && (
          <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-white/35 border-t-white" aria-hidden />
        )}
        {missingRequiredSource
          ? missingRequiredSourceLabel
          : videoModelSwitchRequired
            ? "请切换视频模型"
            : uploadBlocked
              ? "素材上传中"
              : submitLabel}
      </button>
    </div>
  );
}
