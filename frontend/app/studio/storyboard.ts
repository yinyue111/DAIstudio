export type StoryboardShot = Record<string, unknown>;

export type StoryboardFrame = {
  index?: number | string;
  timestamp_seconds?: number | string;
  absolute_timestamp_seconds?: number | string;
  relative_timestamp_seconds?: number | string;
  source_segment_index?: number | string;
};

const COMPILED_SHOT_FIELDS = new Set([
  "compiled_prompt",
  "compilation",
]);

export class StoryboardEditError extends Error {}

function finite(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

function segmentIndex(shot: StoryboardShot): number {
  const parsed = Number(shot.source_segment_index);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
}

function assertEditable(shot: StoryboardShot, action: string): void {
  if (shot.locked) throw new StoryboardEditError(`镜头已锁定，无法${action}。`);
}

function cloneShots(shots: StoryboardShot[]): StoryboardShot[] {
  return shots.map((shot) => ({ ...shot }));
}

function joinedText(left: unknown, right: unknown): string {
  const values = [cleanText(left), cleanText(right)].filter(Boolean);
  return [...new Set(values)].join("；");
}

function evidenceIndices(shot: StoryboardShot): number[] {
  if (!Array.isArray(shot.evidence_frame_indices)) return [];
  return [...new Set(
    shot.evidence_frame_indices
      .map(Number)
      .filter((value) => Number.isInteger(value) && value > 0),
  )];
}

function frameTimestamp(frame: StoryboardFrame): number | null {
  for (const value of [
    frame.absolute_timestamp_seconds,
    frame.timestamp_seconds,
    frame.relative_timestamp_seconds,
  ]) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) return parsed;
  }
  return null;
}

export function clearStoryboardShotCompilation(shot: StoryboardShot): StoryboardShot {
  return Object.fromEntries(
    Object.entries(shot).filter(([key]) => !COMPILED_SHOT_FIELDS.has(key)),
  );
}

export function storyboardShotIdentity(shot: StoryboardShot): string {
  return JSON.stringify([
    segmentIndex(shot),
    finite(shot.start_seconds),
    finite(shot.end_seconds),
    cleanText(shot.visual),
    cleanText(shot.action),
    cleanText(shot.camera),
    cleanText(shot.transition),
    cleanText(shot.audio_cue),
    cleanText(shot.ocr),
    evidenceIndices(shot),
  ]);
}

export type StoryboardMutationAction =
  | "add"
  | "delete"
  | "update"
  | "split"
  | "merge"
  | "reorder"
  | "lock"
  | "boundary";

export function buildStoryboardRevisionRequest(
  result: unknown,
  shots: StoryboardShot[],
  action: StoryboardMutationAction,
  shotIndex: number,
) {
  const payload = result && typeof result === "object" && !Array.isArray(result)
    ? result as Record<string, unknown>
    : {};
  const { shots: _legacyShots, ...payloadWithoutLegacyShots } = payload;
  const analysis = payload.video_analysis && typeof payload.video_analysis === "object" && !Array.isArray(payload.video_analysis)
    ? payload.video_analysis as Record<string, unknown>
    : {};
  return {
    source: "user_edit" as const,
    payload: {
      ...payloadWithoutLegacyShots,
      video_analysis: { ...analysis, shots },
      edit_metadata: {
        action: `storyboard_${action}`,
        shot_index: Math.max(0, Math.floor(Number(shotIndex) || 0)),
      },
    },
  };
}

export function storyboardResultShots(value: unknown): StoryboardShot[] {
  const payload = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
  if (payload.video_analysis && Array.isArray(payload.video_analysis.shots)) {
    return payload.video_analysis.shots as StoryboardShot[];
  }
  return Array.isArray(payload.shots) ? payload.shots as StoryboardShot[] : [];
}

export function storyboardResultWithShots(value: unknown, shots: StoryboardShot[]) {
  const payload = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const { shots: _legacyShots, ...payloadWithoutLegacyShots } = payload;
  const analysis = payload.video_analysis && typeof payload.video_analysis === "object" && !Array.isArray(payload.video_analysis)
    ? payload.video_analysis as Record<string, unknown>
    : {};
  return {
    ...payloadWithoutLegacyShots,
    video_analysis: { ...analysis, shots },
  };
}

export function storyboardShotsFromRevision(value: unknown): StoryboardShot[] {
  const revision = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
  const payload = revision.payload && typeof revision.payload === "object" ? revision.payload : revision;
  return storyboardResultShots(payload);
}

