const path = require("path");

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

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  // Internal tool: allow loading remote reference / result images without the
  // next/image optimizer (we use plain <img>).
  async headers() {
    const scriptSrc = isProd ? "script-src 'self'" : "script-src 'self' 'unsafe-eval' 'unsafe-inline'";
    const apiOrigin = originSource(process.env.NEXT_PUBLIC_API_BASE);
    const apiWsOrigin = websocketOrigin(process.env.NEXT_PUBLIC_API_BASE);
    const localApi = isProd ? [] : ["http://localhost:8000", "http://127.0.0.1:8000"];
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
      ...(isProd ? [] : ["ws://localhost:8000", "ws://127.0.0.1:8000"]),
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

module.exports = nextConfig;
