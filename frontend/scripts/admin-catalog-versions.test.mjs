import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  modelRouteDraft,
  modelRoutePayload,
  modelVersionDraft,
  modelVersionPayload,
  parseJsonObject,
  toolVersionDraft,
  toolVersionPayload,
} from "../app/admin/components/catalog-version-utils.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const component = readFileSync(join(root, "app/admin/components/catalog-versions.jsx"), "utf8");
const adminPage = readFileSync(join(root, "app/admin/page.jsx"), "utf8");
const apiTypes = readFileSync(join(root, "lib/api.d.ts"), "utf8");

function interfaceSource(name) {
  const match = apiTypes.match(new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `${name} interface is missing`);
  return match[1];
}

test("tool version JSON fields reject invalid and non-object input", () => {
  assert.deepEqual(parseJsonObject("", "输入"), {});
  assert.deepEqual(parseJsonObject('{"steps":[]}', "输入"), { steps: [] });
  assert.throws(() => parseJsonObject("[1]", "输入"), /必须是 JSON 对象/);
  assert.throws(() => parseJsonObject("{", "输入"), /不是合法 JSON/);
});

test("tool version drafts round-trip all executable configuration fields", () => {
  const draft = toolVersionDraft({
    schema_version: "tool.v2",
    input_schema: { type: "object" },
    workflow: { nodes: [{ id: "parse" }] },
    pricing_policy: { mode: "sum" },
    capabilities: { media: ["image"] },
    metadata_snapshot: { slug: "must-stay-read-only" },
  });
  assert.deepEqual(toolVersionPayload(draft), {
    schema_version: "tool.v2",
    input_schema: { type: "object" },
    workflow: { nodes: [{ id: "parse" }] },
    pricing_policy: { mode: "sum" },
    capabilities: { media: ["image"] },
  });
});

test("model capability and price drafts validate immutable version payloads", () => {
  const capability = modelVersionDraft("capability", {
    schema_version: "capability.v2",
    capabilities: { image_to_image: true, resolutions: ["2k"] },
    metadata_snapshot: { model_id: "must-stay-read-only" },
  });
  assert.deepEqual(modelVersionPayload(capability), {
    kind: "capability",
    schema_version: "capability.v2",
    capabilities: { image_to_image: true, resolutions: ["2k"] },
  });

  const price = modelVersionDraft("price", {
    schema_version: "credit-price.v2",
    base_cost_credits: 12,
    unlock_cost_credits: 3,
    pricing: { image: { "2k": 12 } },
  });
  assert.deepEqual(modelVersionPayload(price, { includeKind: false }), {
    schema_version: "credit-price.v2",
    base_cost_credits: 12,
    unlock_cost_credits: 3,
    pricing: { image: { "2k": 12 } },
  });
  assert.throws(
    () => modelVersionPayload({ ...price, base_cost_credits: -1 }),
    /调用积分/,
  );
});

test("catalog version responses strongly type read-only metadata snapshots", () => {
  const modelSnapshot = interfaceSource("ModelCatalogMetadataSnapshot");
  for (const field of [
    'schema_version: "model-catalog-metadata.v1"',
    "origin: CatalogMetadataOrigin",
    "model_id: string",
    "display_name: string",
    "is_default: boolean",
    "sort_order: number",
    "enabled: boolean",
  ]) assert.ok(modelSnapshot.includes(field), `model snapshot is missing ${field}`);
  assert.match(interfaceSource("ModelCapabilityVersion"), /metadata_snapshot: ModelCatalogMetadataSnapshot/);

  const toolSnapshot = interfaceSource("ToolCatalogMetadataSnapshot");
  for (const field of [
    'schema_version: "tool-catalog-metadata.v1"',
    "origin: CatalogMetadataOrigin",
    "slug: string",
    "name: string",
    "description: string | null",
    "category: ToolCatalogCategory",
    "renderer: string",
    "entry_path: string",
    "icon: string | null",
    "sort_order: number",
    "enabled: boolean",
    "featured: boolean",
  ]) assert.ok(toolSnapshot.includes(field), `tool snapshot is missing ${field}`);
  assert.match(interfaceSource("ToolCatalogVersion"), /metadata_snapshot: ToolCatalogMetadataSnapshot/);
});

