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

const fiveSecondVideo = promptOptimizationContextKey({
  creationMode: "video_edit",
  category: "video",
  subjectMode: "product",
  productGenerationMode: true,
  duration: 5,
  aspectRatio: "9:16",
  resolution: "1080p",
  productLockMode: "locked",
  productVideoTemplate: "slow_push",
  referenceSignature: "product-a|style-a",
  subjectProfileSource: "product-a",
  targetModelId: "seedance-mini",
  targetModelProvider: "volcengine_ark",
});
const fifteenSecondVideo = promptOptimizationContextKey({
  creationMode: "video_edit",
  category: "video",
  subjectMode: "product",
  productGenerationMode: true,
  duration: 15,
  aspectRatio: "9:16",
  resolution: "1080p",
  productLockMode: "locked",
  productVideoTemplate: "slow_push",
  referenceSignature: "product-a|style-a",
  subjectProfileSource: "product-a",
  targetModelId: "seedance-mini",
  targetModelProvider: "volcengine_ark",
});
assert.notEqual(fiveSecondVideo, fifteenSecondVideo);
assert.notEqual(
  fiveSecondVideo,
  promptOptimizationContextKey({
    creationMode: "video_edit",
    category: "video",
    subjectMode: "product",
    productGenerationMode: true,
    duration: 5,
    aspectRatio: "16:9",
    resolution: "720p",
    productLockMode: "free",
    productVideoTemplate: "soft_splash",
    referenceSignature: "product-a|style-a",
    subjectProfileSource: "product-a",
    targetModelId: "seedance-mini",
    targetModelProvider: "volcengine_ark",
  }),
);
assert.notEqual(
  fiveSecondVideo,
  promptOptimizationContextKey({
    creationMode: "video_edit",
    category: "video",
    subjectMode: "product",
    productGenerationMode: true,
    duration: 5,
    referenceSignature: "product-b|style-a",
    subjectProfileSource: "product-b",
    targetModelId: "grok-imagine-video",
    targetModelProvider: "yinyue",
  }),
);

const request = { id: 7, contextKey: general };
assert.equal(isPromptOptimizationResultCurrent(request, 7, general), true);
assert.equal(isPromptOptimizationResultCurrent(request, 7, product), false);
assert.equal(isPromptOptimizationResultCurrent(request, 8, general), false);

console.log("prompt optimization context tests passed");