function explicitAppliedMarker(operation: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(operation, key)) continue;
    const raw = operation[key];
    const value = Number(raw);
    return {
      present: true,
      value: raw != null && raw !== "" && Number.isInteger(value) && value > 0 ? value : null,
    };
  }
  return { present: false, value: null };
}

export function resolveAppliedRevision(revisionsValue: unknown, operationValue: unknown) {
  const revisions = Array.isArray(revisionsValue) ? revisionsValue : [];
  const operation = operationValue && typeof operationValue === "object" && !Array.isArray(operationValue)
    ? operationValue as Record<string, unknown>
    : {};
  const eligible = revisions.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const revision = value as Record<string, unknown>;
    const id = Number(revision.id);
    return revision.source === "applied"
      && revision.lineage_status === "verified"
      && Number.isInteger(id)
      && id > 0
      ? [revision]
      : [];
  }).sort((left, right) => (
    Number(right.version || 0) - Number(left.version || 0)
    || Number(right.id || 0) - Number(left.id || 0)
  ));
  const versionMarker = explicitAppliedMarker(operation, [
    "applied_result_version",
    "reverse_applied_version",
    "reverseAppliedVersion",
  ]);
  const revisionMarker = explicitAppliedMarker(operation, [
    "applied_result_revision_id",
    "applied_revision_id",
    "reverse_applied_revision_id",
    "reverseAppliedRevisionId",
  ]);
  if (versionMarker.present || revisionMarker.present) {
    if (
      (versionMarker.present && versionMarker.value == null)
      || (revisionMarker.present && revisionMarker.value == null)
    ) return null;
    return eligible.find((revision) => (
      (!versionMarker.present || Number(revision.version) === versionMarker.value)
      && (!revisionMarker.present || Number(revision.id) === revisionMarker.value)
    )) || null;
  }
  // Legacy operation snapshots have no applied marker. Only they may use the newest verified applied revision.
  return eligible[0] || null;
}

export const verifiedAppliedStoryboardRevision = resolveAppliedRevision;

export type StoryboardMutation = {
  action: string;
  apply: (shots: StoryboardShot[]) => StoryboardShot[];
  request: (confirmedShots: StoryboardShot[], optimisticShots: StoryboardShot[]) => Promise<unknown>;
  resolve?: (value: unknown, optimisticShots: StoryboardShot[]) => StoryboardShot[];
};

type QueuedStoryboardMutation = StoryboardMutation & { id: number };

export function createStoryboardMutationCoordinator({
  initialShots = [],
  onChange,
  onError,
  onPendingChange,
}: {
  initialShots?: StoryboardShot[];
  onChange?: (shots: StoryboardShot[]) => void;
  onError?: (error: unknown) => void;
  onPendingChange?: (action: string) => void;
} = {}) {
  let active = true;
  let processing = false;
  let nextId = 1;
  let confirmed = cloneShots(initialShots);
  let optimistic = cloneShots(initialShots);
  let queue: QueuedStoryboardMutation[] = [];
  let idleWaiters: Array<() => void> = [];

  function finishIdleWaiters() {
    if (processing || queue.length > 0) return;
    const waiters = idleWaiters;
    idleWaiters = [];
    waiters.forEach((resolve) => resolve());
  }

  function emitPending() {
    if (active) onPendingChange?.(queue[0]?.action || "");
  }

  function emitOptimistic() {
    if (active) onChange?.(cloneShots(optimistic));
  }

  function replayQueuedMutations() {
    let nextShots = cloneShots(confirmed);
    const retained: QueuedStoryboardMutation[] = [];
    for (const mutation of queue) {
      try {
        nextShots = mutation.apply(nextShots);
        retained.push(mutation);
      } catch (error) {
        if (active) onError?.(error);
      }
    }
    queue = retained;
    optimistic = cloneShots(nextShots);
    emitOptimistic();
    emitPending();
  }

  async function drain() {
    if (!active || processing) return;
    processing = true;
    while (active && queue.length > 0) {
      const mutation = queue[0];
      let nextShots: StoryboardShot[];
      try {
        nextShots = mutation.apply(cloneShots(confirmed));
      } catch (error) {
        queue.shift();
        onError?.(error);
        replayQueuedMutations();
        continue;
      }
      try {
        const value = await mutation.request(cloneShots(confirmed), cloneShots(nextShots));
        if (!active) break;
        const resolved = mutation.resolve?.(value, cloneShots(nextShots)) || nextShots;
        confirmed = cloneShots(resolved);
      } catch (error) {
        if (!active) break;
        onError?.(error);
      }
      if (!active) break;
      if (queue[0]?.id === mutation.id) queue.shift();
      replayQueuedMutations();
    }
    processing = false;
    if (active) emitPending();
    finishIdleWaiters();
  }

  return {
    enqueue(mutation: StoryboardMutation): boolean {
      if (!active) return false;
      try {
        const nextOptimistic = mutation.apply(cloneShots(optimistic));
        queue.push({ ...mutation, id: nextId });
        nextId += 1;
        optimistic = cloneShots(nextOptimistic);
        onError?.(null);
        emitOptimistic();
        emitPending();
        void drain();
        return true;
      } catch (error) {
        onError?.(error);
        return false;
      }
    },
    syncConfirmed(shots: StoryboardShot[]): void {
      if (!active || processing || queue.length > 0) return;
      confirmed = cloneShots(shots);
      optimistic = cloneShots(shots);
    },
    snapshot() {
      return {
        confirmed: cloneShots(confirmed),
        optimistic: cloneShots(optimistic),
        pending: queue.length,
        active,
      };
    },
    whenIdle(): Promise<void> {
      if (!processing && queue.length === 0) return Promise.resolve();
      return new Promise((resolve) => idleWaiters.push(resolve));
    },
    dispose(): void {
      active = false;
      queue = [];
      processing = false;
      idleWaiters.splice(0).forEach((resolve) => resolve());
    },
  };
}

