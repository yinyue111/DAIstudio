import type { Page, Route } from "@playwright/test";
import { installReverseApiMock } from "./reverse-api";

const NOW = "2026-07-20T10:00:00Z";
const PROJECT_ID = 501;
const encoder = new TextEncoder();

export const EXPORTED_MEDIA_BYTES = Buffer.from("e2e-allowed-project-media-bytes");

type ZipEntry = {
  name: string;
  data: Buffer;
};

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

function crc32(bytes: Buffer) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
    }
  }
  return (value ^ 0xffffffff) >>> 0;
}

/** Creates a small method-0 ZIP so the E2E test can inspect the real download without another package. */
function storedZip(entries: ZipEntry[]) {
  const locals: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;

  for (const entry of entries) {
    const name = Buffer.from(encoder.encode(entry.name));
    const crc = crc32(entry.data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(entry.data.length, 18);
    local.writeUInt32LE(entry.data.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, name, entry.data);

    const directory = Buffer.alloc(46);
    directory.writeUInt32LE(0x02014b50, 0);
    directory.writeUInt16LE(20, 4);
    directory.writeUInt16LE(20, 6);
    directory.writeUInt16LE(0, 8);
    directory.writeUInt16LE(0, 10);
    directory.writeUInt16LE(0, 12);
    directory.writeUInt16LE(0, 14);
    directory.writeUInt32LE(crc, 16);
    directory.writeUInt32LE(entry.data.length, 20);
    directory.writeUInt32LE(entry.data.length, 24);
    directory.writeUInt16LE(name.length, 28);
    directory.writeUInt16LE(0, 30);
    directory.writeUInt16LE(0, 32);
    directory.writeUInt16LE(0, 34);
    directory.writeUInt16LE(0, 36);
    directory.writeUInt32LE(0, 38);
    directory.writeUInt32LE(offset, 42);
    central.push(directory, name);
    offset += local.length + name.length + entry.data.length;
  }

  const centralBody = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBody.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...locals, centralBody, end]);
}

export function readStoredZip(zip: Buffer) {
  const endOffset = zip.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (endOffset < 0) throw new Error("ZIP end-of-central-directory record is missing");
  const count = zip.readUInt16LE(endOffset + 10);
  let offset = zip.readUInt32LE(endOffset + 16);
  const entries = new Map<string, Buffer>();

  for (let index = 0; index < count; index += 1) {
    if (zip.readUInt32LE(offset) !== 0x02014b50) throw new Error("Invalid ZIP central-directory record");
    const method = zip.readUInt16LE(offset + 10);
    if (method !== 0) throw new Error(`Unsupported ZIP compression method ${method}`);
    const size = zip.readUInt32LE(offset + 24);
    const nameLength = zip.readUInt16LE(offset + 28);
    const extraLength = zip.readUInt16LE(offset + 30);
    const commentLength = zip.readUInt16LE(offset + 32);
    const localOffset = zip.readUInt32LE(offset + 42);
    const name = zip.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
    if (zip.readUInt32LE(localOffset) !== 0x04034b50) throw new Error(`Invalid ZIP local record for ${name}`);
    const localNameLength = zip.readUInt16LE(localOffset + 26);
    const localExtraLength = zip.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    entries.set(name, zip.subarray(dataStart, dataStart + size));
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

const project = {
  id: PROJECT_ID,
  title: "E2E 安全导出项目",
  description: null,
  project_type: "mixed",
  status: "active",
  cover_asset_ref: "g.7101",
  auto_archive_after_days: null,
  asset_count: 3,
  recipe_count: 0,
  task_count: 0,
  cost_frozen: 3,
  cost_settled: 5,
  assets: [
    {
      id: 1,
      asset_ref: "g.7101",
      role: "source",
      sort_order: 1,
      note: "可导出的授权素材",
      asset: { id: 7101, asset_ref: "g.7101", origin: "generated", type: "image", filename: "allowed.png", url: "/api/uploads/allowed.png", preview_url: "/api/uploads/allowed.png", available: true, created_at: NOW },
      metadata: null,
      duplicate_count: 0,
      similar_count: 0,
      created_at: NOW,
    },
    {
      id: 2,
      asset_ref: "g.7102",
      role: "reference",
      sort_order: 2,
      note: "锁定素材只能保留归集信息",
      asset: { id: 7102, asset_ref: "g.7102", origin: "generated", type: "image", filename: "locked.png", url: "/api/uploads/locked.png", preview_url: "/api/uploads/locked.png", available: true, created_at: NOW },
      metadata: null,
      duplicate_count: 0,
      similar_count: 0,
      created_at: NOW,
    },
    {
      id: 3,
      asset_ref: "g.7103",
      role: "reference",
      sort_order: 3,
      note: "过期素材只能保留归集信息",
      asset: { id: 7103, asset_ref: "g.7103", origin: "generated", type: "image", filename: "expired.png", url: "/api/uploads/expired.png", preview_url: "/api/uploads/expired.png", available: false, created_at: NOW },
      metadata: null,
      duplicate_count: 0,
      similar_count: 0,
      created_at: NOW,
    },
  ],
  recipes: [],
  tasks: [],
  draft_key: `project-${PROJECT_ID}`,
  draft: { content: "E2E 项目导出草稿" },
  draft_updated_at: NOW,
  created_at: NOW,
  updated_at: NOW,
};

const zip = storedZip([
  { name: "project.json", data: Buffer.from(JSON.stringify({ schema_version: "media-project-export.v1", project: { id: PROJECT_ID, title: project.title } })) },
  { name: "tasks/tasks.json", data: Buffer.from("[]") },
  { name: "drafts/project-draft.json", data: Buffer.from(JSON.stringify({ key: project.draft_key, payload: project.draft })) },
  { name: "recipes/index.json", data: Buffer.from("[]") },
  {
    name: "assets/manifest.json",
    data: Buffer.from(JSON.stringify([
      { asset_ref: "g.7101", media_included: true, media_path: "media/001-allowed.png", media_exclusion_reason: null },
      { asset_ref: "g.7102", media_included: false, media_path: null, media_exclusion_reason: "原始媒体未授权" },
      { asset_ref: "g.7103", media_included: false, media_path: null, media_exclusion_reason: "素材不存在或已过期" },
    ])),
  },
  { name: "media/001-allowed.png", data: EXPORTED_MEDIA_BYTES },
]);

export async function installProjectExportApiMock(page: Page) {
  await installReverseApiMock(page);
  const state = { projectId: PROJECT_ID, exportRequests: [] as Array<{ method: string; includeMedia: boolean }> };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/projects" && method === "GET") return fulfillJson(route, [project]);
    if (path === `/api/projects/${PROJECT_ID}` && method === "GET") return fulfillJson(route, project);
    if (path === "/api/asset-folders" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/me/assets" && method === "GET") {
      return fulfillJson(route, { items: project.assets.map((link) => link.asset), total: project.assets.length, next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/export` && method === "GET") {
      const includeMedia = url.searchParams.get("include_media") === "true";
      state.exportRequests.push({ method, includeMedia });
      return route.fulfill({
        status: 200,
        contentType: "application/zip",
        headers: { "content-disposition": "attachment; filename=project-501-safe-export.zip" },
        body: zip,
      });
    }
    return route.fallback();
  });

  return state;
}
