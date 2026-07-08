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

export function classifyGenerationError(error, { category = "" } = {}) {
  const detail = error?.detail && typeof error.detail === "object" ? error.detail : null;
  const explicitType = error?.error_type || detail?.error_type;
  const raw = error?.error_message || error?.error || detail?.error_message || detail?.error || errorMessage(error, typeof error === "string" ? error : "");
  const text = String(raw || "").trim();
  const typeMessages = {
    moderation: {
      kind: "moderation",
      message: "内容审核中，管理员确认后会在历史记录里更新结果；请不要重复提交同一任务。",
    },
    user_input: {
      kind: "user_actionable",
      message: "提示词、参考素材或文件格式需要调整。请弱化敏感描述，或更换为常见 JPG/PNG/MP4 后重试。",
    },
    provider_timeout: {
      kind: "provider",
      message: "模型网关响应超时。系统会按实际结果结算或退回积分，请稍后在历史记录确认。",
    },
    provider_error: {
      kind: "provider",
      message: "模型网关暂时不可用或返回异常。请稍后重试；连续失败时请联系管理员检查模型渠道。",
    },
    system_error: {
      kind: "system",
      message: "系统处理异常，请稍后重试；如果连续失败，请联系管理员查看任务日志。",
    },
  };
  if (typeMessages[explicitType]) {
    if (text && explicitType !== "moderation") {
      return { ...typeMessages[explicitType], message: text };
    }
    return typeMessages[explicitType];
  }
  const lower = text.toLowerCase();
  const isVideo = category === "video";

  if (/needs_review|人工|审核|确认中|review/.test(lower)) {
    return {
      kind: "moderation",
      message: "内容审核中，管理员确认后会在历史记录里更新结果；请不要重复提交同一任务。",
    };
  }
  if (/违规|敏感|policy|safety|unsafe|blocked|moderation|content/.test(lower)) {
    return {
      kind: "user_actionable",
      message: "提示词或参考素材可能触发模型审核。请弱化敏感描述、减少露骨表达，或更换参考图后重试。",
    };
  }
  if (/格式|format|unsupported|不支持|invalid image|invalid video|file/.test(lower)) {
    return {
      kind: "user_actionable",
      message: `${isVideo ? "视频" : "图片"}格式可能不被支持。请换成常见 JPG/PNG/MP4 文件，或重新上传后再试。`,
    };
  }
  if (/余额|额度|积分|credits|insufficient/.test(lower)) {
    return {
      kind: "user_actionable",
      message: text || "积分不足，请联系管理员处理后再生成。",
    };
  }
  if (/timeout|timed out|超时|502|503|504|网关|gateway|provider|upstream/.test(lower)) {
    return {
      kind: "provider",
      message: "模型网关响应超时或暂时不可用。系统会按实际结果结算或退回积分，请稍后在历史记录确认。",
    };
  }
  if (/取消|canceled|cancelled/.test(lower)) {
    return { kind: "user_actionable", message: text || "任务已取消，已冻结积分会按规则退回。" };
  }
  return {
    kind: "system",
    message: text || "生成失败，请稍后重试；如果连续失败，请联系管理员检查模型网关。",
  };
}

export function redirectOnAuthError(error, router, setMessage = null, context = "auth probe") {
  if (error?.status === 401) {
    router.push(loginPath());
    return;
  }
  reportBackgroundError(error, context);
  if (setMessage) showError(setMessage, error, "登录状态检查失败");
}
