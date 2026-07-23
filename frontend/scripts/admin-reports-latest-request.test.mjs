import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createLatestRequestGate } from "../app/admin/components/latest-request.js";
import {
  buildReverseReportQuery,
  updateReverseReportFilters,
} from "../app/admin/components/reverse-report-filters.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

async function runDashboardBatch(gate, requests, label, commits, errors) {
  const generation = gate.begin();
  try {
    await Promise.all(requests.map((request) => request.promise));
    if (gate.isCurrent(generation)) commits.push(label);
  } catch (error) {
    if (gate.isCurrent(generation)) errors.push(error.message);
  }
}

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

test("a slower unfiltered dashboard batch cannot replace newer filtered quality data", async () => {
  const gate = createLatestRequestGate();
  const commits = [];
  const errors = [];
  const unfiltered = [deferred(), deferred(), deferred()];
  const filtered = [deferred(), deferred(), deferred()];

  const staleRun = runDashboardBatch(gate, unfiltered, "unfiltered", commits, errors);
  const currentRun = runDashboardBatch(gate, filtered, "image/product_ad", commits, errors);

  filtered[2].resolve({ filters: { selected: { media_type: "image", focus: "product_ad" } } });
  filtered[0].resolve({ summary: {} });
  filtered[1].resolve({ models: [] });
  await currentRun;

  unfiltered[0].resolve({ summary: {} });
  unfiltered[1].resolve({ models: [] });
  unfiltered[2].resolve({ filters: { selected: {} } });
  await staleRun;

  assert.deepEqual(commits, ["image/product_ad"]);
  assert.deepEqual(errors, []);
});

test("a stale dashboard failure cannot replace the latest successful filter state", async () => {
  const gate = createLatestRequestGate();
  const commits = [];
  const errors = [];
  const stale = [deferred(), deferred(), deferred()];
  const current = [deferred(), deferred(), deferred()];

  const staleRun = runDashboardBatch(gate, stale, "stale", commits, errors);
  const currentRun = runDashboardBatch(gate, current, "current", commits, errors);
  current.forEach((request) => request.resolve({}));
  await currentRun;
  stale[0].reject(new Error("old request failed"));
  stale[1].resolve({});
  stale[2].resolve({});
  await staleRun;

  assert.deepEqual(commits, ["current"]);
  assert.deepEqual(errors, []);
});

test("dashboard report and audit all use latest-only response gates", () => {
  const source = readFileSync(join(root, "app/admin/components/reports-audit.jsx"), "utf8");
  assert.match(source, /createLatestRequestGate/);
  assert.equal((source.match(/requestGateRef/g) || []).length >= 6, true);
});

test("reverse quality filters, dimensions, economics guard, and CSV share the dashboard contract", () => {
  const source = readFileSync(join(root, "app/admin/components/reports-audit.jsx"), "utf8");
  assert.match(source, /changeReverseFilter\("mediaType", e\.target\.value\)/);
  assert.match(source, /changeReverseFilter\("model", e\.target\.value\)/);
  assert.match(source, /changeReverseFilter\("focus", e\.target\.value\)/);
  assert.match(source, /reverseQs\(null, "csv"\)/);
  assert.match(source, /QUALITY_DIMENSIONS[\s\S]*?"overall"[\s\S]*?"media"[\s\S]*?"model"[\s\S]*?"focus"/);
  assert.match(source, /cost_status === "complete"/);
  assert.match(source, /min-w-\[94rem\][\s\S]*?overflow-x-auto|overflow-x-auto[\s\S]*?min-w-\[94rem\]/);
});

test("changing one reverse quality filter preserves and submits the complete filter snapshot", () => {
  const current = { mediaType: "video", model: "vision-pro", focus: "storyboard" };
  const imageFilters = updateReverseReportFilters(current, "mediaType", "image");
  const modelFilters = updateReverseReportFilters(imageFilters, "model", "vision-fast");
  const focusFilters = updateReverseReportFilters(modelFilters, "focus", "product_ad");

  assert.deepEqual(imageFilters, {
    mediaType: "image",
    model: "vision-pro",
    focus: "storyboard",
  });
  assert.deepEqual(focusFilters, {
    mediaType: "image",
    model: "vision-fast",
    focus: "product_ad",
  });
  assert.equal(
    buildReverseReportQuery({
      start: "2026-07-01",
      end: "2026-07-20",
      ...focusFilters,
    }),
    "?start=2026-07-01&end=2026-07-20&media_type=image&model=vision-fast&focus=product_ad",
  );
});

test("reverse quality CSV uses the same active filters as the JSON report", () => {
  const filters = {
    start: "2026-07-01",
    end: "2026-07-20",
    mediaType: "video",
    model: "vision pro/2",
    focus: "camera_motion",
  };

  assert.equal(
    buildReverseReportQuery(filters, "csv"),
    "?start=2026-07-01&end=2026-07-20&media_type=video&model=vision+pro%2F2&focus=camera_motion&format=csv",
  );
});

test("reverse quality API declarations preserve nullable margin semantics", () => {
  const declarations = readFileSync(join(root, "lib/api.d.ts"), "utf8");
  assert.match(declarations, /AdminReverseCostStatus = "complete" \| "partial" \| "unavailable"/);
  assert.match(declarations, /gross_profit_credits: number \| null/);
  assert.match(declarations, /gross_margin_rate: number \| null/);
  assert.match(declarations, /adminReverseUsage\(qs\?: string\): Promise<AdminReverseUsageResponse>/);
});
