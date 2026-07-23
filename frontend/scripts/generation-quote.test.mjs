import assert from "node:assert/strict";

import {
  clearPendingStudioActionRequest,
  confirmedRequestPayload,
  PENDING_STUDIO_ACTION_STORAGE_KEY,
  pendingStudioActionRequestId,
  quotePayload,
  requestAuthoritativeStudioQuote,
  shouldRefreshGenerationQuote,
  studioQuoteEnvelope,
} from "../app/studio/generationQuote.ts";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

const payload = {
  category: "image",
  stage: "preview",
  model_config_id: 7,
  client_request_id: "studio-preview-idempotent-quote",
  prompt: { final_text: "产品静物" },
  params: { n: 1, size: "1024x1024" },
};

assert.deepEqual(
  quotePayload({ ...payload, quote_id: 999 }),
  payload,
  "quote requests must never accidentally reuse a stale quote id",
);
assert.equal(shouldRefreshGenerationQuote({ detail: { code: "QUOTE_EXPIRED" } }), true);
assert.equal(shouldRefreshGenerationQuote({ detail: { code: "QUOTE_MISMATCH" } }), true);
assert.equal(shouldRefreshGenerationQuote({ detail: { code: "QUOTE_REPRICED" } }), true);
assert.equal(shouldRefreshGenerationQuote({ detail: { code: "QUOTE_CONSUMED" } }), false);

{
  const mutableRequest = {
    client_request_id: "studio-deep-snapshot-quote",
    params: { n: 1, tags: ["original"] },
    input: { composition: { shots: [{ id: "shot-1", duration: 3 }] } },
  };
  const envelope = studioQuoteEnvelope("workflow", mutableRequest);
  mutableRequest.params.n = 4;
  mutableRequest.params.tags.push("changed-after-quote");
  mutableRequest.input.composition.shots[0].duration = 9;
  assert.deepEqual(envelope.request, {
    client_request_id: "studio-deep-snapshot-quote",
    input: { composition: { shots: [{ duration: 3, id: "shot-1" }] } },
    params: { n: 1, tags: ["original"] },
  }, "confirmation must execute the exact nested request snapshot that was quoted");
  assert.throws(
    () => studioQuoteEnvelope("workflow", mutableRequest, "studio-different-request-id"),
    /client_request_id 不一致/,
  );
}

{
  const storage = memoryStorage();
  const request = {
    prompt: "private prompt text must not be persisted",
    mode: "faithful",
    target_model_config_id: 9,
  };
  const firstPageRef = { current: null };
  const firstId = pendingStudioActionRequestId(
    firstPageRef,
    "prompt-optimization:7:image",
    request,
    { storage, now: () => 1_000, randomId: () => "first" },
  );
  const refreshedPageRef = { current: null };
  const replayId = pendingStudioActionRequestId(
    refreshedPageRef,
    "prompt-optimization:7:image",
    { ...request, quote_id: 99, idempotency_key: "ignored-for-signature" },
    { storage, now: () => 2_000, randomId: () => "must-not-run" },
  );
  assert.equal(replayId, firstId, "a refresh or retry must reuse the paid action id");
  assert.doesNotMatch(
    storage.getItem(PENDING_STUDIO_ACTION_STORAGE_KEY) || "",
    /private prompt text/,
    "pending id storage must not persist prompt contents",
  );
  clearPendingStudioActionRequest(refreshedPageRef, firstId, { storage, now: () => 3_000 });
  const completedPageRef = { current: null };
  const nextId = pendingStudioActionRequestId(
    completedPageRef,
    "prompt-optimization:7:image",
    request,
    { storage, now: () => 4_000, randomId: () => "second" },
  );
  assert.notEqual(nextId, firstId, "a confirmed success clears the pending id for a new action");
}

{
  const clientRequestId = "studio-action-prompt-paid";
  const promptRequest = {
    prompt: "commercial product photo",
    mode: "commercial",
    target_model_config_id: 9,
    optimizer_model_config_id: 11,
    idempotency_key: clientRequestId,
  };
  const envelope = studioQuoteEnvelope(
    "prompt_optimization",
    promptRequest,
    clientRequestId,
  );
  assert.equal(envelope.client_request_id, promptRequest.idempotency_key);
  assert.equal(
    envelope.request.client_request_id,
    undefined,
    "extra=forbid prompt execution bodies must not receive client_request_id",
  );
  assert.deepEqual(
    studioQuoteEnvelope("asset_unlock", { asset_id: 42 }, "studio-action-unlock-42").request,
    { asset_id: 42 },
    "asset unlock quote bodies contain only the asset id",
  );
}

{
  const quoteRequests = [];
  const generateRequests = [];
  const quoteApi = {
    quote: async (request) => {
      quoteRequests.push(request);
      return {
        id: 41,
        quote_id: 41,
        estimated_credits: 23,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        price_breakdown: { generation: { label: "图片生成", credits: 23 } },
      };
    },
    studioQuote: async () => { throw new Error("generation must keep the flat quote contract"); },
  };
  const envelope = studioQuoteEnvelope("generation", payload);
  const quote = await requestAuthoritativeStudioQuote(quoteApi, envelope, 100);
  assert.equal(generateRequests.length, 0, "requesting a quote must not execute the paid operation");
  assert.equal(quote.totalCredits, 23, "the returned server quote is the authoritative price");
  assert.equal(quote.balanceBefore, 100);
  assert.equal(quote.balanceAfter, 77);
  assert.equal(quote.breakdown[0].credits, 23);
  assert.equal(quoteRequests.length, 1);
  assert.equal(quoteRequests[0].client_request_id, payload.client_request_id);
  const confirmed = confirmedRequestPayload(envelope.request, quote);
  generateRequests.push(confirmed);
  assert.equal(generateRequests.length, 1, "the paid operation runs only after explicit confirmation");
  assert.equal(generateRequests[0].quote_id, 41);
  assert.equal(payload.quote_id, undefined, "the normalized payload must remain quote-free for retries");
}

