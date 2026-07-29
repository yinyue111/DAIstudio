import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

test("batch panel precision dropdown renders server tiers instead of the removed deep value", () => {
  const panelSource = fs.readFileSync(
    new URL("../app/studio/StudioReverseBatchPanel.jsx", import.meta.url),
    "utf8",
  );
  // 后端 Literal 没有 deep；下拉必须由 /api/config 登记的档位渲染。
  assert.doesNotMatch(panelSource, /"deep"/);
  assert.match(panelSource, /reversePrecisionOptions\(\)/);
});
import {
  MAX_REVERSE_BATCH_ITEMS,
  buildReverseBatchItemPayload,
  normalizeReverseBatch,
  normalizeReverseBatchItemOverride,
  normalizeReverseBatchList,
  parseReverseBatchKeyframes,
  parseReverseBatchRanges,
  reverseBatchConfirmationState,
  reverseBatchOverrideCapability,
  reverseBatchOverrideCount,
  reverseBatchSucceededOperations,
} from "../lib/reverseBatches.ts";
import {
  MAX_REVERSE_CUSTOM_KEYFRAMES,
  REVERSE_PRECISION_OPTIONS,
  registerReversePrecisionOptions,
  reversePrecisionOptions,
  validateReverseConfig,
} from "../app/studio/reverseConfig.ts";

function operation(id, status, result = null) {
  return {
    id,
    target: "image",
    source_type: "image",
    status,
    progress: status === "succeeded" ? 100 : 20,
    result,
  };
}

test("normalizes batch items, derives partial state, and exposes successful results", () => {
  const batch = normalizeReverseBatch({
    id: 7,
    target: "image",
    items: [
      { index: 1, operation: operation(12, "failed") },
      { index: 0, asset_url: "https://example.com/a.jpg", operation: operation(11, "succeeded", { final_text: "A" }) },
    ],
  });
  assert.equal(batch.status, "partial");
  assert.deepEqual(batch.items.map((item) => item.operation_id), [11, 12]);
  assert.equal(batch.counts.succeeded, 1);
  assert.deepEqual(reverseBatchSucceededOperations(batch).map((item) => item.id), [11]);
});

test("accepts list envelopes and preserves server counters", () => {
  const rows = normalizeReverseBatchList({
    items: [{ id: 8, status: "running", counts: { total: 3, running: 2 }, items: [] }],
  });
  assert.equal(rows[0].active, true);
  assert.equal(rows[0].counts.total, 3);
  assert.equal(rows[0].counts.running, 2);
  assert.equal(MAX_REVERSE_BATCH_ITEMS, 20);
});

test("normalizes the live batch response contract", () => {
  const batch = normalizeReverseBatch({
    id: 9,
    status: "running",
    total_count: 3,
    status_counts: { queued: 1, running: 2, succeeded: 0, failed: 0, canceled: 0, needs_confirmation: 0 },
    shared_config_snapshot: { analysis_precision: "standard" },
    capabilities: {
      schema_version: "reverse-batch-capabilities.v2",
      item_overrides: true,
      supported_override_keys: ["target", "analysis_precision", "source_ranges", "custom_keyframes", "audio_policy"],
      audio_policies: ["inherit", "exclude", "analyze"],
    },
    items: [],
  });
  assert.equal(batch.counts.total, 3);
  assert.equal(batch.shared_config.analysis_precision, "standard");
  assert.equal(batch.item_override_capability.supported, true);
  assert.deepEqual(batch.capabilities, {
    schema_version: "reverse-batch-capabilities.v2",
    item_overrides: true,
    supported_override_keys: ["target", "analysis_precision", "source_ranges", "custom_keyframes", "audio_policy"],
    audio_policies: ["inherit", "exclude", "analyze"],
  });
  assert.deepEqual(batch.item_override_capability.fields, [
    "target",
    "analysis_precision",
    "source_ranges",
    "custom_keyframes",
    "include_audio",
  ]);
});

test("batch UI contract includes compare, retry, cancel, and recipe actions", () => {
  const source = fs.readFileSync(new URL("../app/studio/StudioReverseBatchPanel.jsx", import.meta.url), "utf8");
  assert.match(source, /结果对比/);
  assert.match(source, /批量保存配方/);
  assert.match(source, /取消批次/);
  assert.match(source, /onRetryItem/);
  assert.match(source, /逐项设置/);
  assert.match(source, /应用首项到全部/);
  assert.match(source, /重置全部覆盖/);
});

