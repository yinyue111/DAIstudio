import type { Page, Route } from "@playwright/test";

export type ReverseScenario = "image_success" | "video_success" | "cancel" | "refresh" | "cover_confirmation";
export type SharedRecipeStatus = "active" | "revoked" | "expired";

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
      reverse: { image_cost: 2, video_preset_costs: { fast: 8, standard: 18, fine: 32, ultra: 48 } },
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
      video_frame_count: 14,
      video_max_cost: 18,
      video_presets: [
        { key: "fast", label: "快速", frame_range: "4-8帧", short_range: "4-8帧", long_range: "4-8帧", max_frames: 8, max_cost: 8 },
        { key: "standard", label: "标准", frame_range: "8-14帧", short_range: "8-14帧", long_range: "8-14帧", max_frames: 14, max_cost: 18 },
        { key: "fine", label: "精细", frame_range: "14-22帧", short_range: "14-22帧", long_range: "14-22帧", max_frames: 22, max_cost: 32 },
        { key: "ultra", label: "超精细", frame_range: "22-36帧", short_range: "22-36帧", long_range: "22-36帧", max_frames: 36, max_cost: 48 },
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
  const workspaceSnapshotV3 = Number(snapshot?.version) === 3 ? snapshot : null;
  const workspaceSnapshotV2 = Number(snapshot?.version) === 2 ? snapshot : null;
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
    workspace_snapshot_v2: workspaceSnapshotV2,
    workspace_snapshot_v3: workspaceSnapshotV3,
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

function resultRevision(
  id: number,
  version: number,
  source: "provider_raw" | "normalized" | "user_edit" | "applied",
  payload: JsonRecord,
  parentRevisionId: number | null = null,
) {
  return {
    id,
    operation_id: 6001,
    version,
    source,
    payload,
    parent_revision_id: parentRevisionId,
    source_content_hash: "e2e-source-hash",
    source_fingerprints: [{ kind: "asset", content_hash: "e2e-source-hash" }],
    payload_hash: `e2e-payload-${version}`,
    lineage_status: "verified",
    evidence_review_action: "not_applicable",
    created_at: NOW,
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

export function sharedRecipeFixture(slug = "E2ESharedRecipeSlug_1234") {
  const payload = {
    schema_version: "creation-recipe.v1",
    prompt: "精确生成稿：银色香水瓶居中，镜头缓慢推进",
    negative: "不要品牌文字漂移",
    structured: {
      "主体": "银色香水瓶",
      "运镜": "slow dolly in",
    },
    generation_params: {
      ratio: "9:16",
      duration: 8,
      resolution: "1080p",
      count: 2,
      seed: 987654,
    },
    reverse_snapshot_v3: {
      version: 3,
      target: "video",
      creation_mode: "video_edit",
      subject_mode: "general",
      final_text: "公开快照中的旧生成稿",
      selected: { type: "video" },
      assets: [{ type: "image" }],
      sources: [
        { source_type: "video", role: "primary" },
        { source_type: "image", role: "product" },
      ],
      analysis_focus: "storyboard",
      analysis_precision: "fine",
      output_purpose: "storyboard",
      custom_instruction: "保留商品文字并重点描述镜头节奏",
      source_range: null,
      source_ranges: [{ start_seconds: 2, end_seconds: 6 }],
      custom_keyframes: [2, 4, 6],
      include_audio: true,
      structured: {
        "主体": "银色香水瓶",
        "运镜": "slow dolly in",
      },
      image_evidence: [{
        field: "brand_text",
        text: "CODEX",
        bbox: [0.1, 0.2, 0.5, 0.35],
        confidence: 0.98,
        review_status: "confirmed",
        mask_mode: "protect",
      }],
      video_analysis: {
        analysis_mode: "multi_segment",
        selected_ranges: [{ start_seconds: 2, end_seconds: 6 }],
        shots: [{
          start_seconds: 2,
          end_seconds: 4,
          visual: "香水瓶居中",
          action: "瓶身缓慢旋转",
          camera: "slow dolly in",
          evidence_frame_indices: [4, 7],
        }],
        ocr_tracks: [{ text: "CODEX", start_seconds: 2.2, end_seconds: 3.8 }],
        audio_evidence: [{ kind: "asr", text: "新品上市", start_seconds: 2, end_seconds: 4 }],
      },
      reverse_result: {
        structured: {
          "主体": "银色香水瓶",
          "运镜": "slow dolly in",
        },
        final_text: "精确生成稿：银色香水瓶居中，镜头缓慢推进",
        negative: "不要品牌文字漂移",
        video_analysis: {
          analysis_mode: "multi_segment",
          selected_ranges: [{ start_seconds: 2, end_seconds: 6 }],
          shots: [{
            start_seconds: 2,
            end_seconds: 4,
            visual: "香水瓶居中",
            action: "瓶身缓慢旋转",
            camera: "slow dolly in",
            evidence_frame_indices: [4, 7],
          }],
        },
      },
      reverse_result_tab: "storyboard",
      result_schema_version: "reverse-result.v3",
      reverse_applied_version: 5,
      result_revisions: [
        { version: 1, source: "provider_raw" },
        { version: 2, source: "normalized" },
        { version: 4, source: "user_edit" },
        { version: 5, source: "applied" },
      ],
      model_selections: { image: 21, video: 31, vision: 11, prompt: 41 },
      generation: {
        ratio: "9:16",
        duration: 8,
        resolution: "1080p",
        count: 2,
        seed: 987654,
        model_selections: { image: 21, video: 31, vision: 11, prompt: 41 },
        model_config_id: 31,
        catalog_versions: {
          video: { model_config_id: 31, capability_version: 4, price_version: 7 },
          vision: { model_config_id: 11, capability_version: 3, price_version: 5 },
        },
      },
    },
    reverse_result: {
      structured: {
        "主体": "银色香水瓶",
        "运镜": "slow dolly in",
      },
      final_text: "精确生成稿：银色香水瓶居中，镜头缓慢推进",
      negative: "不要品牌文字漂移",
    },
    generation: {
      ratio: "9:16",
      duration: 8,
      resolution: "1080p",
      count: 2,
      seed: 987654,
      model_selections: { image: 21, video: 31, vision: 11, prompt: 41 },
      model_config_id: 31,
      catalog_versions: {
        video: { model_config_id: 31, capability_version: 4, price_version: 7 },
        vision: { model_config_id: 11, capability_version: 3, price_version: 5 },
      },
    },
    public_asset_access: {
      status: "unavailable",
      reason: "private_source_assets_not_shared",
      removed_count: 6,
    },
  };
  return {
    share: {
      id: 8101,
      recipe_id: 42,
      version: 3,
      slug,
      status: "active",
      expires_at: "2099-01-01T00:00:00Z",
      revoked_at: null,
      share_url: `/recipes/shared/${slug}`,
      created_at: NOW,
    },
    recipe: {
      id: 42,
      source_operation_id: null,
      title: "E2E 视频广告完整配方",
      category: "video",
      visibility: "public",
      moderation_status: "approved",
      favorite: false,
      current_version: 3,
      approved_version: 3,
      cover_asset_url: null,
      submitted_at: NOW,
      reviewed_at: NOW,
      review_note: null,
      version: {
        id: 4203,
        recipe_id: 42,
        version: 3,
        schema_version: "creation-recipe.v1",
        payload,
        created_at: NOW,
      },
      created_at: NOW,
      updated_at: NOW,
    },
  };
}

export async function installReverseApiMock(
  page: Page,
  {
    scenario = "image_success",
    history = [],
    authenticated = true,
    sharedRecipe = null,
    sharedRecipeStatus = "active",
  }: {
    scenario?: ReverseScenario;
    history?: JsonRecord[];
    authenticated?: boolean;
    sharedRecipe?: JsonRecord | null;
    sharedRecipeStatus?: SharedRecipeStatus;
  } = {},
) {
  const state = {
    scenario,
    history,
    authenticated,
    sharedRecipe,
    sharedRecipeStatus,
    sharedRecipeReads: 0,
    usageBodies: [] as JsonRecord[],
    cloneBodies: [] as JsonRecord[],
    operation: null as JsonRecord | null,
    quoteRequests: [] as JsonRecord[],
    createBodies: [] as JsonRecord[],
    operationReads: 0,
    wsTicketRequests: 0,
    cancelRequests: 0,
    confirmRequests: 0,
    draftReads: 0,
    draftWrites: 0,
    draftPayload: null as JsonRecord | null,
    draftPayloads: [] as JsonRecord[],
    revisions: [] as JsonRecord[],
    applyBodies: [] as JsonRecord[],
    promptOptimizationBodies: [] as JsonRecord[],
    allowCompletion: false,
    confirmed: false,
  };

  function ensureResultRevisions() {
    if (state.revisions.length || !state.operation?.result) return;
    state.revisions = [
      resultRevision(6101, 1, "provider_raw", state.operation.result),
      resultRevision(6102, 2, "normalized", state.operation.result, 6101),
    ];
  }

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/me") {
      if (!state.authenticated) return fulfillJson(route, { detail: "未登录或登录已过期" }, 401);
      return fulfillJson(route, { id: 9001, phone: "13900009001", nickname: "E2E", balance_credits: 200 });
    }
    if (path === "/api/auth/features") {
      return fulfillJson(route, { registration_enabled: true, sms_auth_enabled: false });
    }
    if (path === "/api/config") return fulfillJson(route, configPayload());
    if (path === "/api/quotes" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      const quotedRequest = body.request && typeof body.request === "object" ? body.request : {};
      const target = quotedRequest.target === "video" ? "video" : "image";
      const totalCredits = target === "video" ? 18 : 2;
      const quoteId = 5000 + state.quoteRequests.length + 1;
      state.quoteRequests.push(body);
      return fulfillJson(route, {
        id: quoteId,
        quote_id: quoteId,
        kind: body.kind,
        status: "active",
        total_credits: totalCredits,
        breakdown: [{
          key: "reverse",
          label: target === "video" ? "视频证据反推" : "图片证据反推",
          credits: totalCredits,
        }],
        balance: { before: 200, after: 200 - totalCredits, sufficient: true },
        affordable: true,
        expires_at: "2099-01-01T00:15:00Z",
      }, 201);
    }
    if (path === "/api/navigation") {
      return fulfillJson(route, {
        schema_version: 1,
        default_key: "studio",
        items: [
          { key: "studio", label: "创作", href: "/", width: "w-16", enabled: true, visible: true },
          { key: "catalog", label: "能力", href: "/catalog", width: "w-16", enabled: true, visible: true },
          { key: "prompts", label: "灵感配方", href: "/prompts", width: "w-24", enabled: true, visible: true },
          { key: "projects", label: "项目", href: "/projects", width: "w-16", enabled: true, visible: true },
          { key: "profile", label: "素材", href: "/profile", width: "w-16", enabled: true, visible: true },
          { key: "recharge", label: "充值", href: "/recharge", width: "w-16", enabled: true, visible: true },
          { key: "history", label: "历史", href: "/history", width: "w-16", enabled: true, visible: true },
        ],
      });
    }
    if (path === "/api/events/ws-ticket" && method === "POST") {
      return fulfillJson(route, { detail: "E2E 使用轮询任务中心" }, 503);
    }
    if (path === "/api/tasks") return fulfillJson(route, []);

    const sharedRecipeMatch = path.match(/^\/api\/recipes\/shared\/([^/]+)$/);
    if (sharedRecipeMatch && method === "GET") {
      state.sharedRecipeReads += 1;
      if (!state.sharedRecipe || state.sharedRecipeStatus !== "active") {
        return fulfillJson(route, { detail: "配方分享不存在、已撤销或已过期" }, 404);
      }
      return fulfillJson(route, state.sharedRecipe);
    }

    const recipeActionMatch = path.match(/^\/api\/recipes\/(\d+)\/(usage|clone)$/);
    if (recipeActionMatch && method === "POST") {
      if (!state.authenticated) return fulfillJson(route, { detail: "未登录或登录已过期" }, 401);
      const body = request.postDataJSON() as JsonRecord;
      if (recipeActionMatch[2] === "usage") {
        state.usageBodies.push(body);
        return fulfillJson(route, {
          id: 8200 + state.usageBodies.length,
          recipe_id: Number(recipeActionMatch[1]),
          recipe_version: Number(body.version || state.sharedRecipe?.share?.version || 1),
          user_id: 9001,
          event_type: body.event_type,
          source: body.share_slug ? "share" : "owner",
          share_id: body.share_slug ? Number(state.sharedRecipe?.share?.id || 8101) : null,
          generation_task_id: null,
          client_event_id: body.client_event_id || null,
          context: body.context || null,
          created_at: NOW,
        }, 201);
      }
      state.cloneBodies.push(body);
      const source = state.sharedRecipe?.recipe || {};
      return fulfillJson(route, {
        ...source,
        id: 4300 + state.cloneBodies.length,
        source_operation_id: null,
        title: `${source.title || "共享创作配方"} · 我的派生`,
        visibility: "private",
        moderation_status: "draft",
        approved_version: null,
        version: {
          ...(source.version || {}),
          id: 4400 + state.cloneBodies.length,
          recipe_id: 4300 + state.cloneBodies.length,
          version: 1,
        },
      });
    }

    if (path === "/api/studio/prompt-optimizations" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      state.promptOptimizationBodies.push(body);
      return fulfillJson(route, {
        proposal_id: 9101,
        proposal_version: 1,
        mode: body.mode || "target_model_adaptation",
        optimization_kind: "model_compile",
        original: { final_text: body.prompt || "E2E 原始提示词" },
        suggestion: { final_text: "E2E 目标模型编译稿" },
        segments: [{
          id: "segment-final-text",
          field_path: "final_text",
          label: "生成稿",
          original: body.prompt || "E2E 原始提示词",
          suggestion: "E2E 目标模型编译稿",
          changed: true,
        }],
        constraint_coverage: [],
        warnings: [
          {
            code: "profile_dropped_field",
            field: "audio.voiceover",
            action: "dropped",
            message: "当前图片模型不接收旁白轨。",
          },
          {
            code: "profile_transformed_field",
            field: "structured.camera",
            action: "transformed",
            message: "运镜约束已合并到生成稿。",
          },
          {
            code: "constraint_unverified",
            field: "protected.brand_text",
            action: "unverified",
            message: "品牌文字需要生成后人工核验。",
          },
        ],
        provenance: {},
        compiler_profile: {
          model_config_id: Number(body.target_model_config_id || 21),
          model_id: "image-a",
          provider: "openai",
          capability_version_id: 101,
          capability_version: 1,
          schema_version: "e2e.v1",
          capabilities: { text_to_image: true },
        },
        charged_credits: 0,
      }, 201);
    }
    if (path === "/api/studio/prompt-optimizations/9101/accept" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      return fulfillJson(route, {
        proposal_id: 9101,
        proposal_version: 2,
        status: "accepted",
        accepted_segment_ids: body.accepted_segment_ids || ["segment-final-text"],
        rejected_segment_ids: body.rejected_segment_ids || [],
        result: { final_text: "E2E 目标模型编译稿" },
        revision: null,
      });
    }
    if (path === "/api/studio/prompt-optimizations" && method === "GET") {
      return fulfillJson(route, { items: [], limit: 8, offset: 0, has_more: false });
    }
    if (path === "/api/prompt/reverse-batches" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/prompt/reverse-analyzers/status" && method === "GET") {
      const unsupported = (reason: string) => ({
        status: "unsupported",
        analyzer_version: "unconfigured",
        degraded_reason: reason,
      });
      return fulfillJson(route, {
        image: { status: "unsupported" },
        video: { status: "unsupported" },
        audio: {
          asr: unsupported("E2E 未配置 ASR"),
          speaker: unsupported("E2E 未配置说话人分析"),
          music: unsupported("E2E 未配置音乐分析"),
          beat: unsupported("E2E 未配置节拍分析"),
          sfx: unsupported("E2E 未配置音效分析"),
        },
      });
    }

    if (path === "/api/me/drafts/studio") {
      if (method === "PUT") {
        const body = request.postDataJSON() as JsonRecord;
        state.draftPayload = body.payload || null;
        if (state.draftPayload) state.draftPayloads.push(state.draftPayload);
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
      state.operation = operationPayload(
        6001,
        target,
        initialStatus,
        body.workspace_snapshot_v3 || body.workspace_snapshot_v2 || null,
        {
          quote_id: body.quote_id || null,
          confirmation_expires_at: initialStatus === "needs_confirmation" ? "2099-01-01T00:15:00Z" : null,
        },
      );
      return fulfillJson(route, state.operation, 202);
    }

    const reviewMatch = path.match(/^\/api\/prompt\/reverse-operations\/(\d+)\/(revisions|feedback|apply)$/);
    if (reviewMatch?.[2] === "revisions" && method === "GET") {
      ensureResultRevisions();
      return fulfillJson(route, state.revisions);
    }
    if (reviewMatch?.[2] === "feedback" && method === "GET") {
      return fulfillJson(route, null);
    }
    if (reviewMatch?.[2] === "apply" && method === "POST") {
      ensureResultRevisions();
      const body = request.postDataJSON() as JsonRecord;
      state.applyBodies.push(body);
      const parentRevisionId = Number(body.parent_revision_id || state.revisions.at(-1)?.id || 6102);
      const nextVersion = state.revisions.length + 1;
      const userEdit = resultRevision(6100 + nextVersion, nextVersion, "user_edit", body.payload, parentRevisionId);
      const applied = resultRevision(6101 + nextVersion, nextVersion + 1, "applied", body.payload, userEdit.id);
      state.revisions.push(userEdit, applied);
      if (state.operation) state.operation = { ...state.operation, applied_result_version: applied.version };
      return fulfillJson(route, { user_edit: userEdit, applied });
    }

    const match = path.match(/^\/api\/prompt\/reverse-operations\/(\d+)(?:\/(ws-ticket|confirm-cover|cancel))?$/);
    if (match && match[2] === "ws-ticket") {
      state.wsTicketRequests += 1;
      return fulfillJson(route, { detail: "E2E 强制 WebSocket 断线后轮询" }, 503);
    }
    if (match && match[2] === "cancel") {
      state.cancelRequests += 1;
      state.operation = operationPayload(6001, state.operation?.target || "image", "canceled", state.operation?.workspace_snapshot_v3 || state.operation?.workspace_snapshot_v2 || null, {
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
      state.operation = operationPayload(6001, "video", "queued", state.operation?.workspace_snapshot_v3 || state.operation?.workspace_snapshot_v2 || null);
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
        state.operation = operationPayload(6001, state.operation.target, "succeeded", state.operation.workspace_snapshot_v3 || state.operation.workspace_snapshot_v2 || null, {
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
