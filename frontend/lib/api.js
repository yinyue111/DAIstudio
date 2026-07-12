"use client";

import { loginPath } from "./navigation";

export { loginPath } from "./navigation";

function resolveApiBase() {
  const configured = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");
  if (typeof window === "undefined" || !configured) {
    return configured;
  }
  try {
    const url = new URL(configured, window.location.origin);
    const configuredHost = url.hostname;
    if (["localhost", "127.0.0.1"].includes(configuredHost)) {
      const currentHost = window.location.hostname;
      const currentIsLoopback = ["localhost", "127.0.0.1"].includes(currentHost);
      const allowLanAlias = process.env.NODE_ENV !== "production"
        || process.env.NEXT_PUBLIC_ALLOW_LAN_API_ALIAS === "true";
      if (!currentIsLoopback && !allowLanAlias) return "";
      url.hostname = window.location.hostname;
      return url.origin;
    }
  } catch (e) {
    return configured;
  }
  return configured;
}

export const API_BASE = resolveApiBase();

const DEFAULT_TIMEOUT_MS = 30_000;
const PARSE_TIMEOUT_MS = 90_000;
const REVERSE_TIMEOUT_MS = 240_000;
const UPLOAD_TIMEOUT_MS = 600_000;
const DOWNLOAD_TIMEOUT_MS = 300_000;
const GENERATE_TIMEOUT_MS = 600_000;
const ONLINE_UPDATE_TIMEOUT_MS = 600_000;

export function wsUrl(path) {
  const base = API_BASE || (typeof window !== "undefined" ? window.location.origin : "");
  return `${base.replace(/^http/, "ws")}${path}`;
}

function clearLegacyToken() {
  if (typeof window === "undefined") return;
  // Auth moved to an HttpOnly cookie. Clear any legacy bearer token so an XSS
  // cannot keep using an old localStorage credential.
  try {
    window.localStorage.removeItem("token");
  } catch (e) {
    // Storage can be disabled by browser privacy settings; cookie auth must continue.
  }
}

export function getToken() {
  clearLegacyToken();
  return null;
}

export function setToken(t) {
  clearLegacyToken();
}

export function clearToken() {
  clearLegacyToken();
}

let unauthorizedHandler = null;

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = typeof handler === "function" ? handler : null;
}

function handleUnauthorized() {
  try {
    unauthorizedHandler?.();
  } catch (e) {
    console.warn("[auth unauthorized handler]", e);
  }
  clearToken();
  redirectToLogin();
}

function redirectToLogin() {
  if (typeof window !== "undefined") window.location.href = loginPath();
}

export class ApiError extends Error {
  constructor(message, { status, detail } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      this.retryAfter = Number(detail.retry_after || 0);
    }
  }
}

// Authenticated file download that reuses the same 401 -> clear-token + redirect
// handling as request(), so CSV/asset downloads don't drift from the REST path.
function filenameFromContentDisposition(value) {
  if (!value) return "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(value);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].replace(/^"|"$/g, ""));
    } catch (e) {
      return encoded[1].replace(/^"|"$/g, "");
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(value);
  return plain ? plain[1] : "";
}

function sanitizeDownloadFilename(value) {
  const cleaned = String(value || "")
    .trim()
    .replace(/[\u0000-\u001f<>:"/\\|?*]+/g, "_")
    .replace(/\s+/g, " ");
  return cleaned.slice(0, 180);
}

function extensionFromContentType(value) {
  const contentType = String(value || "").split(";")[0].trim().toLowerCase();
  return {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "text/csv": "csv",
    "application/pdf": "pdf",
    "application/zip": "zip",
  }[contentType] || "bin";
}

function fallbackDownloadFilename(path, contentType) {
  const assetMatch = String(path || "").match(/\/api\/assets\/(\d+)\/download(?:\?|$)/);
  const stem = assetMatch ? `asset-${assetMatch[1]}` : "download";
  return `${stem}.${extensionFromContentType(contentType)}`;
}

export async function downloadBlob(path, filename, options = {}) {
  const headers = {};
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const { res, blob, errorText } = await fetchBlobWithTimeout(
    `${API_BASE}${path}`,
    { method: options.method || "GET", headers, credentials: "include", body },
    DOWNLOAD_TIMEOUT_MS,
  );
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("登录已过期，请重新登录");
  }
  if (!res.ok) throw new Error(errorTextToMessage(errorText, res.status, "下载失败"));
  const u = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = u;
  const responseFilename = filenameFromContentDisposition(res.headers.get("content-disposition"));
  const finalFilename = sanitizeDownloadFilename(responseFilename || filename)
    || fallbackDownloadFilename(path, res.headers.get("content-type"));
  a.download = finalFilename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(u);
    a.remove();
  }, 1000);
  return finalFilename;
}

