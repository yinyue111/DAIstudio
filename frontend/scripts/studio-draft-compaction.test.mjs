import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  compactPendingReverseResultForStudioDraft,
  compactReverseOperationForStudioDraft,
  studioDraftByteSize,
} from "../app/studio/studioDraft.ts";

const studioPageSource = readFileSync(new URL("../app/page.jsx", import.meta.url), "utf8");

test("draft asset sanitizer preserves video duration", () => {
  const sanitizer = studioPageSource.match(
    /function sanitizeAssetForDraft\(asset\) \{([\s\S]*?)\n  \}\n\n  function sanitizeWorkspaceForDraft/,
  );
  assert.ok(sanitizer, "sanitizeAssetForDraft should remain available");
  assert.match(sanitizer[1], /"duration"/, "video duration must survive workspace draft compaction");
});

function evidence(index) {
  return {
    evidence_id: `evidence-${index}`,
    evidence_type: "ocr",
    bbox: { x: 0.1, y: 0.2, width: 0.2, height: 0.1 },
    field_key: "ocr_text",
    evidence_text: `可编辑文字 ${index}`,
    confidence: 0.92,
    source_index: 1,
    fact_status: "visible",
    protected: index === 0,
    editable: index !== 0,
    review_status: "confirmed",
    analyzer_source: "tesseract_tsv",
    analyzer_version: "5.5.2",
  };
}

test("draft compaction preserves reviewed evidence and removes reproducible diagnostics", () => {
  const pending = {
    kind: "pending_reverse_review",
    operation_id: 45,
    dirty: true,
    result: {
      final_text: "保留用户编辑后的提示词",
      structured: { 主体: "保温杯" },
      image_evidence: Array.from({ length: 13 }, (_, index) => evidence(index)),
      image_evidence_analyzers: {
        ocr: [{ languages: Array.from({ length: 500 }, (_, index) => `lang-${index}`) }],
      },
      source_fingerprints: Array.from({ length: 100 }, (_, index) => ({
        index,
        hash: "x".repeat(64),
      })),
      raw_response: "x".repeat(120_000),
    },
  };

  const compacted = compactPendingReverseResultForStudioDraft(pending);

  assert.equal(compacted.result.final_text, "保留用户编辑后的提示词");
  assert.equal(compacted.result.image_evidence.length, 13);
  assert.equal(compacted.result.image_evidence[0].protected, true);
  assert.equal(compacted.result.image_evidence_analyzers, undefined);
  assert.equal(compacted.result.source_fingerprints, undefined);
  assert.equal(compacted.result.raw_response, undefined);
  assert.ok(studioDraftByteSize(compacted) < 32 * 1024);
});

test("draft operation keeps recovery identity without duplicating result payload", () => {
  const compacted = compactReverseOperationForStudioDraft({
    id: 45,
    status: "succeeded",
    target: "image",
    result_schema_version: "reverse.v3",
    applied_result_version: 7,
    result: { final_text: "x".repeat(120_000) },
    request_context: { sources: Array.from({ length: 100 }, () => ({ url: "x".repeat(500) })) },
  });

  assert.deepEqual(compacted, {
    id: 45,
    status: "succeeded",
    target: "image",
    result_schema_version: "reverse.v3",
    applied_result_version: 7,
  });
});
