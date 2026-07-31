"use client";

import { useEffect, useState } from "react";
import { ReferenceAssetPreview } from "./StudioMedia.jsx";
import StudioModelSelector, { VIDEO_IMAGE_INPUT_MODE } from "./StudioModelSelector";
import StudioReverseIntentControls from "./StudioReverseIntentControls";
import StudioReverseBatchPanel from "./StudioReverseBatchPanel";
import StudioReverseSourcesEditor from "./StudioReverseSourcesEditor";
import StudioVideoAnalysisControls from "./StudioVideoAnalysisControls";
import { assetDims, selectedLabel } from "./helpers";

export function analysisModeLabel(mode) {
  return {
    keyframes: "多帧分析",
    multi_frame: "多帧分析",
    cover_fallback: "封面单帧",
    cover: "封面单帧",
    image_motion: "单图运动设计",
  }[mode] || "视频分析";
}

function confirmationTime(value) {
  if (!value) return "15 分钟内";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "15 分钟内";
  return `${date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 前`;
}

export function videoAnalysisEvidenceData(analysis) {
  if (!analysis || typeof analysis !== "object") return null;
  const source = analysis.source || {};
  const frames = Array.isArray(analysis.sampled_frames) ? analysis.sampled_frames : [];
  const gaps = Array.isArray(analysis.analysis_gaps) ? analysis.analysis_gaps : [];
  const audio = analysis.audio && typeof analysis.audio === "object" ? analysis.audio : {};
  const audioAnalyzed = ["analyzed", "partial"].includes(String(audio.status || ""))
    || (Array.isArray(audio.evidence) && audio.evidence.length > 0);
  const coverage = analysis.evidence_coverage && typeof analysis.evidence_coverage === "object"
    ? analysis.evidence_coverage
    : {};
  const numericFrameTimes = frames
    .map((frame) => Number(frame?.timestamp_seconds))
    .filter(Number.isFinite);
  const frameTimes = numericFrameTimes.map((time) => `${time.toFixed(1)}s`);
  const explicitStart = coverage.start_seconds == null ? NaN : Number(coverage.start_seconds);
  const explicitEnd = coverage.end_seconds == null ? NaN : Number(coverage.end_seconds);
  const coverageStart = Number.isFinite(explicitStart) ? explicitStart : numericFrameTimes[0];
  const coverageEnd = Number.isFinite(explicitEnd)
    ? explicitEnd
    : numericFrameTimes[numericFrameTimes.length - 1];
  const degradedReason = analysis.degraded_reason || analysis.fallback_reason || "";
  const gapTexts = gaps.map((gap) => {
    if (typeof gap === "string") return gap;
    if (!gap || typeof gap !== "object") return "";
    if (gap.message || gap.label || gap.field) return gap.message || gap.label || gap.field;
    const start = Number(gap.start_seconds);
    const end = Number(gap.end_seconds);
    if (Number.isFinite(start) && Number.isFinite(end)) {
      return `${start.toFixed(1)}s - ${end.toFixed(1)}s 未覆盖`;
    }
    if (Number.isFinite(start)) return `${start.toFixed(1)}s 后未覆盖`;
    if (Number.isFinite(end)) return `${end.toFixed(1)}s 前未覆盖`;
    return "存在未标注时间的分析缺口";
  }).filter(Boolean);
  return {
    source,
    frameTimes,
    coverageStart,
    coverageEnd,
    degradedReason,
    gapTexts,
    audioAnalyzed,
  };
}

function VideoAnalysisEvidence({ analysis }) {
  const evidence = videoAnalysisEvidenceData(analysis);
  if (!evidence) return null;
  const {
    source,
    frameTimes,
    coverageStart,
    coverageEnd,
    degradedReason,
    gapTexts,
    audioAnalyzed,
  } = evidence;

  return (
    <div className="mt-2 border-t border-line pt-2 text-[11px] leading-relaxed text-fog">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-mist">
        <span>{analysisModeLabel(analysis.analysis_mode)}</span>
        {frameTimes.length > 0 && <span>采样 {frameTimes.join(" / ")}</span>}
        {(Number.isFinite(coverageStart) || Number.isFinite(coverageEnd)) && (
          <span>
            证据覆盖 {Number(coverageStart || 0).toFixed(1)}s - {Number(coverageEnd || coverageStart || 0).toFixed(1)}s
          </span>
        )}
      </div>
      {degradedReason && <p className="mt-1 text-warn">降级原因：{degradedReason}</p>}
      {source.audio_analyzed === false && !audioAnalyzed && (
        <p className="mt-1 text-warn">音频未分析：旁白、音效和镜头音频提示不会自动补写。</p>
      )}
      {gapTexts.length > 0 && <p className="mt-1">分析缺口：{gapTexts.join("；")}</p>}
    </div>
  );
}

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

