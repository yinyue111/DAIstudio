export type CompositionStoryboardShot = Record<string, unknown>;

export type CompositionAssetSnapshot = {
  asset_ref: string;
  generation_task_id?: number | null;
  id?: number | string | null;
  origin?: string | null;
  type?: string | null;
  filename?: string | null;
  preview_url?: string | null;
  thumb?: string | null;
  url?: string | null;
};

export type VideoCompositionTransition = {
  type: "cut" | "fade" | "crossfade" | "wipeleft" | "slideright";
  duration_seconds: number;
};

export type VideoCompositionShotDraft = {
  shot_id: string;
  asset_ref: string;
  generation_task_id: number | null;
  asset?: CompositionAssetSnapshot | null;
  source_start_seconds: number;
  source_end_seconds: number | null;
  duration_seconds: number | null;
  transition: VideoCompositionTransition;
};

export type VideoCompositionSubtitleDraft = {
  start_seconds: number;
  end_seconds: number;
  text: string;
  font_size: number;
  text_color: string;
  background_color: string;
  bottom_margin: number;
};

export type VideoCompositionAudioTrackDraft = {
  asset_ref: string;
  asset?: CompositionAssetSnapshot | null;
  start_seconds: number;
  source_start_seconds: number;
  source_end_seconds: number | null;
  volume: number;
  loop: boolean;
};

export type VideoCompositionDraft = {
  schema_version: "video-composition.v1";
  title: string;
  reverse_operation_id: number | null;
  canvas: {
    width: number;
    height: number;
    fps: number;
    background_color: string;
  };
  shots: VideoCompositionShotDraft[];
  subtitles: VideoCompositionSubtitleDraft[];
  audio_tracks: VideoCompositionAudioTrackDraft[];
  original_audio_volume: number;
};

export type GenerationTaskShotBindingStatus =
  | "bound"
  | "pending_task_bound"
  | "unchanged"
  | "shot_not_found"
  | "task_not_found";

export type GenerationTaskShotBindingOptions = {
  shotId?: unknown;
  task: unknown;
  asset?: unknown;
};

export type GenerationTaskShotBindingResult = {
  status: GenerationTaskShotBindingStatus;
  changed: boolean;
  composition: VideoCompositionDraft;
  shotId: string | null;
  assetRef: string | null;
  generationTaskId: number | null;
};

export type VideoCompositionCapabilityStatus = {
  status: "unsupported" | "degraded" | "partial";
  capability: string | null;
};

const TRANSITION_TYPES = new Set(["cut", "fade", "crossfade", "wipeleft", "slideright"]);

