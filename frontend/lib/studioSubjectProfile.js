function normalized(value) {
  return String(value || "").trim();
}

export function buildSubjectProfileRequestIdentity({
  mode,
  subjectMode,
  assetUrl,
  assetSignature,
  modelConfigId,
}) {
  const request = {
    mode: normalized(mode),
    subjectMode: normalized(subjectMode),
    assetUrl: normalized(assetUrl),
    assetSignature: normalized(assetSignature),
    modelConfigId: normalized(modelConfigId) || "default",
    sourceType: "image",
    target: normalized(subjectMode) === "portrait" ? "portrait_profile" : "product_profile",
  };
  return {
    scope: `subject-profile:${request.mode}`,
    signature: JSON.stringify(request),
  };
}

export function normalizeSubjectProfileResult(profile) {
  return {
    structured: profile?.structured || {},
    final_text: profile?.final_text || "",
  };
}

export function cacheSubjectProfileResult(resultCacheRef, requestIdentity, requestId, profile) {
  if (!resultCacheRef || !requestIdentity?.scope || !requestIdentity?.signature || !requestId) return null;
  const result = normalizeSubjectProfileResult(profile);
  resultCacheRef.current = {
    scope: requestIdentity.scope,
    signature: requestIdentity.signature,
    id: requestId,
    result,
  };
  return result;
}

export function readCachedSubjectProfileResult(resultCacheRef, requestIdentity) {
  const current = resultCacheRef?.current;
  if (
    !current?.result
    || current.scope !== requestIdentity?.scope
    || current.signature !== requestIdentity?.signature
  ) return null;
  return current.result;
}
