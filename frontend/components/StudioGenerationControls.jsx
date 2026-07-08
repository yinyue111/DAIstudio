"use client";

import {
  IMAGE_QUALITY_PRESETS,
  VIDEO_DURATION_PRESETS,
  VIDEO_QUALITIES,
} from "../app/studio/constants";
import { boundedImageCount, boundedVideoDuration, formatDuration } from "../app/studio/helpers";

export default function StudioGenerationControls({
  category,
  ratioOptions,
  ratio,
  onRatioChange,
  imageQuality,
  onImageQualityChange,
  currentImageSize,
  maxImageN,
  imageCount,
  n,
  onImageCountChange,
  maxVideoDuration,
  videoDuration,
  vDuration,
  onVideoDurationChange,
  vResolution,
  onVideoResolutionChange,
  isEditMode = false,
  productGenerationMode = false,
  portraitGenerationMode = false,
  videoProductLockMode = "locked",
  onVideoProductLockModeChange,
  showNegative,
  onToggleNegative,
  seed,
  onSeedChange,
  editMaskMode = "protect_subject",
  onEditMaskModeChange,
  negative,
  onNegativeChange,
  onNegativeTouched,
  submitBar,
}) {
  const controlCardClass = "min-w-0 rounded-xl border border-line bg-white/[0.03] px-3 py-2";
  const controlLabelClass = "block shrink-0 text-xs leading-none text-fog";
  const scrollPillRowClass = "no-scrollbar flex min-w-0 gap-1 overflow-x-auto pb-1";
  const editMaskOptions = [
    {
      key: "protect_subject",
      label: "主体保护",
      hint: "透明 PNG 可精确保护主体；普通图片会尝试识别主体，低置信时不发送蒙版，可手动改用中心保护。",
      status: "自动识别主体",
    },
    {
      key: "center_box",
      label: "中心保护",
      hint: "仅保护画面中心区域，适合主体居中但自动识别不稳定的图片。",
      status: "中心兼容保护",
    },
    {
      key: "off",
      label: "整图编辑",
      hint: "关闭自动蒙版，适合需要整体风格化或模型不支持 mask 的网关。",
      status: "整图编辑",
    },
  ];
  const editMaskStatus = editMaskOptions.find((option) => option.key === editMaskMode)?.status || "自动识别主体";
  const applyVideoProductMode = (mode) => {
    onVideoProductLockModeChange?.(mode);
    if (mode === "locked") {
      const current = boundedVideoDuration(vDuration, maxVideoDuration);
      if (current > 5) onVideoDurationChange?.(5);
    }
  };

  return (
    <>
      <div className="grid min-w-0 gap-3 px-1 py-1">
        <div className="min-w-0">
          <span className="text-xs text-fog">比例</span>
          <div className="no-scrollbar mt-1 flex gap-1 overflow-x-auto pb-1">
            {ratioOptions.map((r) => (
              <button
                key={r.key}
                type="button"
                onClick={() => onRatioChange(r.key)}
                title={r.hint}
                className={`group flex shrink-0 flex-col items-center gap-1 rounded-lg border px-2 py-1.5 transition ${
                  ratio === r.key ? "border-iris bg-iris/15" : "border-line hover:border-line2"
                }`}
              >
                <span
                  className={`block rounded-[3px] ${ratio === r.key ? "bg-iris-400" : "bg-fog"}`}
                  style={{ width: 18 * (r.w >= r.h ? 1 : r.w / r.h), height: 18 * (r.h >= r.w ? 1 : r.h / r.w) }}
                />
                <span className={`text-[10px] ${ratio === r.key ? "text-snow" : "text-fog"}`}>{r.label}</span>
              </button>
            ))}
          </div>
        </div>

        {category === "image" ? (
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <div className={controlCardClass}>
              <div className="flex min-w-0 items-center justify-between gap-3">
                <span className={controlLabelClass}>质量</span>
                <span className="min-w-0 truncate text-right text-xs text-fog">{currentImageSize}</span>
              </div>
              <div className={`${scrollPillRowClass} mt-2`}>
                {IMAGE_QUALITY_PRESETS.map((q) => (
                  <button
                    key={q.key}
                    type="button"
                    onClick={() => onImageQualityChange(q.key)}
                    title={q.hint}
                    className={`chip shrink-0 ${imageQuality === q.key ? "chip-active" : ""}`}
                  >
                    {q.label}
                  </button>
                ))}
              </div>
            </div>
            <div className={controlCardClass}>
              <div className="flex min-w-0 items-center justify-between gap-3">
                <span className={controlLabelClass}>数量</span>
                <span className="text-xs text-fog">最多 {maxImageN} 张</span>
              </div>
              <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_4.5rem] items-center gap-2">
                <div className={scrollPillRowClass}>
                  {[1, 2, 4, 8].filter((v) => v <= maxImageN).map((v) => (
                    <button
                      key={v}
                      type="button"
                      onClick={() => onImageCountChange(v)}
                      className={`chip shrink-0 ${imageCount === v ? "chip-active" : ""}`}
                    >
                      {v}
                    </button>
                  ))}
                </div>
                <input
                  className="input h-[38px] min-w-0 px-2 py-1 text-center text-xs"
                  type="number"
                  min="1"
                  max={maxImageN}
                  value={n}
                  onChange={(e) => onImageCountChange(e.target.value)}
                  onBlur={() => onImageCountChange(boundedImageCount(n, maxImageN))}
                  title={`最多 ${maxImageN} 张`}
                />
              </div>
            </div>
            {isEditMode && productGenerationMode && !portraitGenerationMode && (
              <div className={`${controlCardClass} sm:col-span-2`}>
                <div className="flex min-w-0 items-center justify-between gap-3">
                  <span className={controlLabelClass}>编辑保护</span>
                  <span className="min-w-0 truncate text-right text-xs text-fog">
                    {editMaskStatus}
                  </span>
                </div>
                <div className={`${scrollPillRowClass} mt-2`}>
                  {editMaskOptions.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => onEditMaskModeChange?.(option.key)}
                      title={option.hint}
                      className={`chip shrink-0 ${editMaskMode === option.key ? "chip-active" : ""}`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
            <div className={controlCardClass}>
              <div className="flex min-w-0 items-center justify-between gap-3">
                <span className={controlLabelClass}>时长</span>
                <span className="text-xs text-fog">最长 {formatDuration(maxVideoDuration)}</span>
              </div>
              <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_5.5rem] items-center gap-2">
                <div className={scrollPillRowClass}>
                  {VIDEO_DURATION_PRESETS.filter((p) => p.seconds <= maxVideoDuration).map((p) => (
                    <button
                      key={p.seconds}
                      type="button"
                      onClick={() => onVideoDurationChange(p.seconds)}
                      title={p.hint}
                      className={`chip shrink-0 ${videoDuration === p.seconds ? "chip-active" : ""}`}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <input
                  className="input h-[38px] min-w-0 px-2 py-1 text-center text-xs"
                  type="number"
                  min="1"
                  max={maxVideoDuration}
                  value={vDuration}
                  onChange={(e) => onVideoDurationChange(e.target.value)}
                  onBlur={() => onVideoDurationChange(boundedVideoDuration(vDuration, maxVideoDuration))}
                  title={`最长 ${formatDuration(maxVideoDuration)}`}
                />
              </div>
            </div>
            <div className={controlCardClass}>
              <span className={controlLabelClass}>质量</span>
              <div className={`${scrollPillRowClass} mt-2`}>
                {VIDEO_QUALITIES.map((q) => (
                  <button
                    key={q.key}
                    type="button"
                    onClick={() => onVideoResolutionChange(q.key)}
                    title={q.hint}
                    className={`chip shrink-0 ${vResolution === q.key ? "chip-active" : ""}`}
                  >
                    {q.label}
                  </button>
                ))}
              </div>
            </div>
            {isEditMode && productGenerationMode && !portraitGenerationMode && (
              <div className={`${controlCardClass} sm:col-span-2`}>
                <div className="flex min-w-0 items-center justify-between gap-3">
                  <span className={controlLabelClass}>产品运动</span>
                  <span className="min-w-0 truncate text-right text-xs text-fog">
                    {videoProductLockMode === "locked" ? "文字保真" : "自由运动"}
                  </span>
                </div>
                <div className={`${scrollPillRowClass} mt-2`}>
                  {[
                    { key: "locked", label: "文字保真", hint: "首末帧都使用产品图，并自动压到短时长，更稳保留包装、Logo 和文字。" },
                    { key: "free", label: "自由运动", hint: "只锚定首帧，更适合旋转、泼溅、推拉镜头和动态展示，但包装文字稳定性会下降。" },
                  ].map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => applyVideoProductMode(option.key)}
                      title={option.hint}
                      className={`chip shrink-0 ${videoProductLockMode === option.key ? "chip-active" : ""}`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-xs leading-relaxed text-fog">
                  包装小字多的产品建议使用高清正面图或透明 PNG；文字保真模式会使用更克制的镜头。自由运动模式下，快速旋转、泼溅和运动模糊会增加文字乱码概率。
                </p>
              </div>
            )}
          </div>
        )}

        <div className="grid min-w-0 gap-2 sm:grid-cols-[auto_minmax(12rem,20rem)]">
          <button type="button" onClick={onToggleNegative} className={`chip justify-center ${showNegative ? "chip-active" : ""}`}>
            负向词
          </button>
          {category === "image" && (
            <label className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-full border border-line bg-white/[0.03] px-3 py-1.5 text-xs text-fog">
              <span className="shrink-0">Seed</span>
              <input
                className="input h-[34px] min-w-0 rounded-full px-3 py-1 text-xs"
                placeholder="随机"
                value={seed}
                onChange={(e) => onSeedChange(e.target.value.replace(/[^0-9]/g, ""))}
              />
            </label>
          )}
        </div>
      </div>

      {showNegative && (
        <input
          className="input mt-2"
          placeholder="不想出现的元素：文字, 水印, 多余手指, 畸变…"
          value={negative}
          onChange={(e) => {
            onNegativeChange(e.target.value);
            onNegativeTouched(true);
          }}
        />
      )}

      {submitBar}
    </>
  );
}
