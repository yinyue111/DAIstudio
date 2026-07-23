import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const adminPage = readFileSync(join(root, "app/admin/page.jsx"), "utf8");
const reviews = readFileSync(join(root, "app/admin/components/recipe-reviews.jsx"), "utf8");
const sharedPage = readFileSync(join(root, "app/recipes/shared/[slug]/page.jsx"), "utf8");
const promptsPage = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");
const recipeCard = readFileSync(join(root, "app/prompts/CreationRecipeCard.jsx"), "utf8");
const proxy = readFileSync(join(root, "proxy.js"), "utf8");

assert.match(adminPage, /\["recipes", "配方审核"\]/, "admin navigation should expose recipe moderation");
assert.match(adminPage, /recipes:\s*RecipeReviews/, "recipe moderation tab should render its review queue");
assert.match(reviews, /api\.adminRecipeReviews/, "review queue should load from the admin recipe API");
assert.match(reviews, /api\.adminReviewRecipe/, "review decisions should use the admin review endpoint");
assert.match(reviews, /驳回时必须填写审核意见/, "rejections should require a reason before submission");
assert.match(reviews, /公开封面/, "reviewers should inspect the recipe cover before approval");
assert.match(reviews, /完整待公开配方数据/, "reviewers should inspect the complete payload before approval");
assert.match(reviews, /latestLoadRef/, "review filtering should ignore stale responses");
assert.match(reviews, /requestId !== latestLoadRef\.current/, "older review requests must not overwrite the latest filter");

assert.match(proxy, /pathname\.startsWith\("\/recipes\/shared\/"\)/, "shared recipe pages should be publicly reachable");
assert.match(sharedPage, /api\.sharedCreationRecipe\(slug\)/, "share landing should resolve the non-guessable slug");
assert.match(sharedPage, /share_slug:\s*slug/, "shared usage and derivation should preserve the share slug");
assert.match(sharedPage, /buildCreationRecipeStudioDraft/, "shared recipes should use the common Studio transfer contract");
assert.match(sharedPage, /在 Studio 使用/, "share landing should expose a direct-use action");
assert.match(sharedPage, /派生到我的配方/, "share landing should expose a derivation action");
assert.match(sharedPage, /分享链接不可用/, "share landing should render an explicit invalid state");
assert.match(sharedPage, /api\.me\(\{ redirectOn401: false \}\)\.catch\(\(\) => null\)/, "auth probing must not block a public share");
assert.match(sharedPage, /catch \(cause\) \{\s*setShared\(null\)/, "failed refreshes must not leave stale share content visible");

assert.match(promptsPage, /buildCreationRecipeStudioDraft/, "owned and public recipes should share the transfer contract");
assert.match(recipeCard, />\s*直接使用\s*</, "public recipes should support direct Studio restore");
