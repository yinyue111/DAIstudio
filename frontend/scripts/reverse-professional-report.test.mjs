import assert from "node:assert/strict";
import { buildReverseProfessionalReport } from "../app/studio/reverseProfessionalReport.ts";
import { summarizeVideoAnalyzerHealth } from "../app/studio/reverseAnalyzerHealth.ts";
import {
  composeEvidenceBackedVideoGenerationDraft,
  normalizeLegacyVideoGenerationPrompt,
  withEvidenceBackedVideoGenerationDraft,
} from "../app/studio/helpers.ts";
import { normalizeRestoredVideoPrompt } from "../hooks/studioOwnerRestore.js";

const report = buildReverseProfessionalReport({
  structured: {
    主体: "蓝色纸巾盒",
    旁白: "云朵般柔软",
    音效: "未分析",
    负向: "形变，文字乱码",
  },
  final_text: "蓝色纸巾盒置于云层般柔软的天空场景",
  provider_final_text: "纸巾盒出现后，纸巾在云层中缓慢展开",
  analysis_gaps: [{ start_seconds: 7, end_seconds: 8 }],
  video_analysis: {
    analysis_mode: "multi_frame",
    source: { ratio: "9:16", duration_seconds: 8, audio_analyzed: false },
    sampled_frames: [
      { index: 1, timestamp_seconds: 1 },
      { index: 2, timestamp_seconds: 4 },
      { index: 3, timestamp_seconds: 7 },
    ],
    evidence_coverage: { ratio: 0.875 },
    audio: { status: "not_requested" },
    shots: [
      {
        start_seconds: 0,
        end_seconds: 3,
        visual: "纸巾盒居中",
        action: "纸巾飘动",
        camera: "镜头推近",
        transition: "溶解",
        evidence_frame_indices: [1],
        confidence: 0.92,
      },
      {
        start_seconds: 3,
        end_seconds: 9,
        visual: "纸巾与云层融合",
        subject_tracking: "同一纸巾盒保持居中，纸巾向上延展",
        pose: "纸巾由折叠状态转为展开状态",
        action: "纸巾缓慢展开",
        camera: "镜头向上跟随",
        transition: "柔和叠化",
        evidence_frame_indices: [2, 3],
        confidence: 0.9,
        evidence_gate: {
          subject_tracking: { verified: false, level: "inferred" },
          pose: { verified: false, level: "inferred" },
          action: { verified: false, level: "inferred" },
          transition: { verified: false, level: "inferred" },
          camera: { verified: true, level: "verified" },
        },
      },
      {
        start_seconds: 6,
        end_seconds: 7,
        visual: "建议增加品牌收尾",
        evidence_frame_indices: [],
        confidence: 0,
      },
    ],
  },
});

assert.equal(report.profile.durationSeconds, 8);
assert.equal(report.shots[0].level, "verified");
assert.equal(report.shots[0].action, "", "a single frame must not verify motion");
assert.equal(report.shots[0].camera, "", "a single frame must not verify camera motion");
assert.equal(report.shots[0].transition, "", "a single frame must not verify a transition");
assert.equal(report.shots[1].level, "verified");
assert.equal(report.shots[1].semanticLevel, "inferred");
assert.equal(report.shots[1].subjectTracking, "同一纸巾盒保持居中，纸巾向上延展");
assert.equal(report.shots[1].pose, "纸巾由折叠状态转为展开状态");
assert.equal(report.shots[1].range.end_seconds, 8, "report timelines must not exceed source duration");
assert.equal(report.shots[2].level, "creative");
assert.equal(report.delivery.audioStatus, "未分析");
assert.deepEqual(report.delivery.audioSuggestions, ["云朵般柔软"]);
assert.equal(report.inferredNarrative, "纸巾盒出现后，纸巾在云层中缓慢展开");
assert.deepEqual(report.gaps, ["7.00-8.00s 未覆盖"]);

const staticShotReport = buildReverseProfessionalReport({
  video_analysis: {
    source: { duration_seconds: 2 },
    sampled_frames: [{ index: 1 }, { index: 2 }],
    evidence_analyzers: {
      motion: {
        samples: [{
          evidence_id: "motion-static",
          background_motion_confidence: 0.04,
          subject_motion_confidence: 0.96,
        }],
      },
    },
    shots: [{
      start_seconds: 0,
      end_seconds: 2,
      visual: "商品静止放置",
      evidence_frame_indices: [1, 2],
      motion_evidence_refs: ["motion-static"],
      confidence: 0.9,
    }],
  },
});
assert.equal(
  staticShotReport.shots[0].action,
  "",
  "optical-flow confidence must not be presented as a semantic subject action",
);

