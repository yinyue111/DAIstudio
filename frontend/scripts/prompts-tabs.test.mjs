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
assert.match(
  source,
  /const \[recipesEnabled,\s*setRecipesEnabled\] = useState\(false\)/,
  "creation recipes should remain hidden until the server enables them",
);
assert.match(
  source,
  /setRecipesEnabled\(config\?\.features\?\.recipes_enabled === true\)/,
  "creation recipe visibility should follow the public feature flag",
);
assert.match(source, />\s*系统提示词\s*</, "system prompt tab should be visible");
assert.match(source, />\s*我的提示词\s*</, "my prompt tab should be visible");
assert.match(source, />\s*创作配方\s*</, "creation recipe tab should be visible");
assert.match(
  source,
  /\{recipesEnabled && \(\s*<button[\s\S]*?setActiveTab\("recipes"\)/,
  "creation recipe tab should render only when recipes are enabled",
);
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
  /recipesEnabled && activeTab === "recipes" && me\?\.id/,
  "disabled recipes should never request recipe APIs",
);
assert.match(source, /api\.creationRecipes\(/, "recipe tab should load versioned recipes");
assert.match(source, /reverse_snapshot_v3/, "recipe restore should transfer the complete v3 workspace");
assert.match(source, /api\.favoriteCreationRecipe\(/, "recipes should support favorites");
assert.match(source, /api\.deleteCreationRecipe\(/, "recipes should support deletion");
assert.match(source, /api\.publicCreationRecipes\(/, "recipe discovery should load public recipes");
assert.match(source, /api\.creationRecipeVersions\(/, "owners should be able to browse recipe versions");
assert.match(source, /api\.activateCreationRecipeVersion\(/, "owners should be able to activate historical recipe versions");
assert.match(source, /api\.updateCreationRecipe\(/, "recipe metadata should be editable");
assert.match(source, /api\.cloneCreationRecipe\(/, "public and owned recipes should support derivation");
assert.match(
  source,
  /title="系统提示词"/,
  "the built-in prompt library should be labeled as system prompts",
);
assert.match(
  source,
  /function useSystemPrompt\(item\) \{\s*usePrompt\(item, \{ trackUsage: false \}\);\s*\}/,
  "system prompts should be applied without updating numeric prompt-history ids",
);
assert.match(
  source,
  /onPrimary=\{useSystemPrompt\}/,
  "the built-in prompt library should use the non-tracking apply handler",
);

const browserSource = readFileSync(join(root, "components/PromptLibraryBrowser.jsx"), "utf8");

assert.match(
  browserSource,
  /PromptImageDialog/,
  "prompt library thumbnails should open a large image dialog",
);
assert.match(
  browserSource,
  /点击查看大图/,
  "prompt library cards should expose an obvious large-preview affordance",
);
assert.match(
  browserSource,
  /aria-label=\{`查看大图/,
  "prompt library image previews should be keyboard and screen-reader accessible",
);

const recipeBrowserSource = readFileSync(join(root, "app/prompts/CreationRecipeBrowser.jsx"), "utf8");
const recipeCardSource = readFileSync(join(root, "app/prompts/CreationRecipeCard.jsx"), "utf8");
const recipeGovernanceSource = readFileSync(join(root, "app/prompts/CreationRecipeGovernance.jsx"), "utf8");
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");

assert.match(recipeBrowserSource, />\s*我的配方\s*</, "recipe browser should expose the owner library");
assert.match(recipeBrowserSource, />\s*公开发现\s*</, "recipe browser should expose public discovery");
assert.match(recipeBrowserSource, /type="search"/, "public discovery should be searchable");
assert.match(recipeCardSource, /版本管理/, "owned recipe cards should expose version management");
assert.match(recipeCardSource, /恢复所选/, "a selected historical version should be restorable");
assert.match(recipeCardSource, /设为当前/, "a selected historical version should be activatable");
assert.match(recipeCardSource, /设为公开/, "private recipes should be publishable");
assert.match(recipeCardSource, /转为私有/, "public recipes should be made private again");
assert.match(recipeCardSource, /复制派生/, "owned recipes should be clonable");
assert.match(recipeCardSource, /派生到我的配方/, "public recipes should be derivable into the owner library");
assert.match(recipeCardSource, />\s*直接使用\s*</, "public recipes should be directly restorable in Studio");
assert.match(recipeCardSource, /recordCreationRecipeUsage/, "restoring a recipe should record an apply event");
assert.match(recipeCardSource, /审核中/, "recipe cards should expose moderation state");
assert.match(recipeGovernanceSource, /submitCreationRecipeReview/, "owners should be able to submit public review");
assert.match(recipeGovernanceSource, /createCreationRecipeShare/, "owners should be able to create expiring shares");
assert.match(recipeGovernanceSource, /revokeCreationRecipeShare/, "owners should be able to revoke shares");
assert.match(recipeGovernanceSource, /链接有效期/, "share creation should expose an expiration choice");
assert.match(recipeGovernanceSource, /creationRecipeShareUrl/, "copied shares should point at the frontend landing page");
assert.match(apiSource, /\/api\/recipes\/shared\//, "the client should resolve non-guessable share slugs");
assert.match(apiSource, /\/api\/admin\/recipes\/.*\/review/, "the client should expose the admin review decision API");
