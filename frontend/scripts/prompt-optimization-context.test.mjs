import assert from "node:assert/strict";

import {
  isPromptOptimizationResultCurrent,
  promptOptimizationContextKey,
} from "../app/studio/promptOptimization.ts";

const general = promptOptimizationContextKey({
  creationMode: "image_edit",
  category: "image",
  subjectMode: "general",
  productGenerationMode: false,
});
const product = promptOptimizationContextKey({
  creationMode: "image_edit",
  category: "image",
  subjectMode: "product",
  productGenerationMode: true,
});
const portrait = promptOptimizationContextKey({
  creationMode: "image_edit",
  category: "image",
  subjectMode: "portrait",
  productGenerationMode: false,
});

assert.notEqual(general, product);
assert.notEqual(product, portrait);

const request = { id: 7, contextKey: general };
assert.equal(isPromptOptimizationResultCurrent(request, 7, general), true);
assert.equal(isPromptOptimizationResultCurrent(request, 7, product), false);
assert.equal(isPromptOptimizationResultCurrent(request, 8, general), false);

console.log("prompt optimization context tests passed");
