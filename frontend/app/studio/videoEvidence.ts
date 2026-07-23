export type EvidenceRange = {
  start_seconds: number;
  end_seconds: number;
};

export type AudioEvidenceFeature = {
  status: string;
  analyzer: string | null;
  evidence_count: number;
  degraded_reason: string | null;
};

export type AudioEvidenceRow = {
  id: string;
  kind: "speech" | "music" | "beat" | "sfx" | "audio";
  range: EvidenceRange | null;
  source_segment_index: number | null;
  text: string;
  speaker_id: string | null;
  assessment: string | null;
  confidence: number | null;
  strength: number | null;
  status: string;
};

export const AUDIO_EVIDENCE_FEATURES = ["asr", "speaker", "music", "beat", "sfx"] as const;

const AUDIO_FEATURE_REASONS: Record<(typeof AUDIO_EVIDENCE_FEATURES)[number], string> = {
  asr: "未请求对白转写",
  speaker: "当前未提供说话人分析",
  music: "当前未提供音乐结构分析",
  beat: "当前未提供 BPM 与节拍分析",
  sfx: "当前未提供音效事件分析",
};

function finite(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function rounded(value: number): number {
  return Number(value.toFixed(4));
}

export function evidenceRange(value: unknown): EvidenceRange | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  const start = finite(source.start_seconds);
  const end = finite(source.end_seconds);
  if (start === null || end === null || start < 0 || end <= start) return null;
  return { start_seconds: start, end_seconds: end };
}

export function mergeEvidenceRanges(values: unknown[]): EvidenceRange[] {
  const ranges = values
    .map(evidenceRange)
    .filter((item): item is EvidenceRange => Boolean(item))
    .sort((left, right) => left.start_seconds - right.start_seconds || left.end_seconds - right.end_seconds);
  const merged: EvidenceRange[] = [];
  for (const range of ranges) {
    const previous = merged.at(-1);
    if (!previous || range.start_seconds > previous.end_seconds) {
      merged.push({ ...range });
      continue;
    }
    previous.end_seconds = Math.max(previous.end_seconds, range.end_seconds);
  }
  return merged;
}

export function absoluteEvidenceRange(
  value: unknown,
  selectedRanges: EvidenceRange[],
): EvidenceRange | null {
  const range = evidenceRange(value);
  if (!range) return null;
  const source = value as Record<string, unknown>;
  const selected = selectedRanges.length === 1 ? selectedRanges[0] : null;
  const selectedDuration = selected ? selected.end_seconds - selected.start_seconds : 0;
  if (
    selected
    && selected.start_seconds > 0
    && source.source_segment_index == null
    && range.end_seconds <= selectedDuration + 0.001
  ) {
    return {
      start_seconds: selected.start_seconds + range.start_seconds,
      end_seconds: selected.start_seconds + range.end_seconds,
    };
  }
  return range;
}

function subtractCovered(selected: EvidenceRange, covered: EvidenceRange[]): EvidenceRange[] {
  const gaps: EvidenceRange[] = [];
  let cursor = selected.start_seconds;
  for (const range of covered) {
    const start = Math.max(selected.start_seconds, range.start_seconds);
    const end = Math.min(selected.end_seconds, range.end_seconds);
    if (end <= start) continue;
    if (start > cursor) gaps.push({ start_seconds: cursor, end_seconds: start });
    cursor = Math.max(cursor, end);
    if (cursor >= selected.end_seconds) break;
  }
  if (cursor < selected.end_seconds) {
    gaps.push({ start_seconds: cursor, end_seconds: selected.end_seconds });
  }
  return gaps;
}

