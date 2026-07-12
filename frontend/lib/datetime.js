const OFFSET_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/i;

function pad(value) {
  return String(value).padStart(2, "0");
}

export function formatLocalDateTime(value, { includeSeconds = false } = {}) {
  if (!value) return "-";

  const input = String(value);
  const normalized = OFFSET_SUFFIX.test(input) ? input : `${input}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return "-";

  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return includeSeconds ? `${day} ${time}:${pad(date.getSeconds())}` : `${day} ${time}`;
}

function parseLocalCalendarDate(value) {
  const text = String(value || "").trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return null;
  const year = Number(match[1]);
  const monthIndex = Number(match[2]) - 1;
  const day = Number(match[3]);
  const date = new Date(year, monthIndex, day, 0, 0, 0, 0);
  if (
    date.getFullYear() !== year
    || date.getMonth() !== monthIndex
    || date.getDate() !== day
  ) {
    return null;
  }
  return date;
}

export function localDateRangeToIsoBounds(createdFrom, createdTo) {
  const start = parseLocalCalendarDate(createdFrom);
  const endDay = parseLocalCalendarDate(createdTo);
  const endExclusive = endDay
    ? new Date(endDay.getFullYear(), endDay.getMonth(), endDay.getDate() + 1, 0, 0, 0, 0)
    : null;
  return {
    startInclusive: start ? start.toISOString() : "",
    endExclusive: endExclusive ? endExclusive.toISOString() : "",
  };
}

export function localDateRangeToProfileAssetParams(createdFrom, createdTo) {
  const { startInclusive, endExclusive } = localDateRangeToIsoBounds(createdFrom, createdTo);
  return {
    created_from: startInclusive,
    created_to: endExclusive ? new Date(new Date(endExclusive).getTime() - 1).toISOString() : "",
  };
}
