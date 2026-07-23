export type VideoSourceRange = {
  start_seconds: number;
  end_seconds: number;
};

export type VideoTimelineConfigIssue = {
  field: "source_ranges" | "custom_keyframes";
  message: string;
};

export const VIDEO_TIMELINE_STEP_SECONDS = 0.1;

function finite(value: unknown): number | null {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function rounded(value: number) {
  return Math.round(value * 1000) / 1000;
}

export function videoTimelineDuration(value: unknown) {
  const parsed = finite(value);
  return parsed !== null && parsed > 0 ? rounded(parsed) : 0;
}

function sourceRangeValues(value: unknown) {
  return Array.isArray(value) ? value : [];
}

/**
 * Preserve invalid, restored values long enough for the UI to explain why they
 * cannot be used. Normalizers intentionally repair values for request safety,
 * but should not make a bad historical draft look valid to the creator.
 */
export function videoTimelineConfigIssues(
  rangeValue: unknown,
  keyframeValue: unknown,
  durationValue: unknown,
  maxRanges = 8,
  maxKeyframes = 24,
): VideoTimelineConfigIssue[] {
  const duration = videoTimelineDuration(durationValue);
  if (!duration) return [];
  const issues: VideoTimelineConfigIssue[] = [];
  const validRanges: VideoSourceRange[] = [];
  const sourceRanges = sourceRangeValues(rangeValue);
  if (sourceRanges.length > maxRanges) {
    issues.push({ field: "source_ranges", message: `最多选择 ${maxRanges} 个分析片段。` });
  }
  for (const item of sourceRanges) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      issues.push({ field: "source_ranges", message: "分析片段格式无效。" });
      continue;
    }
    const raw = item as Record<string, unknown>;
    const start = finite(raw.start_seconds ?? raw.start);
    const end = finite(raw.end_seconds ?? raw.end);
    if (start === null || end === null) {
      issues.push({ field: "source_ranges", message: "分析片段必须同时填写开始和结束时间。" });
    } else if (start < 0 || end <= start) {
      issues.push({ field: "source_ranges", message: "结束时间必须大于开始时间，且开始时间不能小于 0。" });
    } else if (end > duration) {
      issues.push({ field: "source_ranges", message: "分析片段不能超过当前视频时长。" });
    } else {
      validRanges.push({ start_seconds: start, end_seconds: end });
    }
  }
  const ordered = [...validRanges].sort((left, right) => left.start_seconds - right.start_seconds);
  if (ordered.some((range, index) => index > 0 && range.start_seconds < ordered[index - 1].end_seconds)) {
    issues.push({ field: "source_ranges", message: "分析片段不能重叠；请拖动边界分开或删除重复片段。" });
  }

  const keyframes = Array.isArray(keyframeValue) ? keyframeValue : [];
  if (keyframes.length > maxKeyframes) {
    issues.push({ field: "custom_keyframes", message: `最多选择 ${maxKeyframes} 个关键帧。` });
  }
  const normalizedRanges = normalizeVideoSourceRanges(validRanges, duration, maxRanges);
  if (keyframes.some((item) => {
    const time = finite(item);
    return time === null || time < 0 || time > duration;
  })) {
    issues.push({ field: "custom_keyframes", message: "关键帧必须位于当前视频时长范围内。" });
  } else if (normalizedRanges.length > 0 && keyframes.some((item) => {
    const time = finite(item);
    return time !== null && !isVideoTimelineTimeSelected(time, normalizedRanges, duration);
  })) {
    issues.push({ field: "custom_keyframes", message: "关键帧必须位于某个已选分析片段内。" });
  }
  return [...new Map(issues.map((issue) => [`${issue.field}:${issue.message}`, issue])).values()];
}

export function clampVideoTimelineTime(value: unknown, durationValue: unknown) {
  const duration = videoTimelineDuration(durationValue);
  const parsed = finite(value) ?? 0;
  return rounded(Math.max(0, Math.min(duration, parsed)));
}

export function normalizeVideoSourceRanges(
  value: unknown,
  durationValue: unknown,
  maxRanges = 8,
): VideoSourceRange[] {
  const duration = videoTimelineDuration(durationValue);
  if (!duration || !Array.isArray(value)) return [];
  const normalized = value.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const raw = item as Record<string, unknown>;
    const startValue = finite(raw.start_seconds ?? raw.start);
    const endValue = finite(raw.end_seconds ?? raw.end);
    if (startValue === null || endValue === null) return [];
    const start = clampVideoTimelineTime(startValue, duration);
    const end = clampVideoTimelineTime(endValue, duration);
    return end > start ? [{ start_seconds: start, end_seconds: end }] : [];
  }).sort((left, right) => (
    left.start_seconds - right.start_seconds || left.end_seconds - right.end_seconds
  ));

  const merged = normalized.reduce<VideoSourceRange[]>((result, range) => {
    const previous = result.at(-1);
    if (previous && range.start_seconds <= previous.end_seconds) {
      previous.end_seconds = Math.max(previous.end_seconds, range.end_seconds);
    } else {
      result.push({ ...range });
    }
    return result;
  }, []);
  return merged.slice(0, Math.max(1, Math.trunc(maxRanges)));
}

