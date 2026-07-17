"use client";

import AssetMedia, { assetPreviewSrc, assetUnavailableText } from "./AssetMedia";
import { unifiedAssetKey } from "../lib/unifiedAssets";

export default function AssetComparePanel({ assets = [], onClose }) {
  const visible = (assets || []).filter(Boolean).slice(0, 4);
  if (visible.length < 2) return null;
  return (
    <section className="mb-5 rounded-xl2 border border-brand/30 bg-brand/10 p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-display font-semibold text-snow">结果对比</h2>
          <p className="text-xs text-fog">对比素材尺寸、类型和来源任务，方便挑选后续变体方向。</p>
        </div>
        <button type="button" onClick={onClose} className="btn-ghost btn-sm">关闭</button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {visible.map((asset) => {
          const src = assetPreviewSrc(asset);
          return (
            <article key={unifiedAssetKey(asset)} className="overflow-hidden rounded-xl border border-line bg-base2/80">
              <div className="relative aspect-square bg-black/20">
                {!src ? (
                  <div className="absolute inset-0 flex items-center justify-center px-3 text-center text-xs text-fog">
                    {assetUnavailableText(asset)}
                  </div>
                ) : (
                  <AssetMedia
                    asset={asset}
                    className="absolute inset-0 h-full w-full object-contain"
                    fallbackClassName="absolute inset-0 flex items-center justify-center px-3 text-center text-xs text-fog"
                  />
                )}
              </div>
              <dl className="grid grid-cols-2 gap-x-2 gap-y-1 p-3 text-[11px] text-fog">
                <dt>类型</dt><dd className="text-right text-mist">{asset.type === "video" ? "视频" : "图片"}</dd>
                <dt>尺寸</dt><dd className="text-right text-mist">{asset.width && asset.height ? `${asset.width}×${asset.height}` : "-"}</dd>
                <dt>任务</dt><dd className="text-right text-mist">#{asset.task_id || "-"}</dd>
                <dt>状态</dt><dd className="text-right text-mist">{asset.unlocked ? "已解锁" : "可预览"}</dd>
              </dl>
            </article>
          );
        })}
      </div>
    </section>
  );
}
