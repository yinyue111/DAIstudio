import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  imageEvidenceConflictGroups,
  imageEvidenceMaskReadiness,
  imageEvidenceSources,
  normalizeImageEvidence,
} from "../app/studio/imageEvidence.ts";
import {
  addManualImageEvidenceItem,
  bboxFromNormalizedDrag,
  buildImageEvidenceMaskPlan,
  mergeImageEvidenceItems,
  removeImageEvidenceItem,
  replaceReverseResultImageEvidence,
  reviewImageEvidenceItem,
  setImageEvidenceRegionMode,
  splitImageEvidenceItem,
  updateImageEvidenceItem,
  validateImageEvidenceMaskPreflight,
} from "../app/studio/imageEvidenceReview.ts";
import { compareReverseResultRevisions } from "../app/studio/reverseResultRevisionDiff.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const evidenceViewSource = readFileSync(join(root, "app/studio/StudioReverseResultViews.jsx"), "utf8");
const resultPanelSource = readFileSync(join(root, "app/studio/StudioReverseResultPanel.jsx"), "utf8");
const evidenceComponentSource = readFileSync(join(root, "app/studio/StudioImageEvidence.jsx"), "utf8");
const generationSubmitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");
const studioPageSource = readFileSync(join(root, "app/page.jsx"), "utf8");

function evidence(overrides = {}) {
  return {
    evidence_type: "ocr",
    bbox: { x: 0.1, y: 0.2, width: 0.3, height: 0.1 },
    field_key: "文字版式",
    evidence_text: "ACME",
    confidence: 0.97,
    source_index: 1,
    fact_status: "visible",
    protected: true,
    editable: false,
    ...overrides,
  };
}

const rows = normalizeImageEvidence([
  evidence(),
  evidence({
    evidence_type: "visual_field",
    bbox: null,
    field_key: "材质",
    evidence_text: "可能是玻璃",
    confidence: 0.4,
    source_index: 2,
    fact_status: "inferred",
    protected: false,
  }),
]);
assert.equal(rows.length, 2);
assert.equal(rows[0].evidence_id, "evidence-1-1");
assert.equal(rows[0].review_status, "pending");
assert.equal(rows[0].bbox?.width, 0.3);
assert.equal(rows[1].evidence_id, "evidence-2-2");
assert.equal(rows[1].bbox, null);
assert.deepEqual(normalizeImageEvidence([evidence()]), normalizeImageEvidence([evidence()]), "legacy IDs must be deterministic");

const duplicateIds = normalizeImageEvidence([
  evidence({ evidence_id: "same-id" }),
  evidence({ evidence_id: "same-id", field_key: "副标题", evidence_text: "SECOND" }),
]);
assert.deepEqual(duplicateIds.map((item) => item.evidence_id), ["same-id", "same-id-2"]);

const conflicts = imageEvidenceConflictGroups([
  evidence({ evidence_id: "vlm", analyzer: "vlm", evidence_text: "ACME" }),
  evidence({ evidence_id: "ocr", analyzer: "ocr", evidence_text: "ACMF" }),
]);
assert.equal(conflicts.length, 1);
assert.deepEqual(conflicts[0].evidence_ids, ["vlm", "ocr"]);
const backendConflicts = normalizeImageEvidence([
  evidence({
    evidence_id: "backend-vlm",
    analyzer_source: "vision_language_model",
    analyzer_status: "analyzed",
    analyzer_version: "provider_snapshot",
    conflict_status: "conflict",
    conflicts_with: ["backend-ocr"],
  }),
  evidence({
    evidence_id: "backend-ocr",
    evidence_text: "ACMF",
    analyzer_source: "paddle_ocr",
    analyzer_status: "degraded",
    analyzer_version: "3.1",
    conflict_status: "conflict",
    conflicts_with: ["backend-vlm"],
  }),
]);
assert.equal(backendConflicts[0].analyzer, "vision_language_model");
assert.equal(backendConflicts[0].analyzer_source, "vision_language_model");
assert.equal(backendConflicts[0].analyzer_status, "analyzed");
assert.equal(backendConflicts[0].analyzer_version, "provider_snapshot");
assert.equal(backendConflicts[0].analysis_status, "ready");
assert.equal(backendConflicts[0].conflict_status, "conflict");
assert.deepEqual(backendConflicts[0].conflicts_with, ["backend-ocr"]);
assert.equal(imageEvidenceConflictGroups(backendConflicts).length, 1);
assert.equal(imageEvidenceConflictGroups([
  evidence({ analyzer: "vlm" }),
  evidence({ analyzer: "ocr" }),
]).length, 0, "matching facts from different analyzers are not conflicts");
assert.equal(imageEvidenceMaskReadiness({}, {}).status, "pending");
assert.equal(imageEvidenceMaskReadiness({}, { expired: true }).status, "source_expired");
assert.equal(imageEvidenceMaskReadiness({ image_mask_readiness: { status: "source_hash_changed" } }, {}).status, "source_hash_changed");
assert.equal(imageEvidenceMaskReadiness({ image_mask_readiness: { status: "ready" } }, {}).status, "ready");

