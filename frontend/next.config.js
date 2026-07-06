import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const isProd = process.env.NODE_ENV === "production";

function splitSources(value) {
  return String(value || "")
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function originSource(value) {
  if (!value) return null;
  try {
    return new URL(value).origin;
  } catch (_err) {
    return value;
  }
}

function uniqueSources(items) {
  return [...new Set(items.filter(Boolean))];
}

function websocketOrigin(value) {
  const origin = originSource(value);
  if (!origin) return null;
  return origin.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

function shouldAllowLoopbackApiOrigin(value) {
  if (!value) return false;
  if (!isProd || process.env.NEXT_PUBLIC_ALLOW_LAN_API_ALIAS === "true") return true;
  try {
    const url = new URL(value);
    return !["localhost", "127.0.0.1"].includes(url.hostname);
  } catch (_err) {
    return true;
  }
}

function publicApiOrigin(value) {
  if (!shouldAllowLoopbackApiOrigin(value)) return null;
  return originSource(value);
}

function publicWebsocketOrigin(value) {
  if (!shouldAllowLoopbackApiOrigin(value)) return null;
  return websocketOrigin(value);
}

function loopbackApiAliases(value) {
  if (!value) return [];
  const allowLanAlias = !isProd || process.env.NEXT_PUBLIC_ALLOW_LAN_API_ALIAS === "true";
  if (isProd && !allowLanAlias) return [];
  try {
    const url = new URL(value);
    if (!["localhost", "127.0.0.1"].includes(url.hostname)) return [];
    const port = url.port ? `:${url.port}` : "";
    const aliases = [
      `${url.protocol}//localhost${port}`,
      `${url.protocol}//127.0.0.1${port}`,
    ];
    if (allowLanAlias) aliases.push(`${url.protocol}//*${port}`);
    return aliases;
  } catch (_err) {
    return [];
  }
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname),
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Internal tool: allow loading remote reference / result images without the
  // next/image optimizer (we use plain <img>).
  async headers() {
    const scriptSrc = isProd ? "script-src 'self' 'unsafe-inline'" : "script-src 'self' 'unsafe-eval' 'unsafe-inline'";
    const apiOrigin = publicApiOrigin(process.env.NEXT_PUBLIC_API_BASE);
    const apiWsOrigin = publicWebsocketOrigin(process.env.NEXT_PUBLIC_API_BASE);
    const localApi = isProd
      ? loopbackApiAliases(process.env.NEXT_PUBLIC_API_BASE)
      : ["http://localhost:8000", "http://127.0.0.1:8000"];
    const localWsApi = localApi.map((source) => websocketOrigin(source));
    const mediaSources = uniqueSources([
      "'self'",
      "data:",
      "blob:",
      apiOrigin,
      ...localApi,
      ...splitSources(process.env.NEXT_PUBLIC_MEDIA_SRC),
    ]);
    const connectSources = uniqueSources([
      "'self'",
      apiOrigin,
      apiWsOrigin,
      ...localApi,
      ...localWsApi,
      ...splitSources(process.env.NEXT_PUBLIC_CONNECT_SRC),
    ]);
    const csp = [
      "default-src 'self'",
      scriptSrc,
      "style-src 'self' 'unsafe-inline'",
      "font-src 'self' data:",
      `img-src ${mediaSources.join(" ")}`,
      `media-src ${mediaSources.join(" ")}`,
      `connect-src ${connectSources.join(" ")}`,
      "object-src 'none'",
      "base-uri 'self'",
      "frame-ancestors 'none'",
      "form-action 'self'",
    ].join("; ");
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
        ],
      },
    ];
  },
};

export default nextConfig;
