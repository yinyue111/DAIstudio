import type { Page, Route } from "@playwright/test";

export type ReverseScenario = "image_success" | "video_success" | "cancel" | "refresh" | "cover_confirmation";

type JsonRecord = Record<string, any>;

const NOW = "2026-07-16T10:00:00Z";
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

function configPayload() {
  return {
    defaults: {},
    features: { reverse_prompt_enabled: true },
    models: {
      vision: { cost_credits: 2, enabled: true },
      image: { cost_credits: 15, unlock_cost: 0, enabled: true },
      video: { cost_credits: 16, unlock_cost: 0, enabled: true },
      prompt: { cost_credits: 1, enabled: true },
    },
    model_options: {
      vision: [
        { id: 11, use: "vision", name: "E2E Vision A", model_id: "vision-a", provider: "openai", provider_label: "OpenAI", is_default: true, sort_order: 0, cost_credits: 2, unlock_cost: 0, capabilities: { image_analysis: true, video_analysis: true, product_profile: true, portrait_profile: true } },
        { id: 12, use: "vision", name: "E2E Vision B", model_id: "vision-b", provider: "custom_openai", provider_label: "自定义兼容", is_default: false, sort_order: 10, cost_credits: 2, unlock_cost: 0, capabilities: { image_analysis: true, video_analysis: true, product_profile: true, portrait_profile: true } },
      ],
      image: [
        { id: 21, use: "image", name: "E2E Image A", model_id: "image-a", provider: "openai", provider_label: "OpenAI", is_default: true, sort_order: 0, cost_credits: 15, unlock_cost: 0, capabilities: { text_to_image: true, image_to_image: true, reference_image: true, multi_reference: true } },
        { id: 22, use: "image", name: "E2E Image B", model_id: "image-b", provider: "custom_openai", provider_label: "自定义兼容", is_default: false, sort_order: 10, cost_credits: 18, unlock_cost: 0, capabilities: { text_to_image: true, image_to_image: true, reference_image: true, multi_reference: true } },
      ],
      video: [
        { id: 31, use: "video", name: "E2E Video A", model_id: "video-a", provider: "volcengine_ark", provider_label: "火山方舟", is_default: true, sort_order: 0, cost_credits: 16, unlock_cost: 0, capabilities: { text_to_video: true, image_to_video: true, video_to_video: true } },
        { id: 32, use: "video", name: "E2E Video B", model_id: "video-b", provider: "custom_openai", provider_label: "自定义兼容", is_default: false, sort_order: 10, cost_credits: 20, unlock_cost: 0, capabilities: { text_to_video: true, image_to_video: true, video_to_video: true } },
      ],
      prompt: [
        { id: 41, use: "prompt", name: "E2E Optimizer A", model_id: "prompt-a", provider: "anthropic", provider_label: "Anthropic", is_default: true, sort_order: 0, cost_credits: 1, unlock_cost: 0, capabilities: { prompt_optimization: true } },
        { id: 42, use: "prompt", name: "E2E Optimizer B", model_id: "prompt-b", provider: "openai", provider_label: "OpenAI", is_default: false, sort_order: 10, cost_credits: 2, unlock_cost: 0, capabilities: { prompt_optimization: true } },
      ],
    },
    pricing: {
      reverse: { image_cost: 2, video_preset_costs: { fast: 8, standard: 18, fine: 32 } },
      image: {
        unit_costs: { "1k": 15, "2k": 40, "4k": 100 },
        edit_unit_costs: { "1k": 20, "2k": 55, "4k": 130 },
      },
      video: { preview_cost: 15, per_second: { "480p": 8, "720p": 16, "1080p": 28 } },
    },
    image_size_max_dim: 2048,
    image_n_max: 8,
    max_upload_image_bytes: 20 * 1024 * 1024,
    max_upload_video_bytes: 500 * 1024 * 1024,
    video_duration_max_seconds: 15,
    reverse: {
      image_cost: 2,
      video_default_preset: "standard",
      video_frame_count: 24,
      video_max_cost: 18,
      video_presets: [
        { key: "fast", label: "快速", short_range: "4帧", long_range: "8-12帧", max_frames: 12, max_cost: 8 },
        { key: "standard", label: "标准", short_range: "6-8帧", long_range: "16-24帧", max_frames: 24, max_cost: 18 },
        { key: "fine", label: "精细", short_range: "10-12帧", long_range: "24-36帧", max_frames: 36, max_cost: 32 },
      ],
    },
    mock_mode: true,
    gateways: {
      vision: { mock_mode: true },
      image: { mock_mode: true },
      video: { mock_mode: true },
    },
  };
}