export function buildStoryboardShotGenerationPrepareRequest(
  shotValue: unknown,
  revisionValue: unknown,
  clientRequestId: string,
) {
  const shot = shotValue && typeof shotValue === "object" && !Array.isArray(shotValue)
    ? shotValue as Record<string, unknown>
    : {};
  const revision = revisionValue && typeof revisionValue === "object" && !Array.isArray(revisionValue)
    ? revisionValue as Record<string, unknown>
    : {};
  const shotId = String(shot.shot_id || "").trim();
  const revisionId = Number(revision.id);
  if (
    !shotId
    || revision.source !== "applied"
    || revision.lineage_status !== "verified"
    || !Number.isInteger(revisionId)
    || revisionId <= 0
  ) return null;
  return {
    client_request_id: clientRequestId,
    shot_id: shotId,
    revision_id: revisionId,
    params: {},
  };
}

export function buildStoryboardEditRequest(
  shots: StoryboardShot[],
  action: "split" | "merge" | "reorder" | "lock" | "boundary",
  shotIndex: number,
  clientRequestId: string,
  detail: { split_seconds?: number; boundary_seconds?: number; direction?: number } = {},
) {
  const shot = shots[shotIndex];
  const shotId = String(shot?.shot_id || "").trim();
  if (!shotId) return null;
  if (action === "split") {
    return { client_request_id: clientRequestId, action, shot_id: shotId, split_seconds: detail.split_seconds };
  }
  if (action === "merge") {
    const nextId = String(shots[shotIndex + 1]?.shot_id || "").trim();
    return nextId ? { client_request_id: clientRequestId, action, shot_ids: [shotId, nextId] } : null;
  }
  if (action === "reorder") {
    const reordered = moveStoryboardShot(shots, shotIndex, Number(detail.direction) < 0 ? -1 : 1);
    const orderedIds = reordered.map((item) => String(item.shot_id || "").trim());
    return orderedIds.every(Boolean)
      ? { client_request_id: clientRequestId, action, ordered_shot_ids: orderedIds }
      : null;
  }
  if (action === "lock") {
    return { client_request_id: clientRequestId, action, shot_id: shotId, locked: !Boolean(shot.locked) };
  }
  if (action === "boundary") {
    const nextId = String(shots[shotIndex + 1]?.shot_id || "").trim();
    return nextId && Number.isFinite(Number(detail.boundary_seconds))
      ? {
          client_request_id: clientRequestId,
          action,
          shot_ids: [shotId, nextId],
          boundary_seconds: Number(detail.boundary_seconds),
        }
      : null;
  }
  return null;
}

export type OptimisticStoryboardState = {
  shots: StoryboardShot[];
  committed: StoryboardShot[];
  pending_token: number | null;
  error: string | null;
};

