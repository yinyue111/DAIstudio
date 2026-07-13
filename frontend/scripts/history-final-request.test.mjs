import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/history/page.jsx"), "utf8");

assert.doesNotMatch(
  source,
  /renderFinal|parent_task_id:\s*taskId|video-final-|渲染完整视频|方向满意/,
  "history should not expose the removed preview-to-final render flow",
);

assert.match(
  source,
  /TASK_POLL_MAX_BACKOFF_MS\s*=\s*30000/,
  "history should keep retrying task status with a bounded backoff after a service restart",
);
assert.doesNotMatch(
  source,
  /failures\s*>=\s*5[\s\S]{0,180}stopped\s*=\s*true/,
  "history must not permanently stop tracking after five transient request failures",
);
assert.match(
  source,
  /任务状态暂时无法同步，正在自动重试/,
  "history should explain that status recovery is automatic",
);
assert.match(source, /taskModelLabel\(t\)/, "history should show the model captured for each generation task");
assert.match(source, /taskPromptRecords\(t\)/, "history should build all persisted prompt records for each task");
assert.match(source, /taskPromptMetadata\(t\)/, "history should show prompt optimizer and compiler provenance");
assert.match(
  source,
  /prompt_text_source\s*===\s*"generation"[\s\S]{0,180}"最终生成提示词"/,
  "history should identify the exact prompt sent to the generation model",
);
assert.match(
  source,
  /prompt_text_source\s*===\s*"request"[\s\S]{0,180}"原始请求提示词"/,
  "history should identify request-only prompts from legacy tasks",
);
assert.match(source, /task\?\.prompt_text/, "history should retain prompt_text as the legacy fallback");
assert.match(source, /raw_prompt_text/, "history should render the raw user input when available");
assert.match(source, /optimized_prompt_text/, "history should render the LLM optimized prompt when available");
assert.match(source, /assembled_prompt_text/, "history should render the assembled request prompt when available");
assert.match(source, /request_prompt_text/, "history should render the original request prompt when available");
assert.match(source, /generation_prompt_text/, "history should render the final generation prompt when available");
assert.match(
  source,
  /用户原始输入[\s\S]{0,1000}LLM 优化稿[\s\S]{0,1000}组装请求稿[\s\S]{0,1000}最终模型稿/,
  "history should expose all prompt stages in execution order",
);
assert.match(source, /prompt_optimizer_model_id/);
assert.match(source, /prompt_compiler_version/);
assert.match(source, /提示词优化模型/);
assert.match(source, /编译器版本/);
assert.match(source, /task\?\.sfx/);
assert.match(source, /后期音效/);
assert.match(source, /未记录（历史任务）/, "legacy tasks without a model snapshot should be explicit");
assert.doesNotMatch(
  source,
  /return model \|\| provider \|\| task\?\.model_use/,
  "history must not present image/video model_use values as model identifiers",
);

console.log("history final render removal test passed");
