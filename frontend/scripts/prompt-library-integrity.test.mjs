import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const LIBRARY_DIR = path.join(ROOT, "public", "prompt-library");
const EXPECTED_CATEGORIES = ["ad-creative", "character", "comparison", "ecommerce", "portrait", "poster", "ui"];
const EXPECTED_SOURCES = [
  "EvoLinkAI/awesome-gpt-image-2-API-and-Prompts",
  "freestylefly/awesome-gpt-image-2",
  "YouMind-OpenLab/awesome-gpt-image-2",
];

async function readJson(name) {
  return JSON.parse(await readFile(path.join(LIBRARY_DIR, name), "utf8"));
}

test("prompt library data, attribution, and previews stay consistent", async () => {
  const [library, preview, license] = await Promise.all([
    readJson("prompt-data.json"),
    readJson("prompt-preview.json"),
    readJson("license.json"),
  ]);

  assert.deepEqual(library.categories.map((category) => category.id).sort(), EXPECTED_CATEGORIES);
  assert.equal(library.stats.categories, EXPECTED_CATEGORIES.length);
  assert.equal(library.stats.total, library.items.length);
  assert.equal(new Set(library.items.map((item) => item.id)).size, library.items.length, "item IDs must be unique");

  const sourceLicenses = new Map(license.sources.map((source) => [source.name, source.license]));
  assert.deepEqual([...sourceLicenses.keys()].sort(), [...EXPECTED_SOURCES].sort());
  for (const source of license.sources) {
    assert.match(source.url, /^https:\/\/github\.com\//);
    assert.match(source.licenseUrl, /^https:\/\//);
    assert.match(source.revision, /^[0-9a-f]{40}$/);
  }

  const categoryCounts = new Map(EXPECTED_CATEGORIES.map((category) => [category, 0]));
  let clearerPreviewCount = 0;
  for (const item of library.items) {
    assert.ok(categoryCounts.has(item.category), `unknown category for ${item.id}`);
    categoryCounts.set(item.category, categoryCounts.get(item.category) + 1);
    assert.equal(typeof item.prompt, "string");
    assert.ok(item.prompt.trim(), `empty prompt for ${item.id}`);
    assert.equal(item.promptLength, item.prompt.length, `promptLength mismatch for ${item.id}`);
    assert.equal(item.sourceLicense, sourceLicenses.get(item.sourceRepository), `source metadata mismatch for ${item.id}`);
    assert.equal(item.previewUrl, `/prompt-library/thumbs/${item.id}.jpg`);

    const previewPath = path.join(ROOT, "public", item.previewUrl);
    assert.ok(existsSync(previewPath), `missing preview for ${item.id}`);
    const metadata = await sharp(previewPath).metadata();
    const maxDimension = Math.max(metadata.width || 0, metadata.height || 0);
    assert.equal(metadata.format, "jpeg", `preview must be JPEG for ${item.id}`);
    assert.ok(maxDimension > 0 && maxDimension <= 720, `preview exceeds 720px for ${item.id}`);
    if (maxDimension > 520) clearerPreviewCount += 1;
  }
  assert.ok(clearerPreviewCount > 0, "sync must produce previews clearer than the previous 520px set");
  assert.ok(library.stats.sync?.existingPreviewsRefreshed > 0, "existing previews must be refreshed");

  for (const category of library.categories) {
    assert.equal(category.count, categoryCounts.get(category.id), `category count mismatch for ${category.id}`);
  }
  assert.equal(preview.stats.total, library.stats.total);
  assert.equal(preview.preview.sourceTotal, library.stats.total);
  assert.ok(preview.items.length <= preview.preview.limit);
  const fullIds = new Set(library.items.map((item) => item.id));
  for (const item of preview.items) {
    assert.ok(fullIds.has(item.id), `preview subset contains unknown item ${item.id}`);
    assert.equal(item.sourceLicense, sourceLicenses.get(item.sourceRepository));
  }
});