function ProductDetailImages({
  assets = [],
  enabled,
  limit = 0,
  validation,
  busy,
  uploadInputRef,
  onOpenPicker,
  onRemove,
  onMove,
}) {
  const safeLimit = Number.isInteger(limit) && limit > 0 ? limit : 0;
  const canAdd = enabled && assets.length < safeLimit && !busy;
  return (
    <div className="mt-3 border-t border-aqua/20 pt-3">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div>
          <p className="text-xs font-display font-semibold text-snow">产品细节图</p>
          <p className="mt-0.5 text-[11px] text-fog">
            {safeLimit > 0
              ? `按从左到右顺序提交，最多 ${safeLimit} 张，同一商品不同角度或局部。`
              : validation?.limit > 0
                ? "当前素材已占满模型的参考图名额。"
                : "当前模型不支持产品细节图。"}
          </p>
        </div>
        <span className="text-[11px] text-fog">{assets.length}/{Math.max(assets.length, safeLimit)}</span>
      </div>
      {assets.length > 0 && (
        <div className="mb-2 grid grid-cols-3 gap-2">
          {assets.map((asset, index) => (
            <div key={asset.asset_ref || asset.url || index} className="relative aspect-square overflow-hidden rounded-lg border border-aqua/35 bg-black/25">
              <ReferenceAssetPreview asset={asset} compact />
              <span className="badge absolute left-1 top-1 bg-black/70 text-[10px] text-white">{index + 1}</span>
              <div className="absolute inset-x-1 bottom-1 grid grid-cols-3 gap-1">
                <button type="button" onClick={() => onMove?.(index, -1)} disabled={busy || index === 0} className="rounded bg-black/75 py-1 text-[10px] text-white disabled:opacity-30" aria-label={`细节图 ${index + 1} 前移`}>←</button>
                <button type="button" onClick={() => onRemove?.(index)} disabled={busy} className="rounded bg-black/75 py-1 text-[10px] text-white disabled:opacity-30" aria-label={`移除细节图 ${index + 1}`}>×</button>
                <button type="button" onClick={() => onMove?.(index, 1)} disabled={busy || index === assets.length - 1} className="rounded bg-black/75 py-1 text-[10px] text-white disabled:opacity-30" aria-label={`细节图 ${index + 1} 后移`}>→</button>
              </div>
            </div>
          ))}
        </div>
      )}
      {!enabled && (
        <p className="mb-2 rounded-lg border border-warn/30 bg-warn/10 px-2.5 py-2 text-[11px] leading-relaxed text-warn">
          {validation?.message || "当前模型没有明确声明多参考图能力，产品细节图暂不可用。"}
        </p>
      )}
      <div className="grid grid-cols-2 gap-2">
        <button type="button" onClick={() => uploadInputRef.current?.click()} disabled={!canAdd} className="btn-secondary btn-sm justify-center">上传细节图</button>
        <button type="button" onClick={() => onOpenPicker?.("product_detail")} disabled={!canAdd} className="btn-secondary btn-sm justify-center">从我的资产选择</button>
      </div>
    </div>
  );
}

