import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readStudioSourceFromUrl } from "./studio-source.mjs";

import {
  beginQuoteConfirmationExecution,
  canInvalidateQuoteConfirmation,
  createQuoteConfirmationAction,
  endQuoteConfirmationExecution,
  executeQuoteActionAutomatically,
  quoteConfirmationBusyResult,
  settleQuoteConfirmationAction,
} from "../app/studio/quoteConfirmationAction.js";

const hookSource = readFileSync(new URL("../hooks/useStudioQuoteConfirmation.js", import.meta.url), "utf8");
const compositionSource = readFileSync(new URL("../app/studio/StudioVideoComposition.jsx", import.meta.url), "utf8");
const pageSource = readStudioSourceFromUrl(import.meta.url);
const promptOptimizationSource = readFileSync(new URL("../hooks/usePromptOptimization.js", import.meta.url), "utf8");
const assetActionsSource = readFileSync(new URL("../hooks/useAssetActions.js", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../lib/api.js", import.meta.url), "utf8");

const settlements = [];
const action = createQuoteConfirmationAction({
  envelope: { kind: "generation", client_request_id: "quote-action-test", request: {} },
  execute: async () => ({ id: 1 }),
  resolve: (value) => settlements.push(value),
  reject: () => undefined,
});

assert.equal(canInvalidateQuoteConfirmation(action), true);
assert.equal(beginQuoteConfirmationExecution(action), true);
assert.equal(canInvalidateQuoteConfirmation(action), false, "input changes must not clear an in-flight execution");
assert.equal(beginQuoteConfirmationExecution(action), false, "double confirmation must not execute twice");

endQuoteConfirmationExecution(action);
assert.equal(canInvalidateQuoteConfirmation(action), true, "repricing releases the execution lock for a second confirmation");
assert.equal(beginQuoteConfirmationExecution(action), true, "the refreshed quote can be confirmed explicitly");
endQuoteConfirmationExecution(action);

assert.equal(settleQuoteConfirmationAction(action, { status: "executed" }), true);
assert.equal(settleQuoteConfirmationAction(action, { status: "executed-again" }), false);
assert.deepEqual(settlements, [{ status: "executed" }], "one action promise settles exactly once");
assert.equal(canInvalidateQuoteConfirmation(action), false);

const busyResult = quoteConfirmationBusyResult();
assert.equal(busyResult.status, "execution_failed");
assert.match(busyResult.error.message, /已有操作正在提交/);
assert.match(
  hookSource,
  /previous\?\.executing[\s\S]*quoteConfirmationBusyResult\(\)/,
  "a concurrent paid action must return a visible error instead of being ignored",
);

assert.match(
  hookSource,
  /balanceCredits === null \|\| balanceCredits === undefined \|\| balanceCredits === ""/,
  "missing account balances must remain unavailable instead of becoming zero",
);
assert.match(hookSource, /executeQuoteActionAutomatically/, "paid actions should execute automatically after server validation");
assert.doesNotMatch(hookSource, /open:\s*true|useState\(/, "the shared paid-action hook must not open confirmation UI");
assert.doesNotMatch(
  pageSource,
  /import\s+StudioQuoteConfirmation|<StudioQuoteConfirmation/,
  "Studio must not import or mount a quote confirmation dialog",
);

const automaticSettlements = [];
const automaticRequests = [];
const automaticAction = createQuoteConfirmationAction({
  envelope: {
    kind: "generation",
    client_request_id: "automatic-paid-action",
    request: { client_request_id: "automatic-paid-action", prompt: "cup" },
  },
  execute: async ({ request }) => {
    automaticRequests.push(request);
    return { id: 42 };
  },
  resolve: (value) => automaticSettlements.push(value),
  reject: () => undefined,
});
await executeQuoteActionAutomatically({
  action: automaticAction,
  loadQuote: async () => ({ id: 91, affordable: true }),
  buildRequest: (request, quote) => ({ ...request, quote_id: quote.id }),
});
assert.equal(automaticRequests.length, 1, "one click should submit exactly one paid action");
assert.equal(automaticRequests[0].quote_id, 91, "the silent server validation token must be attached to execution");
assert.equal(automaticSettlements[0].status, "executed");
assert.equal(automaticSettlements[0].result.id, 42);

let insufficientExecutions = 0;
const insufficientSettlements = [];
const insufficientAction = createQuoteConfirmationAction({
  envelope: { kind: "generation", client_request_id: "insufficient", request: {} },
  execute: async () => { insufficientExecutions += 1; },
  resolve: (value) => insufficientSettlements.push(value),
  reject: () => undefined,
});
await executeQuoteActionAutomatically({
  action: insufficientAction,
  loadQuote: async () => ({ id: 92, affordable: false }),
  buildRequest: (request) => request,
  quoteError: (quote) => quote.affordable === false ? new Error("积分不足") : null,
});
assert.equal(insufficientExecutions, 0, "insufficient balances must fail before execution");
assert.equal(insufficientSettlements[0].status, "quote_failed");

const retryStart = compositionSource.indexOf("async function retryRun()");
const retryEnd = compositionSource.indexOf("async function downloadResult()", retryStart);
assert.notEqual(retryStart, -1);
assert.notEqual(retryEnd, -1);
const retrySource = compositionSource.slice(retryStart, retryEnd);
assert.match(retrySource, /api\.retryWorkflowNode\(/);
assert.doesNotMatch(
  retrySource,
  /requestQuoteConfirmation|quote_id/,
  "retrying an already quoted workflow node must not create a second top-level workflow charge",
);

const optimizeStart = promptOptimizationSource.indexOf("async function optimize()");
const optimizeEnd = promptOptimizationSource.indexOf("async function accept(", optimizeStart);
const unlockStart = assetActionsSource.indexOf("async function unlock(asset)");
const downloadStart = assetActionsSource.indexOf("async function download(asset)", unlockStart);
const optimizeSource = promptOptimizationSource.slice(optimizeStart, optimizeEnd);
const unlockSource = assetActionsSource.slice(unlockStart, downloadStart);

assert.match(
  optimizeSource,
  /requestQuoteConfirmation\(\{[\s\S]*kind: "prompt_optimization"[\s\S]*execute:[\s\S]*createStudioPromptOptimization/,
  "paid prompt rewrites must use the shared silent billing execution",
);
assert.match(
  optimizeSource,
  /compileOnly[\s\S]*requiresQuote = !compileOnly/,
  "free model compilation must continue without a quote",
);
assert.match(
  unlockSource,
  /requestQuoteConfirmation\(\{[\s\S]*kind: "asset_unlock"[\s\S]*request: quoteRequest[\s\S]*api\.unlock\(asset\.id, \{ quote_id:/,
  "HD unlock must execute with the server-issued billing token",
);
assert.doesNotMatch(unlockSource, /window\.confirm/);
assert.match(apiSource, /unlock: \(assetId, body\)[\s\S]{0,140}body/);

console.log("studio quote confirmation tests passed");
