import assert from "node:assert/strict";
import {
  clampVisibleItemCount,
  getVisibleItemWindow,
  nextVisibleItemCount,
} from "../hooks/useVisibleItemWindow.js";

const items = Array.from({ length: 75 }, (_, index) => ({ id: index + 1 }));

assert.equal(clampVisibleItemCount(10, 20), 10);
assert.equal(clampVisibleItemCount(10, -1), 0);

const firstWindow = getVisibleItemWindow(items, 48);
assert.equal(firstWindow.totalCount, 75);
assert.equal(firstWindow.visibleCount, 48);
assert.equal(firstWindow.hasMore, true);
assert.equal(firstWindow.items.length, 48);
assert.equal(firstWindow.items[0].id, 1);
assert.equal(firstWindow.items.at(-1).id, 48);

assert.equal(nextVisibleItemCount(75, 48, 24), 72);
assert.equal(nextVisibleItemCount(75, 72, 24), 75);

const emptyWindow = getVisibleItemWindow(null, 48);
assert.equal(emptyWindow.totalCount, 0);
assert.equal(emptyWindow.visibleCount, 0);
assert.equal(emptyWindow.hasMore, false);
assert.deepEqual(emptyWindow.items, []);

console.log("visible item window tests passed");
