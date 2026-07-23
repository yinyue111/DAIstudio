"use client";

import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import AssetMedia from "../../components/AssetMedia";

const ROLE_OPTIONS = [
  ["subject", "主体参考"],
  ["product", "商品参考"],
  ["style", "风格参考"],
  ["composition", "构图参考"],
  ["lighting", "光线参考"],
  ["text_layout", "文字版式"],
  ["negative", "排除参考"],
  ["first_frame", "首帧参考"],
  ["last_frame", "尾帧参考"],
];

function assetUrl(asset) {
  return String(asset?.url || asset?.preview_url || asset?.thumb || "").trim();
}

function positiveAssetId(asset) {
  const id = Number(asset?.id);
  return Number.isInteger(id) && id > 0 ? id : null;
}

export default function StudioReverseSourcesEditor({
  selected,
  assets = [],
  sources = [],
  disabled = false,
  onChange,
}) {
  const [candidateUrl, setCandidateUrl] = useState("");
  const [role, setRole] = useState("style");
  const auxiliary = Array.isArray(sources) ? sources.filter((source) => source?.role !== "primary") : [];
  const candidates = useMemo(() => assets.filter((asset) => (
    asset?.type === "image"
    && assetUrl(asset)
    && assetUrl(asset) !== assetUrl(selected)
    && !auxiliary.some((source) => source.asset_url === assetUrl(asset))
  )), [assets, selected, auxiliary]);

  function addSource() {
    const asset = candidates.find((item) => assetUrl(item) === candidateUrl);
    if (!asset) return;
    const id = positiveAssetId(asset);
    onChange?.([...auxiliary, {
      asset_url: assetUrl(asset),
      source_type: "image",
      role,
      ...(id ? { asset_id: id } : {}),
    }]);
    setCandidateUrl("");
  }

  return (
    <section className="rounded-xl border border-line bg-black/15 p-2" aria-labelledby="reverse-sources-title">
      <div className="flex items-center justify-between gap-2">
        <h4 id="reverse-sources-title" className="text-xs font-display font-medium text-mist">参考角色</h4>
        <span className="text-[11px] text-fog">1 个主素材 · 最多 11 个辅助图</span>
      </div>

      <div className="mt-2 flex min-w-0 items-center gap-2 border-b border-line pb-2">
        <div className="h-10 w-10 flex-none overflow-hidden rounded-lg border border-aqua/45 bg-black/25">
          <AssetMedia asset={selected} className="h-full w-full object-cover" fallbackClassName="h-full w-full" />
        </div>
        <div className="min-w-0">
          <p className="text-xs font-display font-medium text-snow">主素材</p>
          <p className="truncate text-[10px] text-fog">决定本次反推的主体和媒体类型</p>
        </div>
      </div>

      {auxiliary.length > 0 && (
        <ul className="divide-y divide-line" aria-label="辅助参考素材">
          {auxiliary.map((source, index) => (
            <li key={`${source.asset_url}-${source.role}-${index}`} className="flex min-w-0 items-center gap-2 py-2">
              <div className="h-10 w-10 flex-none overflow-hidden rounded-lg border border-line bg-black/25">
                <AssetMedia asset={{ type: "image", url: source.asset_url }} className="h-full w-full object-cover" fallbackClassName="h-full w-full" />
              </div>
              <select
                className="input min-h-10 min-w-0 flex-1 px-2 py-1.5 text-xs"
                value={source.role}
                disabled={disabled}
                aria-label={`辅助参考 ${index + 1} 的角色`}
                onChange={(event) => onChange?.(auxiliary.map((item, itemIndex) => (
                  itemIndex === index ? { ...item, role: event.target.value } : item
                )))}
              >
                {ROLE_OPTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
              <button
                type="button"
                className="icon-btn h-10 w-10 flex-none text-fog hover:text-bad"
                disabled={disabled}
                aria-label={`移除辅助参考 ${index + 1}`}
                onClick={() => onChange?.(auxiliary.filter((_, itemIndex) => itemIndex !== index))}
              >
                <Trash2 size={15} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {candidates.length > 0 && auxiliary.length < 11 && (
        <div className="mt-2 grid grid-cols-[minmax(0,1fr)_minmax(7rem,0.75fr)_auto] gap-1.5">
          <select
            className="input min-h-11 min-w-0 px-2 py-2 text-xs"
            value={candidateUrl}
            disabled={disabled}
            aria-label="选择辅助参考图片"
            onChange={(event) => setCandidateUrl(event.target.value)}
          >
            <option value="">选择已抓取图片</option>
            {candidates.map((asset, index) => (
              <option key={assetUrl(asset)} value={assetUrl(asset)}>图片 {index + 1}</option>
            ))}
          </select>
          <select
            className="input min-h-11 min-w-0 px-2 py-2 text-xs"
            value={role}
            disabled={disabled}
            aria-label="选择辅助参考角色"
            onChange={(event) => setRole(event.target.value)}
          >
            {ROLE_OPTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
          <button type="button" className="icon-btn h-11 w-11" disabled={disabled || !candidateUrl} aria-label="添加辅助参考" onClick={addSource}>
            <Plus size={16} aria-hidden="true" />
          </button>
        </div>
      )}
    </section>
  );
}
