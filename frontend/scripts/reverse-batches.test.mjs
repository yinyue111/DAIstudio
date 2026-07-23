import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import {
  MAX_REVERSE_BATCH_ITEMS,
  buildReverseBatchItemPayload,
  normalizeReverseBatch,
  normalizeReverseBatchList,
  parseReverseBatchKeyframes,
  parseReverseBatchRanges,
  reverseBatchOverrideCapability,
  reverseBatchOverrideCount,
  reverseBatchSucceededOperations,
} from "../lib/reverseBatches.ts";

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
      analysis_precision: "deep",
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
    analysis_precision: "deep",
    source_ranges: [{ start_seconds: 2, end_seconds: 7 }],
    custom_keyframes: [3.5],
    audio_policy: "exclude",
  });
});
