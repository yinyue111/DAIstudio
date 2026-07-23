import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const page = readFileSync(join(root, "app/page.jsx"), "utf8");
const shell = readFileSync(join(root, "components/AppShell.jsx"), "utf8");
const recharge = readFileSync(join(root, "app/recharge/page.jsx"), "utf8");
const prompts = readFileSync(join(root, "app/prompts/page.jsx"), "utf8");
const admin = readFileSync(join(root, "app/admin/page.jsx"), "utf8");
const adminSettings = readFileSync(join(root, "app/admin/components/payments-settings.jsx"), "utf8");

for (const prop of [
  "reverseBatchEnabled",
  "videoCompositionEnabled",
  "reproductionAssessmentEnabled",
  "recipesEnabled",
]) {
  assert.match(page, new RegExp(`${prop}=\\{${prop}\\}`), `${prop} must reach the Studio UI`);
}
assert.match(page, /enabled: reverseBatchEnabled/, "disabled batch mode must not call batch APIs");
assert.match(page, /assetPicker\.role !== "reverse_batch"/, "disabled batch mode must not open its picker");
assert.match(shell, /router\.replace\("\/"\)/, "hidden protected routes must redirect to Studio");
assert.match(
  prompts,
  /setRecipesEnabled\(config\?\.features\?\.recipes_enabled === true\)/,
  "prompt library must keep its basic tabs while gating creation recipes by server config",
);
assert.match(
  prompts,
  /\{recipesEnabled && \(\s*<button[\s\S]*?>\s*创作配方/,
  "launch-lite must hide the creation recipe tab without hiding the prompt library page",
);
assert.match(
  recharge,
  /!paymentEnabled[\s\S]*ACCOUNT_TABS\.filter\(\(tab\) => tab\.id === "billing"\)/,
  "disabled payments must leave only the credit ledger tab visible",
);
assert.match(admin, /launchLite[\s\S]*key !== "payments"/, "launch-lite admin must hide payment settings");
assert.match(
  adminSettings,
  /payment_enabled: launchLite \? false : !!s\.payment_enabled/,
  "launch-lite admin settings must keep payments disabled",
);

console.log("launch-lite frontend feature gates passed");
