import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, "app/admin/components/payments-settings.jsx"), "utf8");
const reviewSource = readFileSync(join(root, "app/admin/components/model-review-moderation.jsx"), "utf8");

assert.match(
  source,
  /const\s+envManaged\s*=\s*p\.source\s*===\s*"env"/,
  "payment provider cards should derive an env-managed read-only state",
);
assert.match(
  source,
  /停用覆盖/,
  "env-managed payment providers should expose an explicit emergency disable override",
);
assert.match(
  source,
  /current\?\.source\s*===\s*"env"[\s\S]*不能在后台直接覆盖/,
  "saveProvider should guard against saving env-managed providers",
);
assert.match(
  source,
  /disabled=\{!!editingPackageId\}/,
  "payment package edit mode should lock the package id",
);
assert.match(
  source,
  /const\s+normalizedPackage\s*=\s*\{/,
  "payment package save should normalize numeric values before confirmation",
);
assert.match(
  source,
  /paymentPackageDiff\(existing,\s*normalizedPackage\)/,
  "payment package edits should show a diff confirmation",
);
assert.match(
  source,
  /确认新建并启用套餐/,
  "new enabled payment packages should require a strong confirmation",
);
assert.match(
  reviewSource,
  /confirmReviewTaskAction\(task,\s*"退款关闭"/,
  "review task refund should confirm task details before password entry",
);
assert.match(
  reviewSource,
  /confirmReviewTaskAction\(task,\s*"补结果结算"/,
  "review task settlement should confirm task details before password entry",
);
assert.match(
  source,
  /当前渠道由环境变量生效/,
  "admin UI should explain why env-managed providers are read-only",
);

const navSource = readFileSync(join(root, "components/Nav.jsx"), "utf8");
assert.match(navSource, /aria-label=\{`可用积分/, "desktop credit badge should expose an accessible label");
assert.match(navSource, />可用<\/span>/, "desktop credit badge should label available credits");

console.log("admin payment safety test passed");
