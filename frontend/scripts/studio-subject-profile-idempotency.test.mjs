import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  clearPendingReverseRequest,
  generateReverseClientRequestId,
} from "../app/studio/generationRequestId.ts";

const profileIdentity = await import("../lib/studioSubjectProfile.js").catch(() => ({}));
const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pageSource = readFileSync(join(root, "app/page.jsx"), "utf8");
const uploadSource = readFileSync(join(root, "hooks/useMediaUpload.js"), "utf8");
const submitSource = readFileSync(join(root, "hooks/useGenerationSubmit.js"), "utf8");

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

assert.equal(
  typeof profileIdentity.buildSubjectProfileRequestIdentity,
  "function",
  "subject profiling needs one shared request-identity builder",
);

const request = {
  mode: "image_edit",
  subjectMode: "product",
  assetUrl: "/api/uploads/upload/product.png",
  assetSignature: "image|/api/uploads/upload/product.png|",
};
const prefetchIdentity = profileIdentity.buildSubjectProfileRequestIdentity(request);
const submitIdentity = profileIdentity.buildSubjectProfileRequestIdentity(request);

assert.deepEqual(
  submitIdentity,
  prefetchIdentity,
  "upload prefetch and submit-time fallback must use the same stable scope and signature",
);

const storage = memoryStorage();
const prefetchPendingRef = { current: null };
const submitPendingRef = { current: null };
const prefetchId = generateReverseClientRequestId(
  prefetchPendingRef,
  prefetchIdentity.scope,
  prefetchIdentity.signature,
  { storage, now: () => 1000, randomId: () => "prefetch" },
);
const submitId = generateReverseClientRequestId(
  submitPendingRef,
  submitIdentity.scope,
  submitIdentity.signature,
  { storage, now: () => 2000, randomId: () => "submit" },
);
assert.equal(submitId, prefetchId, "a submit retry after a timed-out prefetch must reuse the billed request id");

assert.equal(
  typeof profileIdentity.cacheSubjectProfileResult,
  "function",
  "a successful prefetch needs an in-memory result cache until React commits the workspace state",
);
assert.equal(
  typeof profileIdentity.readCachedSubjectProfileResult,
  "function",
  "submit-time fallback needs to read the successful prefetch result before starting another billed reverse request",
);
const successfulProfile = {
  structured: { subject: "red bottle" },
  final_text: "red bottle with white logo",
};
const profileResultCacheRef = { current: null };
clearPendingReverseRequest(prefetchPendingRef, prefetchId, { storage, now: () => 2500 });
profileIdentity.cacheSubjectProfileResult(
  profileResultCacheRef,
  prefetchIdentity,
  prefetchId,
  successfulProfile,
);
assert.deepEqual(
  profileIdentity.readCachedSubjectProfileResult(profileResultCacheRef, submitIdentity),
  successfulProfile,
  "clearing the pending request id after success must not erase the independent result cache",
);

const staleStorageValues = new Map();
let staleStorageRemoveBlocked = false;
const staleReadableStorage = {
  getItem: (key) => staleStorageValues.get(key) ?? null,
  setItem: (key, value) => staleStorageValues.set(key, String(value)),
  removeItem: (key) => {
    if (staleStorageRemoveBlocked) throw new Error("storage remove blocked");
    staleStorageValues.delete(key);
  },
};
const storageFailurePendingRef = { current: null };
const storageFailureResultCacheRef = { current: null };
const storageFailureId = generateReverseClientRequestId(
  storageFailurePendingRef,
  prefetchIdentity.scope,
  prefetchIdentity.signature,
  { storage: staleReadableStorage, now: () => 4000, randomId: () => "storage-failure" },
);
staleStorageRemoveBlocked = true;
clearPendingReverseRequest(
  storageFailurePendingRef,
  storageFailureId,
  { storage: staleReadableStorage, now: () => 4100 },
);
const pendingStateAfterFailedClear = storageFailurePendingRef.current;
profileIdentity.cacheSubjectProfileResult(
  storageFailureResultCacheRef,
  prefetchIdentity,
  storageFailureId,
  successfulProfile,
);
assert.strictEqual(
  storageFailurePendingRef.current,
  pendingStateAfterFailedClear,
  "caching a successful result must not overwrite pending request state after storage is disabled",
);
assert.deepEqual(
  profileIdentity.readCachedSubjectProfileResult(storageFailureResultCacheRef, prefetchIdentity),
  successfulProfile,
  "the successful profile result should remain readable from its dedicated cache ref",
);
assert.equal(
  generateReverseClientRequestId(
    storageFailurePendingRef,
    prefetchIdentity.scope,
    prefetchIdentity.signature,
    { storage: staleReadableStorage, now: () => 4200, randomId: () => "after-failed-clear" },
  ),
  "studio-reverse-subject-profile:image_edit-after-failed-clear",
  "the cleared request id must not be resurrected from stale readable storage after caching succeeds",
);

