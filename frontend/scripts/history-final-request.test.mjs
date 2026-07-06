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

console.log("history final render removal test passed");
