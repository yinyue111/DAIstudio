import { storyboardResultShots } from "./storyboard";
import {
  evidenceRange,
  mergeEvidenceRanges,
  normalizeAudioEvidenceFeatures,
  normalizeShotEvidenceRows,
  type EvidenceRange,
} from "./videoEvidence";

export type ReportEvidenceLevel = "verified" | "inferred" | "creative";

export type ProfessionalReportShot = {
  index: number;
  range: EvidenceRange | null;
  visual: string;
  subjectTracking: string;
  pose: string;
  action: string;
  camera: string;
  transition: string;
  level: ReportEvidenceLevel;
  semanticLevel: ReportEvidenceLevel | null;
  evidenceFrames: number[];
};

export type ReverseProfessionalReport = {
  profile: {
    ratio: string;
    durationSeconds: number | null;
    analysisMode: string;
    frameCount: number;
    coverageRatio: number | null;
  };
  shots: ProfessionalReportShot[];
  gaps: string[];
  structured: Array<{ key: string; value: string }>;
  inferredNarrative: string;
  delivery: {
    prompt: string;
    negative: string;
    audioStatus: string;
    audioSuggestions: string[];
  };
};

function record(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : {};
}

function finite(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function selectedRanges(analysis: Record<string, any>): EvidenceRange[] {
  const source = record(analysis.source);
  const explicit = Array.isArray(source.source_ranges)
    ? source.source_ranges
    : Array.isArray(analysis.source_ranges) ? analysis.source_ranges : [];
  const ranges = mergeEvidenceRanges(explicit);
  if (ranges.length) return ranges;
  const duration = finite(source.duration_seconds);
  return duration !== null && duration > 0
    ? [{ start_seconds: 0, end_seconds: duration }]
    : [];
}

function clampRange(value: unknown, ranges: EvidenceRange[]): EvidenceRange | null {
  const range = evidenceRange(value);
  if (!range) return null;
  if (!ranges.length) return range;
  for (const selected of ranges) {
    const start = Math.max(range.start_seconds, selected.start_seconds);
    const end = Math.min(range.end_seconds, selected.end_seconds);
    if (end > start) return { start_seconds: start, end_seconds: end };
  }
  return null;
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (value == null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function gapLabel(value: unknown): string {
  if (typeof value === "string") return value.trim();
  const gap = record(value);
  const message = String(gap.message || "").trim();
  if (message) return message;
  const start = finite(gap.start_seconds);
  const end = finite(gap.end_seconds);
  if (start !== null && end !== null) return `${start.toFixed(2)}-${end.toFixed(2)}s 未覆盖`;
  return "未标注的证据缺口";
}

function semanticEvidenceLevel(
  shot: Record<string, any>,
  fields: Array<[string, string]>,
): ReportEvidenceLevel | null {
  const activeKeys = fields.filter(([, text]) => Boolean(text)).map(([key]) => key);
  if (!activeKeys.length) return null;
  const gate = record(shot.evidence_gate);
  const entries = activeKeys
    .map((key) => record(gate[key]))
    .filter((entry) => Object.keys(entry).length > 0);
  if (entries.length && entries.every((entry) => entry.verified === true)) return "verified";
  return "inferred";
}

export function buildReverseProfessionalReport(value: unknown): ReverseProfessionalReport {
  const result = record(value);
  const analysis = record(result.video_analysis);
  const source = record(analysis.source);
  const frames = Array.isArray(analysis.sampled_frames) ? analysis.sampled_frames : [];
  const rawShots = storyboardResultShots(result);
  const ranges = selectedRanges(analysis);
  const evidenceRows = normalizeShotEvidenceRows(rawShots, ranges, analysis);
  const shots = rawShots.flatMap((rawValue, index) => {
    const shot = record(rawValue);
    const evidence = evidenceRows.find((row) => row.index === index);
    const range = clampRange(evidence?.range || shot, ranges);
    if (!range) return [];
    const evidenceFrames = evidence?.evidence_frame_indices || [];
    const hasStaticEvidence = evidenceFrames.length > 0 && Number(shot.confidence || 0) > 0;
    const hasCrossFrameEvidence = evidenceFrames.length >= 2;
    const subjectTracking = hasCrossFrameEvidence ? String(shot.subject_tracking || "").trim() : "";
    const pose = hasCrossFrameEvidence ? String(shot.pose || "").trim() : "";
    const action = hasCrossFrameEvidence ? String(shot.action || "").trim() : "";
    const camera = hasCrossFrameEvidence ? String(evidence?.camera.text || shot.camera || shot.camera_motion || "").trim() : "";
    const transition = hasCrossFrameEvidence ? String(shot.transition || "").trim() : "";
    const level: ReportEvidenceLevel = hasStaticEvidence ? "verified" : "creative";
    const semanticFields: Array<[string, string]> = [
      ["subject_tracking", subjectTracking],
      ["pose", pose],
      ["action", action],
      ["camera", camera],
      ["transition", transition],
    ];
    return [{
      index,
      range,
      visual: String(shot.visual || shot.scene || "").trim(),
      subjectTracking,
      pose,
      action,
      camera,
      transition,
      level,
      semanticLevel: semanticEvidenceLevel(shot, semanticFields),
      evidenceFrames,
    }];
  });
  const structured = Object.entries(record(result.structured))
    .filter(([key]) => !["final_text", "shots", "负向", "negative", "旁白", "音效", "配乐"].includes(key))
    .map(([key, item]) => ({ key, value: displayValue(item) }))
    .filter((item) => item.value);
  const audioFeatures = normalizeAudioEvidenceFeatures(analysis.audio);
  const audioAnalyzed = Boolean(source.audio_analyzed)
    || Object.values(audioFeatures).some((feature) => ["ready", "analyzed", "partial"].includes(feature.status));
  const creativeAudioFields = ["旁白", "音效", "配乐"]
    .map((key) => displayValue(record(result.structured)[key]))
    .filter((text) => text && !["未分析", "未请求", "无"].includes(text));
  const coverage = finite(record(analysis.evidence_coverage).ratio);
  const gaps = (Array.isArray(result.analysis_gaps)
    ? result.analysis_gaps
    : Array.isArray(analysis.analysis_gaps) ? analysis.analysis_gaps : [])
    .map(gapLabel)
    .filter(Boolean);
  const inferredNarrative = String(result.provider_final_text || "").trim();
  return {
    profile: {
      ratio: String(source.ratio || "-").trim(),
      durationSeconds: finite(source.duration_seconds),
      analysisMode: String(analysis.analysis_mode || "unavailable"),
      frameCount: frames.length,
      coverageRatio: coverage,
    },
    shots,
    gaps,
    structured,
    inferredNarrative: inferredNarrative === String(result.final_text || "").trim()
      ? ""
      : inferredNarrative,
    delivery: {
      prompt: String(result.final_text || "").trim(),
      negative: String(result.negative || record(result.structured)["负向"] || "").trim(),
      audioStatus: audioAnalyzed ? "已分析" : "未分析",
      audioSuggestions: creativeAudioFields,
    },
  };
}