export function isVideoTimelineTimeSelected(
  value: unknown,
  ranges: VideoSourceRange[],
  durationValue: unknown,
) {
  const duration = videoTimelineDuration(durationValue);
  const time = finite(value);
  if (!duration || time === null || time < 0 || time > duration) return false;
  return ranges.length === 0 || ranges.some(
    (range) => range.start_seconds <= time && time <= range.end_seconds,
  );
}

export function normalizeVideoKeyframes(
  value: unknown,
  durationValue: unknown,
  rangeValue: unknown = [],
  maxKeyframes = 24,
) {
  const duration = videoTimelineDuration(durationValue);
  if (!duration || !Array.isArray(value)) return [];
  const ranges = normalizeVideoSourceRanges(rangeValue, duration);
  return [...new Set(value
    .map((item) => finite(item))
    .filter((item): item is number => item !== null)
    .map((item) => clampVideoTimelineTime(item, duration))
    .filter((item) => isVideoTimelineTimeSelected(item, ranges, duration)))]
    .sort((left, right) => left - right)
    .slice(0, Math.max(1, Math.trunc(maxKeyframes)));
}

export function appendVideoSourceRange(
  rangeValue: unknown,
  durationValue: unknown,
  maxRanges = 8,
  preferredSpan = 15,
) {
  const duration = videoTimelineDuration(durationValue);
  const ranges = normalizeVideoSourceRanges(rangeValue, duration, maxRanges);
  if (!duration || ranges.length >= maxRanges) return ranges;
  const gaps: Array<{ start: number; end: number }> = [];
  let cursor = 0;
  for (const range of ranges) {
    const start = cursor > 0 ? rounded(cursor + VIDEO_TIMELINE_STEP_SECONDS) : cursor;
    const end = rounded(range.start_seconds - VIDEO_TIMELINE_STEP_SECONDS);
    if (end > start) gaps.push({ start, end });
    cursor = Math.max(cursor, range.end_seconds);
  }
  const tailStart = cursor > 0 ? rounded(cursor + VIDEO_TIMELINE_STEP_SECONDS) : 0;
  if (duration > tailStart) gaps.push({ start: tailStart, end: duration });
  const gap = gaps.sort((left, right) => (
    (right.end - right.start) - (left.end - left.start) || left.start - right.start
  ))[0];
  if (!gap) return ranges;
  const next = {
    start_seconds: gap.start,
    end_seconds: rounded(Math.min(gap.end, gap.start + Math.max(VIDEO_TIMELINE_STEP_SECONDS, preferredSpan))),
  };
  return next.end_seconds > next.start_seconds
    ? normalizeVideoSourceRanges([...ranges, next], duration, maxRanges)
    : ranges;
}

export function videoTimelineTimeFromPosition(
  clientX: unknown,
  leftValue: unknown,
  widthValue: unknown,
  durationValue: unknown,
) {
  const duration = videoTimelineDuration(durationValue);
  const x = finite(clientX) ?? 0;
  const left = finite(leftValue) ?? 0;
  const width = finite(widthValue) ?? 0;
  if (!duration || width <= 0) return 0;
  return clampVideoTimelineTime((x - left) / width * duration, duration);
}

export function videoTimelinePercent(value: unknown, durationValue: unknown) {
  const duration = videoTimelineDuration(durationValue);
  if (!duration) return 0;
  return Math.max(0, Math.min(100, clampVideoTimelineTime(value, duration) / duration * 100));
}

export function videoTimelineSelectedDuration(rangeValue: unknown, durationValue: unknown) {
  return normalizeVideoSourceRanges(rangeValue, durationValue).reduce(
    (total, range) => total + range.end_seconds - range.start_seconds,
    0,
  );
}

export function createVideoSourceRange(
  startValue: unknown,
  endValue: unknown,
  durationValue: unknown,
  minimumSpan = VIDEO_TIMELINE_STEP_SECONDS,
): VideoSourceRange | null {
  const duration = videoTimelineDuration(durationValue);
  if (!duration) return null;
  const start = clampVideoTimelineTime(Math.min(finite(startValue) ?? 0, finite(endValue) ?? 0), duration);
  const end = clampVideoTimelineTime(Math.max(finite(startValue) ?? 0, finite(endValue) ?? 0), duration);
  return end - start >= Math.max(VIDEO_TIMELINE_STEP_SECONDS, minimumSpan)
    ? { start_seconds: start, end_seconds: end }
    : null;
}

