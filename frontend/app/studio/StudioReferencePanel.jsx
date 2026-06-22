"use client";

import { ReferenceAssetPreview } from "./StudioMedia";
import { selectedLabel } from "./helpers";

export default function StudioReferencePanel({
  category,
  selected,
  url,
  setUrl,
  parsing,
  uploading,
  assets,
  refOpen,
  setRefOpen,
  reversing,
  reverseEnabled,
  selectedReverseCost,
  selectedReverseCostLabel,
  videoAnalysisPreset,
  videoAnalysisPresets = [],
  setVideoAnalysisPreset,
  imageUploadInputRef,
  videoUploadInputRef,
  onClear,
  onParse,
  onUploadImage,
  onUploadVideo,
  onPickAsset,
  onReverse,
}) {
  return (
    <aside className="relative overflow-hidden rounded-xl3 border border-iris/35 bg-gradient-to-b from-iris/20 via-base2/80 to-rose/10 p-3 shadow-glow-sm">
      <div className="pointer-events-none absolute -right-16 -top-16 h-32 w-32 rounded-full bg-rose/25 blur-3xl" />
      <div className="relative">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-display font-semibold text-iris-400">参考素材</p>
            <h3 className="mt-1 text-lg font-display font-semibold text-snow">
              {category === "video" ? "视频参考 / 视频编辑" : "链接反推 / 图片编辑"}
            </h3>
          </div>
          {selected && (
            <button type="button" onClick={onClear} className="chip px-2 py-1">
              清除
            </button>
          )}
        </div>

        <div className={`mb-3 overflow-hidden rounded-xl2 border bg-black/20 ${
          selected ? "border-iris/60" : "border-dashed border-line2"
        }`}>
          <div className="relative aspect-video">
            {selected ? (
              <>
                <ReferenceAssetPreview asset={selected} />
                <span className="badge absolute left-2 top-2 bg-black/70 text-white">
                  {selected.type === "video" ? "视频参考" : "图片参考"}
                </span>
              </>
            ) : (
              <button
                type="button"
                onClick={() => (category === "video" ? videoUploadInputRef : imageUploadInputRef).current?.click()}
                className="flex h-full w-full flex-col items-center justify-center gap-3 px-4 text-center transition hover:bg-white/[0.03]"
              >
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-brand text-lg text-white shadow-glow-sm">+</span>
                <div>
                  <span className="text-sm font-display font-medium text-snow">
                    {category === "video" ? "上传视频或首帧图" : "上传参考图片"}
                  </span>
                  <span className="mt-1 block text-xs text-fog">
                    {category === "video" ? "点击上传视频，下方也可改传首帧图" : "点击上传图片，下方也可粘贴链接抓取素材"}
                  </span>
                </div>
              </button>
            )}
          </div>
        </div>

        {selected && (
          <div className="mb-3 min-w-0 rounded-xl border border-line bg-white/5 px-3 py-2 text-xs text-fog">
            <div className="truncate text-mist">{selectedLabel(selected)}</div>
            {selected?.type === "image" && selected?.url?.includes("/api/uploads/upload/") && (
              <div className="mt-1 text-fog">可直接作为编辑源生成。</div>
            )}
            {selected?.type === "video" && selected?.url?.includes("/api/uploads/upload_video/") && (
              <div className="mt-1 text-fog">会抽取关键帧理解内容，并以首帧驱动视频生成。</div>
            )}
          </div>
        )}

        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_auto] gap-2">
            <input
              className="input px-3 py-2 text-xs"
              placeholder="粘贴小红书 / 抖音 / 网页链接…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onParse()}
            />
            <button onClick={onParse} disabled={parsing || !url.trim()} className="btn-primary btn-sm whitespace-nowrap">
              {parsing ? "抓取中" : "抓取"}
            </button>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => imageUploadInputRef.current?.click()}
              disabled={uploading}
              className="btn-secondary btn-sm justify-center border-line2 bg-white/[0.08]"
            >
              {uploading ? "上传中…" : "上传图片"}
            </button>
            <button
              type="button"
              onClick={() => videoUploadInputRef.current?.click()}
              disabled={uploading}
              className={`btn-sm justify-center rounded-full font-display font-medium transition ${
                category === "video"
                    ? "bg-brand text-white shadow-glow-sm hover:brightness-110 active:scale-[0.98]"
                    : "border border-line2 bg-white/[0.08] text-snow hover:bg-white/10"
              }`}
            >
              {uploading ? "上传中…" : "上传视频"}
            </button>
          </div>
          {selected?.type === "video" && videoAnalysisPresets.length > 0 && (
            <div className="rounded-xl border border-line bg-black/15 p-2">
              <div className="mb-2 flex items-center justify-between gap-2 text-xs">
                <span className="font-display font-medium text-mist">分析精度</span>
                <span className="text-fog">优先镜头切换关键帧</span>
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {videoAnalysisPresets.map((preset) => (
                  <button
                    key={preset.key}
                    type="button"
                    onClick={() => setVideoAnalysisPreset?.(preset.key)}
                    className={`rounded-lg border px-2 py-2 text-left transition ${
                      videoAnalysisPreset === preset.key
                        ? "border-iris bg-iris/25 text-snow shadow-glow-sm"
                        : "border-line bg-white/[0.04] text-fog hover:border-line2 hover:text-mist"
                    }`}
                  >
                    <span className="block text-xs font-display font-semibold">{preset.label}</span>
                    <span className="mt-1 block text-[10px] leading-snug">
                      {preset.short_range} / {preset.long_range}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
          <div className="grid grid-cols-1 gap-2">
            <button
              type="button"
              onClick={onReverse}
              disabled={!selected || !reverseEnabled || reversing}
              className={`btn-sm justify-center rounded-full font-display font-medium transition ${
                selected && reverseEnabled
                  ? "bg-brand text-white shadow-glow-sm hover:brightness-110 active:scale-[0.98]"
                  : "cursor-not-allowed border border-line bg-white/5 text-fog"
              }`}
            >
              {reversing ? "反推中…" : `反推提示词${selected && selectedReverseCost ? ` · ${selectedReverseCostLabel}` : ""}`}
            </button>
          </div>
          <input
            ref={imageUploadInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            className="hidden"
            onChange={(e) => onUploadImage(e.target.files?.[0])}
          />
          <input
            ref={videoUploadInputRef}
            type="file"
            accept="video/mp4,video/quicktime,video/webm"
            className="hidden"
            onChange={(e) => onUploadVideo(e.target.files?.[0])}
          />
        </div>

        {selected && !reverseEnabled && (
          <p className="mt-3 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
            反推功能已关闭，当前素材仍会作为参考。
          </p>
        )}

        {assets.length > 0 && (
          <div className="mt-3">
            <div className="mb-2 flex items-center justify-between text-xs">
              <span className="text-fog">已抓取素材</span>
              <button type="button" onClick={() => setRefOpen((v) => !v)} className="text-mist hover:text-snow">
                {refOpen ? "收起" : "展开"}
              </button>
            </div>
            {refOpen && (
              <div className="grid grid-cols-4 gap-2">
                {assets.map((asset, i) => (
                  <button
                    key={i}
                    onClick={() => onPickAsset(asset)}
                    className={`relative aspect-square overflow-hidden rounded-lg border transition ${
                      selected === asset ? "border-iris ring-2 ring-iris/40" : "border-line hover:border-line2"
                    }`}
                  >
                    <ReferenceAssetPreview asset={asset} />
                    <span className="badge absolute left-1 top-1 bg-black/60 text-[10px] text-white">{asset.type}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