assert.equal(normalizeImageEvidence([{ ...rows[0], confidence: "0.9" }]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ evidence_text: "   " })]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ bbox: { x: 0.9, y: 0, width: 0.2, height: 0.2 } })]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ bbox: { x: -0.1, y: 0, width: 0.2, height: 0.2 } })]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ bbox: { x: 0.1, y: 0.1, width: 0, height: 0.2 } })]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ bbox: null })]).length, 0);
assert.equal(normalizeImageEvidence([evidence({ protected: true, editable: true })]).length, 0);

const polygonRow = normalizeImageEvidence([evidence({
  evidence_id: "polygon",
  bbox: null,
  polygon: [{ x: 0.1, y: 0.1 }, { x: 0.7, y: 0.1 }, { x: 0.4, y: 0.7 }],
  protected: false,
  editable: true,
})])[0];
assert.equal(polygonRow.polygon?.length, 3);
assert.equal(normalizeImageEvidence([evidence({
  bbox: null,
  polygon: [{ x: 0, y: 0 }, { x: 1.1, y: 0 }, { x: 0, y: 1 }],
})]).length, 0, "out-of-bounds polygons must be rejected");
assert.equal(normalizeImageEvidence([evidence({
  bbox: null,
  polygon: [{ x: 0, y: 0 }, { x: 0.5, y: 0.5 }, { x: 1, y: 1 }],
})]).length, 0, "degenerate polygons must be rejected");
assert.equal(normalizeImageEvidence([evidence({
  bbox: null,
  polygon: [{ x: 0, y: 0 }, { x: 1, y: 0 }],
})]).length, 0, "polygons require at least three points");

const confirmed = normalizeImageEvidence([evidence({ evidence_id: "confirmed", review_status: "confirmed" })]);
const textEdited = updateImageEvidenceItem(confirmed, "confirmed", { evidence_text: "ACME NEW" });
assert.equal(textEdited[0].evidence_text, "ACME NEW");
assert.equal(textEdited[0].review_status, "pending", "text edits must invalidate confirmation");
const regionEdited = updateImageEvidenceItem(confirmed, "confirmed", {
  bbox: { x: 0.2, y: 0.2, width: 0.2, height: 0.1 },
});
assert.equal(regionEdited[0].review_status, "pending", "region edits must invalidate confirmation");
assert.equal(
  updateImageEvidenceItem(confirmed, "confirmed", { evidence_text: "ACME" })[0].review_status,
  "confirmed",
  "no-op edits must preserve confirmation",
);
assert.equal(
  setImageEvidenceRegionMode(confirmed, "confirmed", "protected")[0].review_status,
  "confirmed",
  "no-op region mode changes must preserve confirmation",
);