{
  let quoteCalls = 0;
  const executedQuoteIds = [];
  const quoteApi = {
    quote: async (request) => {
      quoteCalls += 1;
      assert.equal(request.quote_id, undefined);
      return {
        id: quoteCalls,
        quote_id: quoteCalls,
        estimated_credits: 20 + quoteCalls,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        price_breakdown: {},
      };
    },
    studioQuote: async () => { throw new Error("generation must keep the flat quote contract"); },
  };
  const envelope = studioQuoteEnvelope("generation", payload);
  let quote = await requestAuthoritativeStudioQuote(quoteApi, envelope);
  const execute = async (request) => {
    executedQuoteIds.push(request.quote_id);
    if (request.quote_id === 1) {
      throw Object.assign(new Error("报价已过期"), { detail: { code: "QUOTE_EXPIRED" } });
    }
    return { id: 1002, category: "image", stage: "preview", status: "queued" };
  };
  try {
    await execute(confirmedRequestPayload(envelope.request, quote));
    assert.fail("the first execution should force a refreshed quote");
  } catch (error) {
    assert.equal(shouldRefreshGenerationQuote(error), true);
  }
  quote = await requestAuthoritativeStudioQuote(quoteApi, envelope);
  assert.equal(quoteCalls, 2, "an expired quote should be refreshed once");
  assert.deepEqual(executedQuoteIds, [1], "refreshing must never auto-replay the paid operation");
  const task = await execute(confirmedRequestPayload(envelope.request, quote));
  assert.equal(task.id, 1002);
  assert.deepEqual(executedQuoteIds, [1, 2], "the refreshed price requires a second explicit confirmation");
}

{
  const envelope = studioQuoteEnvelope("workflow", {
    tool_slug: "video-composition",
    client_request_id: "workflow-quote-request",
    input: { composition: { shots: [] } },
  });
  let received = null;
  const quote = await requestAuthoritativeStudioQuote({
    quote: async () => { throw new Error("workflow must use the unified envelope"); },
    studioQuote: async (request) => {
      received = request;
      return {
        quote_id: 88,
        total_credits: 12,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        price_breakdown: {
          items: [{ code: "compose", label: "视频合成", credits: 12 }],
          total_credits: 12,
        },
        balance_after_estimate: -2,
        warnings: [{ code: "BALANCE_LOW", message: "余额不足，请先充值。" }],
      };
    },
  }, envelope);
  assert.deepEqual(received, envelope);
  assert.equal(quote.kind, "workflow");
  assert.equal(quote.totalCredits, 12);
  assert.equal(quote.breakdown.length, 1);
  assert.equal(quote.breakdown[0].credits, 12);
  assert.equal(quote.balanceBefore, 10, "an authoritative remaining balance should imply the pre-charge balance");
  assert.equal(quote.balanceAfter, -2);
  assert.equal(quote.balanceSource, "quote");
  assert.equal(quote.affordable, false, "a negative remaining balance must block confirmation");
  assert.deepEqual(quote.warnings, ["余额不足，请先充值。"]);
  assert.equal(confirmedRequestPayload(envelope.request, quote).quote_id, 88);
}

{
  const envelope = studioQuoteEnvelope("generation", payload);
  const quote = await requestAuthoritativeStudioQuote({
    quote: async () => ({
      quote_id: 89,
      estimated_credits: 3,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      price_breakdown: {},
    }),
    studioQuote: async () => { throw new Error("generation must keep the flat quote contract"); },
  }, envelope, null);
  assert.equal(quote.balanceBefore, null, "an unavailable account balance must not be coerced to zero");
  assert.equal(quote.balanceAfter, null);
  assert.equal(quote.affordable, null);
  assert.equal(quote.balanceSource, "unavailable");
}

{
  const envelope = studioQuoteEnvelope(
    "asset_unlock",
    { asset_id: 42 },
    "studio-action-consumed-unlock",
  );
  const quote = await requestAuthoritativeStudioQuote({
    quote: async () => { throw new Error("asset unlock must use the unified envelope"); },
    studioQuote: async () => ({
      quote_id: 91,
      status: "consumed",
      total_credits: 8,
      expires_at: new Date(Date.now() - 60_000).toISOString(),
      balance_after_estimate: -6,
      affordable: false,
      price_breakdown: {},
    }),
  }, envelope, 2);
  assert.equal(quote.affordable, true, "a consumed quote must allow an idempotent result replay");
  assert.equal(quote.balanceBefore, 2);
  assert.equal(quote.balanceAfter, 2, "replaying a consumed quote must not imply another debit");
  assert.equal(quote.balanceSource, "account_snapshot");
}

console.log("generation quote tests passed");
