import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const catalog = readFileSync(new URL("../app/catalog/page.jsx", import.meta.url), "utf8");
const studio = readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");

test("model catalog compares up to three models and deep-links exact selections", () => {
  assert.match(catalog, /function ModelComparison/);
  assert.match(catalog, /current\.slice\(-2\)/);
  assert.match(catalog, /catalogModelStudioHref/);
  assert.match(catalog, /price_version/);
  assert.match(catalog, /route_availability/);
  assert.match(catalog, /aspect_ratios/);
  assert.match(catalog, /resolutions/);
  assert.match(catalog, /durations/);
  assert.match(catalog, /max_reference_images/);
  assert.match(catalog, /CapabilityState/);
  assert.match(catalog, />不支持</);
  assert.match(catalog, />未知</);
  assert.match(catalog, /在创作中使用/);
});

test("studio resolves server tool versions before mutating workflow state", () => {
  assert.match(studio, /api\.toolCatalogDetail\(slug\)/);
  assert.match(studio, /resolveStudioWorkflowPreset\(window\.location\.search, tool\)/);
  assert.match(studio, /resolution\.status !== "ready"/);
  assert.match(studio, /当前草稿未改变/);
});

test("studio validates catalog model capability and compatible mode before exact selection", () => {
  assert.match(studio, /catalogModelBootstrapRef/);
  assert.match(studio, /MODEL_SELECTION_USES\.includes\(modelUse\)/);
  assert.match(studio, /allModelOptions\[modelUse\]\?\.find/);
  assert.match(studio, /resolveCatalogModelIntent\(option, requestedMode\)/);
  assert.match(studio, /resolution\.creationMode && creationMode !== resolution\.creationMode/);
  assert.match(studio, /modelOptionsForSelection\[modelUse\]\?\.find/);
  assert.match(studio, /changeModelSelection\(modelUse, modelConfigId\)/);
  assert.ok(!studio.includes("setModelSelections({ [modelUse]: modelConfigId })"));
});
