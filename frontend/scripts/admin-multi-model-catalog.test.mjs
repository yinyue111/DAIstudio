import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/admin/components/model-review-moderation.jsx"), "utf8");
const importerSource = readFileSync(join(root, "app/admin/components/model-catalog-importer.jsx"), "utf8");

test("admin model catalog creates and updates model rows through the multi-model API", () => {
  assert.match(source, /api\.adminCreateModel\(payload\)/);
  assert.match(source, /api\.adminUpdateModel\(r\.id, payload\)/);
  assert.match(source, /api\.adminUpdateModel\(row\.id, patch\)/);
  assert.match(source, /已新增，可在创作页对应模型下拉框选择/);
  assert.match(source, /已\$\{targetState\}，可在创作页对应模型下拉框选择/);
  assert.match(source, /row\.id == null \? row\.draftKey : String\(row\.id\)/);
  assert.doesNotMatch(source, /key=\{r\.use\}/);
});

test("admin model catalog exposes all model selection metadata and lifecycle actions", () => {
  for (const label of [
    "新增模型",
    "前端显示名称",
    "用途",
    "调用消耗积分",
    "排序",
    "设为默认",
    "停用",
    "启用",
  ]) {
    assert.match(source, new RegExp(label));
  }
  for (const use of ["vision", "image", "video", "prompt"]) {
    assert.match(source, new RegExp(`<option value="${use}">`));
  }
  assert.match(source, /\{ is_default: true, enabled: true \}/);
  assert.match(source, /默认模型不能直接停用/);
  assert.match(source, /disabled=\{!!r\.is_default\}/);
});

test("admin model catalog never hydrates stored API keys into an input", () => {
  const normalizeSource = source.match(/function normalizeRow\(row\) \{[\s\S]*?\n  \}/)?.[0] || "";
  assert.match(source, /api_key:\s*""/);
  assert.match(source, /api_key_encrypted:\s*_encryptedKey/);
  assert.match(source, /type="password"/);
  assert.match(source, /页面永不回显/);
  assert.doesNotMatch(normalizeSource, /api_key:\s*row\.api_key/);
  assert.doesNotMatch(source, /value=\{r\.api_key_configured/);
});

test("existing model purpose can be edited and saved through PATCH", () => {
  assert.doesNotMatch(source, /disabled=\{r\.id != null\}/);
  assert.match(source, /修改后会从原用途移到新用途/);
  assert.match(source, /api\.adminUpdateModel\(r\.id, payload\)/);
  assert.doesNotMatch(source, /_immutableUse/);
});

test("configured models can be deleted while preserving historical tasks", () => {
  assert.match(source, /api\.adminDeleteModel\(row\.id\)/);
  assert.match(source, /历史任务记录会保留/);
  assert.match(source, /<Trash2/);
  assert.match(source, /aria-label=\{`删除模型/);
});

test("admin model probes and busy states are isolated by model config id", () => {
  assert.match(source, /probeSeqRef\.current\.set\(key, signature\)/);
  assert.match(source, /setProbing\(\(prev\) => \(\{ \.\.\.prev, \[key\]: signature \}\)\)/);
  assert.match(source, /findIndex\(\(item\) => rowKey\(item\) === key\)/);
  assert.doesNotMatch(source, /probing\[r\.use\]/);
  assert.match(source, /model_config_id:\s*r\.id/);
  assert.match(source, /provider_config_id:\s*r\.provider_config_id/);
});

test("switching to environment fallback clears stored gateway overrides", () => {
  assert.match(source, /row\.provider === "env" \? ""/);
  assert.match(source, /base_url: !provider \? ""/);
  assert.match(source, /api_key_clear: !provider && current\.api_key_configured \? true/);
});

test("provider onboarding probes once and atomically imports selected capability models", () => {
  for (const label of [
    "提供商接入向导",
    "探测模型与能力",
    "批量导入",
  ]) {
    assert.match(importerSource, new RegExp(label));
  }
  assert.match(importerSource, /const USE_ORDER = \["vision", "image", "video", "prompt"\]/);
  assert.match(importerSource, /model\.default_extra_by_use\?\.\[use\]/);
  assert.match(importerSource, /\{modelUseLabel\(use\)\} \{counts\[use\]\}/);
  assert.match(importerSource, /api\.adminProbeModels\(connection\)/);
  assert.match(importerSource, /api\.adminImportModels\(\{ \.\.\.connection, models: selectedModels \}\)/);
  assert.match(importerSource, /type="password"/);
  assert.doesNotMatch(importerSource, /localStorage/);
});

test("new models reuse a saved provider connection before probing and selection", () => {
  assert.match(source, /setProviderConnections\(payload\.provider_connections \|\| \[\]\)/);
  assert.match(source, /provider_config_id: r\.provider_config_id \|\| null/);
  assert.match(source, /已有供应商/);
  assert.match(source, /无需再次填写 Base URL 和 API Key/);
  assert.match(source, /provider_config_id: r\.provider_config_id, use: r\.use/);
  assert.match(source, /api\.adminProbeModels\(probePayload\)/);
  assert.match(source, /chooseProbedModel\(i, event\.target\.value\)/);
  assert.match(source, /r\.id == null && \(!r\.provider_config_id \|\| !r\.model_id\)/);
  assert.match(
    source,
    /row\.id == null[\s\S]{0,180}\{ provider_config_id: Number\(row\.provider_config_id\) \|\| null \}[\s\S]{0,260}: \{[\s\S]{0,180}base_url: row\.base_url/,
  );
});

test("model directory supports dense search, use filters, and collapsed editing", () => {
  assert.match(source, /placeholder="搜索模型名称或 ID"/);
  assert.match(source, /useFilter === "all" \|\| row\.use === useFilter/);
  assert.match(source, /aria-expanded=\{isExpanded\}/);
  assert.match(source, /isExpanded \? "收起" : "编辑"/);
});