const editableMode = setImageEvidenceRegionMode(confirmed, "confirmed", "editable");
assert.equal(editableMode[0].editable, true);
assert.equal(editableMode[0].protected, false);
assert.equal(editableMode[0].review_status, "pending");
const noMode = setImageEvidenceRegionMode(editableMode, "confirmed", "none");
assert.equal(noMode[0].editable, false);
assert.equal(noMode[0].protected, false);
assert.throws(() => setImageEvidenceRegionMode(rows, rows[1].evidence_id, "protected"), /只有可见/);
assert.equal(reviewImageEvidenceItem(noMode, "confirmed", "rejected")[0].review_status, "rejected");
assert.throws(() => reviewImageEvidenceItem(noMode, "confirmed", "invalid"), /状态无效/);

const updatedResult = replaceReverseResultImageEvidence(
  { final_text: "保留生成稿", structured: { 主体: "香水瓶" }, image_evidence: confirmed },
  textEdited,
);
assert.equal(updatedResult.final_text, "保留生成稿");
assert.deepEqual(updatedResult.structured, { 主体: "香水瓶" });
assert.deepEqual(updatedResult.image_evidence, textEdited);
assert.throws(
  () => replaceReverseResultImageEvidence({ final_text: "原稿" }, [{ ...confirmed[0], confidence: "0.9" }]),
  /包含无效条目/,
  "result updates must not write invalid evidence into live Studio state",
);

const removable = normalizeImageEvidence([
  evidence({ evidence_id: "keep" }),
  evidence({ evidence_id: "remove", field_key: "副标题" }),
]);
assert.deepEqual(removeImageEvidenceItem(removable, "remove").map((item) => item.evidence_id), ["keep"]);
assert.throws(() => removeImageEvidenceItem(removable, "missing"), /已不存在/);

const mergeable = normalizeImageEvidence([
  evidence({ evidence_id: "merge-a", bbox: { x: 0.1, y: 0.2, width: 0.2, height: 0.1 }, review_status: "confirmed" }),
  evidence({ evidence_id: "merge-b", bbox: { x: 0.4, y: 0.2, width: 0.2, height: 0.1 }, evidence_text: "SECOND", confidence: 0.85 }),
  evidence({ evidence_id: "other", field_key: "背景", evidence_text: "Blue" }),
]);
const merged = mergeImageEvidenceItems(mergeable, ["merge-b", "merge-a"]);
assert.equal(merged.length, 2);
assert.equal(merged[0].evidence_id, "merge-a", "merge keeps the first item in source order");
assert.deepEqual(merged[0].bbox, { x: 0.1, y: 0.2, width: 0.5, height: 0.1 });
assert.equal(merged[0].evidence_text, "ACME；SECOND");
assert.equal(merged[0].confidence, 0.85);
assert.equal(merged[0].review_status, "pending");
assert.throws(() => mergeImageEvidenceItems(mergeable, ["merge-a", "other"]), /仅能合并/);

const manualRows = addManualImageEvidenceItem([], { sourceIndex: 2 });
assert.equal(manualRows.length, 1);
assert.equal(manualRows[0].evidence_id, "manual:2:1");
assert.equal(manualRows[0].source_index, 2);
assert.equal(manualRows[0].analyzer_source, "manual");
assert.equal(manualRows[0].review_status, "pending");
assert.equal(manualRows[0].protected, true);
assert.deepEqual(manualRows[0].bbox, { x: 0.2, y: 0.2, width: 0.6, height: 0.6 });
const secondManual = addManualImageEvidenceItem(manualRows, {
  sourceIndex: 2,
  evidenceType: "ocr",
  fieldKey: "标题",
  evidenceText: "人工校正文字",
});
assert.deepEqual(secondManual.map((item) => item.evidence_id), ["manual:2:1", "manual:2:1-2"]);
assert.equal(secondManual[1].evidence_type, "ocr");
assert.equal(secondManual[1].field_key, "标题");
assert.throws(() => addManualImageEvidenceItem([], { sourceIndex: 0 }), /源图无效/);

