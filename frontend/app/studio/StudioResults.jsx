"use client";

import { Lightbox, MasonryItem, ResultCard } from "./StudioMedia";
import { isTerminalTaskStatus, statusStyle, statusZh, taskResultTitle } from "./helpers";

export default function StudioResults({
  task,
  runningSnapshot,
  showRunningProgress,
  trackingLost,
  finalTaskId,
  submitting,
  videoFinalCost,
  works,
  lightbox,
  setLightbox,
  busyAssetIds,
  onRefreshActiveTask,
  onUnlock,
  onDownload,
  onReport,
  onSubmitFinal,
}) {
  return (
    <>
      {task && (
        <section className="mx-auto mt-8 max-w-5xl animate-fadeup">
          <div className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-sm font-display font-semibold">{taskResultTitle(task, runningSnapshot)}</span>
              <span className={`badge ${statusStyle(task.status)}`}>{statusZh(task.status)}</span>
            </div>
            {showRunningProgress && (
              <div className="mb-4">
                <div className="mb-2 h-1.5 w-full overflow-hidden rounded-full bg-white/8">
                  <div
                    className="h-full rounded-full bg-brand transition-all duration-500"
                    style={{ width: `${task.progress || 8}%` }}
                  />
                </div>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {Array.from({
                    length: runningSnapshot?.category === "image" ? Number(runningSnapshot.n || 1) : 1,
                  }).map((_, i) => (
                    <div
                      key={i}
                      className="skeleton"
                      style={
                        runningSnapshot?.ratio
                          ? { aspectRatio: `${runningSnapshot.ratio.w} / ${runningSnapshot.ratio.h}` }
                          : { aspectRatio: "1 / 1" }
                      }
                    />
                  ))}
                </div>
              </div>
            )}
            {trackingLost && !isTerminalTaskStatus(task?.status) && (
              <div className="mb-4 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <span>前端已停止等待该任务，后端可能仍在生成。</span>
                  <div className="flex gap-2">
                    <button type="button" onClick={onRefreshActiveTask} className="btn-secondary btn-sm">
                      刷新任务状态
                    </button>
                    <a href="/history" className="btn-secondary btn-sm">去历史查看</a>
                  </div>
                </div>
              </div>
            )}
            {task.error && <p className="mb-3 rounded-lg bg-bad/10 px-3 py-2 text-sm text-bad">{task.error}</p>}
            {task.partial && (
              <div className="mb-3 rounded-lg bg-warn/10 px-3 py-2 text-sm text-warn">
                <p>
                  本次批量生成完成 {task.saved_count || task.assets?.length || 0}/{task.requested_count || "?"} 张，
                  失败部分已按实际成功张数结算。
                </p>
                {task.partial_errors?.length > 0 && (
                  <p className="mt-1 text-xs text-warn/80">未完成原因：{task.partial_errors.join("；")}</p>
                )}
              </div>
            )}
            {task.assets?.length > 0 && (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {task.assets.map((asset) => (
                  <ResultCard
                    key={asset.id}
                    a={asset}
                    unlocking={busyAssetIds?.has(asset.id)}
                    onOpen={() => setLightbox(asset)}
                    onUnlock={() => onUnlock(asset)}
                    onDownload={() => onDownload(asset)}
                    onReport={() => onReport(asset)}
                  />
                ))}
              </div>
            )}
            {task.category === "video" && task.stage === "preview" && task.status === "succeeded" && finalTaskId && (
              <a href="/history" className="btn-secondary mt-4 block w-full text-center">
                完整视频已提交 · 去历史查看
              </a>
            )}
            {task.category === "video" && task.stage === "preview" && task.status === "succeeded" && !finalTaskId && (
              <button onClick={onSubmitFinal} disabled={submitting} className="btn-primary mt-4 w-full">
                {submitting ? "提交中…" : `方向满意 → 渲染完整视频 · ${videoFinalCost}积分`}
              </button>
            )}
          </div>
        </section>
      )}

      <section className="mx-auto mt-14 max-w-7xl">
        <div className="mb-5 flex items-end justify-between">
          <div>
            <h2 className="text-xl font-bold">我的作品墙</h2>
            <p className="mt-1 text-sm text-fog">最近生成的创作，点击查看 / 解锁 / 下载。</p>
          </div>
          <a href="/profile" className="btn-secondary btn-sm">查看全部</a>
        </div>
        {works === null ? (
          <div className="masonry">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="skeleton" style={{ height: 160 + (i % 4) * 60 }} />
            ))}
          </div>
        ) : works.length === 0 ? (
          <div className="card flex flex-col items-center justify-center gap-2 p-16 text-center">
            <span className="text-3xl">🪄</span>
            <p className="text-sm text-mist">还没有作品，输入提示词开始你的第一次创作。</p>
          </div>
        ) : (
          <div className="masonry">
            {works.map((asset) => (
              <MasonryItem key={asset.id} a={asset} onOpen={() => setLightbox(asset)} />
            ))}
          </div>
        )}
      </section>

      {lightbox && (
        <Lightbox
          a={lightbox}
          unlocking={busyAssetIds?.has(lightbox.id)}
          onClose={() => setLightbox(null)}
          onUnlock={() => onUnlock(lightbox)}
          onDownload={() => onDownload(lightbox)}
          onReport={() => onReport(lightbox)}
        />
      )}
    </>
  );
}
