import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

process.env.TZ = "Asia/Shanghai";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const modulePath = join(root, "lib/datetime.js");

assert.equal(existsSync(modulePath), true, "a shared API datetime formatter should exist");

const datetime = await import(pathToFileURL(modulePath));
const { formatLocalDateTime } = datetime;

assert.equal(formatLocalDateTime("2026-07-10T08:59:27+00:00"), "2026-07-10 16:59");
assert.equal(
  formatLocalDateTime("2026-07-10T08:59:27Z", { includeSeconds: true }),
  "2026-07-10 16:59:27",
);
assert.equal(
  formatLocalDateTime("2026-07-10T08:59:27"),
  "2026-07-10 16:59",
  "legacy API timestamps without an offset should still be interpreted as UTC",
);
assert.equal(formatLocalDateTime("not-a-date"), "-");
assert.equal(formatLocalDateTime(""), "-");

assert.equal(typeof datetime.localDateRangeToIsoBounds, "function");
assert.equal(typeof datetime.localDateRangeToProfileAssetParams, "function");

const shanghaiBounds = datetime.localDateRangeToIsoBounds("2026-07-10", "2026-07-10");
assert.deepEqual(shanghaiBounds, {
  startInclusive: "2026-07-09T16:00:00.000Z",
  endExclusive: "2026-07-10T16:00:00.000Z",
});
assert.equal(
  new Date(shanghaiBounds.endExclusive).getTime() - new Date(shanghaiBounds.startInclusive).getTime(),
  24 * 60 * 60 * 1000,
  "Asia/Shanghai local-day bounds should describe exactly that local calendar day",
);
assert.deepEqual(
  datetime.localDateRangeToProfileAssetParams("2026-07-10", "2026-07-10"),
  {
    created_from: "2026-07-09T16:00:00.000Z",
    created_to: "2026-07-10T15:59:59.999Z",
  },
  "the existing <= backend end boundary must not include the next local midnight",
);

process.env.TZ = "America/New_York";
const springForwardBounds = datetime.localDateRangeToIsoBounds("2026-03-08", "2026-03-08");
assert.deepEqual(springForwardBounds, {
  startInclusive: "2026-03-08T05:00:00.000Z",
  endExclusive: "2026-03-09T04:00:00.000Z",
});
assert.equal(
  new Date(springForwardBounds.endExclusive).getTime() - new Date(springForwardBounds.startInclusive).getTime(),
  23 * 60 * 60 * 1000,
  "local Date construction should honor a DST spring-forward day",
);
assert.equal(
  datetime.localDateRangeToProfileAssetParams("2026-03-08", "2026-03-08").created_to,
  "2026-03-09T03:59:59.999Z",
);
process.env.TZ = "Asia/Shanghai";

assert.deepEqual(datetime.localDateRangeToIsoBounds("", ""), {
  startInclusive: "",
  endExclusive: "",
});
assert.deepEqual(datetime.localDateRangeToIsoBounds("2026-02-30", "2026-02-30"), {
  startInclusive: "",
  endExclusive: "",
});

const historySource = readFileSync(join(root, "app/history/page.jsx"), "utf8");
const taskCenterSource = readFileSync(join(root, "components/GlobalTaskCenter.jsx"), "utf8");
const auditSource = readFileSync(join(root, "app/admin/components/reports-audit.jsx"), "utf8");

assert.match(historySource, /UnifiedTaskList/, "history should render timestamps through the shared task list");
assert.match(taskCenterSource, /formatLocalDateTime\(task\.created_at\)/, "the shared task list should format unified timestamps locally");
assert.match(auditSource, /formatLocalDateTime\(r\.created_at, \{ includeSeconds: true \}\)/);
console.log("datetime formatting tests passed");
