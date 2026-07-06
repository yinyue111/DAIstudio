import { loginPath } from "./api";

export function errorMessage(error, fallback = "请求失败，请稍后重试") {
  const message = error?.message || error?.detail || "";
  return typeof message === "string" && message.trim() ? message.trim() : fallback;
}

export function reportBackgroundError(error, context = "background request") {
  if (process.env.NODE_ENV === "test") return;
  // Keep best-effort refresh failures visible to developers without interrupting
  // user workflows such as polling, balance refresh, or usage counters.
  console.warn(`[${context}]`, error);
}

export function showError(setMessage, error, prefix = "") {
  const message = errorMessage(error);
  setMessage(prefix ? `${prefix}: ${message}` : message);
}

export function redirectOnAuthError(error, router, setMessage = null, context = "auth probe") {
  if (error?.status === 401) {
    router.push(loginPath());
    return;
  }
  reportBackgroundError(error, context);
  if (setMessage) showError(setMessage, error, "登录状态检查失败");
}