function imageResult() {
  return {
    structured: {
      "主体": "E2E 橙色杯子",
      "场景背景": "纯白棚拍背景",
      "构图": "中心构图，主体占画幅 55%",
      "光线": "柔和顶光，低对比",
      "负向": "文字，水印，畸变",
      "审计附加字段": "仅供历史查看",
    },
    final_text: "E2E 橙色杯子，纯白棚拍背景，中心构图，柔和顶光",
    reference_count: 1,
  };
}

function coverResult() {
  return {
    structured: {
      "分析模式": "封面单帧运动设计",
      "静态观察": "橙色杯子居中，纯白背景",
      "可动元素": "杯子保持结构稳定",
      "主体运动设计": "新设计：仅做轻微呼吸式微动",
      "镜头运动设计": "新设计：镜头缓慢推近",
      "时序设计": "0-5s 缓慢推近",
      "旁白": "未分析",
      "音效": "未分析",
      "负向": "形变，闪烁，新增主体",
    },
    final_text: "基于封面单帧的新运动设计，镜头缓慢推近",
    reference_count: 1,
    video_analysis: {
      analysis_mode: "cover_fallback",
      degraded_reason: "抽帧失败，已经用户确认使用封面",
      source: { width: 640, height: 640, ratio: "1:1", duration_seconds: 5, audio_analyzed: false },
      sampled_frames: [{ timestamp_seconds: 0 }],
      evidence_coverage: { start_seconds: 0, end_seconds: 0 },
      analysis_gaps: [{ start_seconds: 0, end_seconds: 5 }],
    },
  };
}

function videoResult() {
  return {
    structured: {
      "主体": "E2E 橙色杯子",
      "场景背景": "白色棚拍背景",
      "视角构图": "竖屏中心构图",
      "主体动作": "不应由顶层字段下发",
      "字幕卖点": "不应进入视觉提示词",
      "旁白": "未分析",
      "音效": "未分析",
    },
    final_text: "E2E 橙色杯子，白色棚拍背景；镜头1：杯子居中；镜头2：多帧证据显示杯子缓慢旋转",
    reference_count: 3,
    video_analysis: {
      analysis_mode: "multi_frame",
      source: {
        width: 720,
        height: 1280,
        ratio: "9:16",
        duration_seconds: 8,
        audio_analyzed: false,
      },
      sampled_frames: [
        { index: 1, timestamp_seconds: 0 },
        { index: 2, timestamp_seconds: 3.5 },
        { index: 3, timestamp_seconds: 7 },
      ],
      shots: [
        {
          start_seconds: 0,
          end_seconds: 3.5,
          visual: "杯子居中",
          action: "",
          camera: "",
          lighting: "柔和顶光",
          transition: "",
          ocr: "忽略的 OCR",
          audio_cue: "未分析",
          evidence_frame_indices: [1],
          confidence: 0.92,
        },
        {
          start_seconds: 3.5,
          end_seconds: 7,
          visual: "杯子保持完整",
          action: "杯子缓慢旋转",
          camera: "镜头微微推近",
          lighting: "柔和顶光",
          transition: "无转场",
          ocr: "忽略的 OCR",
          audio_cue: "未分析",
          evidence_frame_indices: [2, 3],
          confidence: 0.9,
        },
      ],
      evidence_coverage: { start_seconds: 0, end_seconds: 7, ratio: 0.875 },
      analysis_gaps: [{ start_seconds: 7, end_seconds: 8 }],
    },
  };
}