const manualSubject = updateImageEvidenceItem(secondManual, "manual:2:1-2", {
  evidence_type: "subject_protection",
});
assert.equal(manualSubject[1].protected, true);
assert.equal(manualSubject[1].editable, false);

const splitVertical = splitImageEvidenceItem([merged[0]], "merge-a", "vertical");
assert.deepEqual(splitVertical.map((item) => item.evidence_id), ["merge-a-a", "merge-a-b"]);
assert.equal(splitVertical[0].bbox?.width, 0.25);
assert.equal(splitVertical[1].bbox?.x, 0.35);
assert.ok(splitVertical.every((item) => item.review_status === "pending"));
const splitHorizontal = splitImageEvidenceItem([merged[0]], "merge-a", "horizontal");
assert.equal(splitHorizontal[0].bbox?.height, 0.05);
assert.equal(splitHorizontal[1].bbox?.y, 0.25);
const splitPolygon = splitImageEvidenceItem([polygonRow], "polygon", "vertical");
assert.ok(splitPolygon.every((item) => item.bbox && !item.polygon));

assert.deepEqual(
  bboxFromNormalizedDrag({ x: -1, y: -1 }, { x: 2, y: 2 }),
  { x: 0, y: 0, width: 1, height: 1 },
);
assert.equal(bboxFromNormalizedDrag({ x: 0.1, y: 0.1 }, { x: 0.101, y: 0.101 }), null);
assert.equal(bboxFromNormalizedDrag({ x: Number.NaN, y: 0 }, { x: 1, y: 1 }), null);

const maskRows = normalizeImageEvidence([
  evidence({ evidence_id: "protect", review_status: "confirmed" }),
  evidence({
    evidence_id: "edit-polygon",
    bbox: null,
    polygon: [{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.1 }, { x: 0.3, y: 0.5 }],
    source_index: 2,
    protected: false,
    editable: true,
    review_status: "confirmed",
  }),
  evidence({ evidence_id: "pending", review_status: "pending" }),
  evidence({ evidence_id: "rejected", review_status: "rejected" }),
  evidence({ evidence_id: "inferred", fact_status: "inferred", review_status: "confirmed" }),
  evidence({ evidence_id: "no-mode", protected: false, review_status: "confirmed" }),
]);
const maskPlan = buildImageEvidenceMaskPlan(maskRows);
assert.equal(maskPlan.schema_version, "image-mask-plan.v1");
assert.equal(maskPlan.coordinate_space, "normalized");
assert.deepEqual(maskPlan.sources.map((item) => item.source_index), [1, 2]);
assert.deepEqual(maskPlan.sources[0].protected_regions.map((item) => item.evidence_id), ["protect"]);
assert.equal(maskPlan.sources[0].editable_regions.length, 0);
assert.equal(maskPlan.sources[1].protected_regions.length, 0);
assert.deepEqual(maskPlan.sources[1].editable_regions[0].polygon, [
  { x: 0.1, y: 0.1 },
  { x: 0.5, y: 0.1 },
  { x: 0.3, y: 0.5 },
]);
assert.ok(!JSON.stringify(maskPlan).includes("pending"));
assert.ok(!JSON.stringify(maskPlan).includes("rejected"));
assert.ok(!JSON.stringify(maskPlan).includes("inferred"));
assert.ok(!JSON.stringify(maskPlan).includes("no-mode"));

const maskOperation = {
  request_context: {
    sources: [
      { asset_url: "https://api.example.test/api/uploads/users/1/main.png?signature=old" },
      { asset_url: "https://cdn.example.test/style.png", label: "风格图" },
    ],
  },
};
assert.deepEqual(
  validateImageEvidenceMaskPreflight(
    [evidence({ evidence_id: "same-source", review_status: "confirmed" })],
    maskOperation,
    "https://api.example.test/api/uploads/users/1/main.png?signature=new",
  ),
  { ok: true, message: "", edit_source_index: 1, foreign_source_indexes: [] },
  "owned upload identities must ignore refreshed access signatures",
);
const crossSourcePreflight = validateImageEvidenceMaskPreflight(
  maskRows,
  maskOperation,
  "https://api.example.test/api/uploads/users/1/main.png?signature=new",
);
assert.equal(crossSourcePreflight.ok, false);
assert.deepEqual(crossSourcePreflight.foreign_source_indexes, [2]);
assert.match(crossSourcePreflight.message, /参考图 2/);
assert.match(crossSourcePreflight.message, /保护\/编辑区域/);