export async function authenticatedObjectUrl(pathOrUrl) {
  if (!pathOrUrl) return "";
  const url = /^https?:\/\//.test(pathOrUrl) ? pathOrUrl : `${API_BASE}${pathOrUrl}`;
  const expectedOrigin = new URL(API_BASE || window.location.origin, window.location.origin).origin;
  const actualOrigin = new URL(url, window.location.origin).origin;
  if (actualOrigin !== expectedOrigin) {
    throw new Error("仅支持加载平台内受保护素材");
  }
  const headers = {};
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  const { res, blob, errorText } = await fetchBlobWithTimeout(
    url,
    { headers, credentials: "include" },
    DOWNLOAD_TIMEOUT_MS,
  );
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("登录已过期，请重新登录");
  }
  if (!res.ok) throw new Error(errorTextToMessage(errorText, res.status, "预览加载失败"));
  return URL.createObjectURL(blob);
}

export async function assetDownloadObjectUrl(assetId) {
  if (!assetId) return "";
  const headers = {};
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  const { res, blob, errorText } = await fetchBlobWithTimeout(
    `${API_BASE}/api/assets/${assetId}/download`,
    { headers, credentials: "include" },
    DOWNLOAD_TIMEOUT_MS,
  );
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("登录已过期，请重新登录");
  }
  if (!res.ok) throw new Error(errorTextToMessage(errorText, res.status, "高清预览加载失败"));
  return URL.createObjectURL(blob);
}

async function withTimeout(timeoutMs, operation, externalSignal = null) {
  const controller = new AbortController();
  let abortSource = "";
  const abortFromCaller = () => {
    if (controller.signal.aborted) return;
    abortSource = "caller";
    controller.abort(externalSignal?.reason);
  };
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = setTimeout(() => {
    if (controller.signal.aborted) return;
    abortSource = "timeout";
    controller.abort();
  }, timeoutMs);
  try {
    return await operation(controller.signal);
  } catch (e) {
    if (e?.name === "AbortError" && abortSource === "timeout") {
      throw new Error("请求超时，请稍后重试");
    }
    throw e;
  } finally {
    clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

async function fetchTextWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const externalSignal = options.signal || null;
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetch(url, { ...options, signal });
    const text = await res.text();
    return { res, text };
  }, externalSignal);
}

async function fetchBlobWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetch(url, { ...options, signal });
    if (!res.ok) return { res, blob: null, errorText: await res.text().catch(() => "") };
    const blob = await res.blob();
    return { res, blob, errorText: "" };
  });
}

// FastAPI/Pydantic error `detail` can be a string OR a 422 array of
// {loc,msg,type}; turn either into a readable message (not "[object Object]").
function detailToMessage(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => {
      if (!d || typeof d !== "object") return String(d);
      const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "";
      return [field, d.msg].filter(Boolean).join(": ");
    });
    return msgs.filter(Boolean).join("; ") || `请求失败 (${status})`;
  }
  if (detail && typeof detail === "object") return detail.message || detail.msg || JSON.stringify(detail);
  return `请求失败 (${status})`;
}

function errorTextToMessage(text, status, fallback = "请求失败") {
  if (text) {
    try {
      const data = JSON.parse(text);
      return detailToMessage(data && data.detail, status);
    } catch (e) {
      const clipped = text.trim().slice(0, 160);
      if (clipped) return clipped;
    }
  }
  return `${fallback} (${status})`;
}

