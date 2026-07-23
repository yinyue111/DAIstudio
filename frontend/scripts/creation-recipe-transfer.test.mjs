import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildCreationRecipeStudioDraft,
  creationRecipeSharePath,
  creationRecipeShareUrl,
} from "../lib/creationRecipeTransfer.ts";

function recipe(payload, overrides = {}) {
  return {
    id: 42,
    title: "商品主视觉",
    category: "image",
    current_version: 3,
    version: {
      id: 9,
      recipe_id: 42,
      version: 3,
      schema_version: "creation-recipe.v1",
      payload,
      created_at: null,
    },
    ...overrides,
  };
}

test("pure prompt recipes restore without fabricating reverse evidence", () => {
  const row = recipe({
    schema_version: "creation-recipe.v1",
    prompt: "白底棚拍，保留商品比例",
    negative: "不要文字",
    structured: { lighting: "softbox" },
    generation: { ratio: "3:4", image_quality: "2k", count: 2 },
  });
  const draft = buildCreationRecipeStudioDraft(row, row.version, { source: "public", savedAt: 100 });

  assert.equal(draft.reverse_snapshot_v3, null);
  assert.equal(draft.prompt, "白底棚拍，保留商品比例");
  assert.equal(draft.negative, "不要文字");
  assert.deepEqual(draft.structured, { lighting: "softbox" });
  assert.deepEqual(draft.generation, { ratio: "3:4", image_quality: "2k", count: 2 });
  assert.equal(draft.creationMode, "image");
  assert.equal(draft.creationRecipeId, 42);
  assert.equal(draft.creationRecipeVersion, 3);
  assert.equal(draft.creationRecipeSource, "public");
});

test("structured-only recipes remain replayable without a reverse snapshot", () => {
  const row = recipe({
    schema_version: "creation-recipe.v1",
    prompt: "",
    negative: "",
    structured: { subject: "机械腕表", composition: "正面居中" },
    generation_params: { ratio: "1:1" },
  });
  const draft = buildCreationRecipeStudioDraft(row, row.version, { source: "owner" });

  assert.equal(draft.prompt, "");
  assert.equal(draft.reverse_snapshot_v3, null);
  assert.deepEqual(draft.structured, { subject: "机械腕表", composition: "正面居中" });
  assert.deepEqual(draft.generation, { ratio: "1:1" });
});

test("shared reverse recipes carry exact attribution into the workspace snapshot", () => {
  const slug = "Abcdefghijklmnopqrst_1234";
  const row = recipe({
    schema_version: "creation-recipe.v1",
    prompt: "镜头缓慢推进",
    structured: {},
    reverse_snapshot_v3: {
      version: 3,
      target: "video",
      creation_mode: "video",
      final_text: "旧提示词",
      generation: { duration: 5, product_video_template: "slow_push" },
    },
    generation: { resolution: "1080p" },
  }, { category: "video" });
  const draft = buildCreationRecipeStudioDraft(row, row.version, { shareSlug: slug, source: "share" });

  assert.equal(draft.creationMode, "video");
  assert.equal(draft.creationRecipeShareSlug, slug);
  assert.equal(draft.reverse_snapshot_v3.creation_recipe_id, 42);
  assert.equal(draft.reverse_snapshot_v3.creation_recipe_version, 3);
  assert.equal(draft.reverse_snapshot_v3.creation_recipe_share_slug, slug);
  assert.equal(draft.reverse_snapshot_v3.creation_recipe_source, "share");
  assert.deepEqual(draft.reverse_snapshot_v3.generation, {
    duration: 5,
    product_video_template: "slow_push",
    resolution: "1080p",
  });
});