const inactiveMaskEvidence = [
  evidence({ evidence_id: "pending-only", review_status: "pending" }),
  evidence({ evidence_id: "rejected-only", review_status: "rejected" }),
  evidence({ evidence_id: "inferred-only", fact_status: "inferred", review_status: "confirmed" }),
  evidence({ evidence_id: "no-mode-only", protected: false, editable: false, review_status: "confirmed" }),
];
assert.equal(
  validateImageEvidenceMaskPreflight(inactiveMaskEvidence, maskOperation, "").ok,
  true,
  "non-executable evidence must not block generation",
);
const mismatchedEditSource = validateImageEvidenceMaskPreflight(
  [evidence({ evidence_id: "active-mask", review_status: "confirmed" })],
  maskOperation,
  "https://cdn.example.test/other.png",
);
assert.equal(mismatchedEditSource.ok, false);
assert.match(mismatchedEditSource.message, /来源不一致/);

const evidenceRevisionBefore = {
  payload: {
    source_type: "image",
    final_text: "same prompt",
    image_evidence: [
      evidence({
        evidence_id: "stable-id",
        evidence_text: "OLD",
        review_status: "pending",
        conflict_status: "conflict",
        conflicts_with: ["z-id", "a-id"],
      }),
      evidence({ evidence_id: "removed-id", field_key: "Logo", evidence_text: "OLD LOGO" }),
      evidence({
        source_index: 2,
        field_key: "包装",
        evidence_text: "legacy old",
        analyzer_source: "fixture_detector",
        label: "package",
      }),
    ],
  },
};
const evidenceRevisionAfter = {
  payload: {
    source_type: "image",
    final_text: "same prompt",
    image_evidence: [
      evidence({
        evidence_id: "stable-id",
        evidence_text: "NEW",
        bbox: { x: 0.2, y: 0.2, width: 0.3, height: 0.1 },
        protected: false,
        editable: true,
        review_status: "confirmed",
        conflict_status: "none",
        conflicts_with: [],
      }),
      evidence({ evidence_id: "added-id", field_key: "副标题", evidence_text: "ADDED" }),
      evidence({
        source_index: 2,
        field_key: "包装",
        evidence_text: "legacy new",
        bbox: { x: 0.15, y: 0.2, width: 0.3, height: 0.1 },
        analyzer_source: "fixture_detector",
        label: "package",
        review_status: "confirmed",
      }),
    ],
  },
};
const evidenceRevisionDiff = compareReverseResultRevisions(
  evidenceRevisionBefore,
  evidenceRevisionAfter,
);
const evidenceDiffKeys = evidenceRevisionDiff.diffs.map((item) => item.key);
for (const key of [
  "image_evidence.stable-id.evidence_text",
  "image_evidence.stable-id.region",
  "image_evidence.stable-id.review_status",
  "image_evidence.stable-id.protected",
  "image_evidence.stable-id.editable",
  "image_evidence.stable-id.conflict",
  "image_evidence.added-id",
  "image_evidence.removed-id",
  "image_evidence.legacy:2:包装:ocr:1.evidence_text",
  "image_evidence.legacy:2:包装:ocr:1.region",
  "image_evidence.legacy:2:包装:ocr:1.review_status",
]) {
  assert.ok(evidenceDiffKeys.includes(key), `revision diff must include ${key}`);
}
assert.equal(
  evidenceRevisionDiff.diffs.find((item) => item.key === "image_evidence.added-id")?.status,
  "added",
);
assert.equal(
  evidenceRevisionDiff.diffs.find((item) => item.key === "image_evidence.removed-id")?.status,
  "removed",
);
assert.equal(evidenceRevisionDiff.summary.groups.structured, evidenceRevisionDiff.summary.total);
assert.deepEqual(
  compareReverseResultRevisions(evidenceRevisionBefore, evidenceRevisionAfter).diffs,
  evidenceRevisionDiff.diffs,
  "image evidence revision diff order must be deterministic",
);

