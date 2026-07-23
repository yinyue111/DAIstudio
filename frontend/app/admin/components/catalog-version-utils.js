export const EMPTY_TOOL_VERSION = Object.freeze({
  schema_version: "tool.v1",
  input_schema: {},
  workflow: {},
  pricing_policy: {},
  capabilities: {},
});

export const EMPTY_MODEL_ROUTE = Object.freeze({
  route_key: "",
  name: "",
  model_id: "",
  provider: "",
  base_url: "",
  gateway_format: "openai",
  api_key: "",
  api_key_clear: false,
  extra: {},
  priority: 100,
  enabled: true,
  managed_by_model_config: false,
  failure_threshold: 5,
  window_seconds: 60,
  cooldown_seconds: 60,
});

export function modelVersionDraft(kind, version = {}) {
  if (kind === "capability") {
    return {
      kind,
      schema_version: String(version?.schema_version || "capability.v1"),
      capabilities: jsonObjectText(version?.capabilities),
    };
  }
  if (kind === "price") {
    return {
      kind,
      schema_version: String(version?.schema_version || "credit-price.v1"),
      base_cost_credits: Number(version?.base_cost_credits ?? 0),
      unlock_cost_credits: Number(version?.unlock_cost_credits ?? 0),
      pricing: jsonObjectText(version?.pricing),
    };
  }
  throw new Error("未知模型版本类型");
}

function modelVersionCredits(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > 1_000_000) {
    throw new Error(`${label}必须是 0-1000000 的整数`);
  }
  return parsed;
}

export function modelVersionPayload(draft, { includeKind = true } = {}) {
  const kind = String(draft?.kind || "");
  const schemaVersion = String(draft?.schema_version || "").trim();
  if (!schemaVersion) throw new Error("请填写版本 Schema");
  let payload;
  if (kind === "capability") {
    payload = {
      schema_version: schemaVersion,
      capabilities: parseJsonObject(draft?.capabilities, "能力 JSON"),
    };
  } else if (kind === "price") {
    payload = {
      schema_version: schemaVersion,
      base_cost_credits: modelVersionCredits(draft?.base_cost_credits, "调用积分"),
      unlock_cost_credits: modelVersionCredits(draft?.unlock_cost_credits, "解锁积分"),
      pricing: parseJsonObject(draft?.pricing, "计价 JSON"),
    };
  } else {
    throw new Error("未知模型版本类型");
  }
  return includeKind ? { kind, ...payload } : payload;
}

export function jsonObjectText(value) {
  return JSON.stringify(value && typeof value === "object" && !Array.isArray(value) ? value : {}, null, 2);
}

export function parseJsonObject(value, label) {
  const text = String(value || "").trim();
  if (!text) return {};
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`${label} 不是合法 JSON`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON 对象`);
  }
  return parsed;
}

export function toolVersionDraft(version = EMPTY_TOOL_VERSION) {
  return {
    schema_version: String(version?.schema_version || "tool.v1"),
    input_schema: jsonObjectText(version?.input_schema),
    workflow: jsonObjectText(version?.workflow),
    pricing_policy: jsonObjectText(version?.pricing_policy),
    capabilities: jsonObjectText(version?.capabilities),
  };
}

export function toolVersionPayload(draft) {
  const schemaVersion = String(draft?.schema_version || "").trim();
  if (!schemaVersion) throw new Error("请填写工具 Schema 版本");
  return {
    schema_version: schemaVersion,
    input_schema: parseJsonObject(draft?.input_schema, "输入 Schema"),
    workflow: parseJsonObject(draft?.workflow, "工作流"),
    pricing_policy: parseJsonObject(draft?.pricing_policy, "计价策略"),
    capabilities: parseJsonObject(draft?.capabilities, "能力声明"),
  };
}

function routeInteger(value, label, min, max) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${label}必须是 ${min}-${max} 的整数`);
  }
  return parsed;
}

function optionalRouteText(value) {
  const normalized = String(value || "").trim();
  return normalized || null;
}

export function modelRouteDraft(route = EMPTY_MODEL_ROUTE) {
  return {
    route_key: String(route?.route_key || ""),
    name: String(route?.name || ""),
    model_id: String(route?.model_id || ""),
    provider: String(route?.provider || ""),
    base_url: String(route?.base_url || ""),
    gateway_format: String(route?.gateway_format || "openai"),
    api_key: "",
    api_key_clear: false,
    extra: jsonObjectText(route?.extra),
    priority: Number(route?.priority ?? 100),
    enabled: route?.enabled !== false,
    managed_by_model_config: Boolean(route?.managed_by_model_config),
    failure_threshold: Number(route?.failure_threshold ?? 5),
    window_seconds: Number(route?.window_seconds ?? 60),
    cooldown_seconds: Number(route?.cooldown_seconds ?? 60),
  };
}

export function modelRoutePayload(draft, { creating = false } = {}) {
  const routeKey = String(draft?.route_key || "").trim();
  const name = String(draft?.name || "").trim();
  if (!name) throw new Error("请填写路由名称");
  if (creating && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(routeKey)) {
    throw new Error("路由标识只能使用小写字母、数字和连字符");
  }

  const payload = {
    name,
    priority: routeInteger(draft?.priority, "优先级", 0, 100000),
    enabled: Boolean(draft?.enabled),
    failure_threshold: routeInteger(draft?.failure_threshold, "失败阈值", 1, 100),
    window_seconds: routeInteger(draft?.window_seconds, "统计窗口", 1, 86400),
    cooldown_seconds: routeInteger(draft?.cooldown_seconds, "冷却时间", 1, 86400),
  };

  if (creating) payload.route_key = routeKey;
  if (!creating) payload.managed_by_model_config = Boolean(draft?.managed_by_model_config);

  if (!draft?.managed_by_model_config) {
    payload.model_id = optionalRouteText(draft?.model_id);
    payload.provider = optionalRouteText(draft?.provider);
    payload.base_url = optionalRouteText(draft?.base_url);
    payload.gateway_format = optionalRouteText(draft?.gateway_format);
    payload.extra = parseJsonObject(draft?.extra, "路由扩展配置");
    const apiKey = String(draft?.api_key || "").trim();
    if (apiKey) payload.api_key = apiKey;
    if (!creating && draft?.api_key_clear) payload.api_key_clear = true;
  }
  return payload;
}
