import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  isReproductionAssessmentTerminal,
  normalizeReproductionAssessment,
  normalizeReproductionDimensions,
  normalizeReproductionFindings,
  reproductionCorrectionPayload,
  reproductionFindingTimelineStyle,
  reproductionIdempotencyKey,
  reproductionSourceAsset,
  selectableReproductionFindingIds,
} from "../app/studio/reproductionAssessment.ts";
import {
  executableReproductionFindingIds,
  isReproductionRemediationActive,
  isReproductionRemediationTerminal,
  normalizeReproductionRemediation,
  normalizeReproductionRemediationPage,
  reproductionRemediationCreatePayload,
  reproductionRemediationExecutionIssue,
  reproductionRemediationPendingGenerationRequests,
} from "../app/studio/reproductionRemediation.ts";

test("normalizes dimension evidence without inventing unavailable scores", () => {
  assert.deepEqual(normalizeReproductionDimensions({
    structure: { label: "构图结构", status: "succeeded", score: 0.83, analyzer: "ssim" },
    ocr: { label: "文字", status: "unsupported", score: 100, reason: "未安装 OCR" },
    audio: { label: "音频", status: "degraded", score: 72 },
  }), [
    {
      key: "structure",
      label: "构图结构",
      status: "succeeded",
      score: 83,
      analyzer: "ssim",
      analyzer_version: null,
      degraded_reason: null,
    },
    {
      key: "ocr",
      label: "文字",
      status: "unsupported",
      score: null,
      analyzer: null,
      analyzer_version: null,
      degraded_reason: "未安装 OCR",
    },
    {
      key: "audio",
      label: "音频",
      status: "degraded",
      score: null,
      analyzer: null,
      analyzer_version: null,
      degraded_reason: null,
    },
  ]);
});

test("uses stable Chinese labels for semantic reproduction dimensions", () => {
  const dimensions = normalizeReproductionDimensions({
    subject_semantics: { status: "analyzed", score: 0.91 },
    action_semantics: { status: "analyzed", score: 0.82 },
    camera_motion: { status: "degraded", reason: "特征点不足" },
  });
  assert.deepEqual(dimensions.map((item) => item.label), [
    "主体一致性",
    "动作一致性",
    "运镜一致性",
  ]);
  assert.equal(dimensions[2].score, null);
});

test("normalizes image boxes and video ranges while rejecting invalid coordinates", () => {
  const findings = normalizeReproductionFindings([
    {
      finding_id: 11,
      dimension_key: "ocr",
      severity: "high",
      summary: "包装文字不一致",
      confidence: 0.92,
      bbox: { x: 0.1, y: 0.2, width: 0.3, height: 0.1 },
    },
    {
      id: 12,
      dimension: "camera_motion",
      severity: "medium",
      title: "推镜速度偏快",
      start_seconds: 2,
      end_seconds: 4.5,
      shot_index: 2,
      bbox: { x: -1, y: 0, width: 2, height: 1 },
    },
  ]);
  assert.equal(findings[0].bbox?.width, 0.3);
  assert.equal(findings[1].bbox, null);
  assert.equal(findings[1].start_seconds, 2);
  assert.equal(findings[1].end_seconds, 4.5);
  assert.equal(findings[1].shot_index, 2);
  assert.equal(findings[1].shot_id, null);
});

test("normalizes one assessment and exposes only actionable findings", () => {
  const assessment = normalizeReproductionAssessment({
    assessment: {
      id: 17,
      media_type: "image",
      status: "partial",
      progress: 100,
      source_asset_ref: "u.c291cmNl",
      generated_asset_ref: "g.8",
      generation_task_id: 9,
      reverse_operation_id: 4,
      reverse_revision_id: 6,
      metrics: {
        dimensions: [
          { key: "structure", status: "succeeded", score: 0.81 },
          { key: "ocr", status: "unsupported", score: 100 },
        ],
      },
      findings: [
        { id: 101, dimension: "structure", severity: "high", title: "主体偏移" },
        { id: 102, dimension: "structure", severity: "info", title: "轻微差异" },
      ],
    },
  });
  assert.equal(assessment.dimensions[1].score, null);
  assert.deepEqual(selectableReproductionFindingIds(assessment), [101]);
  assert.deepEqual(executableReproductionFindingIds(assessment), []);
  assert.equal(isReproductionAssessmentTerminal(assessment), true);
  assert.equal(Object.hasOwn(assessment, "overall_score"), false);
});