function finite(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function bounded(value: unknown, min: number, max: number, fallback: number): number {
  return Math.min(max, Math.max(min, finite(value, fallback)));
}

function positiveInteger(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function cleanText(value: unknown): string {
  return String(value || "").trim();
}

function shotDuration(shot: CompositionStoryboardShot): number {
  return Math.max(0.1, finite(shot.end_seconds) - finite(shot.start_seconds));
}

function legacyShotId(shot: CompositionStoryboardShot, index: number): string {
  const segment = positiveInteger(shot.source_segment_index) || 1;
  const start = Math.round(finite(shot.start_seconds) * 1000);
  return `legacy-${segment}-${index + 1}-${Math.max(0, start)}`;
}

function cleanAsset(value: unknown): CompositionAssetSnapshot | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const asset = value as Record<string, unknown>;
  const assetRef = cleanText(asset.asset_ref);
  if (!assetRef) return null;
  return {
    asset_ref: assetRef,
    generation_task_id: positiveInteger(asset.generation_task_id ?? asset.task_id),
    id: asset.id as number | string | null | undefined,
    origin: cleanText(asset.origin) || null,
    type: cleanText(asset.type) || null,
    filename: cleanText(asset.filename) || null,
    preview_url: cleanText(asset.preview_url) || null,
    thumb: cleanText(asset.thumb) || null,
    url: cleanText(asset.url) || null,
  };
}

function objectValue(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
}

function generationTaskRecord(value: unknown): Record<string, any> {
  const input = objectValue(value);
  return input.task && typeof input.task === "object" && !Array.isArray(input.task)
    ? input.task as Record<string, any>
    : input;
}

function generationTaskShotId(task: Record<string, any>, explicitShotId: unknown): string {
  return cleanText(explicitShotId)
    || cleanText(task.shot_id)
    || cleanText(objectValue(task.params).shot_id)
    || cleanText(objectValue(task.metadata).shot_id)
    || cleanText(objectValue(objectValue(task.request).params).shot_id);
}

function cleanGeneratedVideoAsset(
  value: unknown,
  generationTaskId: number | null,
): CompositionAssetSnapshot | null {
  const asset = objectValue(value);
  const type = cleanText(asset.type);
  if (!Object.keys(asset).length || (type && type !== "video")) return null;
  const id = positiveInteger(asset.id);
  const origin = cleanText(asset.origin);
  const assetRef = cleanText(asset.asset_ref)
    || (origin !== "uploaded" && id ? `g.${id}` : "");
  if (!assetRef) return null;
  return cleanAsset({
    ...asset,
    asset_ref: assetRef,
    generation_task_id: generationTaskId
      || positiveInteger(asset.generation_task_id ?? asset.task_id),
  });
}

function generationTaskAsset(
  task: Record<string, any>,
  explicitAsset: unknown,
  generationTaskId: number | null,
): CompositionAssetSnapshot | null {
  const output = objectValue(task.output);
  const result = objectValue(task.result);
  const candidates = [
    explicitAsset,
    task.asset,
    ...(Array.isArray(task.assets) ? task.assets : []),
    output.asset,
    ...(Array.isArray(output.assets) ? output.assets : []),
    result.asset,
    ...(Array.isArray(result.assets) ? result.assets : []),
  ];
  for (const candidate of candidates) {
    const asset = cleanGeneratedVideoAsset(candidate, generationTaskId);
    if (asset) return asset;
  }
  return null;
}

function cleanTransition(value: unknown): VideoCompositionTransition {
  const transition = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const type = TRANSITION_TYPES.has(cleanText(transition.type))
    ? cleanText(transition.type) as VideoCompositionTransition["type"]
    : "cut";
  return {
    type,
    duration_seconds: type === "cut"
      ? 0
      : bounded(transition.duration_seconds, 0.05, 3, 0.4),
  };
}

function cleanSubtitle(value: unknown): VideoCompositionSubtitleDraft | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const subtitle = value as Record<string, unknown>;
  const text = cleanText(subtitle.text);
  const start = bounded(subtitle.start_seconds, 0, 1800, 0);
  const end = bounded(subtitle.end_seconds, 0.05, 1800, start + 1);
  if (!text || end <= start) return null;
  return {
    start_seconds: start,
    end_seconds: end,
    text: text.slice(0, 500),
    font_size: Math.round(bounded(subtitle.font_size, 16, 96, 42)),
    text_color: cleanText(subtitle.text_color) || "#ffffff",
    background_color: cleanText(subtitle.background_color) || "#000000b8",
    bottom_margin: Math.round(bounded(subtitle.bottom_margin, 0, 600, 48)),
  };
}

function cleanAudioTrack(value: unknown): VideoCompositionAudioTrackDraft | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const track = value as Record<string, unknown>;
  const assetRef = cleanText(track.asset_ref);
  if (!assetRef) return null;
  const sourceStart = bounded(track.source_start_seconds, 0, 1800, 0);
  const sourceEndValue = track.source_end_seconds == null
    ? null
    : bounded(track.source_end_seconds, 0.05, 1800, sourceStart + 1);
  return {
    asset_ref: assetRef,
    asset: cleanAsset(track.asset),
    start_seconds: bounded(track.start_seconds, 0, 1800, 0),
    source_start_seconds: sourceStart,
    source_end_seconds: sourceEndValue != null && sourceEndValue > sourceStart
      ? sourceEndValue
      : null,
    volume: bounded(track.volume, 0, 4, 1),
    loop: Boolean(track.loop),
  };
}