export function optimisticStoryboardReducer(
  state: OptimisticStoryboardState,
  event:
    | { type: "apply"; token: number; shots: StoryboardShot[] }
    | { type: "commit"; token: number }
    | { type: "rollback"; token: number; error: string },
): OptimisticStoryboardState {
  if (event.type === "apply") {
    return { ...state, shots: event.shots, pending_token: event.token, error: null };
  }
  if (state.pending_token !== event.token) return state;
  if (event.type === "commit") {
    return { ...state, committed: state.shots, pending_token: null, error: null };
  }
  return { ...state, shots: state.committed, pending_token: null, error: event.error };
}

export function composeStoryboardShotPrompt(
  shot: StoryboardShot,
  { index = 0 }: { index?: number } = {},
): string {
  const start = finite(shot.start_seconds);
  const end = finite(shot.end_seconds, start);
  const segment = segmentIndex(shot);
  const header = [
    `镜头 ${Math.max(1, index + 1)}`,
    `${start.toFixed(2)}-${end.toFixed(2)} 秒`,
    segment ? `来源片段 ${segment}` : "",
  ].filter(Boolean).join(" · ");
  const rows = [
    ["画面", shot.visual],
    ["动作", shot.action],
    ["镜头", shot.camera],
    ["转场", shot.transition],
    ["声音", shot.audio_cue],
    ["画面文字", shot.ocr],
  ].map(([label, value]) => {
    const text = cleanText(value);
    return text ? `${label}：${text}` : "";
  }).filter(Boolean);
  return [header, ...rows].join("\n");
}

export function splitStoryboardShot(
  shots: StoryboardShot[],
  index: number,
  splitAt: number,
  frames: StoryboardFrame[] = [],
): StoryboardShot[] {
  const shot = shots[index];
  if (!shot) throw new StoryboardEditError("没有找到要拆分的镜头。");
  assertEditable(shot, "拆分");
  const start = finite(shot.start_seconds);
  const end = finite(shot.end_seconds, start);
  const split = finite(splitAt, Number.NaN);
  if (!Number.isFinite(split) || split <= start + 0.05 || split >= end - 0.05) {
    throw new StoryboardEditError("拆分时间必须位于镜头内部，并为两侧各保留至少 0.05 秒。");
  }

  const frameByIndex = new Map<number, StoryboardFrame>();
  for (const frame of frames) {
    const frameIndex = Number(frame.index);
    if (Number.isInteger(frameIndex) && frameIndex > 0) frameByIndex.set(frameIndex, frame);
  }
  const originalEvidence = evidenceIndices(shot);
  const leftEvidence: number[] = [];
  const rightEvidence: number[] = [];
  const fallbackBoundary = Math.ceil(originalEvidence.length / 2);
  originalEvidence.forEach((frameIndex, evidenceIndex) => {
    const frame = frameByIndex.get(frameIndex);
    const timestamp = frame ? frameTimestamp(frame) : null;
    if (timestamp === null) {
      (evidenceIndex < fallbackBoundary ? leftEvidence : rightEvidence).push(frameIndex);
      return;
    }
    (timestamp <= split ? leftEvidence : rightEvidence).push(frameIndex);
  });

  const base = clearStoryboardShotCompilation(shot);
  const left: StoryboardShot = {
    ...base,
    end_seconds: Number(split.toFixed(3)),
    transition: "",
    evidence_frame_indices: leftEvidence,
  };
  const right: StoryboardShot = {
    ...base,
    start_seconds: Number(split.toFixed(3)),
    evidence_frame_indices: rightEvidence,
  };
  return [...shots.slice(0, index), left, right, ...shots.slice(index + 1)];
}

