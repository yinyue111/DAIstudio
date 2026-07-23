import type { Page, Route } from "@playwright/test";

import { installReverseApiMock } from "./reverse-api";

type JsonRecord = Record<string, any>;
type MediaType = "image" | "video";

const NOW = "2026-07-20T12:00:00Z";

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function sourceTrace(mediaType: MediaType) {
  const isVideo = mediaType === "video";
  return {
    asset_ref: isVideo ? "u.7002" : "u.7001",
    source_type: mediaType,
    selected_type: mediaType,
    selected_url: isVideo ? "/api/uploads/e2e-video.mp4" : "/api/uploads/e2e-image.png",
    selected_thumb: isVideo ? "/api/uploads/e2e-cover.png" : null,
    mode: isVideo ? "video" : "image",
  };
}

function generatedAsset(mediaType: MediaType, id = 7201) {
  const isVideo = mediaType === "video";
  return {
    id,
    asset_ref: `g.${id}`,
    type: mediaType,
    url: isVideo ? "/api/uploads/e2e-generated-video.mp4" : "/api/uploads/e2e-generated-image.png",
    thumb: isVideo ? "/api/uploads/e2e-generated-cover.png" : null,
    preview_url: isVideo ? "/api/uploads/e2e-generated-cover.png" : "/api/uploads/e2e-generated-image.png",
    width: isVideo ? 720 : 1024,
    height: isVideo ? 1280 : 1024,
    duration: isVideo ? 8 : null,
    available: true,
    created_at: NOW,
  };
}

function sourceTask(mediaType: MediaType, completed: boolean) {
  const isVideo = mediaType === "video";
  return {
    id: 7101,
    category: mediaType,
    stage: isVideo ? "final" : "preview",
    status: completed ? "succeeded" : "running",
    progress: completed ? 100 : 45,
    phase: completed ? null : "E2E 生成收口",
    requested_count: 1,
    saved_count: completed ? 1 : 0,
    assets: completed ? [generatedAsset(mediaType)] : [],
    reverse_operation_id: 6001,
    source_revision_id: 6104,
    model_config_id: isVideo ? 31 : 21,
    params: {
      n: 1,
      duration: isVideo ? 8 : null,
      resolution: isVideo ? "1080p" : null,
      _source_trace: sourceTrace(mediaType),
    },
    created_at: NOW,
    updated_at: NOW,
    finished_at: completed ? NOW : null,
  };
}

function assessmentPayload(mediaType: MediaType, status: "queued" | "succeeded") {
  const isVideo = mediaType === "video";
  const completed = status === "succeeded";
  const findings = isVideo
    ? [
        {
          id: 9001,
          dimension: "action_semantics",
          severity: "high",
          confidence: 0.94,
          title: "镜头 A 主体动作偏差",
          detail: "旋转幅度小于源视频。",
          start_seconds: 0,
          end_seconds: 3.5,
          shot_index: 1,
          shot_id: "shot-a",
          evidence: { aligned_frame_ids: [1, 2] },
        },
        {
          id: 9002,
          dimension: "camera_motion",
          severity: "medium",
          confidence: 0.9,
          title: "镜头 B 推近速度不一致",
          detail: "生成结果的推近过快。",
          start_seconds: 3.5,
          end_seconds: 8,
          shot_index: 2,
          shot_id: "shot-b",
          evidence: { aligned_frame_ids: [3, 4] },
        },
      ]
    : [
        {
          id: 9001,
          dimension: "subject_semantics",
          severity: "high",
          confidence: 0.96,
          title: "商品标签形状偏差",
          detail: "标签右上角轮廓与源图不一致。",
          bbox: { x: 0.58, y: 0.18, width: 0.24, height: 0.2 },
          evidence: { source_region: "label", generated_region: "label" },
        },
        {
          id: 9002,
          dimension: "ocr_text",
          severity: "info",
          confidence: 0.7,
          title: "OCR 分析未配置",
          detail: "当前结果不参与局部重绘。",
        },
      ];
  return {
    id: 7301,
    media_type: mediaType,
    status,
    progress: completed ? 100 : 15,
    phase: completed ? null : "E2E 对齐证据",
    source_asset_ref: isVideo ? "u.7002" : "u.7001",
    generated_asset_ref: "g.7201",
    reverse_operation_id: 6001,
    reverse_revision_id: 6104,
    generation_task_id: 7101,
    dimensions: completed
      ? [
          {
            key: isVideo ? "action_semantics" : "subject_semantics",
            label: isVideo ? "动作一致性" : "主体一致性",
            status: "succeeded",
            score: isVideo ? 0.68 : 0.72,
            analyzer: "e2e-vision",
            analyzer_version: "1",
          },
          {
            key: isVideo ? "audio" : "ocr_text",
            label: isVideo ? "音频一致性" : "文字与 Logo",
            status: "unsupported",
            degraded_reason: isVideo ? "E2E 未配置音频比对" : "E2E 未配置 OCR 比对",
          },
        ]
      : [],
    findings: completed ? findings : [],
    warnings: completed ? ["不可用维度已排除在聚合评估之外"] : [],
    created_at: NOW,
    updated_at: NOW,
    finished_at: completed ? NOW : null,
  };
}

