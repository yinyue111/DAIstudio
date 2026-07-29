"use client";

import { buildReverseProfessionalReport } from "./reverseProfessionalReport";

const LEVELS = {
  verified: { label: "已验证", className: "border-good/35 text-good" },
  inferred: { label: "跨帧推断", className: "border-aqua/35 text-aqua" },
  creative: { label: "创作建议", className: "border-warn/35 text-warn" },
};

const MODE_LABELS = {
  keyframes: "多帧分析",
  multi_frame: "多帧分析",
  cover_fallback: "封面单帧",
  cover: "封面单帧",
  unavailable: "分析不可用",
};

function Section({ title, children }) {
  return (
    <section className="border-t border-line pt-3 first:border-t-0 first:pt-0">
      <h4 className="text-xs font-display font-semibold text-snow">{title}</h4>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function timeRange(range) {
  return `${range.start_seconds.toFixed(2)}-${range.end_seconds.toFixed(2)}s`;
}

export default function StudioReverseProfessionalReport({ result }) {
  const report = buildReverseProfessionalReport(result);
  return (
    <div className="space-y-4 text-xs" aria-label="视频专业拆解报告">
      <Section title="视频画像">
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div><dt className="text-fog">画幅</dt><dd className="mt-0.5 text-mist">{report.profile.ratio}</dd></div>
          <div><dt className="text-fog">分析时长</dt><dd className="mt-0.5 text-mist">{report.profile.durationSeconds == null ? "-" : `${report.profile.durationSeconds.toFixed(2)}s`}</dd></div>
          <div><dt className="text-fog">分析模式</dt><dd className="mt-0.5 text-mist">{MODE_LABELS[report.profile.analysisMode] || report.profile.analysisMode}</dd></div>
          <div><dt className="text-fog">证据覆盖</dt><dd className="mt-0.5 text-mist">{report.profile.coverageRatio == null ? `${report.profile.frameCount} 帧` : `${Math.round(report.profile.coverageRatio * 100)}%`}</dd></div>
        </dl>
      </Section>

      <Section title="拉片分镜">
        {report.shots.length ? (
          <ol className="space-y-2">
            {report.shots.map((shot) => {
              const level = LEVELS[shot.level];
              return (
                <li key={`${shot.index}-${shot.range?.start_seconds}`} className="border-l-2 border-line pl-2.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-display font-medium text-mist">镜头 {shot.index + 1}</span>
                    <span className="text-fog">{shot.range ? timeRange(shot.range) : "时间未确认"}</span>
                    <span className={`border px-1.5 py-0.5 text-[10px] ${level.className}`}>{level.label}</span>
                  </div>
                  {shot.visual && <p className="mt-1 text-mist">画面：{shot.visual}</p>}
                  {(shot.subjectTracking || shot.pose || shot.action || shot.camera || shot.transition) && (
                    <div className="mt-1 text-fog">
                      {shot.semanticLevel && (
                        <span className={`mr-1.5 inline-block border px-1.5 py-0.5 text-[10px] ${LEVELS[shot.semanticLevel].className}`}>
                          {LEVELS[shot.semanticLevel].label}
                        </span>
                      )}
                      <span>{[
                        shot.subjectTracking && `主体追踪：${shot.subjectTracking}`,
                        shot.pose && `姿态：${shot.pose}`,
                        shot.action && `动作：${shot.action}`,
                        shot.camera && `运镜：${shot.camera}`,
                        shot.transition && `转场：${shot.transition}`,
                      ].filter(Boolean).join("；")}</span>
                    </div>
                  )}
                  <p className="mt-1 text-[10px] text-fog">证据帧：{shot.evidenceFrames.length ? shot.evidenceFrames.join("、") : "无"}</p>
                </li>
              );
            })}
          </ol>
        ) : <p className="text-fog">当前没有通过时间和证据校验的分镜。</p>}
        {report.gaps.length > 0 && (
          <div className="mt-3 border-l-2 border-warn pl-2.5">
            <p className="font-display font-medium text-warn">证据缺口</p>
            <ul className="mt-1 space-y-1 text-fog">
              {report.gaps.map((gap, index) => <li key={`${gap}-${index}`}>- {gap}</li>)}
            </ul>
          </div>
        )}
      </Section>

      <Section title="复刻分析">
        {report.inferredNarrative && (
          <div className="mb-3 border-l-2 border-aqua pl-2.5">
            <div className="flex items-center gap-1.5">
              <p className="font-display font-medium text-mist">模型叙述参考</p>
              <span className="border border-aqua/35 px-1.5 py-0.5 text-[10px] text-aqua">跨帧推断</span>
            </div>
            <p className="mt-1 whitespace-pre-wrap break-words text-fog">{report.inferredNarrative}</p>
          </div>
        )}
        {report.structured.length ? (
          <dl className="grid gap-2 sm:grid-cols-2">
            {report.structured.map((item) => (
              <div key={item.key} className="min-w-0">
                <dt className="text-fog">{item.key}</dt>
                <dd className="mt-0.5 break-words text-mist">{item.value}</dd>
              </div>
            ))}
          </dl>
        ) : <p className="text-fog">当前没有结构化复刻字段。</p>}
      </Section>

      <Section title="生成交付">
        <div className="space-y-2">
          <div><p className="text-fog">可生成提示词</p><p className="mt-1 whitespace-pre-wrap break-words text-mist">{report.delivery.prompt || "-"}</p></div>
          {report.delivery.negative && <div><p className="text-fog">负向约束</p><p className="mt-1 break-words text-mist">{report.delivery.negative}</p></div>}
          <div><p className="text-fog">音频证据</p><p className="mt-1 text-mist">{report.delivery.audioStatus}</p></div>
          {report.delivery.audioSuggestions.length > 0 && (
            <div><p className="text-fog">后期创作建议</p><p className="mt-1 text-mist">{report.delivery.audioSuggestions.join("；")}</p></div>
          )}
          <p className="text-[10px] text-fog">提交生成时由平台按所选视频模型确定性编译，不以报告文案替代模型适配。</p>
        </div>
      </Section>
    </div>
  );
}