test("exposes only region-backed image findings and shot-backed video findings for execution", () => {
  const image = normalizeReproductionAssessment({
    id: 20,
    media_type: "image",
    status: "succeeded",
    progress: 100,
    source_asset_ref: "u.aW1hZ2U",
    generated_asset_ref: "g.20",
    findings: [
      { id: 1, dimension: "layout", severity: "high", title: "主体偏移", bbox: { x: 0.1, y: 0.1, width: 0.2, height: 0.2 } },
      { id: 2, dimension: "color", severity: "medium", title: "整体色温偏冷" },
    ],
  });
  const video = normalizeReproductionAssessment({
    id: 21,
    media_type: "video",
    status: "partial",
    progress: 100,
    source_asset_ref: "u.dmlkZW8",
    generated_asset_ref: "g.21",
    findings: [
      { id: 3, dimension: "motion", severity: "high", title: "动作过快", shot_id: "shot-2", start_seconds: 2, end_seconds: 4 },
      { id: 4, dimension: "pacing", severity: "medium", title: "全片偏短", start_seconds: 0, end_seconds: 8 },
    ],
  });
  assert.deepEqual(executableReproductionFindingIds(image), [1]);
  assert.deepEqual(executableReproductionFindingIds(video), [3]);
});

test("builds an immutable correction request from selected findings", () => {
  assert.deepEqual(reproductionCorrectionPayload({
    idempotencyKey: "correction-17-1",
    parentRevisionId: 6,
    findingIds: [2, 1, 2],
    promptPatch: "  保持包装结构  ",
    negativePatch: "错字",
    structuredPatch: { 包装文字: "ACME" },
  }), {
    idempotency_key: "correction-17-1",
    parent_revision_id: 6,
    selected_finding_ids: [2, 1],
    prompt_patch: "保持包装结构",
    negative_prompt_patch: "错字",
    structured_patch: { 包装文字: "ACME" },
    mask_patch: null,
    apply: false,
  });
  assert.throws(() => reproductionCorrectionPayload({
    idempotencyKey: "correction-17-2",
    parentRevisionId: 6,
    findingIds: [],
  }), /至少选择/);
  assert.throws(() => reproductionCorrectionPayload({
    idempotencyKey: "",
    parentRevisionId: 0,
    findingIds: [1],
  }), /有效血缘/);
});

test("maps absolute video finding ranges onto a stable timeline", () => {
  const finding = normalizeReproductionFindings([{
    id: 1,
    dimension: "pacing",
    title: "镜头过短",
    start_seconds: 2,
    end_seconds: 4,
  }])[0];
  assert.deepEqual(reproductionFindingTimelineStyle(finding, 10), { left: "20%", width: "20%" });
  assert.equal(reproductionFindingTimelineStyle(finding, 0), null);
});

test("rejects assessment envelopes without stable lineage", () => {
  assert.throws(() => normalizeReproductionAssessment({ id: 1 }), /有效的复刻度评估/);
});

test("builds bounded deterministic idempotency keys without embedding asset references", () => {
  const first = reproductionIdempotencyKey("reproduction-assessment", 9, "u.a-very-long-private-key", "g.7");
  const repeated = reproductionIdempotencyKey("reproduction-assessment", 9, "u.a-very-long-private-key", "g.7");
  const changed = reproductionIdempotencyKey("reproduction-assessment", 9, "u.other", "g.7");
  assert.equal(first, repeated);
  assert.notEqual(first, changed);
  assert.ok(first.length <= 41);
  assert.equal(first.includes("private"), false);
});