export function mergeStoryboardShots(shots: StoryboardShot[], index: number): StoryboardShot[] {
  const left = shots[index];
  const right = shots[index + 1];
  if (!left || !right) throw new StoryboardEditError("只能合并当前镜头与其下一个镜头。");
  assertEditable(left, "合并");
  assertEditable(right, "合并");
  if (segmentIndex(left) !== segmentIndex(right)) {
    throw new StoryboardEditError("不同来源片段的镜头不能合并。");
  }
  const leftDuration = Math.max(0, finite(left.end_seconds) - finite(left.start_seconds));
  const rightDuration = Math.max(0, finite(right.end_seconds) - finite(right.start_seconds));
  const totalDuration = leftDuration + rightDuration;
  const leftConfidence = finite(left.confidence);
  const rightConfidence = finite(right.confidence);
  const confidence = totalDuration > 0
    ? (leftConfidence * leftDuration + rightConfidence * rightDuration) / totalDuration
    : Math.min(leftConfidence, rightConfidence);
  const merged: StoryboardShot = clearStoryboardShotCompilation({
    ...left,
    start_seconds: Math.min(finite(left.start_seconds), finite(right.start_seconds)),
    end_seconds: Math.max(finite(left.end_seconds), finite(right.end_seconds)),
    visual: joinedText(left.visual, right.visual),
    action: joinedText(left.action, right.action),
    camera: joinedText(left.camera, right.camera),
    transition: cleanText(right.transition) || cleanText(left.transition),
    audio_cue: joinedText(left.audio_cue, right.audio_cue),
    ocr: joinedText(left.ocr, right.ocr),
    evidence_frame_indices: [...new Set([...evidenceIndices(left), ...evidenceIndices(right)])],
    confidence: Number(Math.max(0, Math.min(1, confidence)).toFixed(4)),
    locked: false,
  });
  return [...shots.slice(0, index), merged, ...shots.slice(index + 2)];
}

export function moveStoryboardBoundary(
  shots: StoryboardShot[],
  index: number,
  boundaryValue: number,
  frames: StoryboardFrame[] = [],
): StoryboardShot[] {
  const left = shots[index];
  const right = shots[index + 1];
  if (!left || !right) throw new StoryboardEditError("当前镜头没有可调整的下一镜头。");
  assertEditable(left, "调整边界");
  assertEditable(right, "调整边界");
  if (segmentIndex(left) !== segmentIndex(right)) {
    throw new StoryboardEditError("不能跨不同来源片段调整镜头边界。");
  }
  const boundary = finite(boundaryValue, Number.NaN);
  if (!(finite(left.start_seconds) < boundary && boundary < finite(right.end_seconds))) {
    throw new StoryboardEditError("边界必须位于两个镜头的总时间范围内。");
  }
  const leftEvidence = new Set(evidenceIndices(left));
  const rightEvidence = new Set(evidenceIndices(right));
  const combined = [...new Set([...leftEvidence, ...rightEvidence])];
  const frameByIndex = new Map<number, StoryboardFrame>();
  for (const frame of frames) {
    const frameIndex = Number(frame.index);
    if (Number.isInteger(frameIndex) && frameIndex > 0) frameByIndex.set(frameIndex, frame);
  }
  const supports = (frameIndex: number, direction: "left" | "right") => {
    const frame = frameByIndex.get(frameIndex);
    if (!frame) return direction === "left" ? leftEvidence.has(frameIndex) : rightEvidence.has(frameIndex);
    const timestamps = [
      frame.absolute_timestamp_seconds,
      frame.timestamp_seconds,
      frame.relative_timestamp_seconds,
    ].map(Number).filter(Number.isFinite);
    return timestamps.some((timestamp) => direction === "left" ? timestamp <= boundary : timestamp >= boundary);
  };
  const next = cloneShots(shots);
  next[index] = clearStoryboardShotCompilation({
    ...left,
    end_seconds: boundary,
    evidence_frame_indices: combined.filter((frameIndex) => supports(frameIndex, "left")),
  });
  next[index + 1] = clearStoryboardShotCompilation({
    ...right,
    start_seconds: boundary,
    evidence_frame_indices: combined.filter((frameIndex) => supports(frameIndex, "right")),
  });
  return next;
}

export function moveStoryboardShot(
  shots: StoryboardShot[],
  index: number,
  direction: -1 | 1,
): StoryboardShot[] {
  const target = index + direction;
  const current = shots[index];
  const adjacent = shots[target];
  if (!current || !adjacent) throw new StoryboardEditError("镜头已经位于当前片段边界。");
  assertEditable(current, "移动");
  assertEditable(adjacent, "交换顺序");
  if (segmentIndex(current) !== segmentIndex(adjacent)) {
    throw new StoryboardEditError("镜头不能跨来源片段移动。");
  }
  const next = cloneShots(shots);
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function toggleStoryboardShotLock(shots: StoryboardShot[], index: number): StoryboardShot[] {
  if (!shots[index]) throw new StoryboardEditError("没有找到要锁定的镜头。");
  return shots.map((shot, shotIndex) => (
    shotIndex === index ? { ...shot, locked: !Boolean(shot.locked) } : { ...shot }
  ));
}