export function normalizeVideoComposition(
  value: unknown,
  storyboardShots: CompositionStoryboardShot[] = [],
  operationId: unknown = null,
): VideoCompositionDraft {
  const input = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
  const existingShots = Array.isArray(input.shots) ? input.shots : [];
  const sourceShots = storyboardShots.length ? storyboardShots : existingShots;
  const shots = sourceShots.slice(0, 64).map((storyboardShot, index) => {
    const existing = existingShots.find((item: any) => (
      cleanText(item?.shot_id)
      && cleanText(item?.shot_id) === cleanText(storyboardShot?.shot_id)
    )) || existingShots[index] || {};
    const asset = cleanAsset(existing.asset);
    const sourceStart = bounded(existing.source_start_seconds, 0, 1800, 0);
    const sourceEnd = existing.source_end_seconds == null
      ? null
      : bounded(existing.source_end_seconds, 0.05, 1800, sourceStart + shotDuration(storyboardShot));
    return {
      shot_id: cleanText(storyboardShot?.shot_id)
        || cleanText(existing.shot_id)
        || legacyShotId(storyboardShot, index),
      asset_ref: cleanText(existing.asset_ref) || asset?.asset_ref || "",
      generation_task_id: positiveInteger(existing.generation_task_id)
        || asset?.generation_task_id
        || null,
      asset,
      source_start_seconds: sourceStart,
      source_end_seconds: sourceEnd != null && sourceEnd > sourceStart ? sourceEnd : null,
      duration_seconds: existing.duration_seconds == null
        ? Number(shotDuration(storyboardShot).toFixed(3))
        : bounded(existing.duration_seconds, 0.05, 1800, shotDuration(storyboardShot)),
      transition: cleanTransition(existing.transition),
    };
  });
  const canvas = input.canvas && typeof input.canvas === "object" ? input.canvas : {};
  return {
    schema_version: "video-composition.v1",
    title: (cleanText(input.title) || "分镜合成").slice(0, 128),
    reverse_operation_id: positiveInteger(input.reverse_operation_id)
      || positiveInteger(operationId),
    canvas: {
      width: Math.round(bounded(canvas.width, 64, 1920, 1280) / 2) * 2,
      height: Math.round(bounded(canvas.height, 64, 1920, 720) / 2) * 2,
      fps: Math.round(bounded(canvas.fps, 12, 60, 24)),
      background_color: cleanText(canvas.background_color) || "#000000",
    },
    shots,
    subtitles: (Array.isArray(input.subtitles) ? input.subtitles : [])
      .map(cleanSubtitle)
      .filter(Boolean)
      .slice(0, 200) as VideoCompositionSubtitleDraft[],
    audio_tracks: (Array.isArray(input.audio_tracks) ? input.audio_tracks : [])
      .map(cleanAudioTrack)
      .filter(Boolean)
      .slice(0, 8) as VideoCompositionAudioTrackDraft[],
    original_audio_volume: bounded(input.original_audio_volume, 0, 2, 1),
  };
}

/**
 * Applies a shot-generation task without mutating the current composition.
 * A queued task is persisted by id first so a refreshed page can later match
 * its completed asset even when the task response no longer carries shot_id.
 */
