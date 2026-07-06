"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

export const DEFAULT_VISIBLE_ITEM_COUNT = 48;
export const DEFAULT_VISIBLE_ITEM_STEP = 24;

export function clampVisibleItemCount(totalCount, requestedCount) {
  const total = Math.max(0, Number(totalCount || 0));
  const requested = Math.max(0, Number(requestedCount || 0));
  return Math.min(total, requested);
}

export function nextVisibleItemCount(totalCount, currentCount, step = DEFAULT_VISIBLE_ITEM_STEP) {
  const total = Math.max(0, Number(totalCount || 0));
  const current = Math.max(0, Number(currentCount || 0));
  const increment = Math.max(1, Number(step || DEFAULT_VISIBLE_ITEM_STEP));
  return clampVisibleItemCount(total, current + increment);
}

export function getVisibleItemWindow(items, visibleCount) {
  const list = Array.isArray(items) ? items : [];
  const cappedCount = clampVisibleItemCount(list.length, visibleCount);
  return {
    items: list.slice(0, cappedCount),
    totalCount: list.length,
    visibleCount: cappedCount,
    hasMore: cappedCount < list.length,
  };
}

function defaultResetKey(items) {
  if (!Array.isArray(items) || items.length === 0) return "empty";
  const first = items[0];
  const last = items[items.length - 1];
  return `${items.length}:${first?.id ?? ""}:${last?.id ?? ""}`;
}

export default function useVisibleItemWindow(items, {
  initialCount = DEFAULT_VISIBLE_ITEM_COUNT,
  step = DEFAULT_VISIBLE_ITEM_STEP,
  resetKey,
} = {}) {
  const effectiveResetKey = resetKey ?? defaultResetKey(items);
  const [visibleState, setVisibleState] = useState(() => ({
    resetKey: effectiveResetKey,
    limit: initialCount,
  }));
  const visibleLimit = visibleState.resetKey === effectiveResetKey ? visibleState.limit : initialCount;

  useEffect(() => {
    setVisibleState({ resetKey: effectiveResetKey, limit: initialCount });
  }, [effectiveResetKey, initialCount]);

  const windowState = useMemo(
    () => getVisibleItemWindow(items, visibleLimit),
    [items, visibleLimit],
  );

  const showMore = useCallback(() => {
    setVisibleState((current) => ({
      resetKey: effectiveResetKey,
      limit: nextVisibleItemCount(
        windowState.totalCount,
        current.resetKey === effectiveResetKey ? current.limit : initialCount,
        step,
      ),
    }));
  }, [effectiveResetKey, initialCount, step, windowState.totalCount]);

  const reset = useCallback(() => {
    setVisibleState({ resetKey: effectiveResetKey, limit: initialCount });
  }, [effectiveResetKey, initialCount]);

  return {
    ...windowState,
    initialCount,
    showMore,
    reset,
  };
}