export function evidenceCoverage(
  selectedValues: unknown[],
  coveredValues: unknown[],
) {
  const selected = mergeEvidenceRanges(selectedValues);
  const covered = mergeEvidenceRanges(coveredValues);
  const selectedDuration = selected.reduce(
    (total, range) => total + range.end_seconds - range.start_seconds,
    0,
  );
  const gaps = selected.flatMap((range) => subtractCovered(range, covered));
  const uncoveredDuration = gaps.reduce(
    (total, range) => total + range.end_seconds - range.start_seconds,
    0,
  );
  const coveredDuration = Math.max(0, selectedDuration - uncoveredDuration);
  return {
    selected_ranges: selected,
    covered_ranges: covered,
    gaps,
    selected_duration_seconds: rounded(selectedDuration),
    covered_duration_seconds: rounded(coveredDuration),
    ratio: selectedDuration > 0 ? rounded(coveredDuration / selectedDuration) : 0,
  };
}

export function verifiedVisualCoverage({
  selectedRanges,
  shots,
  frames,
}: {
  selectedRanges: EvidenceRange[];
  shots: unknown[];
  frames: unknown[];
}) {
  const frameByIndex = new Map<number, Record<string, unknown>>();
  for (const raw of frames) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const frame = raw as Record<string, unknown>;
    const index = Number(frame.index);
    if (Number.isInteger(index) && index > 0) frameByIndex.set(index, frame);
  }
  const verifiedRanges: EvidenceRange[] = [];
  for (const raw of shots) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const shot = raw as Record<string, unknown>;
    const confidence = finite(shot.confidence);
    const range = absoluteEvidenceRange(shot, selectedRanges);
    if (!range || confidence === null || confidence <= 0) continue;
    const evidenceIndices = Array.isArray(shot.evidence_frame_indices)
      ? [...new Set(shot.evidence_frame_indices.map(Number))]
      : [];
    const hasMatchingFrame = evidenceIndices.some((index) => {
      const frame = frameByIndex.get(index);
      if (!frame) return false;
      const time = finite(frame.absolute_timestamp_seconds ?? frame.timestamp_seconds);
      const shotSegment = finite(shot.source_segment_index);
      const frameSegment = finite(frame.source_segment_index);
      if (shotSegment !== null && frameSegment !== null && shotSegment !== frameSegment) return false;
      return time !== null && time >= range.start_seconds - 0.001 && time <= range.end_seconds + 0.001;
    });
    if (hasMatchingFrame) verifiedRanges.push(range);
  }
  return evidenceCoverage(selectedRanges, verifiedRanges);
}

export function normalizeAudioEvidenceFeatures(value: unknown) {
  const audio = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  const features = audio.features && typeof audio.features === "object" && !Array.isArray(audio.features)
    ? audio.features as Record<string, unknown>
    : {};
  const legacyStatus = String(audio.status || "not_requested");
  return Object.fromEntries(AUDIO_EVIDENCE_FEATURES.map((feature) => {
    const raw = features[feature] && typeof features[feature] === "object" && !Array.isArray(features[feature])
      ? features[feature] as Record<string, unknown>
      : {};
    const defaultStatus = feature === "asr" ? legacyStatus : "unsupported";
    const evidenceCount = finite(raw.evidence_count);
    return [feature, {
      status: String(raw.status || defaultStatus),
      analyzer: String(raw.analyzer || "").trim() || null,
      evidence_count: evidenceCount === null ? 0 : Math.max(0, Math.floor(evidenceCount)),
      degraded_reason: String(raw.degraded_reason || AUDIO_FEATURE_REASONS[feature]).trim() || null,
    } satisfies AudioEvidenceFeature];
  })) as Record<(typeof AUDIO_EVIDENCE_FEATURES)[number], AudioEvidenceFeature>;
}

function audioEvidenceKind(value: unknown): AudioEvidenceRow["kind"] {
  const kind = String(value || "").trim().toLowerCase();
  if (["speech", "asr", "transcript"].includes(kind)) return "speech";
  if (["music", "music_likelihood"].includes(kind)) return "music";
  if (kind === "beat") return "beat";
  if (["sfx", "transient"].includes(kind)) return "sfx";
  return "audio";
}