export function moveVideoSourceRange(
  rangeValue: unknown,
  durationValue: unknown,
  indexValue: unknown,
  deltaValue: unknown,
) {
  const duration = videoTimelineDuration(durationValue);
  const ranges = normalizeVideoSourceRanges(rangeValue, duration);
  const index = Math.trunc(finite(indexValue) ?? -1);
  const range = ranges[index];
  if (!range) return ranges;
  const span = range.end_seconds - range.start_seconds;
  const previousEnd = index > 0
    ? ranges[index - 1].end_seconds + VIDEO_TIMELINE_STEP_SECONDS
    : 0;
  const nextStart = index < ranges.length - 1
    ? ranges[index + 1].start_seconds - VIDEO_TIMELINE_STEP_SECONDS
    : duration;
  const maxStart = Math.max(previousEnd, nextStart - span);
  const requestedStart = range.start_seconds + (finite(deltaValue) ?? 0);
  const start = rounded(Math.max(previousEnd, Math.min(maxStart, requestedStart)));
  return ranges.map((item, rangeIndex) => rangeIndex === index
    ? { start_seconds: start, end_seconds: rounded(start + span) }
    : item);
}

export function resizeVideoSourceRange(
  rangeValue: unknown,
  durationValue: unknown,
  indexValue: unknown,
  edge: "start" | "end",
  timeValue: unknown,
) {
  const duration = videoTimelineDuration(durationValue);
  const ranges = normalizeVideoSourceRanges(rangeValue, duration);
  const index = Math.trunc(finite(indexValue) ?? -1);
  const range = ranges[index];
  if (!range || !["start", "end"].includes(edge)) return ranges;
  const time = clampVideoTimelineTime(timeValue, duration);
  if (edge === "start") {
    const minimum = index > 0
      ? ranges[index - 1].end_seconds + VIDEO_TIMELINE_STEP_SECONDS
      : 0;
    const maximum = range.end_seconds - VIDEO_TIMELINE_STEP_SECONDS;
    const start = rounded(Math.max(minimum, Math.min(maximum, time)));
    return ranges.map((item, rangeIndex) => rangeIndex === index
      ? { ...item, start_seconds: start }
      : item);
  }
  const minimum = range.start_seconds + VIDEO_TIMELINE_STEP_SECONDS;
  const maximum = index < ranges.length - 1
    ? ranges[index + 1].start_seconds - VIDEO_TIMELINE_STEP_SECONDS
    : duration;
  const end = rounded(Math.max(minimum, Math.min(maximum, time)));
  return ranges.map((item, rangeIndex) => rangeIndex === index
    ? { ...item, end_seconds: end }
    : item);
}

export function toggleVideoTimelineKeyframe(
  timeValue: unknown,
  keyframeValue: unknown,
  durationValue: unknown,
  rangeValue: unknown = [],
  tolerance = 0.15,
) {
  const duration = videoTimelineDuration(durationValue);
  const ranges = normalizeVideoSourceRanges(rangeValue, duration);
  const keyframes = normalizeVideoKeyframes(keyframeValue, duration, ranges);
  const time = clampVideoTimelineTime(timeValue, duration);
  if (!duration || !isVideoTimelineTimeSelected(time, ranges, duration)) return keyframes;
  const match = keyframes.find((item) => Math.abs(item - time) <= Math.max(0, tolerance));
  return normalizeVideoKeyframes(
    match === undefined ? [...keyframes, time] : keyframes.filter((item) => item !== match),
    duration,
    ranges,
  );
}

export function videoTimelineConfigPatch(
  rangeValue: unknown,
  keyframeValue: unknown,
  durationValue: unknown,
) {
  const sourceRanges = normalizeVideoSourceRanges(rangeValue, durationValue);
  return {
    source_range: sourceRanges.length === 1 ? sourceRanges[0] : null,
    source_ranges: sourceRanges,
    custom_keyframes: normalizeVideoKeyframes(keyframeValue, durationValue, sourceRanges),
  };
}

export function videoTimelineFrameTimes(durationValue: unknown, count = 8) {
  const duration = videoTimelineDuration(durationValue);
  const size = Math.max(2, Math.min(12, Math.trunc(count)));
  if (!duration) return [];
  return Array.from({ length: size }, (_, index) => (
    rounded(duration * (index + 0.5) / size)
  ));
}

export function videoTimelinePlaceholderFrames(durationValue: unknown, count = 8) {
  const duration = videoTimelineDuration(durationValue);
  const size = Math.max(2, Math.min(12, Math.trunc(count)));
  if (!duration) return [];
  return Array.from({ length: size }, (_, index) => ({
    id: `placeholder-${index}`,
    time_seconds: rounded(duration * index / (size - 1)),
  }));
}

export function normalizeVideoWaveform(value: unknown, maxBars = 64) {
  if (!Array.isArray(value)) return [];
  const samples = value.map((item) => finite(item))
    .filter((item): item is number => item !== null)
    .map((item) => Math.max(0, Math.min(1, Math.abs(item))));
  const limit = Math.max(8, Math.trunc(maxBars));
  if (samples.length <= limit) return samples;
  return Array.from({ length: limit }, (_, index) => {
    const start = Math.floor(index * samples.length / limit);
    const end = Math.max(start + 1, Math.floor((index + 1) * samples.length / limit));
    return Math.max(...samples.slice(start, end));
  });
}

export function formatVideoTimelineTime(value: unknown) {
  const seconds = Math.max(0, finite(value) ?? 0);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}
