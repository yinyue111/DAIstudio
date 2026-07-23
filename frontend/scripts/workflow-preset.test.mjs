import assert from "node:assert/strict";

import {
  catalogModelStudioHref,
  resolveCatalogModelIntent,
  resolveStudioWorkflowPreset,
  studioWorkflowSlug,
} from "../app/studio/workflowPreset.ts";

function tool(overrides = {}) {
  return {
    id: 10,
    slug: "dynamic-ad-workflow",
    name: "动态广告工作流",
    enabled: true,
    renderer: "studio",
    active_version: {
      id: 101,
      version: 7,
      is_active: true,
      workflow: {
        type: "studio_preset",
        creation_mode: "image_edit",
        reverse: true,
        analysis_focus: "replica",
        output_purpose: "generation",
        reference_roles: ["main", "product"],
      },
    },
    ...overrides,
  };
}

assert.equal(studioWorkflowSlug(""), "");
assert.equal(studioWorkflowSlug("?workflow=Dynamic-Ad-Workflow"), "dynamic-ad-workflow");
assert.equal(resolveStudioWorkflowPreset("", null).status, "absent");
assert.equal(resolveStudioWorkflowPreset("?workflow=unknown", null).status, "unknown");
assert.equal(
  resolveStudioWorkflowPreset("?workflow=dynamic-ad-workflow", tool({ enabled: false })).status,
  "disabled",
);
assert.equal(
  resolveStudioWorkflowPreset("?workflow=dynamic-ad-workflow", tool({ renderer: "custom-canvas" })).status,
  "unsupported_renderer",
);
assert.equal(
  resolveStudioWorkflowPreset("?workflow=dynamic-ad-workflow", tool({ active_version: null })).status,
  "no_active_version",
);
assert.equal(
  resolveStudioWorkflowPreset("?workflow=dynamic-ad-workflow", tool({
    active_version: { id: 102, version: 8, is_active: true, workflow: { type: "dag" } },
  })).status,
  "invalid_workflow",
);

const dynamic = resolveStudioWorkflowPreset("?workflow=dynamic-ad-workflow", tool());
assert.equal(dynamic.status, "ready");
assert.deepEqual(dynamic.preset, {
  key: "dynamic-ad-workflow",
  versionId: 101,
  version: 7,
  creationMode: "image_edit",
  analysisFocus: "replica",
  outputPurpose: "generation",
  referenceRoles: ["main", "product"],
  message: "已进入动态广告工作流，请添加素材并设置分析范围。",
});

const executable = resolveStudioWorkflowPreset("?workflow=storyboard-compose", tool({
  slug: "storyboard-compose",
  name: "分镜合成与导出",
  active_version: {
    id: 109,
    version: 1,
    is_active: true,
    workflow: {
      type: "workflow.v1",
      studio_preset: {
        creation_mode: "video_edit",
        analysis_focus: "storyboard",
        output_purpose: "storyboard",
        message: "已进入分镜合成，请先恢复视频反推分镜并为镜头绑定素材。",
      },
      nodes: [
        { key: "compose", type: "compose", depends_on: [] },
        { key: "export", type: "export", depends_on: ["compose"] },
      ],
      output_node: "export",
    },
  },
}));
assert.equal(executable.status, "ready");
assert.deepEqual(executable.preset, {
  key: "storyboard-compose",
  versionId: 109,
  version: 1,
  creationMode: "video_edit",
  analysisFocus: "storyboard",
  outputPurpose: "storyboard",
  message: "已进入分镜合成，请先恢复视频反推分镜并为镜头绑定素材。",
});
assert.equal(resolveStudioWorkflowPreset("?workflow=storyboard-compose", tool({
  slug: "storyboard-compose",
  active_version: {
    id: 110,
    version: 1,
    is_active: true,
    workflow: { type: "workflow.v1", nodes: [{ key: "compose", type: "compose" }] },
  },
})).status, "invalid_workflow");

const modelCases = [
  ["image", { text_to_image: true }, "image"],
  ["image", { image_to_image: true }, "image_edit"],
  ["video", { text_to_video: true }, "video"],
  ["video", { image_to_video: true }, "video_edit"],
  ["vision", { image_analysis: true }, "image_edit"],
  ["vision", { video_analysis: true }, "video_edit"],
];
for (const [use, capabilities, creationMode] of modelCases) {
  const result = resolveCatalogModelIntent({ id: 42, use, capabilities });
  assert.equal(result.status, "ready", `${use} should resolve from declared capabilities`);
  assert.equal(result.creationMode, creationMode);
}

const prompt = resolveCatalogModelIntent({
  id: 43,
  use: "prompt",
  capabilities: { prompt_optimization: true },
});
assert.equal(prompt.status, "ready");
assert.equal(prompt.creationMode, undefined);
assert.equal(
  resolveCatalogModelIntent({ id: 44, use: "image", capabilities: {} }).status,
  "unsupported_capability",
);
assert.equal(
  resolveCatalogModelIntent(
    { id: 45, use: "video", capabilities: { text_to_video: true } },
    "video_edit",
  ).status,
  "incompatible_mode",
);

const href = catalogModelStudioHref({
  id: 46,
  use: "vision",
  capabilities: { video_analysis: true },
});
assert.equal(href.status, "ready");
assert.match(href.href, /model_config_id=46/);
assert.match(href.href, /model_use=vision/);
assert.match(href.href, /studio_mode=video_edit/);
assert.doesNotMatch(href.href, /workflow=/);

console.log("workflow preset and catalog model intent tests passed");