function audioEvidenceText(record: Record<string, unknown>, kind: AudioEvidenceRow["kind"]): string {
  const direct = String(record.text || record.transcript || "").trim();
  if (direct) return direct;
  if (kind === "music") {
    const assessment = String(record.assessment || "").trim().toLowerCase();
    if (assessment === "likely") return "可能包含音乐";
    if (assessment === "unlikely") return "未发现明显音乐特征";
    if (assessment === "uncertain") return "音乐特征不确定";
  }
  const label = String(record.label || "").trim();
  if (label === "unclassified_transient") return "未分类瞬态";
  if (label) return label;
  if (kind === "beat") return "节拍";
  return kind === "audio" ? "音频证据" : "";
}

export function normalizeAudioEvidenceRows(
  value: unknown,
  selectedRanges: EvidenceRange[] = [],
): AudioEvidenceRow[] {
  const audio = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
  const rawRows: unknown[] = [];
  if (Array.isArray(audio.evidence)) rawRows.push(...audio.evidence);
  if (!rawRows.length && Array.isArray(audio.segments)) rawRows.push(...audio.segments);
  if (!rawRows.length && audio.features && typeof audio.features === "object") {
    for (const feature of ["music", "beat", "sfx"]) {
      const rawFeature = audio.features[feature];
      if (rawFeature && typeof rawFeature === "object" && Array.isArray(rawFeature.evidence)) {
        rawRows.push(...rawFeature.evidence);
      }
    }
  }
  const seen = new Set<string>();
  return rawRows.flatMap((raw, index) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const record = raw as Record<string, unknown>;
    const kind = audioEvidenceKind(record.evidence_type || record.kind || record.type);
    const range = absoluteEvidenceRange(record, selectedRanges);
    const id = String(record.evidence_id || record.id || "").trim()
      || `audio-${kind}-${index}-${range?.start_seconds ?? "unknown"}`;
    if (seen.has(id)) return [];
    seen.add(id);
    const segment = finite(record.source_segment_index);
    const confidence = finite(record.confidence);
    const strength = finite(record.strength);
    return [{
      id,
      kind,
      range,
      source_segment_index: segment !== null && Number.isInteger(segment) ? segment : null,
      text: audioEvidenceText(record, kind),
      speaker_id: String(record.speaker_id || record.speaker || "").trim() || null,
      assessment: String(record.assessment || "").trim() || null,
      confidence,
      strength,
      status: truthStatus(record.analyzer_status || record.status, true),
    }];
  });
}

export function audioEvidenceFeatureSummary(
  value: unknown,
  feature: (typeof AUDIO_EVIDENCE_FEATURES)[number],
): string | null {
  const audio = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
  const raw = audio.features?.[feature] && typeof audio.features[feature] === "object"
    ? audio.features[feature] as Record<string, any>
    : {};
  const rows = normalizeAudioEvidenceRows(audio);
  if (feature === "speaker") {
    const speakers = [...new Set(rows.map((row) => row.speaker_id).filter(Boolean))];
    if (speakers.length) return speakers.join("、");
    const count = finite(raw.speaker_count);
    return count !== null && count > 0 ? `${Math.floor(count)} 位` : null;
  }
  if (feature === "beat") {
    const bpm = finite(raw.bpm);
    if (bpm !== null) return `${Number(bpm.toFixed(1))} BPM`;
    const segmentBpms = (Array.isArray(raw.segments) ? raw.segments : [])
      .flatMap((item: unknown) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return [];
        const record = item as Record<string, unknown>;
        const value = finite(record.bpm);
        const segment = finite(record.source_segment_index);
        return value === null ? [] : [`${segment === null ? "片段" : `片段${segment}`} ${Number(value.toFixed(1))}`];
      });
    return segmentBpms.length ? `${segmentBpms.join(" / ")} BPM` : null;
  }
  if (feature === "music") {
    const labels: Record<string, string> = {
      likely: "可能包含音乐",
      unlikely: "未发现明显音乐",
      uncertain: "判断不确定",
    };
    const assessment = String(raw.assessment || "").trim().toLowerCase();
    if (labels[assessment]) return labels[assessment];
    const assessments = [...new Set((Array.isArray(raw.segments) ? raw.segments : [])
      .map((item: any) => labels[String(item?.assessment || "").toLowerCase()] || "")
      .filter(Boolean))];
    return assessments.length ? assessments.join(" / ") : null;
  }
  if (feature === "sfx") {
    const count = finite(raw.evidence_count);
    return count !== null && count > 0 ? `${Math.floor(count)} 个瞬态` : null;
  }
  const speechCount = rows.filter((row) => row.kind === "speech").length;
  return speechCount > 0 ? `${speechCount} 段` : null;
}