for (const marker of ["服务端蒙版", "分析冲突", "analyzerLabel", "不会自动应用", "人工标注", "新增人工证据", "证据分类"]) {
  assert.match(evidenceComponentSource, new RegExp(marker));
}

assert.deepEqual(imageEvidenceSources({
  request_context: {
    sources: [
      { asset_url: "https://example.com/main.jpg", role: "primary" },
      { asset_url: "https://example.com/style.jpg", role: "style", label: "风格" },
    ],
  },
}), [
  { asset_url: "https://example.com/main.jpg", label: "primary" },
  { asset_url: "https://example.com/style.jpg", label: "风格" },
]);

assert.deepEqual(imageEvidenceSources({
  request_context: {
    sources: [
      { asset_url: "https://example.com/main.jpg", role: "primary" },
      { role: "style" },
      { asset_url: "https://example.com/package.jpg", role: "packaging" },
    ],
  },
}), [
  { asset_url: "https://example.com/main.jpg", label: "primary" },
  { asset_url: "", label: "style" },
  { asset_url: "https://example.com/package.jpg", label: "packaging" },
], "missing previews must not shift source_index alignment");

assert.match(
  evidenceViewSource,
  /onChange=\{\(nextEvidence\) => onChange\?\.\([\s\S]*replaceReverseResultImageEvidence\(result, nextEvidence\)/,
  "evidence view must merge edited evidence into the complete reverse result",
);
assert.match(
  resultPanelSource,
  /<ReverseEvidenceView[\s\S]*?onChange=\{changeResult\}[\s\S]*?onSelectField=\{setSelectedEvidenceField\}/,
  "result panel must route evidence edits through its dirty-state result updater",
);
assert.doesNotMatch(
  resultPanelSource,
  /onSelectField=\{inspectEvidenceField\}/,
  "selecting evidence must not navigate away from the editable evidence tab",
);
assert.match(resultPanelSource, /dirty: true/, "evidence edits must keep the existing save-version path dirty");
assert.match(
  resultPanelSource,
  /已确认图片证据[\s\S]*imageEvidence: selectedFields\.has\("imageEvidence"\)/,
  "image evidence must be an explicit reverse-result application field",
);
assert.match(
  resultPanelSource,
  /enabledFields\.map\(\(field\) => field\.token\)[\s\S]*disabled=\{field\.disabled\}/,
  "select-all must exclude image evidence until at least one row is confirmed",
);
assert.match(
  resultPanelSource,
  /应用版本只携带已确认证据/,
  "the application boundary must be visible next to the evidence selection",
);
assert.match(
  evidenceComponentSource,
  /const editable = typeof onChange === "function";[\s\S]*onChange\(normalized\)/,
  "live evidence controls must commit normalized rows through the supplied callback",
);

const submitFunctionSource = generationSubmitSource.slice(
  generationSubmitSource.indexOf("async function submit"),
  generationSubmitSource.indexOf("function resetOwnerGenerationSubmit"),
);
assert.match(submitFunctionSource, /validateImageEvidenceMaskPreflight/);
assert.ok(
  submitFunctionSource.indexOf("validateImageEvidenceMaskPreflight")
    < submitFunctionSource.indexOf("await requestQuoteConfirmation("),
  "mask preflight must run before quote and generation submission",
);
assert.match(studioPageSource, /reviewedImageEvidence: generationSourceRevision\?\.payload\?\.image_evidence/);
assert.match(studioPageSource, /reverseEvidenceOperation: reverseOperationForPendingResult\(\)/);

console.log("image evidence tests passed");