async function request(
  path,
  {
    method = "GET",
    body,
    auth = true,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    redirectOn401 = true,
    signal = null,
  } = {},
) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const t = getToken();
    if (t) headers["Authorization"] = `Bearer ${t}`;
  }
  const { res, text } = await fetchTextWithTimeout(
    `${API_BASE}${path}`,
    {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      credentials: "include",
      signal,
    },
    timeoutMs,
  );
  if (res.status === 401 && auth) {
    if (redirectOn401 && typeof window !== "undefined" && !path.startsWith("/api/auth")) {
      handleUnauthorized();
    } else {
      clearToken();
    }
    throw new ApiError("登录已过期，请重新登录", { status: 401 });
  }
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      if (!res.ok) {
        throw new Error(text.slice(0, 160) || `请求失败 (${res.status})`);
      }
      throw new Error("服务端返回了非 JSON 响应");
    }
  }
  if (!res.ok) {
    throw new ApiError(detailToMessage(data && data.detail, res.status), {
      status: res.status,
      detail: data && data.detail,
    });
  }
  return data;
}

async function upload(path, formData, { auth = true, signal = null } = {}) {
  const headers = {};
  if (auth) {
    const t = getToken();
    if (t) headers["Authorization"] = `Bearer ${t}`;
  }
  const { res, text } = await fetchTextWithTimeout(
    `${API_BASE}${path}`,
    {
      method: "POST",
      headers,
      body: formData,
      credentials: "include",
      signal,
    },
    UPLOAD_TIMEOUT_MS,
  );
  if (res.status === 401 && auth) {
    handleUnauthorized();
    throw new ApiError("登录已过期，请重新登录", { status: 401 });
  }
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      if (!res.ok) throw new Error(text.slice(0, 160) || `请求失败 (${res.status})`);
      throw new Error("服务端返回了非 JSON 响应");
    }
  }
  if (!res.ok) {
    throw new ApiError(detailToMessage(data && data.detail, res.status), {
      status: res.status,
      detail: data && data.detail,
    });
  }
  return data;
}