export function failedAudioRanges(value: unknown): EvidenceRange[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const audio = value as Record<string, unknown>;
  if (Array.isArray(audio.failed_ranges)) return mergeEvidenceRanges(audio.failed_ranges);
  if (!Array.isArray(audio.segment_results)) return [];
  return mergeEvidenceRanges(audio.segment_results.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const segment = item as Record<string, unknown>;
    return segment.status === "analyzed" ? [] : [segment.source_range];
  }));
}

export type ShotEvidenceRow = {
  index: number;
  source_segment_index: number | null;
  range: EvidenceRange | null;
  evidence_frame_indices: number[];
  ocr_tracks: Array<{ text: string; range: EvidenceRange | null }>;
  ocr_status: string;
  motion: { status: string; text: string };
  camera: { status: string; text: string };
  audio_refs: Array<{
    id: string;
    kind: string;
    text: string;
    status: string;
    range: EvidenceRange | null;
    speaker_id: string | null;
  }>;
};

function truthStatus(value: unknown, hasEvidence: boolean): string {
  const requested = String(value || "").trim().toLowerCase();
  if (["ready", "analyzed", "partial", "unsupported", "degraded", "failed", "pending", "not_requested"].includes(requested)) {
    return requested;
  }
  return hasEvidence ? "analyzed" : "unsupported";
}

