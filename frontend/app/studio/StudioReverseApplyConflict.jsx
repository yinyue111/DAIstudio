"use client";

import { reverseApplyConflictFieldChips } from "./reverseResultRevisions";

export default function StudioReverseApplyConflict({ conflict, onDismiss }) {
  if (!conflict) return null;
  return (
    <section
      className="mt-3 rounded-xl2 border border-warn/35 bg-warn/10 p-3 animate-fadeup"
      role="alert"
      aria-label="反推结果应用冲突详情"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-display font-medium text-warn">
            反推结果应用冲突
            {conflict.revision_id != null && (
              <span className="ml-1 text-fog">· 服务端已记录版本 #{conflict.revision_id}</span>
            )}
          </p>
          <p className="mt-1 text-xs text-mist">以下字段在应用期间被本地修改，已保留当前编辑，未被服务端版本覆盖：</p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {reverseApplyConflictFieldChips(conflict.fields).map(({ field, label }) => (
              <span key={field} className="chip">{label}</span>
            ))}
          </div>
          <p className="mt-1.5 text-[11px] text-fog">请对照上方反推结果重新审阅这些字段后再次应用，或保持当前编辑。</p>
        </div>
        <button
          type="button"
          className="btn-secondary btn-sm px-2.5 py-1 text-xs"
          onClick={onDismiss}
        >
          知道了
        </button>
      </div>
    </section>
  );
}