test("batch item overrides stay capability-gated and normalize payloads", () => {
  const unsupported = reverseBatchOverrideCapability({});
  assert.equal(unsupported.supported, false);
  const supported = reverseBatchOverrideCapability({ capabilities: { item_overrides: { supported: true, fields: ["source_ranges", "custom_keyframes", "include_audio"] } } });
  assert.equal(supported.supported, true);
  assert.deepEqual(parseReverseBatchRanges("8-12, 0-5"), [
    { start_seconds: 0, end_seconds: 5 },
    { start_seconds: 8, end_seconds: 12 },
  ]);
  assert.deepEqual(parseReverseBatchKeyframes("4, 1.5, 4, x"), [1.5, 4]);
  const payload = buildReverseBatchItemPayload(
    { asset_url: "https://example.com/v.mp4", source_type: "video" },
    { target: "video", source_ranges: [{ start_seconds: 0, end_seconds: 5 }], custom_keyframes: [1.5], include_audio: true },
    supported,
  );
  assert.deepEqual({
    source_ranges: payload.source_ranges,
    custom_keyframes: payload.custom_keyframes,
    audio_policy: payload.audio_policy,
  }, {
    source_ranges: [{ start_seconds: 0, end_seconds: 5 }],
    custom_keyframes: [1.5],
    audio_policy: "analyze",
  });
  assert.equal(reverseBatchOverrideCount({ source_ranges: payload.source_ranges, custom_keyframes: payload.custom_keyframes, include_audio: true }), 3);
  const stripped = buildReverseBatchItemPayload(payload, payload, unsupported);
  assert.equal("source_ranges" in stripped, false);
  assert.equal("audio_policy" in stripped, false);
  const advertised = reverseBatchOverrideCapability({ capabilities: { item_overrides: { supported: true, supported_keys: ["target", "audio_policy"], audio_policies: ["inherit", "analyze"] } } });
  assert.deepEqual(advertised.fields, ["target", "include_audio"]);
  assert.deepEqual(advertised.audio_policies, ["inherit", "analyze"]);
  const liveContract = reverseBatchOverrideCapability({ capabilities: {
    schema_version: "reverse-batch-capabilities.v2",
    item_overrides: true,
    supported_override_keys: ["target", "analysis_precision", "source_ranges", "custom_keyframes", "audio_policy"],
    audio_policies: ["inherit", "exclude", "analyze"],
  } });
  assert.equal(liveContract.supported, true);
  assert.deepEqual(liveContract.fields, ["target", "analysis_precision", "source_ranges", "custom_keyframes", "include_audio"]);
  assert.deepEqual(liveContract.audio_policies, ["inherit", "exclude", "analyze"]);
  const livePayload = buildReverseBatchItemPayload(
    { asset_url: "https://example.com/v2.mp4", source_type: "video", overrides: { target: "image" } },
    {
      target: "video",
      analysis_precision: "fine",
      source_ranges: [{ start_seconds: 2, end_seconds: 7 }],
      custom_keyframes: [3.5],
      include_audio: false,
    },
    liveContract,
  );
  assert.deepEqual(livePayload, {
    asset_url: "https://example.com/v2.mp4",
    source_type: "video",
    target: "video",
    analysis_precision: "fine",
    source_ranges: [{ start_seconds: 2, end_seconds: 7 }],
    custom_keyframes: [3.5],
    audio_policy: "exclude",
  });
});

test("batch total cost sums per-item operation credits when no batch-level field exists", () => {
  // serialize_batch 不返回批级 cost 字段；批级总费用从子项 operation 求和。
  const batch = normalizeReverseBatch({
    id: 21,
    target: "video",
    items: [
      { index: 0, operation: { ...operation(31, "succeeded", { final_text: "A" }), cost_frozen: 0, cost_settled: 5 } },
      { index: 1, operation: { ...operation(32, "running"), cost_frozen: 7, cost_settled: 0 } },
      { index: 2, operation: { ...operation(33, "failed"), cost_frozen: 0, cost_settled: 0 } },
    ],
  });
  assert.equal(batch.cost_settled, 5);
  assert.equal(batch.cost_frozen, 7);
  // 服务端一旦下发批级字段则以服务端为准。
  const authoritative = normalizeReverseBatch({
    id: 22,
    cost_settled: 12,
    cost_frozen: 3,
    items: [
      { index: 0, operation: { ...operation(41, "succeeded", { final_text: "A" }), cost_settled: 5 } },
    ],
  });
  assert.equal(authoritative.cost_settled, 12);
  assert.equal(authoritative.cost_frozen, 3);
});