export function normalizeShotEvidenceRows(
  shots: unknown[],
  selectedRanges: EvidenceRange[] = [],
  analysisValue: unknown = {},
): ShotEvidenceRow[] {
  const analysis = analysisValue && typeof analysisValue === "object" && !Array.isArray(analysisValue)
    ? analysisValue as Record<string, any>
    : {};
  const analyzers = analysis.evidence_analyzers && typeof analysis.evidence_analyzers === "object"
    ? analysis.evidence_analyzers as Record<string, any>
    : {};
  const frameOcr = analyzers.frame_ocr && typeof analyzers.frame_ocr === "object"
    ? analyzers.frame_ocr as Record<string, any>
    : {};
  const motionAnalyzer = analyzers.motion && typeof analyzers.motion === "object"
    ? analyzers.motion as Record<string, any>
    : {};
  const trackById = new Map<string, Record<string, unknown>>();
  for (const value of Array.isArray(frameOcr.tracks) ? frameOcr.tracks : []) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const track = value as Record<string, unknown>;
    const id = String(track.track_id || track.evidence_id || "").trim();
    if (id) trackById.set(id, track);
  }
  const motionById = new Map<string, Record<string, unknown>>();
  for (const value of Array.isArray(motionAnalyzer.samples) ? motionAnalyzer.samples : []) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const sample = value as Record<string, unknown>;
    const id = String(sample.evidence_id || "").trim();
    if (id) motionById.set(id, sample);
  }
  const audioEvidence = normalizeAudioEvidenceRows(analysis.audio, selectedRanges);
  const audioById = new Map(audioEvidence.map((row) => [row.id, row]));
  return shots.flatMap((value, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const shot = value as Record<string, any>;
    const referencedOcr = Array.isArray(shot.ocr_track_refs)
      ? shot.ocr_track_refs.map((id: unknown) => trackById.get(String(id))).filter(Boolean)
      : [];
    const rawOcr = referencedOcr.length
      ? referencedOcr
      : Array.isArray(shot.ocr_tracks)
        ? shot.ocr_tracks
        : Array.isArray(shot.ocr_evidence) ? shot.ocr_evidence : [];
    const ocrTracks = rawOcr.flatMap((item: unknown) => {
      if (typeof item === "string") return [{ text: item.trim(), range: null }];
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      const text = String(record.text || record.evidence_text || "").trim();
      return text ? [{ text, range: absoluteEvidenceRange(record, selectedRanges) }] : [];
    });
    const referencedMotion = Array.isArray(shot.motion_evidence_refs)
      ? shot.motion_evidence_refs.map((id: unknown) => motionById.get(String(id))).filter(Boolean) as Record<string, unknown>[]
      : [];
    const confidenceText = referencedMotion.map((sample) => {
      const background = finite(sample.background_motion_confidence);
      const subject = finite(sample.subject_motion_confidence);
      return [
        background === null ? "" : `背景 ${Math.round(background * 100)}%`,
        subject === null ? "" : `主体 ${Math.round(subject * 100)}%`,
      ].filter(Boolean).join(" / ");
    }).filter(Boolean);
    const motionText = String(shot.motion || shot.action || "").trim()
      || [...new Set(confidenceText)].join("；");
    const referencedCameras = referencedMotion
      .map((sample) => String(sample.camera || "").trim())
      .filter(Boolean);
    const cameraText = String(shot.camera_motion || shot.camera || "").trim()
      || [...new Set(referencedCameras)].join("；");
    const analyzerStatus = shot.analyzer_status && typeof shot.analyzer_status === "object"
      ? shot.analyzer_status as Record<string, unknown>
      : {};
    const rawAudio = Array.isArray(shot.audio_refs)
      ? shot.audio_refs
      : Array.isArray(shot.audio_evidence) ? shot.audio_evidence : [];
    const audioRefs = rawAudio.flatMap((item: unknown) => {
      if (typeof item === "string") {
        const id = item.trim();
        const evidence = audioById.get(id);
        return evidence ? [{
          id: evidence.id,
          kind: evidence.kind,
          text: evidence.text,
          status: evidence.status,
          range: evidence.range,
          speaker_id: evidence.speaker_id,
        }] : [];
      }
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      const text = String(record.text || record.label || record.transcript || "").trim();
      if (!text) return [];
      return [{
        id: String(record.evidence_id || record.id || "").trim() || `inline-audio-${index}`,
        kind: String(record.kind || record.type || "audio"),
        text,
        status: truthStatus(record.status, true),
        range: absoluteEvidenceRange(record, selectedRanges),
        speaker_id: String(record.speaker_id || record.speaker || "").trim() || null,
      }];
    });
    return [{
      index,
      source_segment_index: Number.isInteger(Number(shot.source_segment_index))
        ? Number(shot.source_segment_index)
        : null,
      range: absoluteEvidenceRange(shot, selectedRanges),
      evidence_frame_indices: Array.isArray(shot.evidence_frame_indices)
        ? [...new Set(shot.evidence_frame_indices.map(Number).filter(Number.isFinite))]
        : [],
      ocr_tracks: ocrTracks,
      ocr_status: truthStatus(analyzerStatus.ocr ?? shot.ocr_status ?? frameOcr.status, ocrTracks.length > 0),
      motion: {
        status: truthStatus(analyzerStatus.motion ?? shot.motion_status ?? motionAnalyzer.status, referencedMotion.length > 0 || Boolean(motionText)),
        text: motionText,
      },
      camera: {
        status: truthStatus(analyzerStatus.motion ?? shot.camera_status ?? motionAnalyzer.status, referencedMotion.length > 0 || Boolean(cameraText)),
        text: cameraText,
      },
      audio_refs: audioRefs,
    }];
  });
}
