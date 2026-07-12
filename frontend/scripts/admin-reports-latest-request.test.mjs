import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createLatestRequestGate } from "../app/admin/components/latest-request.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));

test("latest admin report response wins when filtered requests resolve out of order", () => {
  const gate = createLatestRequestGate();
  const unfiltered = gate.begin();
  const filtered = gate.begin();

  assert.equal(gate.isCurrent(unfiltered), false);
  assert.equal(gate.isCurrent(filtered), true);
});

test("unmounted admin reports reject every pending response", () => {
  const gate = createLatestRequestGate();
  const pending = gate.begin();
  gate.invalidate();
  assert.equal(gate.isCurrent(pending), false);
});

test("dashboard report and audit all use latest-only response gates", () => {
  const source = readFileSync(join(root, "app/admin/components/reports-audit.jsx"), "utf8");
  assert.match(source, /createLatestRequestGate/);
  assert.equal((source.match(/requestGateRef/g) || []).length >= 6, true);
});
