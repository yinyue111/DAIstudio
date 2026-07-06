import { NextResponse } from "next/server.js";

const AUTH_COOKIE_NAME = process.env.AUTH_COOKIE_NAME
  || process.env.NEXT_PUBLIC_AUTH_COOKIE_NAME
  || "ai_studio_token";

const PUBLIC_PATHS = new Set([
  "/login",
]);

const PUBLIC_FILE_RE = /\.(?:avif|css|gif|ico|jpg|jpeg|js|json|map|mp4|png|svg|txt|webmanifest|webp|woff|woff2)$/i;
const SENSITIVE_QUERY_KEYS = ["phone", "password", "smsCode", "sms_code", "nickname"];

function isPublicRequest(pathname) {
  return pathname.startsWith("/api/")
    || pathname.startsWith("/_next/")
    || pathname.startsWith("/prompt-library/")
    || PUBLIC_PATHS.has(pathname)
    || PUBLIC_FILE_RE.test(pathname);
}

function loginRedirectUrl(request) {
  const url = request.nextUrl.clone();
  for (const key of SENSITIVE_QUERY_KEYS) {
    url.searchParams.delete(key);
  }
  const target = `${url.pathname}${url.search}`;
  url.pathname = "/login";
  url.search = "";
  if (target && target !== "/") {
    url.searchParams.set("next", target);
  }
  return url;
}

function apiBaseForRequest(request) {
  return (
    process.env.API_INTERNAL_BASE
    || process.env.INTERNAL_API_BASE
    || process.env.NEXT_PUBLIC_API_BASE
    || request.nextUrl.origin
  ).replace(/\/$/, "");
}

async function hasValidSession(request) {
  if (!request.cookies.get(AUTH_COOKIE_NAME)?.value) return false;
  try {
    const response = await fetch(`${apiBaseForRequest(request)}/api/me`, {
      headers: {
        cookie: request.headers.get("cookie") || "",
      },
      cache: "no-store",
    });
    return response.ok;
  } catch (_err) {
    return false;
  }
}

export async function proxy(request) {
  const { pathname } = request.nextUrl;
  if (isPublicRequest(pathname)) return NextResponse.next();
  if (await hasValidSession(request)) return NextResponse.next();
  return NextResponse.redirect(loginRedirectUrl(request));
}

export const config = {
  matcher: ["/:path*"],
};
