export type StructuredPath = Array<string | number>;

const ENVELOPE_META_KEYS = [
  "type", "enum", "options", "source", "status", "conflict", "label", "description",
  "reset_value", "source_value", "item_type", "item_default", "multiline",
];

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function cloneStructuredValue<T>(value: T): T {
  if (Array.isArray(value)) return value.map(cloneStructuredValue) as T;
  const object = record(value);
  if (!object) return value;
  return Object.fromEntries(
    Object.entries(object).map(([key, item]) => [key, cloneStructuredValue(item)]),
  ) as T;
}

export function isStructuredFieldEnvelope(value: unknown) {
  const object = record(value);
  return Boolean(
    object
    && Object.prototype.hasOwnProperty.call(object, "value")
    && ENVELOPE_META_KEYS.some((key) => Object.prototype.hasOwnProperty.call(object, key)),
  );
}

export function structuredFieldValue(value: unknown): unknown {
  return isStructuredFieldEnvelope(value)
    ? (value as Record<string, unknown>).value
    : value;
}

export function structuredValueAtPath(root: unknown, path: StructuredPath): unknown {
  let current = root;
  for (const segment of path) {
    current = structuredFieldValue(current);
    if (Array.isArray(current) && typeof segment === "number") current = current[segment];
    else current = record(current)?.[String(segment)];
  }
  return current;
}

function setAtPath(current: unknown, path: StructuredPath, index: number, nextValue: unknown): unknown {
  if (isStructuredFieldEnvelope(current)) {
    const envelope = current as Record<string, unknown>;
    return { ...envelope, value: setAtPath(envelope.value, path, index, nextValue) };
  }
  if (index === path.length) return cloneStructuredValue(nextValue);
  const segment = path[index];
  if (typeof segment === "number") {
    const list = Array.isArray(current) ? [...current] : [];
    list[segment] = setAtPath(list[segment], path, index + 1, nextValue);
    return list;
  }
  const object = record(current) ? { ...(current as Record<string, unknown>) } : {};
  object[segment] = setAtPath(object[segment], path, index + 1, nextValue);
  return object;
}

export function setStructuredValueAtPath(root: unknown, path: StructuredPath, nextValue: unknown) {
  return setAtPath(root, path, 0, nextValue) as Record<string, unknown>;
}

export function removeStructuredListItem(root: unknown, path: StructuredPath, index: number) {
  const list = structuredFieldValue(structuredValueAtPath(root, path));
  if (!Array.isArray(list)) return cloneStructuredValue(root) as Record<string, unknown>;
  return setStructuredValueAtPath(root, path, list.filter((_, itemIndex) => itemIndex !== index));
}

export function structuredPathKey(path: StructuredPath) {
  return path.map((segment) => String(segment).replaceAll("~", "~0").replaceAll("/", "~1")).join("/");
}

function embeddedMetadata(rawValue: unknown) {
  if (!isStructuredFieldEnvelope(rawValue)) return {};
  return Object.fromEntries(
    Object.entries(rawValue as Record<string, unknown>).filter(([key]) => key !== "value"),
  );
}

export function structuredFieldMetadata(
  metadata: unknown,
  path: StructuredPath,
  rawValue: unknown,
) {
  const map = record(metadata) || {};
  const pointer = `/${structuredPathKey(path)}`;
  const dotted = path.join(".");
  const leaf = String(path.at(-1) ?? "");
  const explicit = record(map[pointer]) || record(map[dotted]) || record(map[leaf]) || {};
  return { ...embeddedMetadata(rawValue), ...explicit };
}

export function structuredFieldKind(value: unknown, metadata: Record<string, unknown> = {}) {
  const requested = String(metadata.type || "").toLowerCase();
  if (Array.isArray(metadata.enum) || Array.isArray(metadata.options) || requested === "enum") return "enum";
  if (["string", "number", "boolean", "object", "list", "array"].includes(requested)) {
    return requested === "array" ? "list" : requested;
  }
  const actual = structuredFieldValue(value);
  if (Array.isArray(actual)) return "list";
  if (actual !== null && typeof actual === "object") return "object";
  if (typeof actual === "number") return "number";
  if (typeof actual === "boolean") return "boolean";
  return "string";
}

export function structuredEnumOptions(metadata: Record<string, unknown> = {}) {
  const source = Array.isArray(metadata.options) ? metadata.options : metadata.enum;
  if (!Array.isArray(source)) return [];
  return source.map((item) => {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const option = item as Record<string, unknown>;
      return { value: option.value, label: String(option.label ?? option.value ?? "") };
    }
    return { value: item, label: String(item ?? "") };
  });
}

export function emptyStructuredListItem(
  listValue: unknown,
  metadata: Record<string, unknown> = {},
) {
  if (Object.prototype.hasOwnProperty.call(metadata, "item_default")) {
    return cloneStructuredValue(metadata.item_default);
  }
  const list = Array.isArray(listValue) ? listValue : [];
  const sample = structuredFieldValue(list[0]);
  const kind = String(metadata.item_type || "").toLowerCase() || (
    Array.isArray(sample) ? "list" : sample !== null ? typeof sample : "string"
  );
  if (kind === "object") return {};
  if (kind === "list" || kind === "array") return [];
  if (kind === "number") return 0;
  if (kind === "boolean") return false;
  return "";
}

export function structuredValuesEqual(left: unknown, right: unknown) {
  return JSON.stringify(structuredFieldValue(left)) === JSON.stringify(structuredFieldValue(right));
}

/**
 * 容错还原历史退化数据：早期编辑器把对象/数组 JSON.stringify 后原样写回，
 * 嵌套维度会退化成字符串。这里只在字符串确实能解析回对象/数组时还原，
 * 普通文案（包括解析失败的残缺 JSON）原样保留，绝不抛错。
 */
export function reviveStructuredValue(value: unknown): unknown {
  if (isStructuredFieldEnvelope(value)) {
    const envelope = value as Record<string, unknown>;
    const revived = reviveStructuredValue(envelope.value);
    return revived === envelope.value ? value : { ...envelope, value: revived };
  }
  if (typeof value !== "string") return value;
  const text = value.trim();
  const looksStructured = (text.startsWith("{") && text.endsWith("}"))
    || (text.startsWith("[") && text.endsWith("]"));
  if (!looksStructured) return value;
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed !== null && typeof parsed === "object" ? parsed : value;
  } catch {
    return value;
  }
}

/** 把数字输入框的文本解析回 number；空串或非法输入保留原值，避免维度类型被编辑退化。 */
export function parseStructuredNumberInput(text: unknown, fallback: unknown = 0): unknown {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return fallback;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : fallback;
}
