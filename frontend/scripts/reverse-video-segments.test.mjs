import assert from "node:assert/strict";
import fs from "node:fs";
import {
  normalizeReverseConfig,
  reverseConfigForSourceChange,
  validateReverseConfig,
} from "../app/studio/reverseConfig.ts";
import {
  invalidateReverseAnalyzerHealthCache,
  loadReverseAnalyzerHealth,
  summarizeAudioAnalyzerHealth,
  summarizeImageAnalyzerHealth,
  summarizeVideoAnalyzerHealth,
} from "../app/studio/reverseAnalyzerHealth.ts";
import {
  buildReverseOperationRequestSnapshotV3,
  workspacePatchFromReverseSnapshot,
} from "../app/studio/reverseSnapshot.ts";
import {
  audioEvidenceFeatureSummary,
  evidenceCoverage,
  failedAudioRanges,
  normalizeAudioEvidenceRows,
  normalizeAudioEvidenceFeatures,
  normalizeShotEvidenceRows,
  verifiedVisualCoverage,
} from "../app/studio/videoEvidence.ts";

const config = normalizeReverseConfig({
  source_ranges: [
    { start_seconds: 20, end_seconds: 25 },
    { start_seconds: 2, end_seconds: 8 },
  ],
  custom_keyframes: [4, 22],
}, { category: "video", selectedType: "video", duration: 40 });
assert.deepEqual(config.source_ranges, [
  { start_seconds: 2, end_seconds: 8 },
  { start_seconds: 20, end_seconds: 25 },
]);
assert.equal(config.source_range, null);

assert.deepEqual(reverseConfigForSourceChange({
  analysis_focus: "camera_motion",
  analysis_precision: "fine",
  output_purpose: "storyboard",
  include_audio: true,
  custom_instruction: "只分析主体动作",
  source_range: { start_seconds: 2, end_seconds: 8 },
  source_ranges: [{ start_seconds: 2, end_seconds: 8 }],
  custom_keyframes: [4],
}), {
  analysis_focus: "camera_motion",
  analysis_precision: "fine",
  output_purpose: "storyboard",
  include_audio: true,
  custom_instruction: "只分析主体动作",
  source_range: null,
  source_ranges: [],
  custom_keyframes: [],
});
// 精度契约：后端 Literal 为 fast/standard/fine/ultra，"超精细"(ultra) 不能再被前端白名单拦下；
// 非法值（如历史上误用的 deep）仍要报"分析精度无效"并在归一化时回退 standard。
const ultraPrecision = validateReverseConfig(
  { ...config, analysis_precision: "ultra" },
  { category: "video", selectedType: "video", duration: 40 },
);
assert.equal(ultraPrecision.valid, true);
assert.equal(ultraPrecision.value.analysis_precision, "ultra");
const invalidPrecision = validateReverseConfig(
  { ...config, analysis_precision: "deep" },
  { category: "video", selectedType: "video", duration: 40 },
);
assert.equal(invalidPrecision.valid, false);
assert.match(invalidPrecision.errors[0].message, /分析精度无效/);
assert.equal(
  normalizeReverseConfig({ analysis_precision: "deep" }, { category: "video" }).analysis_precision,
  "standard",
);