export function bindGenerationTaskToVideoCompositionShot(
  composition: VideoCompositionDraft,
  options: GenerationTaskShotBindingOptions,
): GenerationTaskShotBindingResult {
  const task = generationTaskRecord(options?.task);
  let generationTaskId = positiveInteger(
    task.id ?? task.task_id ?? task.generation_task_id,
  );
  let asset = generationTaskAsset(task, options?.asset, generationTaskId);
  generationTaskId = generationTaskId || positiveInteger(asset?.generation_task_id);
  if (asset && generationTaskId && asset.generation_task_id !== generationTaskId) {
    asset = { ...asset, generation_task_id: generationTaskId };
  }

  const requestedShotId = generationTaskShotId(task, options?.shotId);
  const shotIndex = requestedShotId
    ? composition.shots.findIndex((shot) => cleanText(shot.shot_id) === requestedShotId)
    : generationTaskId
      ? composition.shots.findIndex((shot) => (
        positiveInteger(shot.generation_task_id) === generationTaskId
      ))
      : -1;
  const matchedShot = shotIndex >= 0 ? composition.shots[shotIndex] : null;
  const shotId = matchedShot ? cleanText(matchedShot.shot_id) : requestedShotId || null;
  const assetRef = asset?.asset_ref || null;

  const result = (
    status: GenerationTaskShotBindingStatus,
    nextComposition = composition,
  ): GenerationTaskShotBindingResult => ({
    status,
    changed: nextComposition !== composition,
    composition: nextComposition,
    shotId,
    assetRef,
    generationTaskId,
  });

  if (!generationTaskId) return result("task_not_found");
  if (!matchedShot) return result("shot_not_found");

  if (!asset) {
    if (positiveInteger(matchedShot.generation_task_id) === generationTaskId) {
      return result("unchanged");
    }
    return result("pending_task_bound", {
      ...composition,
      shots: composition.shots.map((shot, index) => (
        index === shotIndex
          ? {
              ...shot,
              asset_ref: "",
              generation_task_id: generationTaskId,
              asset: null,
            }
          : shot
      )),
    });
  }

  const existingAsset = cleanAsset(matchedShot.asset);
  if (
    cleanText(matchedShot.asset_ref) === asset.asset_ref
    && positiveInteger(matchedShot.generation_task_id) === generationTaskId
    && existingAsset?.asset_ref === asset.asset_ref
    && positiveInteger(existingAsset.generation_task_id) === generationTaskId
  ) {
    return result("unchanged");
  }

  return result("bound", {
    ...composition,
    shots: composition.shots.map((shot, index) => index === shotIndex
      ? {
          ...shot,
          asset_ref: asset.asset_ref,
          generation_task_id: generationTaskId,
          asset,
        }
      : shot),
  });
}

export function videoCompositionShotTracksGenerationTask(
  composition: VideoCompositionDraft,
  shotId: unknown,
  generationTaskId: unknown,
): boolean {
  const expectedShotId = cleanText(shotId);
  const expectedTaskId = positiveInteger(generationTaskId);
  if (!expectedShotId || !expectedTaskId) return false;
  const shot = composition.shots.find((item) => cleanText(item.shot_id) === expectedShotId);
  return positiveInteger(shot?.generation_task_id) === expectedTaskId;
}

export function videoCompositionDuration(value: VideoCompositionDraft): number {
  return Number(value.shots.reduce((total, shot, index) => {
    const duration = finite(shot.duration_seconds, 0);
    const overlap = index > 0 ? finite(value.shots[index - 1]?.transition.duration_seconds, 0) : 0;
    return total + Math.max(0, duration - overlap);
  }, 0).toFixed(3));
}

export function videoCompositionProblems(value: VideoCompositionDraft): string[] {
  const problems: string[] = [];
  if (!value.shots.length) problems.push("至少需要一个镜头");
  const missing = value.shots.filter((shot) => !cleanText(shot.asset_ref));
  if (missing.length) problems.push(`还有 ${missing.length} 个镜头未绑定视频素材`);
  for (const [index, shot] of value.shots.entries()) {
    if (!cleanText(shot.shot_id)) problems.push(`镜头 ${index + 1} 缺少稳定 shot_id`);
    if (shot.source_end_seconds != null && shot.source_end_seconds <= shot.source_start_seconds) {
      problems.push(`镜头 ${index + 1} 的裁剪结束时间必须晚于开始时间`);
    }
  }
  return [...new Set(problems)];
}

export function buildVideoCompositionInput(value: VideoCompositionDraft) {
  const problems = videoCompositionProblems(value);
  if (problems.length) throw new Error(problems[0]);
  return {
    schema_version: "video-composition.v1" as const,
    title: value.title,
    reverse_operation_id: value.reverse_operation_id,
    canvas: { ...value.canvas },
    shots: value.shots.map((shot) => ({
      shot_id: shot.shot_id,
      asset_ref: shot.asset_ref,
      generation_task_id: shot.generation_task_id,
      source_start_seconds: shot.source_start_seconds,
      source_end_seconds: shot.source_end_seconds,
      duration_seconds: shot.duration_seconds,
      transition: { ...shot.transition },
    })),
    subtitles: value.subtitles.map((subtitle) => ({ ...subtitle })),
    audio_tracks: value.audio_tracks.map((track) => ({
      asset_ref: track.asset_ref,
      start_seconds: track.start_seconds,
      source_start_seconds: track.source_start_seconds,
      source_end_seconds: track.source_end_seconds,
      volume: track.volume,
      loop: track.loop,
    })),
    original_audio_volume: value.original_audio_volume,
  };
}

