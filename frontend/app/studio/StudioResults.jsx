"use client";

import { forwardRef } from "react";
import { classifyGenerationError } from "../../lib/errorHandling";
import { Lightbox, ResultCard } from "./StudioMedia.jsx";
import GroupedAssetGallery from "./GroupedAssetGallery.jsx";
import StudioReproductionAssessment from "./StudioReproductionAssessment.jsx";
import { reproductionSourceAsset } from "./reproductionAssessment";
import { isTerminalTaskStatus, statusStyle, statusZh, taskResultTitle } from "./helpers";

const StudioResults = forwardRef(function StudioResults({
  task,
  runningSnapshot,
  showRunningProgress,
  trackingLost,
  backgroundTasks = [],
  submitting,
  works,
  lightbox,
  setLightbox,
  busyAssetIds,
  onRefreshActiveTask,
  onCancelTask,
  taskEtaText,
  worksError,
  onReloadWorks,
  onDismissBackgroundTask,
  onCancelBackgroundTask,
  onUnlock,
  onDownload,
  onVariation,
  sourceAsset = null,
  reverseOperationId = null,
  reverseRevisionId = null,
  reproductionModelConfigId = null,
  reproductionVideoComposition = null,
  requestQuoteConfirmation,
  onReproductionCorrectionCreated,
  onReproductionRemediationCreated,
  onReproductionRemediationExecutionSubmitted,
  reproductionAssessmentEnabled = true,
}, ref) {
  const cancelableStatus = task?.status;
  const showCancel = ["queued", "running"].includes(cancelableStatus);
  const visibleBackgroundTasks = (backgroundTasks || []).filter((item) => item?.id && item.id !== task?.id);
  const assessmentSourceAsset = reproductionSourceAsset(task, sourceAsset);

  return (
    <>
      {task && (
        <section ref={ref} className="mx-auto mt-8 scroll-mt-5 max-w-5xl animate-fadeup lg:scroll-mt-6">
          <div className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-sm font-display font-semibold">{taskResultTitle(task, runningSnapshot)}</span>
              <div className="flex items-center gap-2">
                {showCancel && (
                  <button type="button" onClick={onCancelTask} className="btn-ghost btn-sm text-bad">
                    {cancelableStatus === "running" ? "提交取消请求" : "取消任务"}
                  </button>
                )}
                <span className={`badge ${statusStyle(task.status)}`}>{statusZh(task.status)}</span>
              </div>
            </div>
            {showRunningProgress && (
              <div className="mb-4">
                {taskEtaText && (
                  <p className="mb-2 rounded-lg border border-line bg-white/[0.035] px-3 py-2 text-xs text-mist">
                    {taskEtaText}
                  </p>
                )}
                <div className="mb-2 h-1.5 w-full overflow-hidden rounded-full bg-white/[0.08]">
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
            {task.status === "needs_review" && (
              <div className="mb-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                {task.phase === "reconciling"
                  ? "模型提交或结果状态待核对；平台会按实际结果结算或退回积分，请勿重复提交。"
                  : "任务状态待确认，请在历史记录查看后续结果和积分状态。"}
              </div>
            )}
            {task.error && (
              <p className="mb-3 rounded-lg bg-bad/10 px-3 py-2 text-sm text-bad">
                {classifyGenerationError(task, { category: task.category }).message}
              </p>
            )}
            {task.partial && (
              <div className="mb-3 rounded-lg bg-warn/10 px-3 py-2 text-sm text-warn">
                {task.status === "needs_review" ? (
                  <p>
                    本次批量生成已保存 {task.saved_count || task.assets?.length || 0}/{task.requested_count || "?"} 张成功结果，
                    剩余部分状态未知，积分暂不结算或退回，待系统确认。
                  </p>
                ) : (
                  <p>
                    本次批量生成完成 {task.saved_count || task.assets?.length || 0}/{task.requested_count || "?"} 张，
                    失败部分已按实际成功张数结算。
                  </p>
                )}
                {task.partial_errors?.length > 0 && (
                  <p className="mt-1 text-xs text-warn/80">未完成原因：{task.partial_errors.join("；")}</p>
                )}
              </div>
            )}
            {task.assets?.length > 0 && (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {task.assets.map((asset) => (
                    <ResultCard
                      key={asset.id}
                      a={asset}
                      unlocking={busyAssetIds?.has(asset.id)}
                      onOpen={() => setLightbox(asset)}
                      onUnlock={() => onUnlock(asset)}
                      onDownload={() => onDownload(asset)}
                      onVariation={onVariation}
                    />
                  ))}
                </div>
                {reproductionAssessmentEnabled && (
                  <StudioReproductionAssessment
                    sourceAsset={assessmentSourceAsset}
                    generatedAssets={task.assets}
                    generationTaskId={task.id}
                    reverseOperationId={task.reverse_operation_id || reverseOperationId}
                    reverseRevisionId={task.source_revision_id || reverseRevisionId}
                    modelConfigId={reproductionModelConfigId}
                    remediationParams={task.params || {}}
                    videoComposition={reproductionVideoComposition}
                    requestQuoteConfirmation={requestQuoteConfirmation}
                    onCorrectionCreated={onReproductionCorrectionCreated}
                    onRemediationCreated={onReproductionRemediationCreated}
                    onRemediationExecutionSubmitted={onReproductionRemediationExecutionSubmitted}
                  />
                )}
              </>
            )}
          </div>
        </section>
      )}

      {visibleBackgroundTasks.length > 0 && (
        <section className="mx-auto mt-4 max-w-5xl">
          <div className="rounded-xl2 border border-line bg-base2/80 p-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-display font-semibold text-snow">后台生成任务</h3>
                <p className="text-xs text-fog">图片任务可并发提交，旧任务完成后会同步到作品墙。</p>
              </div>
              <a href="/history" className="btn-secondary btn-sm">历史记录</a>
            </div>
            <div className="space-y-2">
              {visibleBackgroundTasks.map((item) => {
                const terminal = isTerminalTaskStatus(item.status);
                const cancelable = ["queued", "running"].includes(item.status);
                return (
                  <div key={item.id} className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm text-mist">
                            {item.category === "video" ? "视频" : "图片"} · {item.requested_count || item.params?.n || 1} 张
                          </span>
                          <span className={`badge ${statusStyle(item.status)}`}>{statusZh(item.status)}</span>
                        </div>
                        {item.error && <p className="mt-1 text-xs text-bad">{item.error}</p>}
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        {cancelable && (
                          <button
                            type="button"
                            onClick={() => onCancelBackgroundTask?.(item.id)}
                            className="btn-ghost btn-sm text-bad"
                          >
                            取消
                          </button>
                        )}
                        {(terminal || item.status === "unknown") && (
                          <button
                            type="button"
                            onClick={() => onDismissBackgroundTask?.(item.id)}
                            className="btn-ghost btn-sm"
                          >
                            关闭
                          </button>
                        )}
                      </div>
                    </div>
                    {!terminal && item.status !== "unknown" && (
                      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-white/[0.08]">
                        <div
                          className="h-full rounded-full bg-brand transition-all duration-500"
                          style={{ width: `${item.progress || 8}%` }}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </section>
      )}

      <section className="mx-auto mt-14 max-w-7xl">
        <div className="mb-5 flex items-end justify-between">
          <div>
            <h2 className="text-xl font-bold">我的作品墙</h2>
            <p className="mt-1 text-sm text-fog">最近生成的创作，点击查看、生成变体或下载。</p>
          </div>
          <a href="/profile" className="btn-secondary btn-sm">查看全部</a>
        </div>
        {worksError && (
          <div className="mb-4 flex flex-col gap-2 rounded-xl border border-warn/30 bg-warn/10 px-4 py-3 text-sm text-warn sm:flex-row sm:items-center sm:justify-between">
            <span>{worksError}</span>
            <button type="button" onClick={onReloadWorks} className="btn-secondary btn-sm">
              重试加载
            </button>
          </div>
        )}
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
          <GroupedAssetGallery
            assets={works}
            renderAsset={(asset) => (
              <ResultCard
                a={asset}
                fixedAspect
                unlocking={busyAssetIds?.has(asset.id)}
                onOpen={() => setLightbox(asset)}
                onUnlock={() => onUnlock(asset)}
                onDownload={() => onDownload(asset)}
                onVariation={onVariation}
              />
            )}
          />
        )}
      </section>

      {lightbox && (
        <Lightbox
          a={lightbox}
          unlocking={busyAssetIds?.has(lightbox.id)}
          onClose={() => setLightbox(null)}
          onUnlock={() => onUnlock(lightbox)}
          onDownload={() => onDownload(lightbox)}
          onVariation={onVariation}
        />
      )}
    </>
  );
});

StudioResults.displayName = "StudioResults";

export default StudioResults;