test("model route drafts keep secrets write-only and validate circuit settings", () => {
  const draft = modelRouteDraft({
    route_key: "fallback-a",
    name: "备用 A",
    provider: "openai",
    base_url: "https://gateway.example.com/v1",
    gateway_format: "openai",
    api_key_configured: true,
    extra: { region: "cn" },
    priority: 20,
    enabled: true,
    failure_threshold: 3,
    window_seconds: 120,
    cooldown_seconds: 90,
  });
  assert.equal(draft.api_key, "");
  assert.deepEqual(modelRoutePayload(draft), {
    name: "备用 A",
    priority: 20,
    enabled: true,
    failure_threshold: 3,
    window_seconds: 120,
    cooldown_seconds: 90,
    managed_by_model_config: false,
    model_id: null,
    provider: "openai",
    base_url: "https://gateway.example.com/v1",
    gateway_format: "openai",
    extra: { region: "cn" },
  });
  assert.throws(() => modelRoutePayload({ ...draft, priority: -1 }), /优先级/);
  assert.throws(() => modelRoutePayload({ ...draft, extra: "[1]" }), /JSON 对象/);
});

test("managed legacy route patches never send custom gateway fields", () => {
  const payload = modelRoutePayload({
    ...modelRouteDraft({ route_key: "legacy-default", name: "默认路由" }),
    managed_by_model_config: true,
    api_key: "must-not-leak",
    extra: '{"authorization":"must-not-leak"}',
  });
  assert.deepEqual(payload, {
    name: "默认路由",
    priority: 100,
    enabled: true,
    failure_threshold: 5,
    window_seconds: 60,
    cooldown_seconds: 60,
    managed_by_model_config: true,
  });
});

test("admin catalog UI wires model and tool lifecycle APIs", () => {
  for (const token of [
    "adminModelVersions",
    "adminCreateModelVersion",
    "adminUpdateModelVersion",
    "adminPublishModelVersion",
    "adminDisableModelVersion",
    "adminRetireModelVersion",
    "adminRollbackModelVersion",
    "adminModelRoutes",
    "adminCreateModelRoute",
    "adminUpdateModelRoute",
    "adminModelRouteVersions",
    "adminRollbackModelRouteVersion",
    "adminRetireModelRouteVersion",
    "adminProbeModelRoute",
    "adminResetModelRouteHealth",
    "adminModelRouteHealthEvents",
    "adminTools",
    "adminCreateTool",
    "adminUpdateTool",
    "adminCreateToolVersion",
    "adminUpdateToolVersion",
    "adminPublishToolVersion",
    "adminDisableToolVersion",
    "adminRetireToolVersion",
    "adminRollbackToolVersion",
  ]) assert.match(component, new RegExp(token));
  assert.match(adminPage, /CatalogVersions/);
  assert.match(adminPage, /\["catalog", "能力 \/ 工具"\]/);
  assert.match(component, /草稿不会用于新生成任务/);
  assert.match(component, /历史任务继续引用原快照/);
  assert.match(component, /requestId !== loadRequestRef\.current/);
  assert.match(component, /value=\{selectedId\} disabled=\{Boolean\(busy\)\}/);
  assert.match(component, /保存路由会发布新版本/);
  assert.match(component, /现有 API Key 不会被历史版本覆盖/);
  assert.match(component, /新建版本草稿/);
  assert.match(component, /ModelMetadataSnapshotSummary snapshot=\{version\.metadata_snapshot\}/);
  assert.match(component, /ToolMetadataSnapshotSummary snapshot=\{version\.metadata_snapshot\}/);
  assert.match(component, /目录快照/);
  assert.match(component, /历史回填/);
  assert.doesNotMatch(component, /创建并激活/);
});
