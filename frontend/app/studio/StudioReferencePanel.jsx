"use client";

import { ReferenceAssetPreview } from "./StudioMedia.jsx";
import { assetDims, selectedLabel } from "./helpers";

function referencePreviewFrameProps(asset) {
  const dims = assetDims(asset);
  if (!dims) return { className: "relative aspect-video" };
  const ratio = dims.width / dims.height;
  const heightClass = ratio < 0.85
    ? "min-h-[240px] max-h-[360px] sm:max-h-[420px]"
    : ratio > 1.9
      ? "min-h-[140px] max-h-[240px]"
      : "min-h-[180px] max-h-[320px]";
  return {
    className: `relative w-full ${heightClass}`,
    style: { aspectRatio: `${dims.width} / ${dims.height}` },
  };
}

export default function StudioReferencePanel({
  category,
  creationMode,
  imageEditProductMode = false,
  editSubjectMode = "general",
  isEditMode = false,
  selected,
  productAsset,
  url,
  setUrl,
  parsing,
  uploading,
  productBusy = false,
  assets,
  refOpen,
  setRefOpen,
  reversing,
  reverseEnabled,
  selectedReverseCost,
  selectedReverseCostLabel,
  videoAnalysisPreset,
  videoAnalysisPresets = [],
  reverseVideoAnalysis = null,
  setVideoAnalysisPreset,
  imageUploadInputRef,
  productUploadInputRef,
  videoUploadInputRef,
  onClear,
  onClearProductAsset,
  onParse,
  onUploadImage,
  onUploadProductImage,
  onUploadVideo,
  onPickAsset,
  onReverse,
}) {
  const isImageEditMode = creationMode === "image_edit";
  const directProductVideoMode = creationMode === "video";
  const subjectMode = directProductVideoMode && productAsset
    ? "product"
    : creationMode === "video_edit"
    ? (editSubjectMode === "portrait" ? "portrait" : "product")
    : imageEditProductMode
      ? (editSubjectMode === "portrait" ? "portrait" : "product")
      : "general";
  const productGenerationMode = subjectMode === "product";
  const portraitGenerationMode = subjectMode === "portrait";
  const productSubjectMode = productGenerationMode || directProductVideoMode;
  const productActionBusy = Boolean(uploading || productBusy);
  const imageUploadTargetsProduct = isImageEditMode && productGenerationMode && !productAsset;
  const imageUploadLabel = imageUploadTargetsProduct
    ? "上传产品主体"
    : isImageEditMode
      ? "上传风格参考图"
      : directProductVideoMode
      ? "上传图片参考"
      : "上传图片";
  const modeTitle = isEditMode
    ? (creationMode === "video_edit" ? (portraitGenerationMode ? "视频人物重构" : "图生视频重构") : "图片编辑")
    : (category === "video" ? "文生视频" : "链接反推");
  const styleTitle = isImageEditMode || directProductVideoMode ? "可选风格参考" : (isEditMode ? "风格参考" : "参考素材");
  const styleDescription = isImageEditMode
    ? (portraitGenerationMode ? "可选：反推另一张图/视频的场景、光线、妆造和画面风格" : "可选：反推另一张图的场景、构图、光线和广告质感")
    : directProductVideoMode
    ? "可选：仅用于反推场景、动作、镜头和光影，不会替换上方产品主体"
    : isEditMode
    ? (portraitGenerationMode ? "目标视频/风格参考：只迁移动作、镜头、场景和画面质感，不保证逐帧换脸" : "用于反推场景、构图、光线和广告质感")
    : (category === "video" ? "上传视频或粘贴链接做反推" : "上传图片或粘贴链接做反推");
  const emptyStyleTitle = isImageEditMode
    ? "添加可选风格参考"
    : (portraitGenerationMode ? "上传目标视频 / 风格参考" : category === "video" ? "上传可选视频 / 图片参考" : "上传图片参考");
  const emptyStyleHint = isImageEditMode
    ? "不加也能编辑；需要同款风格时再上传或粘贴链接"
    : directProductVideoMode
    ? "不上传也能生成；这里的素材只提供风格和运动信息"
    : isEditMode
    ? (portraitGenerationMode ? "上传目标视频或粘贴链接，先反推镜头和风格，再上传人物照片做重构" : "也可粘贴小红书、抖音或网页链接抓取素材")
    : (category === "video" ? "点击上传视频，下方也可改传图片或粘贴链接" : "点击上传图片，下方也可粘贴链接抓取素材");
  const productTitle = portraitGenerationMode ? "人物照片" : productSubjectMode ? "产品主体图片" : (isImageEditMode ? "编辑源图片" : "产品主体");
  const productHint = isImageEditMode
    ? (portraitGenerationMode
        ? "上传需要保留身份的人像照片，生成时强保护五官、脸型、发型和人物身份"
        : productSubjectMode
        ? "上传产品图作为唯一产品身份，生成时强保护Logo、包装和细节"
        : "上传需要被编辑的图片，未要求修改的内容默认保留")
    : (portraitGenerationMode
        ? "上传要生成进目标视频风格的人物照片，参考视频只提供动作、镜头和风格"
        : "上传产品图作为视频中的唯一商品主体，不作为风格参考；保留Logo、包装、颜色、形状和文字标识");

  return (
    <aside className="relative min-w-0 overflow-hidden rounded-xl3 border border-iris/35 bg-gradient-to-b from-iris/20 via-base2/80 to-rose/10 p-3 shadow-glow-sm">
      <div className="pointer-events-none absolute -right-16 -top-16 h-32 w-32 rounded-full bg-rose/25 blur-3xl" />
      <div className="relative">
        <div className="mb-3 flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-display font-semibold text-iris-400">{directProductVideoMode ? "产品与参考" : "参考素材"}</p>
            <h3 className="mt-1 text-lg font-display font-semibold text-snow">
              {modeTitle}
            </h3>
          </div>
          {selected && (
            <button type="button" onClick={onClear} className="chip px-2 py-1">
              清除参考
            </button>
          )}
        </div>

        {(isImageEditMode || directProductVideoMode) && (
          <div className="mb-3 rounded-xl2 border border-aqua/30 bg-aqua/10 p-2">
            <div className="mb-2 flex items-start justify-between gap-2">
              <div>
                <p className="text-sm font-display font-semibold text-snow">{productTitle}</p>
                <p className="mt-0.5 text-[11px] text-fog">{productHint}</p>
              </div>
              {productAsset && (
                <button type="button" onClick={onClearProductAsset} className="chip px-2 py-1">
                  清除
                </button>
              )}
            </div>
            <div className={`overflow-hidden rounded-xl2 border bg-black/20 ${
              productAsset ? "border-aqua/60" : "border-dashed border-aqua/30"
            }`}>
              <div {...referencePreviewFrameProps(productAsset)}>
                {productAsset ? (
                  <>
                    <ReferenceAssetPreview asset={productAsset} />
                    <span className="badge absolute left-2 top-2 bg-aqua/90 text-black">
                      {portraitGenerationMode ? "人像" : productSubjectMode ? "产品主体" : "编辑源"}
                    </span>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => productUploadInputRef.current?.click()}
                    disabled={productActionBusy}
                    className="flex h-full w-full flex-col items-center justify-center gap-3 px-4 text-center transition hover:bg-white/[0.03]"
                  >
                    <span className="flex h-11 w-11 items-center justify-center rounded-full bg-aqua text-lg text-black shadow-glow-sm">+</span>
                    <div>
                      <span className="text-sm font-display font-medium text-snow">
                        {portraitGenerationMode ? "上传人物照片" : productSubjectMode ? "上传产品图片" : "上传要编辑的图片"}
                      </span>
                      <span className="mt-1 block text-xs text-fog">
                        {portraitGenerationMode
                          ? "作为人像身份参考"
                          : directProductVideoMode
                            ? "作为视频唯一产品主体，不是风格参考"
                            : productSubjectMode
                              ? "作为高保真产品生成源"
                              : "作为图片编辑源"}
                      </span>
                    </div>
                  </button>
                )}
              </div>
            </div>
            {productAsset && (
              <div className="mt-2 min-w-0 rounded-xl border border-line bg-white/5 px-3 py-2 text-xs text-fog">
                <div className="truncate text-mist">{selectedLabel(productAsset)}</div>
                <div className="mt-1 text-fog">
                  {portraitGenerationMode
                    ? "生成时会锁定人物身份，只迁移参考素材的场景、光线、构图和风格。"
                    : productSubjectMode
                    ? "生成时会锁定产品身份，只迁移或生成广告场景、光线、构图和质感。"
                    : "生成时会按提示词编辑这张图，未指定修改的部分默认保留。"}
                </div>
              </div>
            )}
            <button
              type="button"
              onClick={() => productUploadInputRef.current?.click()}
              disabled={productActionBusy}
              className="btn-secondary btn-sm mt-2 w-full justify-center border-aqua/30 bg-aqua/10 text-snow"
            >
              {uploading ? "上传中…" : productBusy ? "识别中…" : productAsset
                ? (portraitGenerationMode ? "替换人物照片" : productSubjectMode ? "替换产品图片" : "替换编辑源图片")
                : (portraitGenerationMode ? "上传人物照片" : productSubjectMode ? "上传产品图片" : "上传编辑源图片")}
            </button>
          </div>
        )}

        <div className="mb-3 min-w-0 rounded-xl2 border border-line bg-black/15 p-2">
          <div className="mb-2 flex min-w-0 items-center justify-between gap-2">
            <div className="min-w-0">
              <p className="text-sm font-display font-semibold text-snow">{styleTitle}</p>
              <p className="mt-0.5 text-[11px] text-fog">{styleDescription}</p>
            </div>
            {selected && (
              <span className="badge shrink-0 bg-iris/25 text-mist">
                {selected.type === "video" ? "视频" : "图片"}
              </span>
            )}
          </div>
          <div className={`overflow-hidden rounded-xl2 border bg-black/20 ${
            selected ? "border-iris/60" : "border-dashed border-line2"
          }`}>
            <div {...referencePreviewFrameProps(selected)}>
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
                      {emptyStyleTitle}
                    </span>
                    <span className="mt-1 block text-xs text-fog">
                      {emptyStyleHint}
                    </span>
                  </div>
                </button>
              )}
            </div>
          </div>

          {selected && (
            <div className="mt-2 min-w-0 rounded-xl border border-line bg-white/5 px-3 py-2 text-xs text-fog">
              <div className="truncate text-mist">{selectedLabel(selected)}</div>
              {selected?.type === "image" && selected?.url?.includes("/api/uploads/upload/") && (
                <div className="mt-1 text-fog">
                  {isEditMode || directProductVideoMode
                    ? "仅作为风格参考，不会覆盖产品主体。"
                    : "可直接作为编辑源生成。"}
                </div>
              )}
              {selected?.type === "video" && selected?.url?.includes("/api/uploads/upload_video/") && (
                <div className="mt-1 text-fog">会抽取关键帧理解内容，并提取镜头节奏和画面风格。</div>
              )}
            </div>
          )}
        </div>

        {isEditMode && !isImageEditMode && (
          <div className="mb-3 rounded-xl2 border border-aqua/30 bg-aqua/10 p-2">
            <div className="mb-2 flex items-start justify-between gap-2">
              <div>
                <p className="text-sm font-display font-semibold text-snow">{productTitle}</p>
                <p className="mt-0.5 text-[11px] text-fog">{productHint}</p>
              </div>
              {productAsset && (
                <button type="button" onClick={onClearProductAsset} className="chip px-2 py-1">
                  清除
                </button>
              )}
            </div>
            <div className={`overflow-hidden rounded-xl2 border bg-black/20 ${
              productAsset ? "border-aqua/60" : "border-dashed border-aqua/30"
            }`}>
              <div {...referencePreviewFrameProps(productAsset)}>
                {productAsset ? (
                  <>
                    <ReferenceAssetPreview asset={productAsset} />
                    <span className="badge absolute left-2 top-2 bg-aqua/90 text-black">
                      {portraitGenerationMode ? "人物照片" : isImageEditMode ? "编辑源" : "产品主体"}
                    </span>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => productUploadInputRef.current?.click()}
                    disabled={productActionBusy}
                    className="flex h-full w-full flex-col items-center justify-center gap-3 px-4 text-center transition hover:bg-white/[0.03]"
                  >
                    <span className="flex h-11 w-11 items-center justify-center rounded-full bg-aqua text-lg text-black shadow-glow-sm">+</span>
                    <div>
                      <span className="text-sm font-display font-medium text-snow">
                        {portraitGenerationMode ? "上传人物照片" : isImageEditMode ? "上传要编辑的图片" : "上传产品图片"}
                      </span>
                      <span className="mt-1 block text-xs text-fog">
                        {portraitGenerationMode
                          ? "作为视频人物身份参考"
                          : isImageEditMode
                          ? "作为图片编辑源"
                          : "作为产品身份参考，不限定视频首帧"}
                      </span>
                    </div>
                  </button>
                )}
              </div>
            </div>
            {productAsset && (
              <div className="mt-2 min-w-0 rounded-xl border border-line bg-white/5 px-3 py-2 text-xs text-fog">
                <div className="truncate text-mist">{selectedLabel(productAsset)}</div>
                <div className="mt-1 text-fog">
                  {isImageEditMode
                    ? "生成时会按提示词编辑这张图，未指定修改的部分默认保留。"
                    : portraitGenerationMode
                    ? "生成时保留这张人物照片的身份，目标视频只迁移动作、镜头和风格；当前不是逐帧换脸。"
                    : "生成时以这张图片锁定同一产品身份，场景、动作和首尾画面由提示词决定。"}
                </div>
              </div>
            )}
            <button
              type="button"
              onClick={() => productUploadInputRef.current?.click()}
              disabled={productActionBusy}
              className="btn-secondary btn-sm mt-2 w-full justify-center border-aqua/30 bg-aqua/10 text-snow"
            >
              {uploading ? "上传中…" : productBusy ? "识别中…" : productAsset
                ? (portraitGenerationMode ? "替换人物照片" : isImageEditMode ? "替换编辑源图片" : "替换产品图片")
                : (portraitGenerationMode ? "上传人物照片" : isImageEditMode ? "上传编辑源图片" : "上传产品图片")}
            </button>
          </div>
        )}

        <div className="space-y-2">
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-2">
            <input
              className="input min-w-0 px-3 py-2 text-xs"
              placeholder={isImageEditMode ? "可选：粘贴风格参考链接…" : "粘贴小红书 / 抖音 / X / 网页链接…"}
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
              onClick={() => (imageUploadTargetsProduct ? productUploadInputRef : imageUploadInputRef).current?.click()}
              disabled={imageUploadTargetsProduct ? productActionBusy : uploading}
              className="btn-secondary btn-sm justify-center border-line2 bg-white/[0.08]"
            >
              {uploading ? "上传中…" : imageUploadLabel}
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
              {reverseVideoAnalysis?.source && (
                <div className="mt-2 text-xs text-mist">
                  已识别 {reverseVideoAnalysis.source.ratio || "未知画幅"}
                  {Number.isFinite(Number(reverseVideoAnalysis.source.duration_seconds))
                    ? ` · ${Number(reverseVideoAnalysis.source.duration_seconds).toFixed(2)}s`
                    : ""}
                  {Array.isArray(reverseVideoAnalysis.shots) && reverseVideoAnalysis.shots.length > 0
                    ? ` · ${reverseVideoAnalysis.shots.length} 个镜头`
                    : ""}
                </div>
              )}
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
              {reversing ? "反推中…" : `${isImageEditMode ? "反推可选风格" : "反推提示词"}${selected && selectedReverseCost ? ` · ${selectedReverseCostLabel}` : ""}`}
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
          {(isEditMode || directProductVideoMode) && (
            <input
              ref={productUploadInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={(e) => onUploadProductImage(e.target.files?.[0])}
            />
          )}
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
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {assets.map((asset, i) => (
                  <button
                    key={i}
                    onClick={() => onPickAsset(asset)}
                    className={`relative aspect-square overflow-hidden rounded-lg border transition ${
                      selected === asset ? "border-iris ring-2 ring-iris/40" : "border-line hover:border-line2"
                    }`}
                  >
                    <ReferenceAssetPreview asset={asset} compact />
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