function generationRequest(
  mediaType: MediaType,
  remediationId: number,
  itemId: string,
  shotId: string | null,
) {
  const isVideo = mediaType === "video";
  return {
    category: mediaType,
    stage: isVideo ? "final" : "preview",
    model_config_id: isVideo ? 31 : 21,
    client_request_id: `e2e-remediation-${itemId}`,
    prompt: isVideo ? `重生成镜头 ${shotId}` : "修正商品标签轮廓",
    negative_prompt: "不要改动未选区域",
    reproduction_remediation_id: remediationId,
    reproduction_plan_item_id: itemId,
    ...(shotId ? { shot_id: shotId, shot_context: { shot_id: shotId } } : {}),
    params: isVideo
      ? { duration: shotId === "shot-a" ? 3.5 : 4.5, resolution: "1080p" }
      : { n: 1, edit_mask_mode: "evidence_edit" },
  };
}

function remediationPayload(
  mediaType: MediaType,
  submittedCount: number,
  final = false,
) {
  const isVideo = mediaType === "video";
  const findingIds = isVideo ? [9001, 9002] : [9001];
  const shotIds = isVideo ? ["shot-a", "shot-b"] : [];
  const itemIds = isVideo ? ["shot-a-item", "shot-b-item"] : ["image-region-item"];
  const requests = itemIds.map((itemId, index) => generationRequest(
    mediaType,
    7400,
    itemId,
    isVideo ? shotIds[index] : null,
  ));
  const planItems = itemIds.map((itemId, index) => ({
    id: itemId,
    index,
    kind: isVideo ? "video_shot_regenerate" : "image_inpaint",
    shot_id: isVideo ? shotIds[index] : null,
    finding_ids: isVideo ? [findingIds[index]] : findingIds,
    status: final ? "succeeded" : index < submittedCount ? "queued" : "planned",
    generation_request: requests[index],
    generation_task_id: index < submittedCount ? 7501 + index : null,
    asset_id: final ? 7601 + index : null,
    asset_ref: final ? `g.${7601 + index}` : null,
  }));
  return {
    id: 7400,
    assessment_id: 7301,
    correction_id: 7350,
    parent_revision_id: 6104,
    applied_revision_id: 6105,
    mode: isVideo ? "video_shot_regenerate" : "image_inpaint",
    status: final ? "succeeded" : "planned",
    phase: final
      ? (isVideo ? "已重生成并合成成片" : "已完成局部重绘")
      : "等待逐项执行",
    selected_finding_ids: findingIds,
    selected_shot_ids: shotIds,
    plan_items: planItems,
    generation_requests: requests,
    generation_task_ids: Array.from({ length: submittedCount }, (_, index) => 7501 + index),
    composition_tool_run_id: final && isVideo ? 7701 : null,
    composition_workflow_run_id: final && isVideo ? 7702 : null,
    final_asset_id: final ? 7699 : null,
    final_asset_ref: final ? "g.7699" : null,
    successor_assessment_id: final ? 7801 : null,
    auto_reassess: true,
    created_at: NOW,
    updated_at: NOW,
    finished_at: final ? NOW : null,
  };
}

