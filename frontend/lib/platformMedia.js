const DEFAULT_PLATFORM_MEDIA_PREFIXES = ["/media/", "/api/uploads/"];
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

function isLoopbackHost(hostname) {
  return LOOPBACK_HOSTS.has(String(hostname || "").toLowerCase());
}

function portOf(url) {
  if (url.port) return url.port;
  if (url.protocol === "https:") return "443";
  if (url.protocol === "http:") return "80";
  return "";
}

export function normalizeLoopbackPlatformMediaUrl(
  value,
  {
    apiBase = "",
    pageOrigin = "",
    allowedPrefixes = DEFAULT_PLATFORM_MEDIA_PREFIXES,
  } = {},
) {
  if (!value || !pageOrigin) return "";
  try {
    const url = value instanceof URL ? value : new URL(value, pageOrigin);
    const apiUrl = new URL(apiBase || pageOrigin, pageOrigin);
    if (
      !["http:", "https:"].includes(url.protocol)
      || !isLoopbackHost(url.hostname)
      || !isLoopbackHost(apiUrl.hostname)
      || portOf(url) !== portOf(apiUrl)
      || !allowedPrefixes.some((prefix) => url.pathname.startsWith(prefix))
    ) return "";
    return `${apiUrl.origin}${url.pathname}${url.search}${url.hash}`;
  } catch (e) {
    return "";
  }
}
