import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createAbortableRequestRegistry } from "../lib/abortableRequestRegistry.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));

test("owner reset aborts every active media upload and clears the registry", () => {
  const registry = createAbortableRequestRegistry();
  const first = registry.capture();
  const second = registry.capture();
  registry.abortAll();

  assert.equal(first.signal.aborted, true);
  assert.equal(second.signal.aborted, true);
  assert.equal(registry.size(), 0);
});

test("released uploads are not aborted by a later owner reset", () => {
  const registry = createAbortableRequestRegistry();
  const request = registry.capture();
  request.release();
  registry.abortAll();
  assert.equal(request.signal.aborted, false);
});

test("caller abort signal reaches upload fetch and is not reported as a timeout", async () => {
  process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
  globalThis.window = {
    location: new URL("http://127.0.0.1:3000/"),
    localStorage: { removeItem() {} },
  };
  let observedSignal = null;
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    observedSignal = options.signal;
    options.signal.addEventListener("abort", () => {
      reject(new DOMException("owner changed", "AbortError"));
    }, { once: true });
  });

  const { api } = await import(`../lib/api.js?upload-abort=${Date.now()}`);
  const controller = new AbortController();
  const pending = api.uploadImage(new Blob(["x"], { type: "image/png" }), {
    signal: controller.signal,
  });
  await Promise.resolve();
  controller.abort();

  await assert.rejects(pending, (error) => {
    assert.equal(error.name, "AbortError");
    assert.doesNotMatch(error.message, /请求超时/);
    return true;
  });
  assert.equal(observedSignal?.aborted, true);
});

test("caller abort signal reaches video upload fetch and is not reported as a timeout", async () => {
  process.env.NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000";
  globalThis.window = {
    location: new URL("http://127.0.0.1:3000/"),
    localStorage: { removeItem() {} },
  };
  let observedSignal = null;
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    observedSignal = options.signal;
    options.signal.addEventListener("abort", () => {
      reject(new DOMException("owner changed", "AbortError"));
    }, { once: true });
  });

  const { api } = await import(`../lib/api.js?video-upload-abort=${Date.now()}`);
  const controller = new AbortController();
  const pending = api.uploadVideo(new Blob(["x"], { type: "video/mp4" }), {
    signal: controller.signal,
  });
  await Promise.resolve();
  controller.abort();

  await assert.rejects(pending, (error) => {
    assert.equal(error.name, "AbortError");
    assert.doesNotMatch(error.message, /请求超时/);
    return true;
  });
  assert.equal(observedSignal?.aborted, true);
});

test("media upload hook aborts active requests on owner reset and unmount", () => {
  const source = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
  assert.match(source, /activeUploadRequestsRef\.current\.abortAll\(\)/);
  assert.match(source, /useEffect\(\(\) => \(\) => \{\s*resetOwnerMediaUpload\(\);\s*\}, \[\]\)/s);
});
