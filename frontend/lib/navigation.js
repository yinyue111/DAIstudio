const SENSITIVE_QUERY_KEYS = ["phone", "password", "smsCode", "sms_code", "nickname"];

export function sanitizeNextPath(value) {
  if (!value || !String(value).startsWith("/") || String(value).startsWith("//")) return "";
  try {
    const url = new URL(String(value), "http://local");
    if (url.origin !== "http://local") return "";
    if (url.pathname === "/login") return "";
    for (const key of SENSITIVE_QUERY_KEYS) {
      url.searchParams.delete(key);
    }
    const nestedNext = url.searchParams.get("next");
    if (nestedNext) {
      const cleanNestedNext = sanitizeNextPath(nestedNext);
      if (cleanNestedNext) url.searchParams.set("next", cleanNestedNext);
      else url.searchParams.delete("next");
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch (_e) {
    return "";
  }
}

export function loginPath(nextPath = null) {
  if (typeof window === "undefined" && !nextPath) return "/login";
  const raw = nextPath ?? `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const safeNext = sanitizeNextPath(raw);
  return safeNext ? `/login?next=${encodeURIComponent(safeNext)}` : "/login";
}

export function currentNextPath() {
  if (typeof window === "undefined") return "";
  try {
    return sanitizeNextPath(new URLSearchParams(window.location.search).get("next"));
  } catch (_e) {
    return "";
  }
}

export function scrubCredentialQuery() {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    let changed = false;
    for (const key of SENSITIVE_QUERY_KEYS) {
      if (url.searchParams.has(key)) {
        url.searchParams.delete(key);
        changed = true;
      }
    }
    if (url.searchParams.has("next")) {
      const rawNext = url.searchParams.get("next");
      const safeNext = sanitizeNextPath(rawNext);
      if (safeNext) {
        if (safeNext !== rawNext) {
          url.searchParams.set("next", safeNext);
          changed = true;
        }
      } else {
        url.searchParams.delete("next");
        changed = true;
      }
    }
    if (changed) {
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  } catch (_e) {
    // Best-effort privacy cleanup only.
  }
}
