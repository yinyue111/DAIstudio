import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const helperPath = join(root, "app/recharge/polling.js");
const polling = existsSync(helperPath) ? await import(pathToFileURL(helperPath)) : {};
const packageJson = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function fakeTimers() {
  let nextId = 1;
  const callbacks = new Map();
  return {
    callbacks,
    setTimer(callback) {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    clearTimer(id) {
      callbacks.delete(id);
    },
    async runNext() {
      const entry = callbacks.entries().next().value;
      assert.ok(entry, "expected a scheduled payment poll");
      const [id, callback] = entry;
      callbacks.delete(id);
      await callback();
    },
  };
}

test("recharge polling uses a serial timeout loop instead of async setInterval", () => {
  const source = readFileSync(join(root, "app/recharge/page.jsx"), "utf8");
  assert.doesNotMatch(
    source,
    /pollRef\.current\s*=\s*setInterval/,
    "an async setInterval can overlap payment status requests",
  );
  assert.match(source, /createPaymentOrderPoller/, "recharge should use the tested serial poller");
});

test("payment polling never schedules another request while one is in flight", async () => {
  assert.equal(typeof polling.createPaymentOrderPoller, "function");
  const timers = fakeTimers();
  const first = deferred();
  let requestCount = 0;
  const poller = polling.createPaymentOrderPoller({
    request: () => {
      requestCount += 1;
      return first.promise;
    },
    onResult: () => true,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    intervalMs: 10,
  });

  assert.equal(timers.callbacks.size, 1);
  const inFlight = timers.runNext();
  await Promise.resolve();
  assert.equal(requestCount, 1);
  assert.equal(timers.callbacks.size, 0, "no timer should exist until the request settles");

  first.resolve({ order_no: "A", status: "pending" });
  await inFlight;
  assert.equal(timers.callbacks.size, 1, "pending results should schedule exactly one later poll");
  poller.stop();
});

test("paid is terminal and stops polling", async () => {
  assert.equal(typeof polling.createPaymentOrderPoller, "function");
  const timers = fakeTimers();
  const results = [];
  polling.createPaymentOrderPoller({
    request: async () => ({ order_no: "A", status: "paid" }),
    onResult: (order) => {
      results.push(order.status);
      return order.status === "pending";
    },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    intervalMs: 10,
  });

  await timers.runNext();
  assert.deepEqual(results, ["paid"]);
  assert.equal(timers.callbacks.size, 0, "a paid order must not schedule another poll");
});

test("stopping a poller invalidates its in-flight response", async () => {
  assert.equal(typeof polling.createPaymentOrderPoller, "function");
  const timers = fakeTimers();
  const response = deferred();
  const results = [];
  const poller = polling.createPaymentOrderPoller({
    request: () => response.promise,
    onResult: (order) => {
      results.push(order);
      return true;
    },
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    intervalMs: 10,
  });

  const inFlight = timers.runNext();
  await Promise.resolve();
  poller.stop();
  response.resolve({ order_no: "old-order", status: "pending" });
  await inFlight;

  assert.deepEqual(results, [], "an unmounted or replaced order must ignore its old response");
  assert.equal(timers.callbacks.size, 0);
});

test("payment order state cannot regress after reaching a terminal status", () => {
  assert.equal(typeof polling.selectMonotonicPaymentOrder, "function");
  const paid = { order_no: "A", status: "paid", marker: "new" };
  const stalePending = { order_no: "A", status: "pending", marker: "old" };
  const closed = { order_no: "B", status: "closed", marker: "new" };

  assert.equal(polling.selectMonotonicPaymentOrder(paid, stalePending), paid);
  assert.equal(polling.selectMonotonicPaymentOrder(closed, { ...stalePending, order_no: "B" }), closed);
  assert.equal(polling.selectMonotonicPaymentOrder(stalePending, paid), paid);
});

test("payment order list refresh preserves newer terminal statuses from stale responses", () => {
  assert.equal(typeof polling.selectMonotonicPaymentOrders, "function");
  const current = [
    { order_no: "A", status: "paid", marker: "new" },
    { order_no: "removed", status: "pending" },
  ];
  const incoming = [
    { order_no: "A", status: "pending", marker: "old" },
    { order_no: "B", status: "pending", marker: "incoming" },
  ];

  assert.deepEqual(polling.selectMonotonicPaymentOrders(current, incoming), [
    current[0],
    incoming[1],
  ]);
});

test("a terminal event for another order must not stop the active order poller", () => {
  assert.equal(typeof polling.shouldStopPaymentOrderPoll, "function");
  assert.equal(
    polling.shouldStopPaymentOrderPoll(
      { order_no: "active", status: "pending" },
      { order_no: "other", status: "paid" },
    ),
    false,
  );
  assert.equal(
    polling.shouldStopPaymentOrderPoll(
      { order_no: "active", status: "pending" },
      { order_no: "active", status: "paid" },
    ),
    true,
  );
});

test("the standard frontend unit suite executes recharge polling regressions", () => {
  assert.match(packageJson.scripts["test:unit"], /recharge-polling\.test\.mjs/);
});