const videoHealth = summarizeVideoAnalyzerHealth({
  video: {
    subject_tracking: { status: "unsupported", configured: false },
    pose: { status: "unsupported", configured: false },
    action: { status: "unsupported", configured: false },
    transition: { status: "unsupported", configured: false },
    camera_motion: { status: "available", configured: true },
  },
});
for (const key of ["subject_tracking", "pose", "action", "transition"]) {
  assert.equal(videoHealth.capabilities.find((item) => item.key === key)?.fallbackLabel, "主模型跨帧推断");
}
assert.match(videoHealth.summary, /主模型跨帧推断/);

const draftShots = Array.from({ length: 6 }, (_, index) => ({
  start_seconds: index * 6,
  end_seconds: (index + 1) * 6,
  visual: `第${index + 1}镜画面`,
  subject_tracking: `第${index + 1}镜主体连续`,
  pose: `第${index + 1}镜姿态`,
  action: `第${index + 1}镜动作`,
  camera: `第${index + 1}镜运镜`,
  lighting: `第${index + 1}镜光线`,
  transition: index < 5 ? "硬切" : "定帧收尾",
  ocr: index === 2 ? "厚实吸水，画面右侧浮现" : "",
  audio_cue: index === 2 ? "水滴入水声" : "未分析",
  evidence_frame_indices: [index * 2 + 1, index * 2 + 2],
  confidence: 0.9,
}));
const richDraft = composeEvidenceBackedVideoGenerationDraft(
  {
    主体: "蓝色纸巾盒",
    场景背景: "蓝天云层",
    一致性约束: "保持同一纸巾盒",
    字幕卖点: "干湿两用",
    旁白: "未分析",
    音效: "检测到持续音乐可能性较高；节拍约 170.5 BPM；在 5.92秒、6.24秒等 检测到 11 个未分类瞬态声",
  },
  {
    source: { width: 720, height: 1280, ratio: "9:16", duration_seconds: 36 },
    sampled_frames: Array.from({ length: 12 }, (_, index) => ({
      index: index + 1,
      timestamp_seconds: index * 3,
    })),
    shots: draftShots,
  },
);
assert.match(richDraft, /参考片含 6 个高密度剪辑镜头/);
assert.match(richDraft, /镜头1：/);
assert.match(richDraft, /镜头6：/);
assert.doesNotMatch(richDraft, /输出规格|720x1280|9:16|36\.00秒|镜头1（/);
assert.match(richDraft, /主体追踪：第6镜主体连续/);
assert.match(richDraft, /姿态：第6镜姿态/);
assert.match(richDraft, /运镜：第6镜运镜/);
assert.match(richDraft, /主体：蓝色纸巾盒/);
assert.match(richDraft, /画面字幕：干湿两用/);
assert.match(richDraft, /画面文字：厚实吸水，画面右侧浮现/);
assert.match(richDraft, /声音：持续背景音乐/);
assert.match(richDraft, /声音：水滴入水声/);
assert.doesNotMatch(richDraft, /BPM|未分类瞬态声/);
assert.doesNotMatch(richDraft, /5\.92秒|6\.24秒/);
assert.doesNotMatch(richDraft, /未分析/);

const recoveredIncompleteDraft = withEvidenceBackedVideoGenerationDraft({
  structured: {
    主体: "穿着米色家居服的女性与白色洗脸巾包装",
    源视频规格: "608x1080，9:16，10.05秒",
  },
  final_text: "女性与白色洗脸巾包装",
  provider_final_text: "女性伸懒腰后走向壁挂包装，抽出洗脸巾并展开，浸水、拧干、擦拭脸颊，最后展示包装。",
  video_analysis: {
    source: { width: 608, height: 1080, duration_seconds: 10.05 },
    sampled_frames: Array.from({ length: 14 }, (_, index) => ({
      index: index + 1,
      timestamp_seconds: index * 0.7,
    })),
    shots: [],
  },
}, "video");
assert.doesNotMatch(recoveredIncompleteDraft.final_text, /输出规格|608x1080|9:16|10\.05秒/);
assert.match(recoveredIncompleteDraft.final_text, /伸懒腰后走向壁挂包装/);
assert.ok(recoveredIncompleteDraft.final_text.length > 50);

const recoveredLegacyDraft = withEvidenceBackedVideoGenerationDraft({
  structured: {
    主体: "蓝色纸巾盒",
    源视频规格: "608x1080，9:16，10.05秒",
  },
  final_text: `输出规格：608x1080，9:16，10.05秒\n${"旧版冗长提示词".repeat(80)}`,
  video_analysis: {
    sampled_frames: [
      { index: 1, timestamp_seconds: 0 },
      { index: 2, timestamp_seconds: 2 },
    ],
    shots: [{
      start_seconds: 0,
      end_seconds: 2,
      visual: "纸巾盒居中",
      action: "缓慢展开纸巾",
      evidence_frame_indices: [1, 2],
      confidence: 0.9,
    }],
  },
}, "video");
assert.doesNotMatch(recoveredLegacyDraft.final_text, /输出规格|608x1080|9:16|10\.05秒/);
assert.match(recoveredLegacyDraft.final_text, /镜头1：画面：纸巾盒居中；动作：缓慢展开纸巾/);