function operationPayload(
  id: number,
  target: "image" | "video",
  status: string,
  snapshot: JsonRecord | null,
  overrides: JsonRecord = {},
) {
  const progress = status === "succeeded" || status === "failed" || status === "canceled" ? 100 : status === "queued" ? 0 : 35;
  return {
    id,
    target,
    source_type: target === "video" ? "video" : "image",
    status,
    phase: status === "running" ? "E2E 后台分析" : status === "queued" ? "E2E 等待反推" : null,
    progress,
    result: null,
    video_analysis: null,
    request_context: {
      workspace_mode: snapshot?.creation_mode || target,
      source_signature: snapshot?.source_signature || "",
    },
    workspace_snapshot_v2: snapshot,
    reference_count: 1,
    charged_credits: 0,
    cost_frozen: target === "video" ? 18 : 2,
    cost_settled: 0,
    confirmation_expires_at: null,
    cancel_requested: false,
    error_code: null,
    error: null,
    created_at: NOW,
    updated_at: NOW,
    started_at: status === "queued" ? null : NOW,
    finished_at: null,
    timestamps: {
      created_at: NOW,
      updated_at: NOW,
      started_at: status === "queued" ? null : NOW,
      finished_at: null,
    },
    ...overrides,
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export function reverseHistoryV2Row() {
  return {
    id: 501,
    title: "E2E V2 反推历史",
    prompt: "V2 恢复提示词",
    category: "image",
    source: "reverse",
    favorite: false,
    usage_count: 0,
    params: {
      reverse_snapshot_v2: {
        version: 2,
        creation_mode: "image",
        subject_mode: "general",
        target: "image",
        selected: {
          id: 800,
          type: "image",
          url: "/api/uploads/history-e2e.png",
          thumb: "/api/uploads/history-e2e.png",
          width: 640,
          height: 640,
          retention_expires_at: "2099-01-01T00:00:00Z",
        },
        assets: [],
        structured: { "主体": "历史 E2E 杯子", "光线": "历史柔和侧光" },
        final_text: "V2 恢复提示词",
        video_analysis_preset: "standard",
        video_analysis: null,
        subject_profile: null,
      },
    },
  };
}

export async function installReverseApiMock(
  page: Page,
  { scenario = "image_success", history = [] }: { scenario?: ReverseScenario; history?: JsonRecord[] } = {},
) {
  const state = {
    scenario,
    history,
    operation: null as JsonRecord | null,
    createBodies: [] as JsonRecord[],
    operationReads: 0,
    wsTicketRequests: 0,
    cancelRequests: 0,
    confirmRequests: 0,
    draftReads: 0,
    draftWrites: 0,
    draftPayload: null as JsonRecord | null,
    allowCompletion: false,
    confirmed: false,
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/me") {
      return fulfillJson(route, { id: 9001, phone: "13900009001", nickname: "E2E", balance_credits: 200 });
    }
    if (path === "/api/config") return fulfillJson(route, configPayload());
    if (path === "/api/tasks") return fulfillJson(route, []);

    if (path === "/api/me/drafts/studio") {
      if (method === "PUT") {
        const body = request.postDataJSON() as JsonRecord;
        state.draftPayload = body.payload || null;
        state.draftWrites += 1;
      } else if (method === "GET") {
        state.draftReads += 1;
      }
      return fulfillJson(route, { key: "studio", payload: state.draftPayload });
    }

    if (path === "/api/uploads/image" && method === "POST") {
      return fulfillJson(route, {
        id: 7001,
        type: "image",
        url: "/api/uploads/e2e-image.png",
        preview_url: "/api/uploads/e2e-image.png",
        width: 640,
        height: 640,
      });
    }
    if (path === "/api/uploads/video" && method === "POST") {
      return fulfillJson(route, {
        id: 7002,
        type: "video",
        url: "/api/uploads/e2e-video.mp4",
        thumb: "/api/uploads/e2e-cover.png",
        width: 640,
        height: 640,
      });
    }
    if (path.startsWith("/api/uploads/") && method === "GET") {
      return route.fulfill({ status: 200, contentType: "image/png", body: PNG });
    }

    if (path === "/api/prompt/reverse-operations" && method === "GET") {
      const active = state.operation && ["queued", "running", "needs_confirmation"].includes(state.operation.status);
      return fulfillJson(route, active ? [state.operation] : []);
    }
    if (path === "/api/prompt/reverse-operations" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      state.createBodies.push(body);
      const target = body.target === "video" ? "video" : "image";
      const initialStatus = scenario === "cover_confirmation"
        ? "needs_confirmation"
        : ["image_success", "video_success"].includes(scenario) ? "queued" : "running";
      state.operation = operationPayload(6001, target, initialStatus, body.workspace_snapshot_v2 || null, {
        confirmation_expires_at: initialStatus === "needs_confirmation" ? "2099-01-01T00:15:00Z" : null,
      });
      return fulfillJson(route, state.operation, 202);
    }

    const match = path.match(/^\/api\/prompt\/reverse-operations\/(\d+)(?:\/(ws-ticket|confirm-cover|cancel))?$/);
    if (match && match[2] === "ws-ticket") {
      state.wsTicketRequests += 1;
      return fulfillJson(route, { detail: "E2E 强制 WebSocket 断线后轮询" }, 503);
    }
    if (match && match[2] === "cancel") {
      state.cancelRequests += 1;
      state.operation = operationPayload(6001, state.operation?.target || "image", "canceled", state.operation?.workspace_snapshot_v2 || null, {
        cost_frozen: 0,
        cancel_requested: true,
        error: "用户已取消",
        finished_at: NOW,
      });
      return fulfillJson(route, state.operation);
    }
    if (match && match[2] === "confirm-cover") {
      state.confirmRequests += 1;
      state.confirmed = true;
      state.operation = operationPayload(6001, "video", "queued", state.operation?.workspace_snapshot_v2 || null);
      return fulfillJson(route, state.operation);
    }
    if (match && !match[2] && method === "GET") {
      state.operationReads += 1;
      if (!state.operation) return fulfillJson(route, { detail: "not found" }, 404);
      const shouldSucceed = (
        scenario === "image_success"
        || scenario === "video_success"
        || (scenario === "refresh" && state.allowCompletion)
        || (scenario === "cover_confirmation" && state.confirmed)
      );
      if (shouldSucceed) {
        const result: JsonRecord = scenario === "cover_confirmation"
          ? coverResult()
          : scenario === "video_success"
            ? videoResult()
            : imageResult();
        const settledCost = scenario === "video_success" ? 18 : 2;
        state.operation = operationPayload(6001, state.operation.target, "succeeded", state.operation.workspace_snapshot_v2 || null, {
          result,
          video_analysis: result.video_analysis || null,
          cost_frozen: 0,
          cost_settled: settledCost,
          charged_credits: settledCost,
          reference_count: Number(result.reference_count || 1),
          finished_at: NOW,
        });
      } else if (state.operation.status !== "canceled") {
        state.operation = { ...state.operation, status: "running", phase: "E2E 后台分析", progress: 35 };
      }
      return fulfillJson(route, state.operation);
    }

    if (path === "/api/prompts/history" && method === "GET") return fulfillJson(route, state.history);
    if (/^\/api\/prompts\/history\/\d+$/.test(path) && method === "PATCH") {
      const row = state.history.find((item) => String(item.id) === path.split("/").at(-1));
      return fulfillJson(route, row || {});
    }

    return fulfillJson(route, { detail: `Unhandled E2E API route: ${method} ${path}` }, 501);
  });

  return state;
}
