import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");

assert.match(
  source,
  /const \[activeTab,\s*setActiveTab\] = useState\("system"\)/,
  "prompt library page should default to the system prompt tab",
);
assert.match(source, />\s*系统提示词\s*</, "system prompt tab should be visible");
assert.match(source, />\s*我的提示词\s*</, "my prompt tab should be visible");
assert.match(
  source,
  /activeTab === "system" \? \(\s*<PromptLibraryBrowser/,
  "system prompts should render as the first tab content",
);
assert.match(
  source,
  /setActiveTab\("mine"\)/,
  "user should be able to switch to my prompts",
);
assert.match(
  source,
  /title="系统提示词"/,
  "the built-in prompt library should be labeled as system prompts",
);
