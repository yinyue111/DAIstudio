import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/history/page.jsx"), "utf8");

assert.doesNotMatch(
  source,
  /api\.tasks\(|GenTask/,
  "history must not fetch a generation-only task list",
);
assert.match(source, /useUnifiedTaskCenter/, "history should use the shared unified task center hook");
assert.match(source, /UnifiedTaskList/, "history should render generation, reverse, and parse items through one shared list");
assert.match(source, /UnifiedTaskFilters/, "history should expose unified type, status, and category filters");
assert.match(source, /实时同步/, "history should expose the event-stream transport state");
assert.match(source, /center\.counts/, "history should use server aggregated status counts");

console.log("unified history task center test passed");
