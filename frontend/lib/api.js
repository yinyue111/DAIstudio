"use client";

export const API_BASE =
  (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");

const DEFAULT_TIMEOUT_MS = 30_000;
const PARSE_TIMEOUT_MS = 90_000;
const REVERSE_TIMEOUT_MS = 120_000;
const UPLOAD_TIMEOUT_MS = 120_000;
const DOWNLOAD_TIMEOUT_MS = 300_000;

export function wsUrl(path) {
  const base = API_BASE || (typeof window !== "undefined" ? window.location.origin : "");
  return `${base.replace(/^http/, "ws")}${path}`;
}

export function getToken() {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem("token");
}

export function setToken(t) {
  window.localStorage.setItem("token", t);
}

export function clearToken() {
  window.localStorage.removeItem("token");
}

// Authenticated file download that reuses the same 401 -> clear-token + redirect
// handling as request(), so CSV/asset downloads don't drift from the REST path.
export async function downloadBlob(path, filename) {
  const headers = {};
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  const { res, blob } = await fetchBlobWithTimeout(
    `${API_BASE}${path}`,
    { headers },
    DOWNLOAD_TIMEOUT_MS,
  );
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("登录已过期，请重新登录");
  }
  if (!res.ok) throw new Error(`下载失败 (${res.status})`);
  const u = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = u;
  if (filename) a.download = filename;
  a.click();
  URL.revokeObjectURL(u);
}

async function withTimeout(timeoutMs, operation) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await operation(controller.signal);
  } catch (e) {
    if (e?.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function fetchTextWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetch(url, { ...options, signal });
    const text = await res.text();
    return { res, text };
  });
}

async function fetchBlobWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetch(url, { ...options, signal });
    if (!res.ok) return { res, blob: null };
    const blob = await res.blob();
    return { res, blob };
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
  if (detail && typeof detail === "object") return detail.msg || JSON.stringify(detail);
  return `请求失败 (${status})`;
}

async function request(path, { method = "GET", body, auth = true, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
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
    },
    timeoutMs,
  );
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined" && !path.startsWith("/api/auth")) {
      window.location.href = "/login";
    }
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
    throw new Error(detailToMessage(data && data.detail, res.status));
  }
  return data;
}

async function upload(path, formData, { auth = true } = {}) {
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
    },
    UPLOAD_TIMEOUT_MS,
  );
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
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
    throw new Error(detailToMessage(data && data.detail, res.status));
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
  sendSmsCode: (phone) =>
    request("/api/auth/sms/send", { method: "POST", body: { phone }, auth: false }),
  login: (phone, password) =>
    request("/api/auth/login", { method: "POST", body: { phone, password }, auth: false }),
  me: () => request("/api/me"),
  config: () => request("/api/config"),
  profile: () => request("/api/profile"),
  profileAssets: ({ type = "all", favorite = false, limit = 60, offset = 0 } = {}) =>
    request(`/api/profile/assets?type=${type}&favorite=${favorite}&limit=${limit}&offset=${offset}`),
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
  uploadImage: (file) => {
    const form = new FormData();
    form.append("file", file);
    return upload("/api/uploads/image", form);
  },
  reverse: (asset_url, target = "image", fallback_image = null) =>
    request("/api/prompt/reverse", {
      method: "POST",
      body: { asset_url, target, fallback_image },
      timeoutMs: REVERSE_TIMEOUT_MS,
    }),
  generate: (payload) => request("/api/generate", { method: "POST", body: payload }),
  task: (id) => request(`/api/tasks/${id}`),
  taskWsTicket: (id) => request(`/api/tasks/${id}/ws-ticket`, { method: "POST" }),
  tasks: (limit = 30, offset = 0) => request(`/api/tasks?limit=${limit}&offset=${offset}`),
  unlock: (assetId) => request(`/api/assets/${assetId}/unlock`, { method: "POST" }),
  favoriteAsset: (assetId) => request(`/api/assets/${assetId}/favorite`, { method: "POST" }),
  deleteAsset: (assetId) => request(`/api/assets/${assetId}`, { method: "DELETE" }),
  retryTask: (taskId) => request(`/api/tasks/${taskId}/retry`, { method: "POST" }),
  downloadUrl: (assetId) => `${API_BASE}/api/assets/${assetId}/download`,
  // admin
  adminWhitelist: () => request("/api/admin/whitelist"),
  adminAddWhitelist: (body) =>
    request("/api/admin/whitelist", { method: "POST", body }),
  adminRemoveWhitelist: (phone, body) =>
    request(`/api/admin/whitelist/${encodeURIComponent(phone)}`, { method: "DELETE", body }),
  adminUsers: () => request("/api/admin/users"),
  adminGrant: (body) => request("/api/admin/quota/grant", { method: "POST", body }),
  adminSetUserStatus: (userId, body) =>
    request(`/api/admin/users/${userId}/status`, { method: "PATCH", body }),
  adminResetPassword: (userId, body) =>
    request(`/api/admin/users/${userId}/reset_password`, { method: "POST", body }),
  adminModels: () => request("/api/admin/models"),
  adminSaveModel: (body) => request("/api/admin/models", { method: "PUT", body }),
  adminReport: (qs = "") => request(`/api/admin/usage/report${qs}`),
  adminReportCsvUrl: (qs = "") => `${API_BASE}/api/admin/usage/report${qs}`,
  adminAudit: (qs = "") => request(`/api/admin/audit${qs}`),
  adminSettings: () => request("/api/admin/settings"),
  adminSaveSettings: (body) => request("/api/admin/settings", { method: "PUT", body }),
  adminGateway: () => request("/api/admin/gateway"),
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