function LastFrameImage({
  asset,
  firstFrameReady,
  busy,
  uploadInputRef,
  onOpenPicker,
  onClear,
}) {
  const canChoose = firstFrameReady && !busy;
  return (
    <div className="mt-3 border-t border-iris/25 pt-3">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-display font-semibold text-snow">视频尾帧</p>
          <p className="mt-0.5 text-[11px] text-fog">可选，和上方首帧共同约束视频起止画面。</p>
        </div>
        {asset && (
          <button type="button" onClick={onClear} disabled={busy} className="chip px-2 py-1">
            清除
          </button>
        )}
      </div>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(160px,0.9fr)]">
        <div className={`relative aspect-video min-h-28 overflow-hidden rounded-lg border bg-black/20 ${
          asset ? "border-iris/60" : "border-dashed border-line2"
        }`}>
          {asset ? (
            <>
              <ReferenceAssetPreview asset={asset} />
              <span className="badge absolute left-2 top-2 bg-iris/80 text-white">尾帧</span>
            </>
          ) : (
            <button
              type="button"
              onClick={() => uploadInputRef.current?.click()}
              disabled={!canChoose}
              className="flex h-full w-full flex-col items-center justify-center gap-2 px-3 text-center transition hover:bg-white/[0.03] disabled:cursor-not-allowed disabled:opacity-45"
            >
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-iris text-base text-white">+</span>
              <span className="text-xs font-display font-medium text-mist">
                {firstFrameReady ? "添加尾帧图片" : "请先添加首帧图片"}
              </span>
            </button>
          )}
        </div>
        <div className="flex min-w-0 flex-col justify-center gap-2">
          {asset && <p className="truncate text-[11px] text-fog">{selectedLabel(asset)}</p>}
          <button type="button" onClick={() => uploadInputRef.current?.click()} disabled={!canChoose} className="btn-secondary btn-sm justify-center">
            {busy ? "上传中…" : asset ? "替换尾帧" : "上传尾帧"}
          </button>
          <button type="button" onClick={() => onOpenPicker?.("last_frame")} disabled={!canChoose} className="btn-secondary btn-sm justify-center">
            从我的资产选择
          </button>
        </div>
      </div>
    </div>
  );
}

