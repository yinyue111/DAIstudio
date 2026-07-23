"use client";

import {
  AudioLines,
  KeyRound,
  Loader2,
  MoveHorizontal,
  Plus,
  Trash2,
  VolumeX,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useReferenceVideoSource } from "./StudioMedia.jsx";
import {
  appendVideoSourceRange,
  clampVideoTimelineTime,
  createVideoSourceRange,
  formatVideoTimelineTime,
  isVideoTimelineTimeSelected,
  moveVideoSourceRange,
  normalizeVideoKeyframes,
  normalizeVideoSourceRanges,
  resizeVideoSourceRange,
  toggleVideoTimelineKeyframe,
  videoTimelineConfigIssues,
  videoTimelineConfigPatch,
  videoTimelinePercent,
  videoTimelineSelectedDuration,
  videoTimelineTimeFromPosition,
} from "./videoSourceTimeline";
import useVideoSourceTimelineMedia from "./useVideoSourceTimelineMedia";

const KEYBOARD_STEP_SECONDS = 0.1;

function sameTimelinePatch(config, patch) {
  return JSON.stringify({
    source_range: config?.source_range || null,
    source_ranges: Array.isArray(config?.source_ranges) ? config.source_ranges : [],
    custom_keyframes: Array.isArray(config?.custom_keyframes) ? config.custom_keyframes : [],
  }) === JSON.stringify(patch);
}

function TimelineFrames({ frames, status, message }) {
  if (status === "ready" && frames.length > 0) {
    return (
      <div className="absolute inset-0 flex overflow-hidden bg-black/30" aria-hidden="true">
        {frames.map((frame) => (
          <img
            key={frame.id}
            src={frame.src}
            alt=""
            className="h-full min-w-0 flex-1 select-none border-r border-black/25 object-cover last:border-r-0"
            draggable={false}
          />
        ))}
      </div>
    );
  }
  if (status === "loading") {
    return (
      <div className="absolute inset-0 grid grid-cols-8 overflow-hidden bg-black/25" aria-label="正在提取视频缩略图">
        {Array.from({ length: 8 }, (_, index) => (
          <span key={index} className="animate-pulse border-r border-black/25 bg-white/[0.045] last:border-r-0" />
        ))}
      </div>
    );
  }
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/25 px-3 text-center text-[10px] text-fog">
      {message || "缩略图不可用"}
    </div>
  );
}

function Waveform({ values, status, message, onAnalyze, disabled }) {
  const ready = status === "ready" && values.length > 0;
  return (
    <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
      <div className="relative flex h-11 min-w-0 items-center overflow-hidden rounded-lg border border-line bg-black/20 px-1.5" aria-live="polite">
        {ready ? (
          <div className="flex h-8 w-full items-center gap-px" aria-label="视频音轨波形">
            {values.map((value, index) => (
              <span
                key={index}
                className="min-w-px flex-1 bg-aqua/70"
                style={{ height: `${Math.max(8, Math.round(value * 100))}%` }}
              />
            ))}
          </div>
        ) : (
          <div className={`flex min-w-0 items-center gap-2 text-[10px] ${status === "no_audio" || status === "unavailable" ? "text-warn" : "text-fog"}`}>
            {status === "loading" ? <Loader2 size={13} className="shrink-0 animate-spin" aria-hidden="true" /> : <VolumeX size={13} className="shrink-0" aria-hidden="true" />}
            <span className="truncate" title={message}>{message}</span>
          </div>
        )}
      </div>
      <button
        type="button"
        className="btn-secondary btn-sm min-h-10 px-2.5"
        disabled={disabled || status === "loading"}
        onClick={onAnalyze}
        title="读取当前视频的真实音轨并生成波形"
      >
        {status === "loading" ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <AudioLines size={14} aria-hidden="true" />}
        {ready ? "重读" : "波形"}
      </button>
    </div>
  );
}