test("restores the source asset from immutable generation provenance", () => {
  const fallback = { asset_ref: "u.d3Jvbmc", type: "image", url: "/wrong.png" };
  assert.deepEqual(reproductionSourceAsset({
    params: {
      _source_trace: {
        asset_ref: "u.dXBsb2FkL3NvdXJjZS5wbmc",
        source_type: "image",
        selected_url: "/api/uploads/upload/source.png",
        selected_thumb: "/api/uploads/upload_preview/source.png",
      },
    },
  }, fallback), {
    asset_ref: "u.dXBsb2FkL3NvdXJjZS5wbmc",
    origin: "uploaded",
    type: "image",
    url: "/api/uploads/upload/source.png",
    thumb: "/api/uploads/upload_preview/source.png",
    original_url: null,
    original_thumb: null,
  });
  assert.equal(reproductionSourceAsset({ params: {} }, fallback), null);
});

test("builds media-specific remediation requests without client-side masks", () => {
  const imageAssessment = {
    media_type: "image",
    findings: [
      { id: 11, bbox: { x: 0.1, y: 0.2, width: 0.3, height: 0.2 }, shot_id: null },
      { id: 12, bbox: null, shot_id: null },
    ],
  };
  assert.deepEqual(reproductionRemediationCreatePayload({
    assessment: imageAssessment,
    idempotencyKey: "remediation-image-1",
    parentRevisionId: 7,
    findingIds: [11, 11],
    modelConfigId: 3,
    promptPatch: "  保持包装文字  ",
    negativePatch: "  错字  ",
    params: { strength: 0.42 },
    autoReassess: true,
  }), {
    idempotency_key: "remediation-image-1",
    parent_revision_id: 7,
    parent_remediation_id: null,
    selected_finding_ids: [11],
    selected_shot_ids: [],
    mode: "image_inpaint",
    model_config_id: 3,
    prompt_patch: "保持包装文字",
    negative_prompt_patch: "错字",
    params: { strength: 0.42 },
    video_composition: null,
    auto_reassess: true,
  });
  assert.throws(() => reproductionRemediationCreatePayload({
    assessment: imageAssessment,
    idempotencyKey: "remediation-image-2",
    parentRevisionId: 7,
    findingIds: [12],
  }), /已定位图像区域/);
  assert.throws(() => reproductionRemediationCreatePayload({
    assessment: imageAssessment,
    idempotencyKey: "remediation-image-3",
    parentRevisionId: 7,
    findingIds: [11],
  }), /生成模型/);
  assert.equal(reproductionRemediationCreatePayload({
    assessment: imageAssessment,
    idempotencyKey: "remediation-image-retry",
    parentRevisionId: 7,
    parentRemediationId: 40,
    findingIds: [11],
    modelConfigId: 3,
  }).parent_remediation_id, 40);

  const videoAssessment = {
    media_type: "video",
    findings: [
      { id: 21, bbox: null, shot_id: "shot-a" },
      { id: 22, bbox: null, shot_id: null },
    ],
  };
  assert.equal(reproductionRemediationCreatePayload({
    assessment: videoAssessment,
    idempotencyKey: "remediation-video-1",
    parentRevisionId: 8,
    findingIds: [21],
    modelConfigId: 4,
    videoComposition: { transition: "cut" },
    autoReassess: false,
  }).selected_shot_ids[0], "shot-a");
  assert.throws(() => reproductionRemediationCreatePayload({
    assessment: videoAssessment,
    idempotencyKey: "remediation-video-2",
    parentRevisionId: 8,
    findingIds: [22],
  }), /稳定 shot_id/);
});