export default function StudioReferencePanel({
  category,
  creationMode,
  imageEditProductMode = false,
  editSubjectMode = "general",
  isEditMode = false,
  selected,
  lastFrameAsset = null,
  firstLastFrameEnabled = false,
  videoRequiresFirstFrame = false,
  videoSubjectImageInputMode = "",
  productAsset,
  productDetailAssets = [],
  productDetailValidation = { ok: true, message: "" },
  productDetailLimit = 0,
  url,
  appliedUrl = "",
  setUrl,
  parsing,
  uploading,
  productBusy = false,
  productProfiling = false,
  profileOperation = null,
  parseNotice = null,
  assets,
  refOpen,
  setRefOpen,
  reversing,
  reverseEnabled,
  reverseImageCost,
  selectedReverseCost,
  selectedReverseCostLabel,
  reverseConfig,
  setReverseConfig,
  reverseSources = [],
  setReverseSources,
  videoAnalysisPreset,
  videoAnalysisPresets = [],
  reverseVideoAnalysis = null,
  reverseOperation = null,
  batchReverseAssets = [],
  reverseBatch = null,
  recentReverseBatches = [],
  reverseBatchBusyAction = "",
  reverseBatchCapabilities = null,
  reverseBatchEnabled = true,
  visionModelOptions = [],
  selectedVisionModelConfigId = null,
  onVisionModelChange,
  setVideoAnalysisPreset,
  imageUploadInputRef,
  productUploadInputRef,
  productDetailUploadInputRef,
  lastFrameUploadInputRef,
  videoUploadInputRef,
  onClear,
  onClearProductAsset,
  onParse,
  onUploadImage,
  onUploadProductImage,
  onUploadProductDetailImages,
  onUploadLastFrameImage,
  onOpenAssetPicker,
  onClearLastFrameAsset,
  onRemoveProductDetail,
  onMoveProductDetail,
  onUploadVideo,
  onPickAsset,
  onReverse,
  onConfirmCover,
  onCancelReverse,
  onToggleBatchAsset,
  onRemoveBatchAsset,
  onOpenBatchAssetPicker,
  onStartReverseBatch,
  onCancelReverseBatch,
  onOpenReverseBatch,
  onOpenReverseBatchItem,
  onRetryReverseBatchItem,
  onSaveReverseBatchRecipes,
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
  const subjectImageIsFirstFrame = videoSubjectImageInputMode === VIDEO_IMAGE_INPUT_MODE.FIRST_FRAME;
  const productActionBusy = Boolean(uploading || productBusy);
  const firstFrameInputEnabled = firstLastFrameEnabled || videoRequiresFirstFrame;
  const imageUploadTargetsProduct = isImageEditMode && productGenerationMode && !productAsset;
  const imageUploadLabel = imageUploadTargetsProduct
    ? "上传产品主体"
    : firstFrameInputEnabled
      ? (selected?.type === "image" ? "替换视频首帧" : "上传视频首帧")
    : isImageEditMode
      ? "上传风格参考图"
      : directProductVideoMode
      ? "上传图片参考"
      : "上传图片";
  const modeTitle = firstFrameInputEnabled
    ? (firstLastFrameEnabled ? "首尾帧视频" : "首帧图生视频")
    : isEditMode
    ? (creationMode === "video_edit" ? (portraitGenerationMode ? "视频人物重构" : "图生视频重构") : "图片编辑")
    : (category === "video" ? "文生视频" : "链接反推");
  const styleTitle = firstFrameInputEnabled
    ? "视频首帧"
    : isImageEditMode || directProductVideoMode ? "可选风格参考" : (isEditMode ? "风格参考" : "参考素材");
  const styleDescription = firstFrameInputEnabled
    ? firstLastFrameEnabled
      ? "上传图片作为视频起始画面；尾帧可在下方单独设置"
      : "当前模型必须上传图片作为视频首帧"
    : isImageEditMode
    ? (portraitGenerationMode ? "可选：反推另一张图/视频的场景、光线、妆造和画面风格" : "可选：反推另一张图的场景、构图、光线和广告质感")
    : directProductVideoMode
    ? "可选：仅用于反推场景、动作、镜头和光影，不会替换上方产品主体"
    : isEditMode
    ? (portraitGenerationMode ? "目标视频/风格参考：只迁移动作、镜头、场景和画面质感，不保证逐帧换脸" : "用于反推场景、构图、光线和广告质感")
    : (category === "video" ? "上传视频或粘贴链接做反推" : "上传图片或粘贴链接做反推");
  const emptyStyleTitle = firstFrameInputEnabled
    ? "上传视频首帧"
    : isImageEditMode
    ? "添加可选风格参考"
    : (portraitGenerationMode ? "上传目标视频 / 风格参考" : category === "video" ? "上传可选视频 / 图片参考" : "上传图片参考");
  const emptyStyleHint = firstFrameInputEnabled
    ? "支持 JPG、PNG、WebP 或 GIF"
    : isImageEditMode
    ? "不加也能编辑；需要同款风格时再上传或粘贴链接"
    : directProductVideoMode
    ? "不上传也能生成；这里的素材只提供风格和运动信息"
    : isEditMode
    ? (portraitGenerationMode ? "上传目标视频或粘贴链接，先反推镜头和风格，再上传人物照片做重构" : "也可粘贴小红书、抖音或网页链接抓取素材")
    : (category === "video" ? "点击上传视频，下方也可改传图片或粘贴链接" : "点击上传图片，下方也可粘贴链接抓取素材");
  const productTitle = portraitGenerationMode ? "人物照片" : productSubjectMode ? "产品主题图" : (isImageEditMode ? "编辑源图片" : "产品主体");
  const detailImagesEnabled = Boolean(
    category === "video"
    && productSubjectMode
    && productDetailLimit > 0
    && productDetailValidation.ok,
  );
  const productHint = isImageEditMode
    ? (portraitGenerationMode
        ? "上传需要保留身份的人像照片，生成时强保护五官、脸型、发型和人物身份"
        : productSubjectMode
        ? "上传产品图作为唯一产品身份，生成时强保护Logo、包装和细节"
        : "上传需要被编辑的图片，未要求修改的内容默认保留")
    : subjectImageIsFirstFrame
      ? "当前模型会把这张主体图作为视频首帧，不提供独立身份锁定"
    : (portraitGenerationMode
        ? "上传要生成进目标视频风格的人物照片，参考视频只提供动作、镜头和风格"
        : "上传产品图作为视频中的唯一商品主体，不作为风格参考；保留Logo、包装、颜色、形状和文字标识");
  const [fallbackCoverFile, setFallbackCoverFile] = useState(null);
  const [batchSelectionMode, setBatchSelectionMode] = useState(false);
  const reverseNeedsConfirmation = reverseOperation?.status === "needs_confirmation";
  const imageMotionSource = category === "video" && selected?.type === "image";

  useEffect(() => {
    setFallbackCoverFile(null);
  }, [reverseOperation?.id, reverseOperation?.status]);

  useEffect(() => {
    if (!reverseBatchEnabled) setBatchSelectionMode(false);
  }, [reverseBatchEnabled]);

  // 真正生效的精度状态源是 reverseConfig.analysis_precision（由精度选择器写入）。
  // 这里把它同步回 workspace.videoAnalysisPreset，让费用/帧数预估读到真实档位，
  // 避免预估一直停留在默认 standard（如 fine 实际 22 帧却按 14 帧提示）。
  const reverseAnalysisPrecision = reverseConfig?.analysis_precision || "";
  useEffect(() => {
    if (!reverseAnalysisPrecision || reverseAnalysisPrecision === videoAnalysisPreset) return;
    setVideoAnalysisPreset?.(reverseAnalysisPrecision);
  }, [reverseAnalysisPrecision, videoAnalysisPreset, setVideoAnalysisPreset]);

  return (
    <aside id="studio-reference-panel" className="relative min-w-0 overflow-hidden rounded-xl3 border border-iris/35 bg-gradient-to-b from-iris/20 via-base2/80 to-rose/10 p-3 shadow-glow-sm">
      <div className="pointer-events-none absolute -right-16 -top-16 h-32 w-32 rounded-full bg-rose/25 blur-3xl" />
      <div className="relative">
        <div className="mb-3 flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-display font-semibold text-iris-400">{firstFrameInputEnabled ? "视频首帧" : directProductVideoMode ? "产品与参考" : "参考素材"}</p>
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
        <div className="mb-3 flex min-w-0 justify-end border-b border-line/80 pb-2">
          <StudioModelSelector
            use="vision"
            options={visionModelOptions}
            value={selectedVisionModelConfigId}
            onChange={onVisionModelChange}
          />
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
            <div className={`mt-2 grid gap-2 ${productSubjectMode ? "grid-cols-2" : "grid-cols-1"}`}>
              <button
                type="button"
                onClick={() => productUploadInputRef.current?.click()}
                disabled={productActionBusy}
                className="btn-secondary btn-sm justify-center border-aqua/30 bg-aqua/10 text-snow"
              >
                {uploading ? "上传中…" : productBusy ? "处理中…" : productProfiling ? "识别中，可替换" : productAsset
                  ? (portraitGenerationMode ? "替换人物照片" : productSubjectMode ? "替换主题图" : "替换编辑源图片")
                  : (portraitGenerationMode ? "上传人物照片" : productSubjectMode ? "上传主题图" : "上传编辑源图片")}
              </button>
              {productSubjectMode && (
                <button type="button" onClick={() => onOpenAssetPicker?.("product_theme")} disabled={productActionBusy} className="btn-secondary btn-sm justify-center">
                  从我的资产选择
                </button>
              )}
            </div>
            {directProductVideoMode && productSubjectMode && (
              <ProductDetailImages
                assets={productDetailAssets}
                enabled={detailImagesEnabled}
                limit={productDetailLimit}
                validation={productDetailValidation}
                busy={productActionBusy}
                uploadInputRef={productDetailUploadInputRef}
                onOpenPicker={onOpenAssetPicker}
                onRemove={onRemoveProductDetail}
                onMove={onMoveProductDetail}
              />
            )}
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
                    {firstFrameInputEnabled && selected.type === "image" ? "首帧" : selected.type === "video" ? "视频参考" : "图片参考"}
                  </span>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => (category === "video" && !firstFrameInputEnabled ? videoUploadInputRef : imageUploadInputRef).current?.click()}
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
                  {firstFrameInputEnabled
                    ? firstLastFrameEnabled
                      ? "作为视频首帧约束起始画面，可在下方继续添加尾帧。"
                      : "作为当前模型必需的视频首帧。"
                    : isEditMode || directProductVideoMode
                    ? "仅作为风格参考，不会覆盖产品主体。"
                    : "可直接作为编辑源生成。"}
                </div>
              )}
              {selected?.type === "video" && selected?.url?.includes("/api/uploads/upload_video/") && (
                <div className="mt-1 text-fog">会抽取关键帧理解内容，并提取镜头节奏和画面风格。</div>
              )}
            </div>
          )}
          {firstLastFrameEnabled && (
            <LastFrameImage
              asset={lastFrameAsset}
              firstFrameReady={selected?.type === "image"}
              busy={uploading}
              uploadInputRef={lastFrameUploadInputRef}
              onOpenPicker={onOpenAssetPicker}
              onClear={onClearLastFrameAsset}
            />
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
                      {subjectImageIsFirstFrame ? "视频首帧" : portraitGenerationMode ? "人物照片" : isImageEditMode ? "编辑源" : "产品主体"}
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
                        {subjectImageIsFirstFrame
                          ? "作为视频首帧，不是独立主体参考"
                          : portraitGenerationMode
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
                    : subjectImageIsFirstFrame
                    ? "生成时将这张图作为视频首帧；主体延续由模型根据首帧推演，不是独立身份锁定。"
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
              {uploading ? "上传中…" : productBusy ? "处理中…" : productProfiling ? "识别中，可替换" : productAsset
                ? (portraitGenerationMode ? "替换人物照片" : isImageEditMode ? "替换编辑源图片" : "替换产品图片")
                : (portraitGenerationMode ? "上传人物照片" : isImageEditMode ? "上传编辑源图片" : "上传产品图片")}
            </button>
            {productGenerationMode && (
              <>
                <button type="button" onClick={() => onOpenAssetPicker?.("product_theme")} disabled={productActionBusy} className="btn-secondary btn-sm mt-2 w-full justify-center">
                  从我的资产选择主题图
                </button>
                <ProductDetailImages
                  assets={productDetailAssets}
                  enabled={detailImagesEnabled}
                  limit={productDetailLimit}
                  validation={productDetailValidation}
                  busy={productActionBusy}
                  uploadInputRef={productDetailUploadInputRef}
                  onOpenPicker={onOpenAssetPicker}
                  onRemove={onRemoveProductDetail}
                  onMove={onMoveProductDetail}
                />
              </>
            )}
          </div>
        )}

        <div className="space-y-2">
          {profileOperation && ["queued", "running"].includes(profileOperation.status) && (
            <p className="rounded-xl border border-aqua/25 bg-aqua/10 px-3 py-2 text-xs text-mist" role="status">
              {profileOperation.phase || "正在识别主体身份档案"} · {Math.round(Number(profileOperation.progress || 0))}%
            </p>
          )}
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
          {parseNotice?.message && (
            <p
              role={parseNotice.kind === "bad" ? "alert" : "status"}
              aria-live={parseNotice.kind === "bad" ? "assertive" : "polite"}
              className={`rounded-lg border px-3 py-2 text-[11px] leading-relaxed ${
                parseNotice.kind === "bad"
                  ? "border-bad/35 bg-bad/10 text-bad"
                  : parseNotice.kind === "ok"
                    ? "border-ok/35 bg-ok/10 text-ok"
                    : "border-warn/35 bg-warn/10 text-warn"
              }`}
            >
              {parseNotice.message}
            </p>
          )}
          {appliedUrl && appliedUrl !== url.trim() && (
            <p className="truncate text-[11px] text-fog" title={appliedUrl}>当前素材仍来自：{appliedUrl}</p>
          )}
          <div className={`grid gap-2 ${videoRequiresFirstFrame ? "grid-cols-1" : "grid-cols-2"}`}>
            <button
              type="button"
              onClick={() => (imageUploadTargetsProduct ? productUploadInputRef : imageUploadInputRef).current?.click()}
              disabled={imageUploadTargetsProduct ? productActionBusy : uploading}
              className="btn-secondary btn-sm justify-center border-line2 bg-white/[0.08]"
            >
              {uploading ? "上传中…" : imageUploadLabel}
            </button>
            {!videoRequiresFirstFrame && (
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
            )}
          </div>
          <button
            type="button"
            onClick={() => onOpenAssetPicker?.("reverse_source")}
            disabled={uploading || reversing || reverseNeedsConfirmation}
            className="btn-secondary btn-sm w-full justify-center border-line2 bg-white/[0.08]"
          >
            从我的素材库选择
          </button>
          {selected && reverseConfig && (
            <StudioReverseIntentControls
              category={category}
              config={reverseConfig}
              disabled={reversing || reverseNeedsConfirmation}
              onChange={setReverseConfig}
            />
          )}
          {selected && (
            <StudioReverseSourcesEditor
              selected={selected}
              assets={assets}
              sources={reverseSources}
              disabled={reversing || reverseNeedsConfirmation}
              onChange={setReverseSources}
            />
          )}
          {category === "video" && selected && reverseConfig && videoAnalysisPresets.length > 0 && (
            <StudioVideoAnalysisControls
              config={reverseConfig}
              presets={videoAnalysisPresets}
              timelineEnabled={selected.type === "video"}
              disabled={reversing || reverseNeedsConfirmation}
              maxDuration={selected.duration || reverseVideoAnalysis?.source?.total_duration_seconds || null}
              selectedCost={selectedReverseCost}
              onChange={setReverseConfig}
            />
          )}
          {imageMotionSource && selectedReverseCost > reverseImageCost && (
            <p className="rounded-lg border border-line bg-black/15 px-2.5 py-2 text-[11px] leading-relaxed text-fog">
              单图运动设计成功后按图片反推结算 {reverseImageCost} 积分，并退还
              {Number(selectedReverseCost) - Number(reverseImageCost)} 积分差额。
            </p>
          )}
          {reverseVideoAnalysis && (
            <div className="rounded-xl border border-line bg-black/15 p-2">
              {reverseVideoAnalysis.source && (
                <div className="text-xs text-mist">
                  已识别 {reverseVideoAnalysis.source.ratio || "未知画幅"}
                  {Number.isFinite(Number(reverseVideoAnalysis.source.duration_seconds))
                    ? ` · ${Number(reverseVideoAnalysis.source.duration_seconds).toFixed(2)}s`
                    : ""}
                  {Array.isArray(reverseVideoAnalysis.shots) && reverseVideoAnalysis.shots.length > 0
                    ? ` · ${reverseVideoAnalysis.shots.length} 个镜头`
                    : ""}
                </div>
              )}
              <VideoAnalysisEvidence analysis={reverseVideoAnalysis} />
            </div>
          )}
          {reverseNeedsConfirmation && (
            <div className="rounded-xl border border-warn/40 bg-warn/10 p-3" role="alert">
              <p className="text-sm font-display font-semibold text-snow">视频抽帧失败</p>
              <p className="mt-1 text-xs leading-relaxed text-fog">
                是否改用封面单帧设计视频运动？确认后仅结算 2 积分，并退还
                {Math.max(0, Number(reverseOperation.cost_frozen || 0) - 2)} 积分差额；
                {confirmationTime(reverseOperation.confirmation_expires_at)}未确认将自动取消并全额退款。
              </p>
              <label className="mt-2 block text-[11px] text-mist">
                可选：上传新的封面图片
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  onChange={(event) => setFallbackCoverFile(event.target.files?.[0] || null)}
                  className="mt-1 block w-full min-w-0 text-[11px] text-fog file:mr-2 file:rounded-full file:border-0 file:bg-white/10 file:px-2.5 file:py-1.5 file:text-[11px] file:text-mist hover:file:bg-white/15"
                />
              </label>
              <p className="mt-1 truncate text-[11px] text-fog" aria-live="polite">
                {fallbackCoverFile ? `将使用：${fallbackCoverFile.name}` : "未选择时使用任务中已有封面"}
              </p>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <button type="button" onClick={onCancelReverse} className="btn-secondary btn-sm justify-center">
                  取消并退款
                </button>
                <button type="button" onClick={() => onConfirmCover?.(fallbackCoverFile)} className="btn-primary btn-sm justify-center">
                  使用封面分析
                </button>
              </div>
            </div>
          )}
          {reverseOperation && ["queued", "running"].includes(reverseOperation.status) && (
            <div className="rounded-xl border border-aqua/30 bg-aqua/10 px-3 py-2" role="status" aria-live="polite">
              <div className="flex items-center justify-between gap-2 text-xs text-mist">
                <span>{reverseOperation.phase || (reverseOperation.status === "queued" ? "等待反推" : "正在分析")}</span>
                <span>{Math.round(Number(reverseOperation.progress || 0))}%</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-black/25">
                <div className="h-full bg-aqua transition-[width]" style={{ width: `${Math.max(2, Number(reverseOperation.progress || 0))}%` }} />
              </div>
              <button type="button" onClick={onCancelReverse} className="mt-2 text-xs text-fog underline-offset-2 hover:text-snow hover:underline">
                取消反推
              </button>
            </div>
          )}
          <div className="grid grid-cols-1 gap-2">
            <button
              type="button"
              onClick={onReverse}
              disabled={!selected || !reverseEnabled || reversing || reverseNeedsConfirmation}
              className={`btn-sm justify-center rounded-full font-display font-medium transition ${
                selected && reverseEnabled
                  ? "bg-brand text-white shadow-glow-sm hover:brightness-110 active:scale-[0.98]"
                  : "cursor-not-allowed border border-line bg-white/5 text-fog"
              }`}
            >
              {reverseNeedsConfirmation
                ? "请先处理封面确认"
                : reversing
                  ? "反推中…"
                  : `${isImageEditMode ? "反推可选风格" : "反推提示词"}${selected && selectedReverseCost ? ` · ${selectedReverseCostLabel}` : ""}`}
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
          {firstLastFrameEnabled && (
            <input
              ref={lastFrameUploadInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={(e) => onUploadLastFrameImage(e.target.files?.[0])}
            />
          )}
          {(isEditMode || directProductVideoMode) && (
            <input
              ref={productUploadInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={(e) => onUploadProductImage(e.target.files?.[0])}
            />
          )}
          {category === "video" && productSubjectMode && (
            <input
              ref={productDetailUploadInputRef}
              type="file"
              multiple
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={(e) => onUploadProductDetailImages(e.target.files)}
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
            <div className="mb-2 flex items-center justify-between gap-2 text-xs">
              <span className="text-fog">已抓取素材</span>
              <div className="flex items-center gap-2">
                {reverseBatchEnabled && (
                  <button
                    type="button"
                    onClick={() => {
                      setBatchSelectionMode((value) => !value);
                      setRefOpen(true);
                    }}
                    className={batchSelectionMode ? "text-aqua" : "text-mist hover:text-snow"}
                  >
                    {batchSelectionMode ? "完成多选" : "批量选择"}
                  </button>
                )}
                <button type="button" onClick={() => setRefOpen((v) => !v)} className="text-mist hover:text-snow">
                  {refOpen ? "收起" : "展开"}
                </button>
              </div>
            </div>
            {refOpen && (
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {assets.map((asset, i) => {
                  const batchSelected = batchReverseAssets.some((item) => (
                    (item.asset_ref && asset.asset_ref && item.asset_ref === asset.asset_ref)
                    || (item.url && asset.url && item.url === asset.url)
                  ));
                  const batchCompatible = category === "video" || asset.type === "image";
                  return (
                    <button
                      key={asset.asset_ref || asset.url || i}
                      type="button"
                      onClick={() => (batchSelectionMode
                        ? batchCompatible && onToggleBatchAsset?.(asset)
                        : onPickAsset(asset))}
                      aria-pressed={batchSelectionMode ? batchSelected : selected === asset}
                      disabled={batchSelectionMode && !batchCompatible}
                      className={`relative aspect-square overflow-hidden rounded-lg border transition disabled:cursor-not-allowed disabled:opacity-40 ${
                        batchSelected
                          ? "border-aqua ring-2 ring-aqua/35"
                          : selected === asset && !batchSelectionMode
                            ? "border-iris ring-2 ring-iris/40"
                            : "border-line hover:border-line2"
                      }`}
                    >
                      <ReferenceAssetPreview asset={asset} compact />
                      <span className="badge absolute left-1 top-1 bg-black/60 text-[10px] text-white">{asset.type}</span>
                      {batchSelectionMode && (
                        <span className={`absolute right-1 top-1 flex h-6 w-6 items-center justify-center rounded-full border text-xs font-bold ${
                          batchSelected ? "border-aqua bg-aqua text-black" : "border-white/30 bg-black/70 text-white"
                        }`} aria-hidden="true">
                          {batchSelected ? "✓" : "+"}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {reverseBatchEnabled && (
          <StudioReverseBatchPanel
            assets={batchReverseAssets}
            batch={reverseBatch}
            recentBatches={recentReverseBatches}
            busyAction={reverseBatchBusyAction}
            reverseEnabled={reverseEnabled}
            sharedConfig={reverseConfig}
            batchCapabilities={reverseBatchCapabilities}
            onOpenAssetPicker={onOpenBatchAssetPicker}
            onRemoveAsset={onRemoveBatchAsset}
            onStart={onStartReverseBatch}
            onCancel={onCancelReverseBatch}
            onOpenBatch={onOpenReverseBatch}
            onOpenItem={onOpenReverseBatchItem}
            onRetryItem={onRetryReverseBatchItem}
            onSaveAll={onSaveReverseBatchRecipes}
          />
        )}
      </div>
    </aside>
  );
}