export function subtitlesFromStoryboard(
  storyboardShots: CompositionStoryboardShot[],
  composition: VideoCompositionDraft,
): VideoCompositionSubtitleDraft[] {
  let cursor = 0;
  return composition.shots.flatMap((shot, index) => {
    const source = storyboardShots[index] || {};
    const duration = Math.max(0.1, finite(shot.duration_seconds, shotDuration(source)));
    const start = cursor;
    const end = cursor + duration;
    cursor = end - finite(shot.transition.duration_seconds, 0);
    const text = cleanText(source.ocr)
      || (cleanText(source.audio_cue) !== "未分析" ? cleanText(source.audio_cue) : "");
    return text ? [{
      start_seconds: Number(start.toFixed(3)),
      end_seconds: Number(end.toFixed(3)),
      text: text.slice(0, 500),
      font_size: 42,
      text_color: "#ffffff",
      background_color: "#000000b8",
      bottom_margin: 48,
    }] : [];
  }).slice(0, 200);
}

export function videoCompositionWorkflowOutput(run: unknown): Record<string, any> | null {
  if (!run || typeof run !== "object" || Array.isArray(run)) return null;
  const value = run as Record<string, any>;
  if (value.output && typeof value.output === "object") return value.output;
  const nodes = Array.isArray(value.nodes) ? value.nodes : [];
  for (const node of [...nodes].reverse()) {
    if (node?.output && typeof node.output === "object") return node.output;
  }
  return null;
}

export function videoCompositionWorkflowRecord(
  run: unknown,
  storedWorkflow: unknown = {},
): Record<string, any> {
  const stored = storedWorkflow && typeof storedWorkflow === "object" && !Array.isArray(storedWorkflow)
    ? storedWorkflow as Record<string, any>
    : {};
  if (run == null) return stored;
  if (typeof run !== "object" || Array.isArray(run)) return stored;
  const value = run as Record<string, any>;
  return {
    run_id: positiveInteger(value.id ?? value.run_id),
    client_request_id: cleanText(value.client_request_id)
      || cleanText(stored.client_request_id)
      || null,
    status: cleanText(value.status) || null,
    current_node_key: cleanText(value.current_node_key) || null,
    output: videoCompositionWorkflowOutput(value),
    error_code: cleanText(value.error_code) || null,
    error: cleanText(value.error) || null,
    updated_at: cleanText(value.updated_at) || new Date().toISOString(),
  };
}

export function videoCompositionCapabilityStatus(run: unknown): VideoCompositionCapabilityStatus | null {
  const output = videoCompositionWorkflowOutput(run);
  const status = cleanText(output?.capability_status);
  if (!new Set(["unsupported", "degraded", "partial"]).has(status)) return null;
  return {
    status: status as VideoCompositionCapabilityStatus["status"],
    capability: cleanText(output?.capability) || null,
  };
}

export function videoCompositionWorkflowProgress(run: unknown): number {
  if (!run || typeof run !== "object" || Array.isArray(run)) return 0;
  const value = run as Record<string, any>;
  const nodes = Array.isArray(value.nodes) ? value.nodes : [];
  if (!nodes.length) return value.status === "succeeded" ? 100 : 0;
  const weights: Record<string, number> = {
    succeeded: 1,
    failed: 1,
    canceled: 1,
    running: 0.5,
    waiting_external: 0.35,
    waiting_review: 0.35,
  };
  const progress = nodes.reduce((total: number, node: Record<string, unknown>) => (
    total + (weights[cleanText(node.status)] || 0)
  ), 0) / nodes.length;
  return Math.round(Math.min(1, Math.max(0, progress)) * 100);
}
