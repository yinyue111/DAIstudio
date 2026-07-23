import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { promptOptimizationDisplayProposal } from "../app/studio/promptOptimization.ts";

const apiSource = readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");
const workspaceSource = readFileSync(
  new URL("../app/studio/StudioPromptWorkspace.jsx", import.meta.url),
  "utf8",
);

test("history detail is normalized without mutating the workspace", () => {
  const detail = {
    proposal_id: 27,
    proposal_version: 1,
    mode: "commercial",
    status: "proposed",
    original: { final_text: "source prompt" },
    suggestion: { final_text: "optimized prompt" },
    segments: [],
    compiler_profile: { model_id: "prompt-pro" },
  };

  const reopened = promptOptimizationDisplayProposal(detail);

  assert.equal(reopened.raw_text, "source prompt");
  assert.equal(reopened.optimized_text, "optimized prompt");
  assert.equal(reopened.model_name, "prompt-pro");
  assert.equal(reopened.direction, "commercial");
  assert.equal(reopened.status, "proposed");
  assert.equal("workspace" in reopened, false);
  assert.deepEqual(detail, {
    proposal_id: 27,
    proposal_version: 1,
    mode: "commercial",
    status: "proposed",
    original: { final_text: "source prompt" },
    suggestion: { final_text: "optimized prompt" },
    segments: [],
    compiler_profile: { model_id: "prompt-pro" },
  });
});

test("Studio exposes an inline paginated read-only history entry", () => {
  assert.match(apiSource, /studioPromptOptimizations:[\s\S]{0,500}limit[\s\S]{0,300}offset/);
  assert.match(apiSource, /studioPromptOptimization:[\s\S]{0,180}prompt-optimizations\/\$\{encodeURIComponent/);
  assert.match(workspaceSource, />\s*优化历史\s*</);
  assert.match(workspaceSource, /查看与重新打开/);
  assert.match(workspaceSource, /api\.studioPromptOptimizations\(\{ limit: HISTORY_PAGE_SIZE, offset \}\)/);
  assert.match(workspaceSource, /api\.studioPromptOptimization\(proposalId\)/);
  assert.match(workspaceSource, /readOnly[\s\S]{0,120}已重新打开的历史建议/);
  assert.match(workspaceSource, /历史建议仅供对比，重新打开不会覆盖当前工作区/);
  assert.doesNotMatch(workspaceSource, /href=["']\/prompt-optimizations/);
});
