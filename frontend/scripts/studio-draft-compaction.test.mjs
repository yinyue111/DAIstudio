import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  compactPendingReverseResultForStudioDraft,
  compactReverseOperationForStudioDraft,
  studioDraftByteSize,
} from "../app/studio/studioDraft.ts";
import { compactStudioSessionDraftForCloud } from "../app/studio/studioDraftSession.js";

const studioDraftSessionSource = readFileSync(new URL("../app/studio/studioDraftSession.js", import.meta.url), "utf8");

test("draft asset sanitizer preserves video duration", () => {
  const sanitizer = studioDraftSessionSource.match(
    /function sanitizeAssetForDraft\(asset\) \{([\s\S]*?)\n\}\n\nexport function sanitizeWorkspaceForDraft/,
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

test("cloud draft drops inactive reports before the 96KB API limit", () => {
  const oversizedResult = {
    kind: "pending_reverse_review",
    operation_id: 82,
    dirty: false,
    result: {
      final_text: "可恢复提示词",
      structured: { 主体: "洗脸巾" },
      image_evidence: Array.from({ length: 128 }, (_, index) => ({
        ...evidence(index),
        evidence_text: "证据".repeat(200),
      })),
    },
  };
  const draft = {
    version: 1,
    creationMode: "video",
    workspaces: {
      image: {
        pendingReverseResult: oversizedResult,
        reverseOperation: { id: 82, status: "succeeded" },
        reverseUndoSnapshot: { prompt: "x".repeat(40_000) },
      },
      video: {
        prompt: "视频提示词",
        pendingReverseResult: null,
        reverseOperation: null,
      },
    },
  };

  assert.ok(studioDraftByteSize(draft) > 96 * 1024);
  const compacted = compactStudioSessionDraftForCloud(draft);
  assert.ok(studioDraftByteSize(compacted) < 96 * 1024);
  assert.equal(compacted.workspaces.image.pendingReverseResult, null);
  assert.deepEqual(compacted.workspaces.image.reverseOperation, { id: 82, status: "succeeded" });
  assert.equal(compacted.workspaces.video.prompt, "视频提示词");
});