function remediationTask(mediaType: MediaType, index: number) {
  const id = 7501 + index;
  return {
    id,
    category: mediaType,
    stage: mediaType === "video" ? "final" : "preview",
    status: "queued",
    progress: 0,
    requested_count: 1,
    assets: [],
    created_at: NOW,
    updated_at: NOW,
  };
}

export async function installReproductionRemediationApiMock(
  page: Page,
  mediaType: MediaType,
) {
  const base = await installReverseApiMock(page);
  const requiredSubmissions = mediaType === "video" ? 2 : 1;
  const state = {
    ...base,
    mediaType,
    sourceTaskCompleted: false,
    assessmentReads: 0,
    assessmentCreateBodies: [] as JsonRecord[],
    remediationCreateBodies: [] as JsonRecord[],
    remediationReads: 0,
    generationQuoteBodies: [] as JsonRecord[],
    generationBodies: [] as JsonRecord[],
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/tasks" && method === "GET") {
      return fulfillJson(route, [sourceTask(mediaType, state.sourceTaskCompleted)]);
    }
    if (path === "/api/tasks/7101/ws-ticket" && method === "POST") {
      return fulfillJson(route, { detail: "E2E 使用轮询" }, 503);
    }
    if (path === "/api/tasks/7101" && method === "GET") {
      state.sourceTaskCompleted = true;
      return fulfillJson(route, sourceTask(mediaType, true));
    }
    if (/^\/api\/tasks\/75\d{2}\/ws-ticket$/.test(path) && method === "POST") {
      return fulfillJson(route, { detail: "E2E 使用轮询" }, 503);
    }
    if (/^\/api\/tasks\/75\d{2}$/.test(path) && method === "GET") {
      const index = Number(path.split("/").at(-1)) - 7501;
      return fulfillJson(route, {
        ...remediationTask(mediaType, index),
        status: "succeeded",
        progress: 100,
        saved_count: 1,
        assets: [generatedAsset(mediaType, 7601 + index)],
        finished_at: NOW,
      });
    }

    if (path === "/api/reproduction-assessments" && method === "POST") {
      state.assessmentCreateBodies.push(request.postDataJSON() as JsonRecord);
      return fulfillJson(route, assessmentPayload(mediaType, "queued"), 202);
    }
    if (path === "/api/reproduction-assessments/7301" && method === "GET") {
      state.assessmentReads += 1;
      return fulfillJson(route, assessmentPayload(mediaType, "succeeded"));
    }
    if (path === "/api/reproduction-assessments/7301/remediations" && method === "GET") {
      const remediation = state.remediationCreateBodies.length
        ? remediationPayload(
            mediaType,
            state.generationBodies.length,
            state.generationBodies.length >= requiredSubmissions,
          )
        : null;
      return fulfillJson(route, {
        items: remediation ? [remediation] : [],
        total: remediation ? 1 : 0,
        limit: 20,
        offset: 0,
      });
    }
    if (path === "/api/reproduction-assessments/7301/remediations" && method === "POST") {
      state.remediationCreateBodies.push(request.postDataJSON() as JsonRecord);
      return fulfillJson(route, remediationPayload(mediaType, 0), 201);
    }
    if (path === "/api/reproduction-assessments/7301/remediations/7400" && method === "GET") {
      state.remediationReads += 1;
      return fulfillJson(route, remediationPayload(
        mediaType,
        state.generationBodies.length,
        state.generationBodies.length >= requiredSubmissions,
      ));
    }

    if (path === "/api/quotes" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      if (!body.reproduction_remediation_id) return route.fallback();
      state.generationQuoteBodies.push(body);
      const quoteId = 7901 + state.generationQuoteBodies.length;
      const credits = mediaType === "video" ? 16 : 20;
      return fulfillJson(route, {
        id: quoteId,
        quote_id: quoteId,
        kind: "generation",
        status: "active",
        total_credits: credits,
        breakdown: [{ key: "generation", label: mediaType === "video" ? "镜头重生成" : "局部重绘", credits }],
        balance: { before: 200, after: 200 - credits, sufficient: true },
        affordable: true,
        expires_at: "2099-01-01T00:15:00Z",
      }, 201);
    }
    if (path === "/api/generate" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      state.generationBodies.push(body);
      return fulfillJson(route, remediationTask(mediaType, state.generationBodies.length - 1), 202);
    }

    return route.fallback();
  });

  return state;
}
