const REVERSE_FILTER_KEYS = new Set(["mediaType", "model", "focus"]);

export function updateReverseReportFilters(current, key, value) {
  if (!REVERSE_FILTER_KEYS.has(key)) {
    throw new Error(`Unsupported reverse report filter: ${key}`);
  }
  return {
    mediaType: String(current?.mediaType || ""),
    model: String(current?.model || ""),
    focus: String(current?.focus || ""),
    [key]: String(value || ""),
  };
}

export function buildReverseReportQuery(filters, format = "json") {
  const params = new URLSearchParams();
  if (filters?.start) params.set("start", String(filters.start));
  if (filters?.end) params.set("end", String(filters.end));
  if (filters?.mediaType) params.set("media_type", String(filters.mediaType));
  if (filters?.model) params.set("model", String(filters.model));
  if (filters?.focus) params.set("focus", String(filters.focus));
  if (format === "csv") params.set("format", "csv");
  const query = params.toString();
  return query ? `?${query}` : "";
}
