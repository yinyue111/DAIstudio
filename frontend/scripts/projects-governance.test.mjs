import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const read = (path) => readFileSync(join(root, path), "utf8");

const page = read("app/projects/page.jsx");
const workspace = read("app/projects/ProjectWorkspace.jsx");
const projectTasks = read("app/projects/ProjectTasks.jsx");
const projectRecipes = read("app/projects/ProjectRecipes.jsx");
const api = read("lib/api.js");
const declarations = read("lib/api.d.ts");

assert.match(api, /exportProject:.*downloadBlob\(/s, "project export should use the authenticated blob download helper");
assert.match(api, /updateProjectAssetTags:/, "the client should expose unified asset tag updates");
assert.match(api, /findProjectSimilarAssets:/, "the client should expose exact and perceptual duplicate analysis");
assert.match(declarations, /interface AssetSimilarityResult/, "the similarity result should have a typed client contract");
assert.match(declarations, /auto_archive_after_days: number \| null/, "project archive policy should be typed");
assert.match(declarations, /cost_settled: number/, "project settled cost should be typed");

assert.match(workspace, /导出 ZIP/, "project owners should have an explicit ZIP export command");
assert.match(workspace, /已结算.*积分/s, "the workspace should show settled project credits");
assert.match(workspace, /累计冻结/, "the workspace should show frozen project credits");
assert.match(workspace, /id="project-auto-archive"/, "the workspace should expose an auto-archive selector");
assert.match(workspace, /保存素材标签/, "each project asset should support tag editing");
assert.match(workspace, /查重与相似检测/, "each project asset should expose duplicate analysis");
assert.match(workspace, /精确重复.*感知相似/s, "duplicate results should distinguish exact and perceptual matches");
assert.match(workspace, /similarity\.message/, "degraded analysis messages should stay visible to the user");
assert.match(workspace, /import ProjectTasks from "\.\/ProjectTasks"/, "project tasks should use the dedicated deep-link component");
assert.match(workspace, /import ProjectRecipes from "\.\/ProjectRecipes"/, "project recipes should use the dedicated restore component");
assert.match(workspace, /onRestore=\{onRestoreRecipe\}/, "the workspace should wire recipe restore actions");

assert.match(projectTasks, /taskDetailHref\(/, "project task links should use the shared task detail route");
assert.match(projectTasks, /<Link href=\{href\}/, "valid project tasks should link to history details");
assert.match(projectTasks, /parseUnifiedTaskKey/, "malformed or expired task links should not become clickable");
assert.match(projectRecipes, /恢复到创作/, "project recipes should expose a Studio restore command");

assert.match(page, /api\.updateProjectAssetTags\(/, "the project page should persist tag edits");
assert.match(page, /api\.findProjectSimilarAssets\(/, "the project page should run real duplicate analysis");
assert.match(page, /api\.project\(project\.id\)/, "analysis should refresh persisted metadata and aggregate counts");
assert.match(page, /api\.exportProject\(/, "the project page should start authenticated export downloads");
assert.match(page, /useSearchParams/, "project deep links should consume the URL query");
assert.match(page, /searchParams\.get\("project"\)/, "the requested project should be selected from the URL");
assert.match(page, /new URLSearchParams\(searchParams\.toString\(\)\)/, "project selection should preserve unrelated query keys");
assert.match(page, /next\.set\("project", String\(normalizedId\)\)/, "project selection should update the project query");
assert.match(page, /api\.creationRecipe\(recipeLink\.recipe_id\)/, "recipe restore should resolve the owner-gated recipe");
assert.match(page, /api\.creationRecipeVersion\(/, "recipe restore should resolve a missing version payload");
assert.match(page, /buildCreationRecipeStudioDraft/, "recipe restore should use the shared Studio transfer contract");
assert.match(page, /saveStudioUserDraft\(/, "recipe restore should persist a user-scoped Studio draft");
assert.match(page, /STUDIO_DRAFT_PROMPT_KEY/, "recipe restore should use the Studio prompt transfer key");

console.log("project governance UI test passed");
