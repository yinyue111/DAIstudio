"use client";

export default function AssetWindowControls({
  totalCount,
  visibleCount,
  initialCount,
  hasMore,
  onShowMore,
  onReset,
  className = "",
}) {
  const total = Number(totalCount || 0);
  const visible = Number(visibleCount || 0);
  const initial = Number(initialCount || 0);
  const canReset = visible > initial && initial > 0;

  if (!total || total <= initial) return null;

  return (
    <div className={`mx-auto mt-3 flex max-w-7xl flex-col gap-2 rounded-xl border border-line bg-white/[0.035] px-3 py-2 text-xs text-fog sm:flex-row sm:items-center sm:justify-between ${className}`}>
      <span>
        已展示最近 <b className="text-mist">{Math.min(visible, total)}</b> / {total} 个素材，更多作品可进入个人中心查看。
      </span>
      <div className="flex gap-2">
        {canReset && (
          <button type="button" onClick={onReset} className="btn-secondary btn-sm">
            收起
          </button>
        )}
        {hasMore && (
          <button type="button" onClick={onShowMore} className="btn-secondary btn-sm">
            展示更多
          </button>
        )}
      </div>
    </div>
  );
}