test("complete reverse recipes restore evidence, lineage, asset roles, catalog versions, parameters, and seed exactly", () => {
  const payload = {
    schema_version: "creation-recipe.v1",
    prompt: "精确生成稿",
    negative: "不要品牌文字漂移",
    structured: { subject: "银色香水瓶", camera: "slow dolly in" },
    generation_params: { ratio: "1:1", count: 1 },
    reverse_snapshot_v3: {
      version: 3,
      target: "video",
      creation_mode: "video_edit",
      final_text: "旧生成稿",
      selected: { id: 301, type: "video", url: "/api/uploads/source.mp4" },
      sources: [
        { source_type: "video", role: "primary", asset_ref: "upload:301" },
        { source_type: "image", role: "product", asset_ref: "upload:302" },
        { source_type: "image", role: "style", asset_ref: "gen:901" },
      ],
      image_evidence: [{
        evidence_id: "evidence-logo",
        source_index: 1,
        field: "brand_text",
        text: "CODEX",
        bbox: [0.1, 0.2, 0.5, 0.35],
        confidence: 0.98,
        review_status: "confirmed",
        mask_mode: "protect",
      }],
      video_analysis: {
        analysis_mode: "multi_segment",
        selected_ranges: [{ start_seconds: 2, end_seconds: 6 }],
        shots: [{
          shot_id: "shot-1",
          start_seconds: 2,
          end_seconds: 4,
          visual: "香水瓶居中",
          action: "瓶身缓慢旋转",
          camera: "slow dolly in",
          evidence_frame_indices: [4, 7],
          ocr_track_ids: ["ocr-1"],
          audio_evidence_ids: ["asr-1"],
        }],
        ocr_tracks: [{ id: "ocr-1", text: "CODEX", start_seconds: 2.2, end_seconds: 3.8 }],
        audio_evidence: [{ id: "asr-1", kind: "asr", text: "新品上市", start_seconds: 2, end_seconds: 4 }],
      },
      pending_result: { operation_id: 88, normalized_revision_id: 102 },
      applied_version: 5,
      applied_revision_id: 105,
      result_revisions: [
        { id: 101, version: 1, kind: "provider_raw", parent_revision_id: null },
        { id: 102, version: 2, kind: "normalized", parent_revision_id: 101 },
        { id: 104, version: 4, kind: "user_edit", parent_revision_id: 102 },
        { id: 105, version: 5, kind: "applied", parent_revision_id: 104 },
      ],
      generation: { duration: 5, resolution: "720p", seed: 123 },
    },
    generation: {
      ratio: "9:16",
      duration: 8,
      resolution: "1080p",
      count: 2,
      seed: 987654,
      model_selections: { image: 21, video: 31, vision: 11, prompt: 41 },
      generation_model_config_id: 31,
      vision_model_config_id: 11,
      prompt_model_config_id: 41,
      product_video_template: "stable_showcase",
      catalog_versions: {
        video: {
          model_config_id: 31,
          capability_version_id: 311,
          capability_version: 4,
          price_version_id: 312,
          price_version: 7,
        },
        vision: {
          model_config_id: 11,
          capability_version_id: 111,
          capability_version: 3,
          price_version_id: 112,
          price_version: 5,
        },
      },
    },
  };
  const row = recipe(payload, { category: "video" });
  const draft = buildCreationRecipeStudioDraft(row, row.version, { source: "owner", savedAt: 777 });

  assert.equal(draft.creationMode, "video_edit");
  assert.equal(draft.prompt, "精确生成稿");
  assert.equal(draft.negative, "不要品牌文字漂移");
  assert.deepEqual(draft.structured, payload.structured);
  assert.deepEqual(draft.generation, {
    ratio: "9:16",
    count: 2,
    duration: 8,
    resolution: "1080p",
    seed: 987654,
    model_selections: payload.generation.model_selections,
    generation_model_config_id: 31,
    vision_model_config_id: 11,
    prompt_model_config_id: 41,
    product_video_template: "stable_showcase",
    catalog_versions: payload.generation.catalog_versions,
  });
  assert.deepEqual(draft.reverse_snapshot_v3.sources, payload.reverse_snapshot_v3.sources);
  assert.deepEqual(draft.reverse_snapshot_v3.image_evidence, payload.reverse_snapshot_v3.image_evidence);
  assert.deepEqual(draft.reverse_snapshot_v3.video_analysis, payload.reverse_snapshot_v3.video_analysis);
  assert.deepEqual(draft.reverse_snapshot_v3.result_revisions, payload.reverse_snapshot_v3.result_revisions);
  assert.equal(draft.reverse_snapshot_v3.applied_revision_id, 105);
  assert.deepEqual(draft.reverse_snapshot_v3.generation, draft.generation);
  assert.equal(draft.savedAt, 777);

  payload.reverse_snapshot_v3.sources[0].role = "mutated";
  payload.reverse_snapshot_v3.video_analysis.shots[0].visual = "mutated";
  payload.generation.catalog_versions.video.capability_version = 999;
  assert.equal(draft.reverse_snapshot_v3.sources[0].role, "primary");
  assert.equal(draft.reverse_snapshot_v3.video_analysis.shots[0].visual, "香水瓶居中");
  assert.equal(draft.generation.catalog_versions.video.capability_version, 4);
});

test("share URLs point at the frontend landing page", () => {
  const slug = "Abcdefghijklmnopqrst_1234";
  assert.equal(creationRecipeSharePath(slug), `/recipes/shared/${slug}`);
  assert.equal(
    creationRecipeShareUrl(slug, "https://studio.example.com/"),
    `https://studio.example.com/recipes/shared/${slug}`,
  );
});
