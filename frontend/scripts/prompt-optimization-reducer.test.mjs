import assert from "node:assert/strict";

import {
  applyPromptOptimizationDecision,
  isSupportedPromptOptimizationSegment,
  undoPromptOptimization,
} from "../app/studio/promptOptimization.ts";

const workspace = {
  prompt: "original prompt",
  negative: "blur",
  structured: { scene: "studio", camera: "wide", retained: "keep" },
  ratio: "16:9",
  vDuration: 5,
  vResolution: "720p",
};

const segments = [
  ["segment-final", "final_text", "original prompt", "optimized prompt"],
  ["segment-negative", "negative", "blur", "blur, watermark"],
  ["segment-scene", "structured.scene", "studio", "beach"],
  ["segment-camera", "structured.camera", "wide", null],
  ["segment-ratio", "parameters.aspect_ratio", "16:9", "9:16"],
  ["segment-duration", "parameters.duration", 5, 10],
  ["segment-resolution", "parameters.resolution", "720p", "1080p"],
  ["segment-metadata", "compiler_metadata", null, { schema: "v1" }],
].map(([id, field_path, original, suggestion]) => ({
  id,
  field_path,
  original,
  suggestion,
  changed: true,
}));

const proposal = {
  proposal_id: 42,
  proposal_version: 1,
  original: {
    final_text: "original prompt",
    negative: "blur",
    structured: { scene: "studio", camera: "wide", retained: "keep" },
    parameters: { aspect_ratio: "16:9", duration: 5, resolution: "720p" },
  },
  suggestion: {},
  segments,
};

const authoritative = {
  final_text: "server authoritative prompt",
  negative: "server negative",
  structured: { scene: "server beach", retained: "keep" },
  parameters: { aspect_ratio: "4:3", duration: 12, resolution: "4k" },
  compiler_metadata: { schema: "server-only" },
};

assert.equal(isSupportedPromptOptimizationSegment("final_text"), true);
assert.equal(isSupportedPromptOptimizationSegment("structured.scene"), true);
assert.equal(isSupportedPromptOptimizationSegment("parameters.vDuration"), true);
assert.equal(isSupportedPromptOptimizationSegment("compiler_metadata"), false);
assert.equal(isSupportedPromptOptimizationSegment("parameters.seed"), false);

const acceptedIds = segments.map((segment) => segment.id);
const applied = applyPromptOptimizationDecision(workspace, proposal, authoritative, acceptedIds);
assert.equal(applied.applied, true);
assert.equal(applied.stale, false);
assert.deepEqual(applied.workspace, {
  prompt: "server authoritative prompt",
  negative: "server negative",
  structured: { scene: "server beach", retained: "keep" },
  ratio: "4:3",
  vDuration: 12,
  vResolution: "4k",
});
assert.deepEqual(
  [...applied.changedFields].sort(),
  ["negative", "prompt", "ratio", "structured", "vDuration", "vResolution"].sort(),
);
assert.deepEqual(applied.undo.before, workspace);
assert.deepEqual(applied.undo.after, applied.workspace);

// Snapshots are cloned and cannot be changed through the caller's objects.
authoritative.structured.scene = "mutated after apply";
assert.equal(applied.workspace.structured.scene, "server beach");

const undone = undoPromptOptimization(applied.workspace, applied.undo);
assert.deepEqual(undone, { applied: true, stale: false, workspace });

const staleApply = applyPromptOptimizationDecision(
  { ...workspace, structured: { ...workspace.structured, scene: "manually edited" } },
  proposal,
  { ...authoritative, structured: { scene: "server beach", retained: "keep" } },
  ["segment-scene"],
);
assert.equal(staleApply.stale, true);
assert.equal(staleApply.workspace.structured.scene, "manually edited");

const unrelatedSelectedFieldEdit = applyPromptOptimizationDecision(
  { ...workspace, negative: "edited negative" },
  proposal,
  { ...authoritative, structured: { scene: "server beach", retained: "keep" } },
  ["segment-scene"],
);
assert.equal(unrelatedSelectedFieldEdit.stale, false);
assert.equal(unrelatedSelectedFieldEdit.workspace.negative, "edited negative");
assert.equal(unrelatedSelectedFieldEdit.workspace.structured.scene, "server beach");

const staleUndoWorkspace = {
  ...applied.workspace,
  ratio: "1:1",
};
const staleUndo = undoPromptOptimization(staleUndoWorkspace, applied.undo);
assert.equal(staleUndo.stale, true);
assert.deepEqual(staleUndo.workspace, staleUndoWorkspace);

const unsupportedOnly = applyPromptOptimizationDecision(
  workspace,
  proposal,
  authoritative,
  ["segment-metadata"],
);
assert.equal(unsupportedOnly.applied, false);
assert.deepEqual(unsupportedOnly.workspace, workspace);

console.log("prompt optimization reducer tests passed");