test("needs_confirmation surfaces as its own batch state with the earliest deadline", () => {
  const expiresSoon = new Date(Date.now() + 5 * 60 * 1000).toISOString();
  const expiresLater = new Date(Date.now() + 10 * 60 * 1000).toISOString();
  const batch = normalizeReverseBatch({
    id: 23,
    target: "video",
    items: [
      { index: 0, operation: { ...operation(51, "needs_confirmation"), confirmation_expires_at: expiresLater } },
      { index: 1, operation: { ...operation(52, "needs_confirmation"), confirmation_expires_at: expiresSoon } },
      { index: 2, operation: operation(53, "succeeded", { final_text: "A" }) },
    ],
  });
  // 不再折叠成 queued（"等待中"），否则用户错过 15 分钟确认 TTL。
  assert.equal(batch.status, "needs_confirmation");
  assert.equal(batch.active, true);
  assert.equal(batch.counts.needs_confirmation, 2);
  const confirmation = reverseBatchConfirmationState(batch);
  assert.equal(confirmation.count, 2);
  assert.equal(confirmation.expires_at, expiresSoon);
  assert.deepEqual(confirmation.operations.map((item) => item.id), [51, 52]);
  const idle = reverseBatchConfirmationState(normalizeReverseBatch({ id: 24, items: [{ index: 0, operation: operation(61, "queued") }] }));
  assert.equal(idle.count, 0);
  assert.equal(idle.expires_at, null);
});

test("batch panel renders an actionable confirmation banner with countdown", () => {
  const source = fs.readFileSync(new URL("../app/studio/StudioReverseBatchPanel.jsx", import.meta.url), "utf8");
  assert.match(source, /需要你确认/);
  assert.match(source, /去确认/);
  assert.match(source, /reverseBatchConfirmationState/);
  assert.match(source, /useConfirmationCountdown/);
  assert.match(source, /全额退回积分/);
  // 待确认不得再显示为"待确认/等待中"这类不驱动操作的文案。
  assert.doesNotMatch(source, /needs_confirmation: "待确认"/);
});

test("custom keyframe cap follows the backend schema (36) and rejects overflow loudly", () => {
  // 后端 schemas/reverse.py: custom_keyframes Field(max_length=36)。
  assert.equal(MAX_REVERSE_CUSTOM_KEYFRAMES, 36);
  const backendSchema = fs.readFileSync(
    new URL("../../backend/app/schemas/reverse.py", import.meta.url),
    "utf8",
  );
  assert.match(backendSchema, /custom_keyframes: list\[float\] = Field\(default_factory=list, max_length=36\)/);
  // 30 个关键帧（旧前端上限 24 会静默截断）现在原样保留。
  const thirty = Array.from({ length: 30 }, (_, index) => index);
  const ok = validateReverseConfig(
    { custom_keyframes: thirty },
    { category: "video", selectedType: "video", duration: 120 },
  );
  assert.equal(ok.valid, true);
  assert.deepEqual(ok.value.custom_keyframes, thirty);
  // 超过 36 个必须报错提示，而不是静默改数。
  const overflow = validateReverseConfig(
    { custom_keyframes: Array.from({ length: 37 }, (_, index) => index) },
    { category: "video", selectedType: "video", duration: 120 },
  );
  assert.equal(overflow.valid, false);
  assert.ok(overflow.errors.some(
    (item) => item.field === "custom_keyframes" && item.message.includes("36"),
  ));
});

test("analysis precision overrides follow the backend enum (fast/standard/fine/ultra)", () => {
  // 后端 Literal 是 fast/standard/fine/ultra；历史上前端误发 "deep" 导致整批 422。
  for (const precision of ["fast", "standard", "fine", "ultra"]) {
    assert.deepEqual(
      normalizeReverseBatchItemOverride({ analysis_precision: precision }),
      { analysis_precision: precision },
    );
  }
  assert.deepEqual(normalizeReverseBatchItemOverride({ analysis_precision: "deep" }), {});
});

test("precision whitelist tracks tiers registered from /api/config", () => {
  registerReversePrecisionOptions([
    { key: "fast", label: "快速" },
    { key: "standard", label: "标准" },
    { key: "fine", label: "精细" },
    { key: "ultra", label: "超精细" },
    { key: "extreme", label: "极限" },
  ]);
  assert.deepEqual(
    normalizeReverseBatchItemOverride({ analysis_precision: "extreme" }),
    { analysis_precision: "extreme" },
  );
  assert.deepEqual(
    reversePrecisionOptions().map((option) => option.key),
    ["fast", "standard", "fine", "ultra", "extreme"],
  );
  // 恢复为与后端 Literal 一致的档位，避免影响其它用例。
  registerReversePrecisionOptions(
    REVERSE_PRECISION_OPTIONS.map((option) => ({ ...option })),
  );
  assert.deepEqual(normalizeReverseBatchItemOverride({ analysis_precision: "extreme" }), {});
});