export default function StudioVideoSourceTimeline({
  asset,
  config,
  configuredDuration = null,
  disabled = false,
  onChange,
}) {
  const source = useReferenceVideoSource(asset);
  const media = useVideoSourceTimelineMedia({
    src: source.status === "ready" ? source.src : "",
    configuredDuration,
  });
  const [interactionMode, setInteractionMode] = useState("keyframe");
  const [playhead, setPlayhead] = useState(0);
  const [draftRange, setDraftRange] = useState(null);
  const [timelineError, setTimelineError] = useState("");
  const trackRef = useRef(null);
  const trackPointerRef = useRef(null);
  const rangeDragRef = useRef(null);
  const configRef = useRef(config);
  configRef.current = config;
  const duration = media.metadata.duration;
  const requestedRanges = useMemo(() => (
    Array.isArray(config?.source_ranges) && config.source_ranges.length > 0
      ? config.source_ranges
      : config?.source_range ? [config.source_range] : []
  ), [config?.source_range, config?.source_ranges]);
  const ranges = useMemo(
    () => normalizeVideoSourceRanges(requestedRanges, duration),
    [requestedRanges, duration],
  );
  const keyframes = useMemo(
    () => normalizeVideoKeyframes(config?.custom_keyframes, duration, ranges),
    [config?.custom_keyframes, duration, ranges],
  );
  const selectedDuration = videoTimelineSelectedDuration(ranges, duration);
  const configIssues = useMemo(
    () => videoTimelineConfigIssues(requestedRanges, config?.custom_keyframes, duration),
    [config?.custom_keyframes, duration, requestedRanges],
  );

  function commitTimeline(nextRanges, nextKeyframes = keyframes) {
    if (!duration) return;
    const patch = videoTimelineConfigPatch(nextRanges, nextKeyframes, duration);
    onChange?.({ ...configRef.current, ...patch });
  }

  useEffect(() => {
    if (!duration) return;
    if (configIssues.length > 0) return;
    const patch = videoTimelineConfigPatch(requestedRanges, config?.custom_keyframes, duration);
    if (!sameTimelinePatch(config, patch)) onChange?.({ ...config, ...patch });
  }, [config, config?.custom_keyframes, configIssues.length, duration, onChange, requestedRanges]);

  useEffect(() => {
    setPlayhead(0);
    setDraftRange(null);
    setTimelineError("");
    trackPointerRef.current = null;
    rangeDragRef.current = null;
  }, [source.src]);

  function timeFromPointer(event) {
    const rect = trackRef.current?.getBoundingClientRect();
    return rect
      ? videoTimelineTimeFromPosition(event.clientX, rect.left, rect.width, duration)
      : 0;
  }

  function toggleKeyframe(time) {
    if (!isVideoTimelineTimeSelected(time, ranges, duration)) {
      setTimelineError("关键帧必须位于某个已选分析片段内。");
      return;
    }
    const next = toggleVideoTimelineKeyframe(time, keyframes, duration, ranges);
    setTimelineError("");
    setPlayhead(clampVideoTimelineTime(time, duration));
    commitTimeline(ranges, next);
  }

  function onTrackPointerDown(event) {
    if (disabled || !duration || event.button !== 0) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const time = timeFromPointer(event);
    trackPointerRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startTime: time,
    };
    setPlayhead(time);
    if (interactionMode === "range") setDraftRange({ start: time, end: time });
  }

  function onTrackPointerMove(event) {
    const pointer = trackPointerRef.current;
    if (!pointer || pointer.pointerId !== event.pointerId) return;
    const time = timeFromPointer(event);
    setPlayhead(time);
    if (interactionMode === "range") setDraftRange({ start: pointer.startTime, end: time });
  }

  function finishTrackPointer(event, canceled = false) {
    const pointer = trackPointerRef.current;
    if (!pointer || pointer.pointerId !== event.pointerId) return;
    const time = timeFromPointer(event);
    if (!canceled && interactionMode === "keyframe" && Math.abs(event.clientX - pointer.startClientX) < 8) {
      toggleKeyframe(time);
    } else if (!canceled && interactionMode === "range") {
      const range = createVideoSourceRange(pointer.startTime, time, duration);
      if (range) {
        setTimelineError("");
        commitTimeline([...ranges, range], keyframes);
      } else {
        setTimelineError("分析片段至少需要 0.1 秒，请拖动出有效区间。");
      }
    }
    trackPointerRef.current = null;
    setDraftRange(null);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  function nudgeRange(event, index, kind) {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    const direction = event.key === 'ArrowLeft' ? -1 : 1;
    const range = ranges[index];
    if (!range) return;
    const next = kind === "move"
      ? moveVideoSourceRange(ranges, duration, index, direction * KEYBOARD_STEP_SECONDS)
      : resizeVideoSourceRange(
          ranges,
          duration,
          index,
          kind,
          range[`${kind}_seconds`] + direction * KEYBOARD_STEP_SECONDS,
        );
    commitTimeline(next, keyframes);
  }

  function startRangeDrag(event, index, kind) {
    if (disabled || !duration || event.button !== 0) return;
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    rangeDragRef.current = {
      pointerId: event.pointerId,
      index,
      kind,
      originTime: timeFromPointer(event),
      ranges,
    };
  }

  function moveRangeDrag(event) {
    const drag = rangeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.stopPropagation();
    const time = timeFromPointer(event);
    const delta = time - drag.originTime;
    const next = drag.kind === "move"
      ? moveVideoSourceRange(drag.ranges, duration, drag.index, delta)
      : resizeVideoSourceRange(
          drag.ranges,
          duration,
          drag.index,
          drag.kind,
          drag.ranges[drag.index][`${drag.kind}_seconds`] + delta,
        );
    commitTimeline(next, keyframes);
  }

  function finishRangeDrag(event) {
    if (rangeDragRef.current?.pointerId !== event.pointerId) return;
    event.stopPropagation();
    rangeDragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  function onTrackKeyDown(event) {
    if (disabled || !duration) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const direction = event.key === "ArrowLeft" ? -1 : 1;
      setPlayhead((value) => clampVideoTimelineTime(value + direction * KEYBOARD_STEP_SECONDS, duration));
    } else if ((event.key === "Enter" || event.key === " ") && interactionMode === "keyframe") {
      event.preventDefault();
      toggleKeyframe(playhead);
    }
  }

  const unavailable = source.status === "unavailable" || media.metadata.status === "error";
  const statusMessage = source.message || media.metadata.message;
  const metadataLabel = media.metadata.width && media.metadata.height
    ? ` · ${media.metadata.width}×${media.metadata.height}`
    : "";

  return (
    <section className="rounded-xl border border-line bg-black/15 p-2" aria-labelledby="source-video-timeline-title">
      <video
        ref={media.videoRef}
        src={source.status === "ready" ? source.src : undefined}
        muted
        playsInline
        preload="auto"
        className="pointer-events-none absolute h-px w-px opacity-0"
        aria-hidden="true"
        onLoadedMetadata={media.onLoadedMetadata}
        onError={media.onVideoError}
      />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h4 id="source-video-timeline-title" className="text-xs font-display font-medium text-mist">输入视频时间线</h4>
          <p className="mt-0.5 text-[10px] text-fog" aria-live="polite">
            {duration
              ? `${formatVideoTimelineTime(duration)}${metadataLabel} · ${ranges.length ? `${ranges.length} 段 / ${formatVideoTimelineTime(selectedDuration)}` : "全片"} · ${keyframes.length} 个关键帧`
              : source.status === "loading" || media.metadata.status === "loading" ? "正在读取视频元数据…" : statusMessage || "等待视频源"}
          </p>
        </div>
        <div className="flex rounded-lg border border-line bg-black/20 p-0.5" role="radiogroup" aria-label="时间线交互模式">
          <button
            type="button"
            role="radio"
            aria-checked={interactionMode === "keyframe"}
            disabled={disabled || unavailable || !duration}
            onClick={() => setInteractionMode("keyframe")}
            className={`flex min-h-9 items-center gap-1 rounded-md px-2 text-[10px] transition ${interactionMode === "keyframe" ? "bg-iris/30 text-snow" : "text-fog hover:text-mist"}`}
          >
            <KeyRound size={12} aria-hidden="true" />关键帧
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={interactionMode === "range"}
            disabled={disabled || unavailable || !duration}
            onClick={() => setInteractionMode("range")}
            className={`flex min-h-9 items-center gap-1 rounded-md px-2 text-[10px] transition ${interactionMode === "range" ? "bg-iris/30 text-snow" : "text-fog hover:text-mist"}`}
          >
            <MoveHorizontal size={12} aria-hidden="true" />片段
          </button>
        </div>
      </div>

      <div
        ref={trackRef}
        role="group"
        tabIndex={disabled || unavailable || !duration ? -1 : 0}
        aria-label={`输入视频时间线，当前 ${formatVideoTimelineTime(playhead)}`}
        className="relative mt-2 h-20 touch-none select-none overflow-hidden rounded-lg border border-line bg-black/30 outline-none focus:border-iris/70 focus:ring-2 focus:ring-iris/25"
        onPointerDown={onTrackPointerDown}
        onPointerMove={onTrackPointerMove}
        onPointerUp={(event) => finishTrackPointer(event)}
        onPointerCancel={(event) => finishTrackPointer(event, true)}
        onKeyDown={onTrackKeyDown}
      >
        <TimelineFrames frames={media.thumbnails} status={media.thumbnailStatus} message={media.thumbnailMessage} />
        <div className="pointer-events-none absolute inset-0 bg-black/10" />
        {ranges.length > 0 && <div className="pointer-events-none absolute inset-0 bg-black/50" />}
        {ranges.map((range, index) => {
          const left = videoTimelinePercent(range.start_seconds, duration);
          const right = videoTimelinePercent(range.end_seconds, duration);
          return (
            <div key={`range-${index}`} className="absolute inset-y-0" style={{ left: `${left}%`, width: `${Math.max(0.15, right - left)}%` }}>
              <div className="pointer-events-none absolute inset-0 border-y border-aqua/80 bg-aqua/15" />
              <button
                type="button"
                disabled={disabled}
                aria-label={`移动片段 ${index + 1}，${formatVideoTimelineTime(range.start_seconds)} 到 ${formatVideoTimelineTime(range.end_seconds)}`}
                className="absolute inset-y-0 left-2 right-2 cursor-grab bg-transparent outline-none focus:ring-2 focus:ring-inset focus:ring-aqua active:cursor-grabbing"
                onPointerDown={(event) => startRangeDrag(event, index, "move")}
                onPointerMove={moveRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
                onKeyDown={(event) => nudgeRange(event, index, "move")}
              />
              <button
                type="button"
                disabled={disabled}
                aria-label={`调整片段 ${index + 1} 开始时间`}
                className="absolute inset-y-0 left-0 w-3 cursor-ew-resize border-l-2 border-aqua bg-black/25 outline-none focus:ring-2 focus:ring-inset focus:ring-white"
                onPointerDown={(event) => startRangeDrag(event, index, "start")}
                onPointerMove={moveRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
                onKeyDown={(event) => nudgeRange(event, index, "start")}
              />
              <button
                type="button"
                disabled={disabled}
                aria-label={`调整片段 ${index + 1} 结束时间`}
                className="absolute inset-y-0 right-0 w-3 cursor-ew-resize border-r-2 border-aqua bg-black/25 outline-none focus:ring-2 focus:ring-inset focus:ring-white"
                onPointerDown={(event) => startRangeDrag(event, index, "end")}
                onPointerMove={moveRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
                onKeyDown={(event) => nudgeRange(event, index, "end")}
              />
            </div>
          );
        })}
        {draftRange && (
          <div
            className="pointer-events-none absolute inset-y-0 border border-dashed border-white bg-aqua/20"
            style={{
              left: `${videoTimelinePercent(Math.min(draftRange.start, draftRange.end), duration)}%`,
              width: `${Math.abs(videoTimelinePercent(draftRange.end, duration) - videoTimelinePercent(draftRange.start, duration))}%`,
            }}
          />
        )}
        {keyframes.map((time, index) => (
          <button
            key={time}
            type="button"
            disabled={disabled}
            aria-label={`移除关键帧 ${formatVideoTimelineTime(time)}`}
            title={`${formatVideoTimelineTime(time)} · 点击移除`}
            className="absolute inset-y-0 z-10 w-4 -translate-x-1/2 outline-none focus:ring-2 focus:ring-aqua"
            style={{ left: `${videoTimelinePercent(time, duration)}%` }}
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              toggleKeyframe(time);
            }}
          >
            <span className="absolute inset-y-0 left-1/2 w-px bg-rose" />
            <span className="absolute left-1/2 top-1 h-2.5 w-2.5 -translate-x-1/2 rotate-45 border border-white/70 bg-rose" />
          </button>
        ))}
        <span className="pointer-events-none absolute inset-y-0 z-20 w-px bg-white/90" style={{ left: `${videoTimelinePercent(playhead, duration)}%` }} />
      </div>

      <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-fog">
        <span>0:00.0</span>
        <span>{formatVideoTimelineTime(playhead)}</span>
        <span>{formatVideoTimelineTime(duration)}</span>
      </div>

      {ranges.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5" aria-label="已选择的视频片段">
          {ranges.map((range, index) => (
            <span key={`${range.start_seconds}-${range.end_seconds}`} className="chip min-h-9 py-1 pl-2 pr-1 text-[10px]">
              片段 {index + 1} · {formatVideoTimelineTime(range.start_seconds)}–{formatVideoTimelineTime(range.end_seconds)}
              <button
                type="button"
                className="icon-btn h-7 w-7 text-fog hover:text-bad"
                disabled={disabled}
                aria-label={`删除片段 ${index + 1}`}
                onClick={() => commitTimeline(ranges.filter((_, itemIndex) => itemIndex !== index), keyframes)}
              >
                <Trash2 size={12} aria-hidden="true" />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-secondary btn-sm min-h-10"
          disabled={disabled || unavailable || !duration || ranges.length >= 8}
          onClick={() => commitTimeline(appendVideoSourceRange(ranges, duration), keyframes)}
        >
          <Plus size={14} aria-hidden="true" />添加片段
        </button>
        {ranges.length > 0 && (
          <button type="button" className="btn-ghost btn-sm min-h-10" disabled={disabled} onClick={() => commitTimeline([], keyframes)}>
            全片分析
          </button>
        )}
      </div>

      <Waveform
        values={media.waveform}
        status={media.waveformStatus}
        message={media.waveformMessage}
        onAnalyze={media.analyzeWaveform}
        disabled={disabled || unavailable || !duration}
      />
      {(timelineError || configIssues.length > 0) && (
        <div className="mt-2 space-y-1 rounded-lg border border-bad/35 bg-bad/10 px-2.5 py-2 text-[10px] leading-relaxed text-bad" role="alert">
          {[...new Set([timelineError, ...configIssues.map((issue) => issue.message)].filter(Boolean))].map((message) => (
            <p key={message}>{message}</p>
          ))}
          {configIssues.length > 0 && (
            <button
              type="button"
              className="btn-ghost btn-sm mt-1 min-h-8 px-2 text-[10px] text-mist hover:text-snow"
              disabled={disabled}
              onClick={() => {
                setTimelineError("");
                commitTimeline(ranges, keyframes);
              }}
            >
              按当前视频修正配置
            </button>
          )}
        </div>
      )}
      {source.status === "unavailable" && (
        <p className="mt-2 text-[10px] leading-relaxed text-warn" role="status">{source.message}</p>
      )}
    </section>
  );
}
