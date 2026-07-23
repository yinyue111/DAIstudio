"use client";

import { Activity, Captions, Film, Mic2, Music2, Zap } from "lucide-react";
import {
  absoluteEvidenceRange,
  audioEvidenceFeatureSummary,
  failedAudioRanges,
  mergeEvidenceRanges,
  normalizeAudioEvidenceRows,
  normalizeAudioEvidenceFeatures,
  normalizeShotEvidenceRows,
  verifiedVisualCoverage,
} from "./videoEvidence";

const AUDIO_FEATURE_META = [
  ["asr", "ASR"],
  ["speaker", "说话人"],
  ["music", "音乐"],
  ["beat", "BPM/节拍"],
  ["sfx", "音效"],
];

const STATUS_LABELS = {
  analyzed: "已分析",
  ready: "已就绪",
  partial: "部分完成",
  degraded: "降级",
  unsupported: "未支持",
  disabled: "未启用",
  no_audio: "无音轨",
  failed: "失败",
  pending: "处理中",
  not_requested: "未请求",
};

function finite(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function percent(value, duration) {
  if (!(duration > 0)) return 0;
  return Math.max(0, Math.min(100, value / duration * 100));
}

function markerPosition(start, end, duration, minimumWidth = 0.7) {
  const left = percent(start, duration);
  const naturalWidth = percent(Math.max(start, end), duration) - left;
  return { left: `${left}%`, width: `${Math.max(minimumWidth, naturalWidth)}%` };
}

function statusClass(status) {
  if (["analyzed", "ready"].includes(status)) return "border-aqua/35 bg-aqua/10 text-aqua";
  if (["partial", "degraded", "failed"].includes(status)) return "border-warn/35 bg-warn/10 text-warn";
  return "border-line bg-white/[0.035] text-fog";
}

function uniqueGapMessages(gaps) {
  return [...new Set(gaps.map((gap) => {
    if (typeof gap === "string") return gap.trim();
    if (!gap || typeof gap !== "object") return "";
    return String(gap.message || gap.label || gap.field || "").trim();
  }).filter(Boolean))];
}

export default function StudioVideoEvidenceTimeline({
  analysis = {},
  shots = [],
  gaps = [],
  activeShotIndex = -1,
  onSelectShot,
}) {
  const source = analysis.source && typeof analysis.source === "object" ? analysis.source : {};
  const frames = Array.isArray(analysis.sampled_frames) ? analysis.sampled_frames : [];
  const audio = analysis.audio && typeof analysis.audio === "object" ? analysis.audio : {};
  const audioSegments = Array.isArray(audio.segments) ? audio.segments : [];
  const audioFeatures = normalizeAudioEvidenceFeatures(audio);
  const rawRanges = Array.isArray(source.source_ranges) && source.source_ranges.length
    ? source.source_ranges
    : source.source_range ? [source.source_range] : [];
  const ranges = mergeEvidenceRanges(rawRanges);
  const selectedRanges = ranges.length ? ranges : [{ start_seconds: 0, end_seconds: 1 }];
  const audioEvidenceRows = normalizeAudioEvidenceRows(audio, selectedRanges);
  const speechEvidence = audioEvidenceRows.filter((item) => item.kind === "speech" && item.range);
  const musicEvidence = audioEvidenceRows.filter((item) => item.kind === "music" && item.range);
  const beatEvidence = audioEvidenceRows.filter((item) => item.kind === "beat" && item.range);
  const sfxEvidence = audioEvidenceRows.filter((item) => item.kind === "sfx" && item.range);
  const audioFailureRanges = failedAudioRanges(audio);
  const allEnds = [
    finite(source.total_duration_seconds),
    finite(source.duration_seconds),
    ...ranges.map((item) => item.end_seconds),
    ...frames.map((item) => finite(item.absolute_timestamp_seconds ?? item.timestamp_seconds)),
    ...shots.map((item) => finite(item.end_seconds)),
    ...audioSegments.map((item) => finite(item.end_seconds)),
    ...audioEvidenceRows.map((item) => finite(item.range?.end_seconds)),
    ...audioFailureRanges.map((item) => item.end_seconds),
  ];
  const duration = Math.max(1, ...allEnds);
  if (!ranges.length) selectedRanges[0].end_seconds = duration;
  const absoluteRange = (value) => absoluteEvidenceRange(value, selectedRanges);
  const visualCoverage = verifiedVisualCoverage({ selectedRanges, shots, frames });
  const providedGapRanges = gaps.map(absoluteRange).filter(Boolean);
  const visualGapRanges = mergeEvidenceRanges([
    ...visualCoverage.gaps,
    ...providedGapRanges,
  ]);
  const gapMessages = uniqueGapMessages(gaps);
  const coveragePercent = Math.round(visualCoverage.ratio * 100);
  const shotEvidenceRows = normalizeShotEvidenceRows(shots, selectedRanges, analysis);

  function shotForFrame(frame) {
    const frameIndex = Number(frame.index);
    const direct = shots.findIndex((shot) => (
      Array.isArray(shot.evidence_frame_indices)
      && shot.evidence_frame_indices.map(Number).includes(frameIndex)
    ));
    if (direct >= 0) return direct;
    const time = finite(frame.absolute_timestamp_seconds ?? frame.timestamp_seconds);
    return shots.findIndex((shot) => {
      const range = absoluteRange(shot);
      return range && range.start_seconds <= time && time <= range.end_seconds;
    });
  }

  return (
    <section aria-labelledby="video-evidence-timeline-title">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p id="video-evidence-timeline-title" className="font-display font-medium text-mist">视频证据时间线</p>
          <p className="mt-0.5 text-[10px] text-fog">
            原视频绝对时间 · 视觉证据覆盖 {visualCoverage.covered_duration_seconds.toFixed(2)}/
            {visualCoverage.selected_duration_seconds.toFixed(2)}s（{coveragePercent}%）
          </p>
        </div>
        <div className="flex flex-wrap gap-2 text-[10px] text-fog" aria-label="时间线图例">
          <span><i className="mr-1 inline-block h-2 w-2 bg-aqua/60" />已选片段</span>
          <span><i className="mr-1 inline-block h-2 w-2 bg-iris/70" />分镜</span>
          <span><i className="mr-1 inline-block h-2 w-2 bg-bad/55" />证据缺口</span>
          <span><i className="mr-1 inline-block h-2 w-2 bg-warn/65" />对白</span>
          <span><i className="mr-1 inline-block h-2 w-2 bg-snow/65" />音乐/节拍/瞬态</span>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5" aria-label="音频证据子能力状态">
        {AUDIO_FEATURE_META.map(([key, label]) => {
          const feature = audioFeatures[key];
          const status = feature.status;
          const summary = audioEvidenceFeatureSummary(audio, key);
          return (
            <span
              key={key}
              className={`border px-2 py-1 text-[10px] ${statusClass(status)}`}
              title={feature.degraded_reason || undefined}
            >
              {label} · {STATUS_LABELS[status] || status}
              {summary ? ` · ${summary}` : ""}
              {feature.evidence_count > 0 ? ` · ${feature.evidence_count}` : ""}
            </span>
          );
        })}
      </div>
      {audio.degraded_reason && (
        <p className="mt-1.5 text-[10px] text-warn">音频证据：{audio.degraded_reason}</p>
      )}

      <div className="mt-3 overflow-x-auto rounded-lg border border-line bg-black/25 p-3">
        <div className="relative h-60 min-w-[42rem]" aria-label="可交互视频证据时间线">
          <div className="absolute inset-x-0 top-5 h-5 bg-white/[0.025]">
            {selectedRanges.map((range, index) => (
              <div
                key={`${range.start_seconds}-${range.end_seconds}-${index}`}
                className="absolute inset-y-0 border-x border-aqua/50 bg-aqua/15"
                style={markerPosition(range.start_seconds, range.end_seconds, duration)}
                title={`片段 ${index + 1}: ${range.start_seconds.toFixed(2)}-${range.end_seconds.toFixed(2)}s`}
              />
            ))}
          </div>

          <div className="absolute inset-x-0 top-12 h-7 border-y border-line/70 bg-white/[0.018]">
            {visualGapRanges.map((gap, index) => (
              <div
                key={`gap-${gap.start_seconds}-${gap.end_seconds}-${index}`}
                className="absolute inset-y-0 border-x border-bad/50 bg-bad/15"
                style={markerPosition(gap.start_seconds, gap.end_seconds, duration)}
                title={`视觉证据缺口 ${gap.start_seconds.toFixed(2)}-${gap.end_seconds.toFixed(2)}s`}
              />
            ))}
            {shots.map((shot, index) => {
              const range = absoluteRange(shot);
              if (!range) return null;
              return (
                <button
                  key={`shot-${index}`}
                  type="button"
                  className={`absolute inset-y-1 overflow-hidden border px-1 text-left text-[9px] transition ${
                    activeShotIndex === index
                      ? "z-20 border-snow bg-iris/70 text-white"
                      : "z-10 border-iris/65 bg-iris/35 text-mist hover:bg-iris/55"
                  }`}
                  style={markerPosition(range.start_seconds, range.end_seconds, duration, 1.4)}
                  title={`镜头 ${index + 1}: ${shot.visual || shot.action || "查看分镜"}`}
                  onClick={() => onSelectShot?.(index)}
                >
                  {index + 1}
                </button>
              );
            })}
          </div>

          <div className="absolute inset-x-0 top-[5.5rem] h-10 border-t border-line/50">
            {frames.map((frame, index) => {
              const time = finite(frame.absolute_timestamp_seconds ?? frame.timestamp_seconds);
              const shotIndex = shotForFrame(frame);
              return (
                <button
                  key={`frame-${frame.index ?? index}`}
                  type="button"
                  className="absolute top-0 flex h-8 w-7 -translate-x-1/2 items-center justify-center border border-aqua/45 bg-base2 text-aqua hover:border-snow hover:text-snow"
                  style={{ left: `${percent(time, duration)}%` }}
                  title={`证据帧 ${frame.index ?? index + 1} · ${time.toFixed(2)}s${frame.source_segment_index ? ` · 片段 ${frame.source_segment_index}` : ""}`}
                  aria-label={`证据帧 ${frame.index ?? index + 1}，${time.toFixed(2)} 秒`}
                  onClick={() => shotIndex >= 0 && onSelectShot?.(shotIndex)}
                >
                  <Film size={12} aria-hidden="true" />
                </button>
              );
            })}
          </div>

          <div className="absolute inset-x-0 top-[8.5rem] h-7 border-t border-line/50" aria-label="对白与说话人证据轨道">
            {audioFailureRanges.map((range, index) => (
              <span
                key={`audio-gap-${range.start_seconds}-${range.end_seconds}-${index}`}
                className="absolute top-1 h-5 border border-bad/45 bg-bad/15"
                style={markerPosition(range.start_seconds, range.end_seconds, duration, 0.9)}
                title={`ASR 证据缺口 ${range.start_seconds.toFixed(2)}-${range.end_seconds.toFixed(2)}s`}
              />
            ))}
            {["analyzed", "partial"].includes(audioFeatures.asr.status) && speechEvidence.map((segment, index) => {
              const range = segment.range;
              return range ? (
                <span
                  key={segment.id || `asr-${index}`}
                  className="absolute top-1 z-10 flex h-5 items-center overflow-hidden border border-warn/45 bg-warn/15 px-1 text-[9px] text-warn"
                  style={markerPosition(range.start_seconds, range.end_seconds, duration, 0.9)}
                  title={`${range.start_seconds.toFixed(2)}-${range.end_seconds.toFixed(2)}s ${segment.speaker_id ? `${segment.speaker_id}: ` : ""}${segment.text || "ASR 证据"}`}
                >
                  {segment.speaker_id
                    ? <Mic2 size={10} className="mr-0.5 shrink-0" aria-hidden="true" />
                    : <Captions size={10} className="mr-0.5 shrink-0" aria-hidden="true" />}
                  <span className="truncate">{segment.speaker_id || segment.text}</span>
                </span>
              ) : null;
            })}
          </div>

          <div className="absolute inset-x-0 top-[10.5rem] h-7 border-t border-line/50" aria-label="音乐判断证据轨道">
            {musicEvidence.map((item, index) => (
              <span
                key={item.id || `music-${index}`}
                className="absolute top-1 flex h-5 items-center overflow-hidden border border-snow/35 bg-white/[0.07] px-1 text-[9px] text-mist"
                style={markerPosition(item.range.start_seconds, item.range.end_seconds, duration, 1.2)}
                title={`${item.range.start_seconds.toFixed(2)}-${item.range.end_seconds.toFixed(2)}s ${item.text}`}
              >
                <Music2 size={10} className="mr-0.5 shrink-0" aria-hidden="true" />
                <span className="truncate">{item.text}</span>
              </span>
            ))}
          </div>

          <div className="absolute inset-x-0 top-[12.5rem] h-7 border-t border-line/50" aria-label="节拍与瞬态证据轨道">
            {beatEvidence.map((item, index) => (
              <span
                key={item.id || `beat-${index}`}
                className="absolute top-1 flex h-5 min-w-1 items-center border border-aqua/55 bg-aqua/20 text-aqua"
                style={markerPosition(item.range.start_seconds, item.range.end_seconds, duration, 0.45)}
                title={`节拍 ${item.range.start_seconds.toFixed(2)}s${item.strength === null ? "" : ` · 强度 ${Math.round(item.strength * 100)}%`}`}
              >
                <Activity size={9} className="mx-auto" aria-hidden="true" />
              </span>
            ))}
            {sfxEvidence.map((item, index) => (
              <span
                key={item.id || `sfx-${index}`}
                className="absolute top-1 flex h-5 min-w-1 items-center border border-warn/55 bg-warn/20 text-warn"
                style={markerPosition(item.range.start_seconds, item.range.end_seconds, duration, 0.55)}
                title={`${item.text} ${item.range.start_seconds.toFixed(2)}s${item.strength === null ? "" : ` · 强度 ${Math.round(item.strength * 100)}%`}`}
              >
                <Zap size={9} className="mx-auto" aria-hidden="true" />
              </span>
            ))}
          </div>

          <span className="absolute bottom-0 left-0 text-[9px] text-fog">0s</span>
          <span className="absolute bottom-0 right-0 text-[9px] text-fog">{duration.toFixed(1)}s</span>
        </div>
      </div>
      {(gapMessages.length > 0 || visualGapRanges.length > 0) && (
        <p className="mt-1.5 text-[10px] text-fog">
          证据缺口：{gapMessages.length ? gapMessages.join("；") : `${visualGapRanges.length} 个选区尚无可验证分镜证据`}
        </p>
      )}

      {shotEvidenceRows.length > 0 && (
        <ol className="mt-3 divide-y divide-line border-y border-line" aria-label="逐镜头证据明细">
          {shotEvidenceRows.map((row) => {
            const shot = shots[row.index] || {};
            const selected = activeShotIndex === row.index;
            return (
              <li key={row.index} className={selected ? "bg-aqua/[0.05]" : ""}>
                <button
                  type="button"
                  className="grid min-h-16 w-full grid-cols-[5rem_minmax(0,1fr)] gap-2 px-2 py-2 text-left sm:grid-cols-[7rem_minmax(0,1fr)]"
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelectShot?.(row.index)}
                >
                  <span>
                    <span className="block text-[11px] font-display font-medium text-mist">镜头 {row.index + 1}</span>
                    <span className="mt-0.5 block text-[10px] text-fog">
                      {row.range ? `${row.range.start_seconds.toFixed(2)}-${row.range.end_seconds.toFixed(2)}s` : "时间未提供"}
                      {row.source_segment_index ? ` · 片段 ${row.source_segment_index}` : ""}
                    </span>
                  </span>
                  <span className="min-w-0 space-y-1 text-[10px] text-fog">
                    <span className="block truncate text-mist">{shot.visual || "未提供画面描述"}</span>
                    <span className="flex flex-wrap gap-x-2 gap-y-1">
                      <span>证据帧 {row.evidence_frame_indices.length ? row.evidence_frame_indices.join("、") : "未提供"}</span>
                      <span>OCR {row.ocr_tracks.length
                        ? row.ocr_tracks.map((item) => `${item.text}${item.range ? ` ${item.range.start_seconds.toFixed(2)}-${item.range.end_seconds.toFixed(2)}s` : ""}`).join("；")
                        : STATUS_LABELS[row.ocr_status] || row.ocr_status || "未提供"}</span>
                      <span>动作 {row.motion.status === "unsupported" ? "未支持" : row.motion.text || STATUS_LABELS[row.motion.status] || row.motion.status}</span>
                      <span>运镜 {row.camera.status === "unsupported" ? "未支持" : row.camera.text || STATUS_LABELS[row.camera.status] || row.camera.status}</span>
                      <span>音频引用 {row.audio_refs.length ? row.audio_refs.map((item) => [
                        item.speaker_id ? `${item.speaker_id}：` : "",
                        item.kind === "speech" ? "对白" : item.kind === "music" ? "音乐" : item.kind === "beat" ? "节拍" : item.kind === "sfx" ? "瞬态" : "音频",
                        item.text ? ` ${item.text}` : "",
                        item.range ? ` ${item.range.start_seconds.toFixed(2)}-${item.range.end_seconds.toFixed(2)}s` : "",
                      ].join("")).join("；") : "未提供"}</span>
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