const recoveredAudioMetricDraft = withEvidenceBackedVideoGenerationDraft({
  structured: {
    主体: "蓝色纸巾盒",
    音效: "检测到持续音乐可能性较高；节拍约 170.5 BPM；检测到 2 个未分类瞬态声",
  },
  final_text: "主体：蓝色纸巾盒\n声音：节拍约 170.5 BPM；检测到 2 个未分类瞬态声",
  video_analysis: { shots: [] },
}, "video");
assert.match(recoveredAudioMetricDraft.final_text, /声音：持续背景音乐/);
assert.doesNotMatch(recoveredAudioMetricDraft.final_text, /BPM|未分类瞬态声/);

const restoredCloudDraft = normalizeRestoredVideoPrompt({
  video: {
    prompt: "蓝色纸巾盒",
    promptDirty: false,
    structured: { 主体: "蓝色纸巾盒", 场景背景: "云层背景" },
    reverseVideoAnalysis: {
      sampled_frames: [
        { index: 1, timestamp_seconds: 0 },
        { index: 2, timestamp_seconds: 2 },
      ],
      shots: [{
        start_seconds: 0,
        end_seconds: 2,
        visual: "纸巾盒置于云层中央",
        action: "纸巾缓慢展开",
        evidence_frame_indices: [1, 2],
        confidence: 0.9,
      }],
    },
    pendingReverseResult: {
      dirty: false,
      result: {
        structured: { 主体: "白色洗脸巾包装" },
        final_text: "白色洗脸巾包装",
        video_analysis: {
          sampled_frames: [
            { index: 1, timestamp_seconds: 0 },
            { index: 2, timestamp_seconds: 2 },
          ],
          shots: [{
            start_seconds: 0,
            end_seconds: 2,
            visual: "洗脸巾包装悬挂在墙面",
            action: "手从下方抽出一张洗脸巾",
            evidence_frame_indices: [1, 2],
            confidence: 0.9,
          }],
        },
      },
    },
  },
});
assert.match(restoredCloudDraft.video.prompt, /镜头1：画面：纸巾盒置于云层中央/);
assert.match(
  restoredCloudDraft.video.pendingReverseResult.result.final_text,
  /镜头1：画面：洗脸巾包装悬挂在墙面；动作：手从下方抽出一张洗脸巾/,
);

const restoredHistoricalDraft = withEvidenceBackedVideoGenerationDraft({
  structured: { 主体: "白色洗脸巾包装" },
  final_text: "输出规格：608x1080，9:16，10.05秒\n镜头1（0-2秒）：女主走向壁挂包装并从下方抽出洗脸巾\n镜头2（2-4秒）：微距展示3D如意云纹",
  video_analysis: {
    shots: [{ visual: "壁挂包装" }],
  },
}, "video");
assert.match(restoredHistoricalDraft.final_text, /女主走向壁挂包装/);
assert.match(restoredHistoricalDraft.final_text, /微距展示3D如意云纹/);
assert.doesNotMatch(restoredHistoricalDraft.final_text, /608x1080|9:16|10\.05秒|镜头1（|镜头2（|0-2秒|2-4秒/);

const restoredLegacyPrompt = normalizeLegacyVideoGenerationPrompt(
  "输出规格：608x1080，9:16，10.05秒\n"
  + "声音：节拍约 170.5 BPM；在 5.92秒、6.24秒等 检测到 11 个未分类瞬态声\n"
  + "镜头1（0.00-1.25秒）：画面：人物伸懒腰；动作：双臂上举\n"
  + "镜头2（1.25-2.75秒）：画面：抽出洗脸巾；转场：硬切",
);
assert.doesNotMatch(restoredLegacyPrompt, /输出规格|608x1080|9:16|10\.05秒|5\.92秒|6\.24秒|镜头1（|镜头2（/);
assert.doesNotMatch(restoredLegacyPrompt, /声音：|BPM|未分类瞬态声/);
assert.match(restoredLegacyPrompt, /镜头1：画面：人物伸懒腰；动作：双臂上举/);
assert.match(restoredLegacyPrompt, /镜头2：画面：抽出洗脸巾；转场：硬切/);
assert.ok(richDraft.length > 220, "canonical generation draft must not use the model prompt budget");

console.log("reverse professional report tests passed");