export const api = {
  register: (phone, password, sms_code, nickname) =>
    request("/api/auth/register", {
      method: "POST",
      body: { phone, password, sms_code, nickname },
      auth: false,
    }),
  authFeatures: () => request("/api/auth/features", { auth: false }),
  sendSmsCode: (phone) =>
    request("/api/auth/sms/send", { method: "POST", body: { phone }, auth: false }),
  login: (phone, password) =>
    request("/api/auth/login", { method: "POST", body: { phone, password }, auth: false }),
  me: (options = {}) => request("/api/me", options),
  getDraft: (key) => request(`/api/me/drafts/${encodeURIComponent(key)}`),
  saveDraft: (key, payload) =>
    request(`/api/me/drafts/${encodeURIComponent(key)}`, { method: "PUT", body: { payload } }),
  deleteDraft: (key) => request(`/api/me/drafts/${encodeURIComponent(key)}`, { method: "DELETE" }),
  config: () => request("/api/config"),
  profile: () => request("/api/profile"),
  profileAssets: ({
    type = "all",
    favorite = false,
    limit = 60,
    offset = 0,
    created_from = "",
    created_to = "",
    model_use = "",
    size = "",
    min_width = "",
    min_height = "",
    max_width = "",
    max_height = "",
  } = {}) => {
    const qs = new URLSearchParams({
      type,
      favorite: String(Boolean(favorite)),
      limit: String(limit),
      offset: String(offset),
    });
    for (const [key, value] of Object.entries({
      created_from,
      created_to,
      model_use,
      size,
      min_width,
      min_height,
      max_width,
      max_height,
    })) {
      if (value !== undefined && value !== null && String(value).trim() !== "") qs.set(key, String(value));
    }
    return request(`/api/profile/assets?${qs.toString()}`);
  },
  paymentPackages: () => request("/api/payments/packages"),
  paymentConfig: () => request("/api/payments/config"),
  paymentOrders: (limit = 20) => request(`/api/payments/orders?limit=${limit}`),
  createPaymentOrder: (body) => request("/api/payments/orders", { method: "POST", body }),
  paymentOrder: (orderNo) => request(`/api/payments/orders/${orderNo}`),
  mockPayOrder: (orderNo) => request(`/api/payments/orders/${orderNo}/mock-pay`, { method: "POST" }),
  changePassword: (old_password, new_password) =>
    request("/api/me/password", { method: "POST", body: { old_password, new_password } }),
  logout: () => request("/api/me/logout", { method: "POST" }),
  parse: (url) =>
    request("/api/parse", { method: "POST", body: { url }, timeoutMs: PARSE_TIMEOUT_MS }),
  parseStatus: (id) =>
    request(`/api/parse/${id}`, { timeoutMs: DEFAULT_TIMEOUT_MS }),
  uploadImage: (file, options = {}) => {
    const form = new FormData();
    form.append("file", file);
    return upload("/api/uploads/image", form, options);
  },
  uploadVideo: (file, options = {}) => {
    const form = new FormData();
    form.append("file", file);
    return upload("/api/uploads/video", form, options);
  },
  reverse: (
    asset_url,
    target = "image",
    fallback_image = null,
    source_type = null,
    video_analysis_preset = null,
    client_request_id = null,
  ) =>
    request("/api/prompt/reverse", {
      method: "POST",
      body: { asset_url, target, fallback_image, source_type, video_analysis_preset, client_request_id },
      timeoutMs: REVERSE_TIMEOUT_MS,
    }),
  optimizePrompt: (prompt, category = "image", product_mode = false) =>
    request("/api/prompt/optimize", {
      method: "POST",
      body: { prompt, category, product_mode },
      timeoutMs: REVERSE_TIMEOUT_MS,
    }),
  subjectProtectionPreview: (asset_url, edit_mask_mode = "protect_subject") => {
    const controller = new AbortController();
    const promise = request("/api/subject-protection/preview", {
      method: "POST",
      body: { asset_url, edit_mask_mode },
      timeoutMs: REVERSE_TIMEOUT_MS,
      signal: controller.signal,
    });
    promise.cancel = () => controller.abort();
    return promise;
  },
  generate: (payload) =>
    request("/api/generate", { method: "POST", body: payload, timeoutMs: GENERATE_TIMEOUT_MS }),
  task: (id) => request(`/api/tasks/${id}`),
  taskEta: ({ duration = 5, resolution = "720p", stage = "preview" } = {}) => {
    const qs = new URLSearchParams({
      duration: String(duration),
      resolution,
      stage,
    });
    return request(`/api/tasks/eta/video?${qs.toString()}`);
  },
  taskWsTicket: (id) => request(`/api/tasks/${id}/ws-ticket`, { method: "POST" }),
  eventWsTicket: () => request("/api/events/ws-ticket", { method: "POST" }),
  tasks: (limit = 30, offset = 0) => request(`/api/tasks?limit=${limit}&offset=${offset}`),
  cancelTask: (taskId) => request(`/api/tasks/${taskId}/cancel`, { method: "POST" }),
  unlock: (assetId) => request(`/api/assets/${assetId}/unlock`, { method: "POST" }),
  playbackTicket: (assetId) => request(`/api/assets/${assetId}/playback-ticket`, { method: "POST" }),
  playbackUrl: (assetId) => `${API_BASE}/api/assets/${assetId}/stream`,
  favoriteAsset: (assetId) => request(`/api/assets/${assetId}/favorite`, { method: "POST" }),
  reportAsset: (assetId, body) =>
    request(`/api/assets/${assetId}/report`, { method: "POST", body }),
  deleteAsset: (assetId) => request(`/api/assets/${assetId}`, { method: "DELETE" }),
  batchDeleteAssets: (assetIds) => request("/api/assets/batch/delete", { method: "POST", body: { asset_ids: assetIds } }),
  batchDownloadAssets: (assetIds) => downloadBlob("/api/assets/batch/download", "assets.zip", {
    method: "POST",
    body: { asset_ids: assetIds },
  }),
  retryTask: (taskId) => request(`/api/tasks/${taskId}/retry`, { method: "POST" }),
  downloadUrl: (assetId) => `${API_BASE}/api/assets/${assetId}/download`,
  promptHistory: ({ favorite = null, category = "", source = "", q = "", limit = 50, offset = 0 } = {}) => {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (favorite !== null && favorite !== undefined) qs.set("favorite", String(Boolean(favorite)));
    if (category) qs.set("category", category);
    if (source) qs.set("source", source);
    if (q) qs.set("q", q);
    return request(`/api/prompts/history?${qs.toString()}`);
  },
  createPromptHistory: (body) => request("/api/prompts/history", { method: "POST", body }),
  updatePromptHistory: (id, body) => request(`/api/prompts/history/${id}`, { method: "PATCH", body }),
  favoritePromptHistory: (id) => request(`/api/prompts/history/${id}/favorite`, { method: "POST" }),
  deletePromptHistory: (id) => request(`/api/prompts/history/${id}`, { method: "DELETE" }),
  // admin
  adminWhitelist: () => request("/api/admin/whitelist"),
  adminAddWhitelist: (body) =>
    request("/api/admin/whitelist", { method: "POST", body }),
  adminRemoveWhitelist: (phone, body) =>
    request(`/api/admin/whitelist/${encodeURIComponent(phone)}`, { method: "DELETE", body }),
  adminUsers: ({ q = "", status = "", is_admin = "", limit = 50, offset = 0 } = {}) => {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (q) qs.set("q", q);
    if (status) qs.set("status", status);
    if (is_admin !== "" && is_admin !== null && is_admin !== undefined) {
      qs.set("is_admin", is_admin === true || is_admin === "true" ? "true" : "false");
    }
    return request(`/api/admin/users?${qs.toString()}`);
  },
  adminGrant: (body) => request("/api/admin/quota/grant", { method: "POST", body }),
  adminBulkGrant: (body) => request("/api/admin/quota/bulk-grant", { method: "POST", body }),
  adminSetUserStatus: (userId, body) =>
    request(`/api/admin/users/${userId}/status`, { method: "PATCH", body }),
  adminResetPassword: (userId, body) =>
    request(`/api/admin/users/${userId}/reset_password`, { method: "POST", body }),
  adminModels: () => request("/api/admin/models"),
  adminSaveModel: (body) => request("/api/admin/models", { method: "PUT", body }),
  adminProbeModels: (body) => request("/api/admin/models/probe", { method: "POST", body }),
  adminReport: (qs = "") => request(`/api/admin/usage/report${qs}`),
  adminUsageDashboard: (qs = "") => request(`/api/admin/usage/dashboard${qs}`),
  adminModelCosts: (qs = "") => request(`/api/admin/usage/model-costs${qs}`),
  adminReportCsvUrl: (qs = "") => `${API_BASE}/api/admin/usage/report${qs}`,
  adminAudit: (qs = "") => request(`/api/admin/audit${qs}`),
  adminReviewTasks: (limit = 50, offset = 0) =>
    request(`/api/admin/tasks/review?limit=${limit}&offset=${offset}`),
  adminAssetReports: ({ status = "open", limit = 50, offset = 0 } = {}) =>
    request(`/api/admin/asset-reports?status=${encodeURIComponent(status)}&limit=${limit}&offset=${offset}`),
  adminHandleAssetReport: (reportId, body) =>
    request(`/api/admin/asset-reports/${reportId}/handle`, { method: "POST", body }),
  adminRefundReviewTask: (taskId, body) =>
    request(`/api/admin/tasks/${taskId}/refund_review`, { method: "POST", body }),
  adminSettleReviewTask: (taskId, body) =>
    request(`/api/admin/tasks/${taskId}/settle_review`, { method: "POST", body }),
  adminSettings: () => request("/api/admin/settings"),
  adminSaveSettings: (body) => request("/api/admin/settings", { method: "PUT", body }),
  adminGateway: () => request("/api/admin/gateway"),
  adminUpdateStatus: (checkRemote = false) =>
    request(`/api/admin/update/status${checkRemote ? "?check_remote=true" : ""}`, {
      timeoutMs: ONLINE_UPDATE_TIMEOUT_MS,
    }),
  adminRunUpdate: (body) =>
    request("/api/admin/update/run", { method: "POST", body, timeoutMs: ONLINE_UPDATE_TIMEOUT_MS }),
  adminPaymentConfig: () => request("/api/admin/payments/config"),
  adminPaymentPackages: () => request("/api/admin/payments/packages"),
  adminSavePaymentPackage: (body) =>
    request("/api/admin/payments/packages", { method: "POST", body }),
  adminDisablePaymentPackage: (id, body) =>
    request(`/api/admin/payments/packages/${encodeURIComponent(id)}`, { method: "DELETE", body }),
  adminPaymentProviders: () => request("/api/admin/payments/providers"),
  adminSavePaymentProvider: (provider, body) =>
    request(`/api/admin/payments/providers/${provider}`, { method: "PUT", body }),
};