test("restores immutable remediation plans and filters already submitted generation items", () => {
  const request = {
    category: "image",
    stage: "final",
    client_request_id: "remediation-44-region-101",
    reproduction_remediation_id: 44,
    reproduction_plan_item_id: "region-101",
    model_config_id: 3,
  };
  const planned = normalizeReproductionRemediation({
    remediation: {
      id: 44,
      assessment_id: 17,
      parent_revision_id: 6,
      applied_revision_id: 9,
      mode: "image_inpaint",
      status: "awaiting_quote",
      selected_finding_ids: [101],
      selected_shot_ids: [],
      plan_snapshot: [{
        item_id: "region-101",
        kind: "image_inpaint",
        shot_id: null,
        finding_ids: [101],
        request,
      }],
      plan_items: [{
        item_id: "region-101",
        kind: "image_inpaint",
        shot_id: null,
        finding_ids: [101],
        status: "planned",
      }],
      generation_requests: [request],
      generation_task_ids: [],
      auto_reassess: true,
    },
  });
  assert.equal(planned.status, "planned");
  assert.deepEqual(planned.plan_items[0].generation_request, request);
  assert.deepEqual(reproductionRemediationPendingGenerationRequests(planned), [request]);
  assert.equal(reproductionRemediationExecutionIssue(planned), null);
  assert.equal(isReproductionRemediationActive(planned), false);
  assert.equal(isReproductionRemediationTerminal(planned), false);

  const submitted = normalizeReproductionRemediation({
    ...planned,
    status: "generating",
    plan_snapshot: [{
      item_id: "region-101",
      kind: "image_inpaint",
      shot_id: null,
      finding_ids: [101],
      request,
    }],
    plan_items: [{
      item_id: "region-101",
      kind: "image_inpaint",
      shot_id: null,
      finding_ids: [101],
      status: "queued",
      generation_task_id: 88,
    }],
    generation_requests: [request],
    generation_task_ids: [88],
  });
  assert.deepEqual(reproductionRemediationPendingGenerationRequests(submitted), []);
  assert.equal(submitted.plan_items[0].generation_task_id, 88);
  assert.match(reproductionRemediationExecutionIssue(submitted), /正在执行/);

  const partial = normalizeReproductionRemediation({
    ...submitted,
    status: "partial",
    plan_items: [{
      item_id: "region-101",
      kind: "image_inpaint",
      shot_id: null,
      finding_ids: [101],
      status: "failed",
      generation_task_id: 88,
      error: "供应商生成失败",
    }],
  });
  assert.equal(isReproductionRemediationTerminal(partial), true);
  assert.deepEqual(reproductionRemediationPendingGenerationRequests(partial), []);
  assert.equal(partial.plan_items[0].error, "供应商生成失败");

  const terminal = normalizeReproductionRemediation({
    ...submitted,
    status: "succeeded",
    final_asset_ref: "g.99",
    successor_assessment_id: 45,
  });
  assert.equal(isReproductionRemediationTerminal(terminal), true);
  assert.equal(terminal.final_asset_ref, "g.99");
  assert.equal(terminal.successor_assessment_id, 45);
});

test("fails closed when remediation generation lineage is incomplete", () => {
  const remediation = normalizeReproductionRemediation({
    id: 50,
    assessment_id: 18,
    parent_revision_id: 10,
    mode: "image_inpaint",
    status: "planned",
    selected_finding_ids: [1],
    generation_requests: [{
      category: "image",
      stage: "final",
      client_request_id: "missing-plan-item",
      reproduction_remediation_id: 50,
    }],
  });
  assert.match(reproductionRemediationExecutionIssue(remediation), /计划项标识/);
});

test("normalizes remediation list envelopes for refresh recovery", () => {
  const page = normalizeReproductionRemediationPage({
    items: [{
      id: 61,
      parent_revision_id: 12,
      mode: "video_shot_regenerate",
      status: "generating",
      selected_finding_ids: [5],
      selected_shot_ids: ["shot-1"],
    }],
    total: 1,
    limit: 20,
    offset: 0,
  }, 19);
  assert.equal(page.items[0].assessment_id, 19);
  assert.equal(page.items[0].status, "generating");
  assert.equal(isReproductionRemediationActive(page.items[0]), true);
});

test("declares remediation API paths and keeps paid execution behind the quote callback", async () => {
  const apiSource = await readFile(new URL("../lib/api.js", import.meta.url), "utf8");
  const componentSource = await readFile(
    new URL("../app/studio/StudioReproductionAssessment.jsx", import.meta.url),
    "utf8",
  );
  assert.match(apiSource, /createReproductionRemediation/);
  assert.match(apiSource, /reproduction-assessments\/\$\{encodeURIComponent\(assessmentId\)\}\/remediations/);
  assert.match(componentSource, /requestQuoteConfirmation/);
  assert.match(componentSource, /execute: \(\{ request: confirmedRequest \}\) => api\.generate\(confirmedRequest\)/);
  assert.match(componentSource, /onRemediationCreated/);
  assert.match(componentSource, /onRemediationExecutionSubmitted/);
  assert.doesNotMatch(componentSource, /api\.generate\(request\)/);
});
