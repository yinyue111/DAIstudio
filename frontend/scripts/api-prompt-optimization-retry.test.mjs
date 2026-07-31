import assert from "node:assert/strict";

import {
  ApiError,
  createStudioPromptOptimizationRequest,
  isTransientNetworkError,
  retryTransientNetworkOperation,
} from "../lib/api.js";

assert.equal(isTransientNetworkError(new TypeError("Failed to fetch")), true);
assert.equal(isTransientNetworkError(new TypeError("Load failed")), true);
assert.equal(isTransientNetworkError(new Error("Network request failed")), true);
assert.equal(isTransientNetworkError(new ApiError("bad request", { status: 400 })), false);
assert.equal(isTransientNetworkError(Object.assign(new Error("aborted"), { name: "AbortError" })), false);

const waits = [];
let attempts = 0;
const recovered = await retryTransientNetworkOperation(
  async () => {
    attempts += 1;
    if (attempts < 3) throw new TypeError("Failed to fetch");
    return { proposal_id: 42 };
  },
  {
    delaysMs: [20, 40],
    wait: async (delayMs) => waits.push(delayMs),
  },
);
assert.deepEqual(recovered, { proposal_id: 42 });
assert.equal(attempts, 3);
assert.deepEqual(waits, [20, 40]);

const originalFetch = globalThis.fetch;
try {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: options?.body });
    if (requests.length === 1) throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify({ proposal_id: 7 }), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  };
  const body = {
    prompt: "keep the product unchanged",
    idempotency_key: "studio-action-stable-request",
  };
  const result = await createStudioPromptOptimizationRequest(body, {
    delaysMs: [1],
    wait: async () => undefined,
  });
  assert.equal(result.proposal_id, 7);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body, requests[1].body, "a retry must reuse the exact idempotent request");

  let callsWithoutKey = 0;
  globalThis.fetch = async () => {
    callsWithoutKey += 1;
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(
    createStudioPromptOptimizationRequest(
      { prompt: "missing request key" },
      { delaysMs: [1, 1], wait: async () => undefined },
    ),
    (error) => {
      assert.match(error.message, /暂时无法连接到服务/);
      assert.doesNotMatch(error.message, /已自动重试/);
      return true;
    },
  );
  assert.equal(callsWithoutKey, 1, "non-idempotent POST requests must never retry automatically");

  let canceledCalls = 0;
  const controller = new AbortController();
  globalThis.fetch = async () => {
    canceledCalls += 1;
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(
    createStudioPromptOptimizationRequest(
      {
        prompt: "cancel this stale optimization",
        idempotency_key: "studio-action-canceled-request",
      },
      {
        delaysMs: [1, 1],
        signal: controller.signal,
        wait: async () => controller.abort(),
      },
    ),
    (error) => error?.name === "AbortError",
  );
  assert.equal(
    canceledCalls,
    1,
    "editing the prompt after the first network failure must cancel every pending retry",
  );
} finally {
  globalThis.fetch = originalFetch;
}

console.log("prompt optimization network retry tests passed");
