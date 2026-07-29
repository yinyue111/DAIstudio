import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { readStudioSource } from "./studio-source.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const layoutSource = readFileSync(join(root, "app/layout.jsx"), "utf8");
const apiSource = readFileSync(join(root, "lib/api.js"), "utf8");
const errorSource = readFileSync(join(root, "lib/errorHandling.js"), "utf8");
const taskTrackingSource = readFileSync(join(root, "hooks/useTaskTracking.js"), "utf8");
const ownerSessionSource = readFileSync(join(root, "hooks/useStudioOwnerSession.js"), "utf8");
const pageSource = readStudioSource(root);
const historySource = readFileSync(join(root, "app/history/page.jsx"), "utf8");
const profileSource = readFileSync(join(root, "app/profile/page.jsx"), "utf8");
const promptsSource = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");
const rechargeSource = readFileSync(join(root, "app/recharge/page.jsx"), "utf8");
const adminSource = readFileSync(join(root, "app/admin/page.jsx"), "utf8");
const reportsSource = readFileSync(join(root, "app/admin/components/reports-audit.jsx"), "utf8");

assert.match(
  layoutSource,
  /ToastProvider/,
  "root layout should mount a global toast provider so every page gets mobile-visible notifications",
);
for (const [name, source] of [
  ["studio", pageSource],
  ["history", historySource],
  ["profile", profileSource],
  ["prompts", promptsSource],
  ["recharge", rechargeSource],
]) {
  assert.match(source, /useToast|notify\./, `${name} page should route key feedback through global toast`);
}
assert.match(apiSource, /getDraft:/, "API client should expose cloud draft loading");
assert.match(apiSource, /saveDraft:/, "API client should expose cloud draft saving");
assert.match(apiSource, /deleteDraft:/, "API client should expose cloud draft deletion");
assert.match(apiSource, /taskEta:/, "API client should expose historical task ETA lookup");
assert.match(apiSource, /adminUsageDashboard:/, "API client should expose admin business dashboard");
assert.match(apiSource, /adminModelCosts:/, "API client should expose model cost dashboard");
assert.match(errorSource, /error_type/, "generation error classifier should prefer backend error_type codes");
assert.match(taskTrackingSource, /eta_remaining_seconds|eta_total_seconds/, "video task tracking should use backend historical ETA fields");
assert.match(
  ownerSessionSource,
  /saveDraft|getDraft/,
  "studio owner session should sync recoverable drafts to the backend",
);
assert.match(pageSource, /promptSaveTitle|promptSaveFavorite|promptSaveCategory/, "reverse prompt saving should support title, favorite, and category");
assert.match(historySource, /UnifiedTaskList|useUnifiedTaskCenter/, "history page should use the unified task-center surface");
assert.match(profileSource, /compareSelection|Compare/, "profile page should offer a comparison mode");
assert.match(adminSource, /dashboard/, "admin page should expose a dashboard tab");
assert.match(reportsSource, /adminUsageDashboard|adminModelCosts/, "admin report view should render business and model cost metrics");

console.log("global toast experience contract passed");