const parsingHook = fs.readFileSync(new URL("../hooks/useReferenceParsing.js", import.meta.url), "utf8");
assert.match(
  parsingHook,
  /reverseConfig:\s*reverseConfigForSourceChange\(current\.reverseConfig\)/,
  "changing the primary source should clear media-local reverse selections",
);
assert.match(
  parsingHook,
  /const configValidation = validateReverseConfig\(rawConfig,[\s\S]*const normalizedConfig = configValidation\.value/,
  "submission must validate raw user times before using the normalized config",
);

const beyondDuration = validateReverseConfig({
  ...config,
  source_ranges: [
    { start_seconds: 0, end_seconds: 2 },
    { start_seconds: 3, end_seconds: 7 },
  ],
}, { category: "video", selectedType: "video", duration: 6.016 });
assert.equal(beyondDuration.valid, false);
assert.match(beyondDuration.errors[0].message, /素材时长/);
assert.equal(
  beyondDuration.value.source_ranges[1].end_seconds,
  6.016,
  "normalization may produce a display-safe value but must not make an invalid submission valid",
);

const keyframeBeyondDuration = validateReverseConfig({
  ...config,
  source_ranges: [{ start_seconds: 0, end_seconds: 6.016 }],
  custom_keyframes: [7],
}, { category: "video", selectedType: "video", duration: 6.016 });
assert.equal(keyframeBeyondDuration.valid, false);
assert.match(keyframeBeyondDuration.errors[0].message, /素材时长/);

const mergedOverlap = validateReverseConfig({
  ...config,
  source_ranges: [
    { start_seconds: 2, end_seconds: 8 },
    { start_seconds: 7, end_seconds: 10 },
  ],
  custom_keyframes: [4],
}, { category: "video", duration: 40 });
assert.equal(mergedOverlap.valid, true);
assert.deepEqual(mergedOverlap.value.source_ranges, [
  { start_seconds: 2, end_seconds: 10 },
]);
assert.deepEqual(normalizeReverseConfig({
  source_ranges: [
    { start_seconds: 9, end_seconds: 15 },
    { start_seconds: 2, end_seconds: 10 },
    { start_seconds: 15, end_seconds: 18 },
  ],
}, { category: "video", selectedType: "video", duration: 40 }).source_ranges, [
  { start_seconds: 2, end_seconds: 18 },
]);
assert.match(validateReverseConfig({
  ...config,
  custom_keyframes: [15],
}, { category: "video", duration: 40 }).errors[0].message, /已选分析片段/);

const snapshot = buildReverseOperationRequestSnapshotV3({
  creationMode: "video",
  subjectMode: "general",
  target: "video",
  selected: { id: 1, type: "video", url: "https://cdn.example.com/source.mp4", duration: 40 },
  reverseConfig: config,
});
assert.deepEqual(snapshot.source_ranges, config.source_ranges);
const restored = workspacePatchFromReverseSnapshot(snapshot);
assert.deepEqual(restored.workspace.reverseConfig.source_ranges, config.source_ranges);
assert.equal(restored.workspace.selected.duration, 40);

const controls = fs.readFileSync(new URL("../app/studio/StudioVideoAnalysisControls.jsx", import.meta.url), "utf8");
assert.match(controls, /source_ranges/);
assert.match(controls, /添加片段/);
assert.match(controls, /api\.reverseAnalyzerStatus\(\)/);
assert.match(controls, /loadReverseAnalyzerHealth/);
assert.match(controls, /summarizeVideoAnalyzerHealth/);
assert.match(controls, /视觉证据能力/);
assert.match(controls, /音频证据分析/);
assert.match(controls, /audioHealth\.explicitlyUnavailable/);
assert.match(
  controls,
  /关键帧时间必须位于素材时长范围内。[\s\S]*关键帧必须位于某个已选分析片段内。/,
  "the inline control should report duration overflow before segment membership",
);
assert.doesNotMatch(controls, /不包含说话人、音乐、节拍和音效分析/);
// 预估打通：精度选择器写入 reverseConfig.analysis_precision，参考面板必须把它同步回
// workspace.videoAnalysisPreset，viewModel 才能按真实档位显示费用/帧数；同时 viewModel
// 要把 /api/config 下发的档位登记为精度白名单，避免与后端枚举漂移。
const referencePanelSource = fs.readFileSync(
  new URL("../app/studio/StudioReferencePanel.jsx", import.meta.url),
  "utf8",
);
assert.match(
  referencePanelSource,
  /setVideoAnalysisPreset\?\.\(reverseAnalysisPrecision\)/,
  "reference panel must sync reverseConfig.analysis_precision into workspace.videoAnalysisPreset",
);
const viewModelSource = fs.readFileSync(new URL("../app/studio/viewModel.ts", import.meta.url), "utf8");
assert.match(
  viewModelSource,
  /registerReversePrecisionOptions\(reverseVideoPresets\)/,
  "view model must register server precision tiers as the frontend whitelist",
);
const intentControls = fs.readFileSync(
  new URL("../app/studio/StudioReverseIntentControls.jsx", import.meta.url),
  "utf8",
);
assert.match(intentControls, /loadReverseAnalyzerHealth/);
assert.match(intentControls, /summarizeImageAnalyzerHealth/);
assert.match(intentControls, /图片证据能力/);
assert.match(intentControls, /category === "image"/);
const apiClient = fs.readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
assert.match(apiClient, /reverseAnalyzerStatus:[\s\S]*\/api\/prompt\/reverse-analyzers\/status/);

const imageAnalyzerHealth = summarizeImageAnalyzerHealth({
  image: {
    ocr: { status: "available", analyzer: "tesseract_tsv" },
    region_proposal: {
      status: "available",
      analyzer: "pillow_region_proposal",
      semantic_detection: false,
    },
    detector: {
      status: "unsupported",
      configured: false,
      degraded_reason: "detector provider 未配置",
    },
    segmenter: {
      status: "degraded",
      configured: true,
      degraded_reason: "provider 已配置，但尚未验证",
    },
  },
});
assert.deepEqual(
  imageAnalyzerHealth.capabilities.map(({ key }) => key),
  ["ocr", "region_proposal", "detector", "segmenter"],
);
assert.match(imageAnalyzerHealth.summary, /可用：OCR 文字识别、区域提议（非语义）/);
assert.match(imageAnalyzerHealth.summary, /降级：精细分割/);
assert.match(imageAnalyzerHealth.summary, /未配置：语义检测/);
assert.equal(imageAnalyzerHealth.capabilities[1].reason, "仅提供非语义区域提议，不代表商品或人物检测。");
assert.match(imageAnalyzerHealth.issues, /语义检测：detector provider 未配置/);

const videoAnalyzerHealth = summarizeVideoAnalyzerHealth({
  video: {
    subject_tracking: { status: "unsupported", configured: false },
    pose: {
      status: "degraded",
      configured: true,
      degraded_reason: "健康探针失败",
    },
    action: { status: "unsupported", configured: false },
    transition: { status: "available", configured: true },
    camera_motion: { status: "available", analyzer: "opencv_lk_homography" },
  },
});
assert.deepEqual(
  videoAnalyzerHealth.capabilities.map(({ key }) => key),
  ["subject_tracking", "pose", "action", "transition", "camera_motion"],
);
assert.match(videoAnalyzerHealth.summary, /可用：转场、运镜/);
assert.match(videoAnalyzerHealth.summary, /降级：姿态/);
assert.match(videoAnalyzerHealth.summary, /未配置：主体追踪、动作/);
assert.equal(videoAnalyzerHealth.capabilities[1].statusLabel, "降级");

invalidateReverseAnalyzerHealthCache();
let analyzerHealthRequests = 0;
const sharedAnalyzerPayload = {
  image: {},
  video: {},
  audio: {
    contract_version: "audio-evidence.v1",
    status: "unsupported",
    analysis_enabled: false,
    asr: { status: "unsupported" },
    speaker: { status: "unsupported" },
    music: { status: "unsupported" },
    beat: { status: "unsupported" },
    sfx: { status: "unsupported" },
  },
};
const analyzerHealthFetcher = async () => {
  analyzerHealthRequests += 1;
  await Promise.resolve();
  return sharedAnalyzerPayload;
};
const [firstAnalyzerHealth, secondAnalyzerHealth] = await Promise.all([
  loadReverseAnalyzerHealth(analyzerHealthFetcher),
  loadReverseAnalyzerHealth(analyzerHealthFetcher),
]);
assert.equal(analyzerHealthRequests, 1, "concurrent image/video controls must share one health request");
assert.strictEqual(firstAnalyzerHealth, sharedAnalyzerPayload);
assert.strictEqual(secondAnalyzerHealth, sharedAnalyzerPayload);
await loadReverseAnalyzerHealth(analyzerHealthFetcher);
assert.equal(analyzerHealthRequests, 1, "the short frontend cache must prevent immediate repeat probes");
invalidateReverseAnalyzerHealthCache();
await loadReverseAnalyzerHealth(analyzerHealthFetcher);
assert.equal(analyzerHealthRequests, 2, "cache invalidation must permit a fresh probe");
invalidateReverseAnalyzerHealthCache();
let failedAnalyzerHealthRequests = 0;
const failingAnalyzerHealthFetcher = async () => {
  failedAnalyzerHealthRequests += 1;
  throw new Error("health unavailable");
};
await assert.rejects(Promise.all([
  loadReverseAnalyzerHealth(failingAnalyzerHealthFetcher),
  loadReverseAnalyzerHealth(failingAnalyzerHealthFetcher),
]), /health unavailable/);
assert.equal(failedAnalyzerHealthRequests, 1, "concurrent failed probes must still be single-flight");
await assert.rejects(loadReverseAnalyzerHealth(failingAnalyzerHealthFetcher), /health unavailable/);
assert.equal(failedAnalyzerHealthRequests, 2, "failed probes must not poison the short cache");
invalidateReverseAnalyzerHealthCache();

const partialAnalyzerHealth = summarizeAudioAnalyzerHealth({
  image: {},
  video: {},
  audio: {
    contract_version: "audio-evidence.v1",
    status: "partial",
    analysis_enabled: true,
    asr: { status: "unsupported" },
    speaker: { status: "unsupported" },
    music: { status: "available" },
    beat: { status: "available" },
    sfx: { status: "available" },
  },
});
assert.equal(partialAnalyzerHealth.explicitlyUnavailable, false);
assert.match(partialAnalyzerHealth.summary, /可用：音乐倾向、BPM\/节拍、瞬态音效/);
assert.match(partialAnalyzerHealth.summary, /未配置：ASR、说话人/);

const unsupportedAnalyzerHealth = summarizeAudioAnalyzerHealth({
  image: {},
  video: {},
  audio: {
    contract_version: "audio-evidence.v1",
    status: "unsupported",
    analysis_enabled: false,
    asr: { status: "unsupported" },
    speaker: { status: "unsupported" },
    music: { status: "unsupported" },
    beat: { status: "unsupported" },
    sfx: { status: "unsupported" },
  },
});
assert.equal(unsupportedAnalyzerHealth.explicitlyUnavailable, true);
assert.match(unsupportedAnalyzerHealth.summary, /当前音频分析能力不可用/);
assert.equal(unsupportedAnalyzerHealth.capabilities[0].statusLabel, "未配置");
assert.equal(unsupportedAnalyzerHealth.capabilities[2].statusLabel, "不可用");

const degradedAnalyzerHealth = summarizeAudioAnalyzerHealth({
  image: {},
  video: {},
  audio: {
    contract_version: "audio-evidence.v1",
    status: "degraded",
    analysis_enabled: true,
    asr: { status: "degraded", degraded_reason: "已配置但尚未验证" },
    speaker: { status: "unsupported" },
    music: { status: "unsupported" },
    beat: { status: "unsupported" },
    sfx: { status: "unsupported" },
  },
});
assert.equal(degradedAnalyzerHealth.explicitlyUnavailable, false);
assert.match(degradedAnalyzerHealth.summary, /降级：ASR/);
assert.equal(degradedAnalyzerHealth.capabilities[0].reason, "已配置但尚未验证");

const unknownAnalyzerHealth = summarizeAudioAnalyzerHealth(null);
assert.equal(unknownAnalyzerHealth.explicitlyUnavailable, false);
assert.match(unknownAnalyzerHealth.summary, /以后端实际分析结果为准/);
const evidenceTimeline = fs.readFileSync(new URL("../app/studio/StudioVideoEvidenceTimeline.jsx", import.meta.url), "utf8");
for (const marker of ["sampled_frames", "source_ranges", "audioSegments", "gaps", "onSelectShot"]) {
  assert.match(evidenceTimeline, new RegExp(marker));
}
for (const marker of ["ASR", "说话人", "音乐", "节拍", "音效", "视觉证据覆盖"]) {
  assert.match(evidenceTimeline, new RegExp(marker));
}
assert.doesNotMatch(evidenceTimeline, /shot\.ocr/);

const disjointCoverage = evidenceCoverage(
  [
    { start_seconds: 2, end_seconds: 8 },
    { start_seconds: 20, end_seconds: 26 },
  ],
  [{ start_seconds: 2, end_seconds: 8 }],
);
assert.equal(disjointCoverage.selected_duration_seconds, 12);
assert.equal(disjointCoverage.covered_duration_seconds, 6);
assert.equal(disjointCoverage.ratio, 0.5);
assert.deepEqual(disjointCoverage.gaps, [{ start_seconds: 20, end_seconds: 26 }]);

const fullDisjointCoverage = evidenceCoverage(
  [
    { start_seconds: 2, end_seconds: 8 },
    { start_seconds: 20, end_seconds: 26 },
  ],
  [
    { start_seconds: 2, end_seconds: 8 },
    { start_seconds: 20, end_seconds: 26 },
  ],
);
assert.equal(fullDisjointCoverage.ratio, 1);
assert.deepEqual(fullDisjointCoverage.gaps, []);

const verifiedCoverage = verifiedVisualCoverage({
  selectedRanges: [
    { start_seconds: 2, end_seconds: 8 },
    { start_seconds: 20, end_seconds: 26 },
  ],
  frames: [
    { index: 1, absolute_timestamp_seconds: 3, source_segment_index: 1 },
    { index: 2, absolute_timestamp_seconds: 22, source_segment_index: 2 },
  ],
  shots: [
    {
      source_segment_index: 1,
      start_seconds: 2,
      end_seconds: 8,
      evidence_frame_indices: [1],
      confidence: 0.9,
    },
    {
      source_segment_index: 2,
      start_seconds: 20,
      end_seconds: 26,
      evidence_frame_indices: [999],
      confidence: 0.9,
    },
  ],
});
assert.equal(verifiedCoverage.ratio, 0.5, "missing frame evidence must not report 100% coverage");

const audioFeatures = normalizeAudioEvidenceFeatures({
  status: "partial",
  features: {
    asr: { status: "partial", analyzer: "timestamped_asr_gateway", evidence_count: 1 },
  },
});
assert.equal(audioFeatures.asr.status, "partial");
assert.equal(audioFeatures.asr.evidence_count, 1);
for (const feature of ["speaker", "music", "beat", "sfx"]) {
  assert.equal(audioFeatures[feature].status, "unsupported");
}
assert.deepEqual(failedAudioRanges({
  segment_results: [
    { status: "analyzed", source_range: { start_seconds: 2, end_seconds: 8 } },
    { status: "failed", source_range: { start_seconds: 20, end_seconds: 26 } },
  ],
}), [{ start_seconds: 20, end_seconds: 26 }]);

const audioEvidence = {
  status: "partial",
  segments: [{
    evidence_id: "speech-1",
    evidence_type: "speech",
    start_seconds: 20.2,
    end_seconds: 21.4,
    text: "新品上市",
    speaker_id: "speaker-1",
    analyzer_status: "analyzed",
    source_segment_index: 2,
  }],
  evidence: [
    {
      evidence_id: "speech-1",
      evidence_type: "speech",
      start_seconds: 20.2,
      end_seconds: 21.4,
      text: "新品上市",
      speaker_id: "speaker-1",
      analyzer_status: "analyzed",
      source_segment_index: 2,
    },
    {
      evidence_id: "music-1",
      evidence_type: "music_likelihood",
      start_seconds: 20,
      end_seconds: 26,
      assessment: "likely",
      confidence: 0.78,
      source_segment_index: 2,
    },
    {
      evidence_id: "beat-1",
      evidence_type: "beat",
      start_seconds: 20.5,
      end_seconds: 20.58,
      strength: 0.9,
      source_segment_index: 2,
    },
    {
      evidence_id: "transient-1",
      evidence_type: "transient",
      start_seconds: 22,
      end_seconds: 22.12,
      label: "unclassified_transient",
      source_segment_index: 2,
    },
  ],
  features: {
    asr: { status: "analyzed", evidence_count: 1 },
    speaker: { status: "analyzed", evidence_count: 1, speaker_count: 1 },
    music: { status: "analyzed", evidence_count: 1, assessment: "likely" },
    beat: { status: "analyzed", evidence_count: 1, bpm: 120.04 },
    sfx: { status: "analyzed", evidence_count: 1 },
  },
};
const normalizedAudioEvidence = normalizeAudioEvidenceRows(
  audioEvidence,
  [{ start_seconds: 20, end_seconds: 26 }],
);
assert.equal(normalizedAudioEvidence.length, 4);
assert.equal(normalizedAudioEvidence[0].speaker_id, "speaker-1");
assert.equal(normalizedAudioEvidence[1].kind, "music");
assert.equal(normalizedAudioEvidence[1].text, "可能包含音乐");
assert.equal(normalizedAudioEvidence[2].kind, "beat");
assert.equal(normalizedAudioEvidence[3].text, "未分类瞬态");
assert.equal(audioEvidenceFeatureSummary(audioEvidence, "speaker"), "speaker-1");
assert.equal(audioEvidenceFeatureSummary(audioEvidence, "beat"), "120 BPM");
assert.equal(audioEvidenceFeatureSummary(audioEvidence, "music"), "可能包含音乐");
assert.equal(audioEvidenceFeatureSummary(audioEvidence, "sfx"), "1 个瞬态");

const shotRows = normalizeShotEvidenceRows([{
  source_segment_index: 2,
  start_seconds: 20,
  end_seconds: 26,
  evidence_frame_indices: [2],
  ocr_tracks: [{ text: "SALE", start_seconds: 21, end_seconds: 22 }],
  action: "旋转",
  camera: "推近",
  audio_refs: [{ kind: "asr", text: "新品", status: "analyzed" }],
}], [{ start_seconds: 20, end_seconds: 26 }]);
assert.equal(shotRows[0].range.start_seconds, 20);
assert.equal(shotRows[0].source_segment_index, 2);
assert.equal(shotRows[0].ocr_tracks[0].text, "SALE");
assert.equal(shotRows[0].motion.status, "analyzed");
assert.equal(shotRows[0].camera.status, "analyzed");
assert.equal(shotRows[0].audio_refs[0].kind, "asr");

const registryRows = normalizeShotEvidenceRows([{
  shot_id: "shot-1",
  source_segment_index: 2,
  start_seconds: 20,
  end_seconds: 26,
  evidence_frame_indices: [2],
  ocr_track_refs: ["ocr-track-1"],
  motion_evidence_refs: ["motion-1"],
  audio_refs: ["speech-1", "beat-1"],
  analyzer_status: { ocr: "analyzed", motion: "degraded" },
}], [{ start_seconds: 20, end_seconds: 26 }], {
  evidence_analyzers: {
    frame_ocr: {
      status: "analyzed",
      tracks: [{
        track_id: "ocr-track-1",
        text: "SUMMER SALE",
        source_segment_index: 2,
        start_seconds: 21,
        end_seconds: 23,
        frame_indices: [2],
        analyzer_status: "analyzed",
      }],
    },
    motion: {
      status: "degraded",
      samples: [{
        evidence_id: "motion-1",
        source_segment_index: 2,
        start_seconds: 20,
        end_seconds: 22,
        camera: "zoom",
        camera_confidence: 0.82,
        background_motion_confidence: 0.7,
        subject_motion_confidence: 0.3,
      }],
    },
  },
  audio: audioEvidence,
});
assert.equal(registryRows[0].ocr_tracks[0].text, "SUMMER SALE");
assert.deepEqual(registryRows[0].ocr_tracks[0].range, { start_seconds: 21, end_seconds: 23 });
assert.equal(registryRows[0].ocr_status, "analyzed");
assert.equal(registryRows[0].camera.text, "zoom");
assert.equal(registryRows[0].camera.status, "degraded");
assert.equal(registryRows[0].motion.status, "degraded");
assert.match(registryRows[0].motion.text, /背景 70%/);
assert.equal(registryRows[0].audio_refs.length, 2);
assert.equal(registryRows[0].audio_refs[0].text, "新品上市");
assert.equal(registryRows[0].audio_refs[0].speaker_id, "speaker-1");
assert.equal(registryRows[0].audio_refs[1].kind, "beat");
assert.notEqual(registryRows[0].audio_refs[0].text, "speech-1", "evidence ids must resolve to evidence content");
assert.match(evidenceTimeline, /逐镜头证据明细/);
assert.match(evidenceTimeline, /BPM\/节拍/);
for (const marker of ["音乐判断证据轨道", "节拍与瞬态证据轨道", "speaker_id", "audioEvidenceFeatureSummary"]) {
  assert.match(evidenceTimeline, new RegExp(marker));
}

console.log("reverse video segments frontend test passed");
