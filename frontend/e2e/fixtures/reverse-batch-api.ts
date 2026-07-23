import type { Page, Route } from "@playwright/test";
import { installReverseApiMock } from "./reverse-api";

type JsonRecord = Record<string, any>;

const NOW = "2026-07-20T10:00:00Z";

const ASSETS = [
  {
    id: 8101,
    asset_ref: "u.8101",
    origin: "uploaded",
    type: "image",
    filename: "batch-a.png",
    url: "/api/uploads/batch-a.png",
    preview_url: "/api/uploads/batch-a.png",
    available: true,
    created_at: NOW,
  },
  {
    id: 8102,
    asset_ref: "u.8102",
    origin: "uploaded",
    type: "image",
    filename: "batch-b.png",
    url: "/api/uploads/batch-b.png",
    preview_url: "/api/uploads/batch-b.png",
    available: true,
    created_at: NOW,
  },
];

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function operation(id: number, index: number, status: "running" | "succeeded") {
  const completed = status === "succeeded";
  return {
    id,
    target: "image",
    source_type: "image",
    status,
    progress: completed ? 100 : 35,
    phase: completed ? "" : "E2E 批量分析",
    analysis_focus: "comprehensive",
    analysis_precision: "standard",
    output_purpose: "generation",
    result_schema_version: "reverse.v3",
    result: completed
      ? {
          structured: { "主体": `批量素材 ${index + 1}` },
          final_text: `E2E 批量生成稿 ${index + 1}`,
          reference_count: 1,
        }
      : null,
    cost_frozen: completed ? 0 : 2,
    cost_settled: completed ? 2 : 0,
    created_at: NOW,
    updated_at: NOW,
    finished_at: completed ? NOW : null,
  };
}

function batchPayload(status: "running" | "succeeded") {
  const completed = status === "succeeded";
  return {
    id: 7701,
    client_request_id: "reverse-batch-e2e",
    name: "图片批量反推 · 2 项",
    target: "image",
    status,
    total_count: 2,
    status_counts: completed
      ? { queued: 0, running: 0, needs_confirmation: 0, succeeded: 2, failed: 0, canceled: 0 }
      : { queued: 0, running: 2, needs_confirmation: 0, succeeded: 0, failed: 0, canceled: 0 },
    shared_config_snapshot: {
      analysis_focus: "comprehensive",
      analysis_precision: "standard",
      output_purpose: "generation",
    },
    cost_frozen: completed ? 0 : 4,
    cost_settled: completed ? 4 : 0,
    items: ASSETS.map((asset, index) => ({
      id: 7801 + index,
      index,
      asset_url: asset.url,
      source_type: "image",
      source_snapshot: {
        asset_ref: asset.asset_ref,
        asset_url: asset.url,
        source_type: "image",
      },
      operation_id: 7601 + index,
      operation: operation(7601 + index, index, status),
    })),
    created_at: NOW,
    updated_at: NOW,
    finished_at: completed ? NOW : null,
  };
}

export async function installReverseBatchApiMock(page: Page) {
  const base = await installReverseApiMock(page);
  const state = {
    ...base,
    batchQuoteBodies: [] as JsonRecord[],
    batchCreateBodies: [] as JsonRecord[],
    batchDetailReads: 0,
    recipeBodies: [] as JsonRecord[],
    batch: null as JsonRecord | null,
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/asset-folders" && method === "GET") {
      return fulfillJson(route, []);
    }
    if (path === "/api/me/assets" && method === "GET") {
      return fulfillJson(route, { items: ASSETS, total: ASSETS.length, next_cursor: null });
    }
    if (path === "/api/quotes" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      if (body.kind !== "reverse_batch") return route.fallback();
      state.batchQuoteBodies.push(body);
      return fulfillJson(route, {
        id: 5701,
        quote_id: 5701,
        kind: "reverse_batch",
        status: "active",
        total_credits: 4,
        breakdown: ASSETS.map((asset, index) => ({
          key: `reverse_batch_item_${index + 1}`,
          label: asset.filename,
          credits: 2,
        })),
        balance: { before: 200, after: 196, sufficient: true },
        affordable: true,
        expires_at: "2099-01-01T00:15:00Z",
      }, 201);
    }
    if (path === "/api/prompt/reverse-batches" && method === "GET") {
      return fulfillJson(route, state.batch ? [state.batch] : []);
    }
    if (path === "/api/prompt/reverse-batches" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      state.batchCreateBodies.push(body);
      state.batch = batchPayload("running");
      return fulfillJson(route, state.batch, 202);
    }
    if (path === "/api/prompt/reverse-batches/7701" && method === "GET") {
      state.batchDetailReads += 1;
      state.batch = batchPayload("succeeded");
      return fulfillJson(route, state.batch);
    }
    if (path === "/api/recipes" && method === "POST") {
      const body = request.postDataJSON() as JsonRecord;
      state.recipeBodies.push(body);
      return fulfillJson(route, {
        id: 8800 + state.recipeBodies.length,
        ...body,
        current_version: 1,
        moderation_status: "draft",
        created_at: NOW,
        updated_at: NOW,
      }, 201);
    }
    return route.fallback();
  });

  return state;
}