const otherAssetIdentity = profileIdentity.buildSubjectProfileRequestIdentity({
  ...request,
  assetUrl: "/api/uploads/upload/other.png",
  assetSignature: "image|/api/uploads/upload/other.png|",
});
const otherAssetId = generateReverseClientRequestId(
  submitPendingRef,
  otherAssetIdentity.scope,
  otherAssetIdentity.signature,
  { storage, now: () => 3000, randomId: () => "other-asset" },
);
assert.notEqual(otherAssetId, prefetchId, "different subject assets must not reuse a profile request id");

const portraitIdentity = profileIdentity.buildSubjectProfileRequestIdentity({ ...request, subjectMode: "portrait" });
assert.notEqual(
  portraitIdentity.signature,
  prefetchIdentity.signature,
  "different subject-profile requests must not share a signature",
);

assert.match(pageSource, /const subjectProfilePendingRequestRef = useRef\(null\)/);
assert.match(pageSource, /const subjectProfileResultCacheRef = useRef\(null\)/);
assert.equal(
  (pageSource.match(/subjectProfilePendingRequestRef,/g) || []).length >= 2,
  true,
  "the page should pass one pending request ref to upload prefetch and generation fallback",
);
assert.equal(
  (pageSource.match(/subjectProfileResultCacheRef,/g) || []).length >= 2,
  true,
  "the page should pass one result cache ref to upload prefetch and generation fallback",
);
for (const [label, source] of [["upload", uploadSource], ["submit", submitSource]]) {
  assert.match(source, /buildSubjectProfileRequestIdentity\(/, `${label} path should use the shared identity builder`);
  assert.match(source, /subjectProfilePendingRequestRef/, `${label} path should consume the page-owned pending ref`);
  assert.match(source, /subjectProfileResultCacheRef/, `${label} path should consume the page-owned result cache ref`);
  assert.match(
    source,
    /fallbackSubjectProfilePendingRequestRef = useRef\(null\)/,
    `${label} path should have an independent pending-ref fallback`,
  );
  assert.match(
    source,
    /fallbackSubjectProfileResultCacheRef = useRef\(null\)/,
    `${label} path should have an independent result-cache fallback`,
  );
  assert.doesNotMatch(
    source,
    /(?:cacheSubjectProfileResult|readCachedSubjectProfileResult)\(\s*pendingProfileReverseRequestRef,/,
    `${label} path must not store successful results in the pending request ref`,
  );
  assert.match(
    source,
    /!isRequestTimeoutError\(e\)[\s\S]{0,120}!shouldKeepPendingReverseRequest\(e\)[\s\S]{0,160}clearPendingReverseRequest\(pendingProfileReverseRequestRef,\s*profileRequestId\)/,
    `${label} path must retain the billed profile request id while the backend reports it is still running`,
  );
}
assert.match(
  uploadSource,
  /cacheSubjectProfileResult\(\s*profileResultCacheRef,/,
  "upload prefetch should retain its successful result in the dedicated result cache ref",
);
assert.match(
  submitSource,
  /readCachedSubjectProfileResult\(\s*profileResultCacheRef,/,
  "generation should read a successful prefetch from the dedicated result cache ref",
);
assert.match(
  submitSource,
  /cacheSubjectProfileResult\(\s*profileResultCacheRef,/,
  "generation fallback should retain its successful result in the dedicated result cache ref",
);
const cachedReadIndex = submitSource.indexOf("readCachedSubjectProfileResult(");
const reverseCallIndex = submitSource.indexOf("await api.reverse(");
assert.ok(
  cachedReadIndex >= 0 && reverseCallIndex > cachedReadIndex,
  "generation should consult the successful prefetch cache before issuing a reverse request",
);
assert.doesNotMatch(uploadSource, /profile-prefetch/, "prefetch must not use a phase-specific idempotency scope");

console.log("studio subject-profile idempotency tests passed");
