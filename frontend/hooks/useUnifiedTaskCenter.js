"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, wsUrl } from "../lib/api";
import {
  EMPTY_TASK_COUNTS,
  isLiveUnifiedTask,
  mergeUnifiedTasks,
  normalizeUnifiedTaskPage,
} from "../lib/unifiedTasks";

const ACTIVE_POLL_MS = 6_000;
const IDLE_POLL_MS = 30_000;
const MAX_WS_RECONNECTS = 3;

function normalizeFilters(filters = {}) {
  return {
    kind: filters.kind || "all",
    status: filters.status || "all",
    category: filters.category || "all",
  };
}

export default function useUnifiedTaskCenter({
  enabled = true,
  initialFilters = {},
  limit = 20,
  live = true,
} = {}) {
  const [filters, setFiltersState] = useState(() => normalizeFilters(initialFilters));
  const [items, setItems] = useState([]);
  const [counts, setCounts] = useState(EMPTY_TASK_COUNTS);
  const [total, setTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(Boolean(enabled));
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [transport, setTransport] = useState("polling");

  const mountedRef = useRef(false);
  const loadingRef = useRef(false);
  const itemsRef = useRef([]);
  const cursorRef = useRef(null);
  const filtersRef = useRef(filters);
  const requestRef = useRef(0);
  const pollTimerRef = useRef(null);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const refreshTimerRef = useRef(null);

  filtersRef.current = filters;

  const applyPage = useCallback((page, { reset }) => {
    const normalized = normalizeUnifiedTaskPage(page);
    const nextItems = mergeUnifiedTasks(itemsRef.current, normalized.items, reset);
    itemsRef.current = nextItems;
    cursorRef.current = normalized.next_cursor || null;
    if (!mountedRef.current) return;
    setItems(nextItems);
    setCounts(normalized.counts);
    setTotal(normalized.total);
    setNextCursor(normalized.next_cursor || null);
    setHasMore(normalized.has_more);
  }, []);

  const load = useCallback(async ({ reset = true, silent = false } = {}) => {
    if (!enabled || (!reset && loadingRef.current)) return null;
    const cursor = reset ? "" : cursorRef.current;
    if (!reset && !cursor) return null;
    // A filter change or event refresh is newer than any outstanding reset.
    // Let the old response finish, but prevent it from replacing newer rows.
    loadingRef.current = true;
    const requestId = ++requestRef.current;
    if (mountedRef.current) {
      if (reset && !silent) setLoading(true);
      if (!reset) setLoadingMore(true);
      if (!silent) setError("");
    }
    try {
      const page = await api.taskCenter({ ...filtersRef.current, limit, cursor });
      if (requestId === requestRef.current) applyPage(page, { reset });
      return page;
    } catch (loadError) {
      if (requestId === requestRef.current && mountedRef.current && !silent) {
        setError(loadError?.message || "任务中心加载失败");
      }
      return null;
    } finally {
      if (requestId === requestRef.current && mountedRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
      if (requestId === requestRef.current) loadingRef.current = false;
    }
  }, [applyPage, enabled, limit]);

  const refresh = useCallback((options = {}) => load({ reset: true, ...options }), [load]);
  const loadMore = useCallback(() => load({ reset: false }), [load]);

  const setFilters = useCallback((next) => {
    setFiltersState((current) => ({
      ...current,
      ...(typeof next === "function" ? next(current) : next),
    }));
  }, []);

  useEffect(() => {
    if (!enabled) {
      itemsRef.current = [];
      cursorRef.current = null;
      setItems([]);
      setCounts(EMPTY_TASK_COUNTS);
      setTotal(0);
      setNextCursor(null);
      setHasMore(false);
      setLoading(false);
      return undefined;
    }
    itemsRef.current = [];
    cursorRef.current = null;
    load({ reset: true });
    return undefined;
  }, [enabled, filters.kind, filters.status, filters.category, load]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!enabled || !live) return undefined;
    let stopped = false;
    let wsAttempts = 0;

    const clearReconnect = () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    };
    const scheduleRefresh = () => {
      if (refreshTimerRef.current) return;
      refreshTimerRef.current = setTimeout(() => {
        refreshTimerRef.current = null;
        refresh({ silent: true });
      }, 250);
    };
    const schedulePoll = () => {
      if (stopped) return;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      const hasLiveTask = itemsRef.current.some(isLiveUnifiedTask);
      pollTimerRef.current = setTimeout(async () => {
        await refresh({ silent: true });
        schedulePoll();
      }, hasLiveTask ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };
    const connect = async () => {
      if (stopped) return;
      try {
        const ticketPayload = await api.eventWsTicket();
        if (stopped || !ticketPayload?.ticket) return;
        const ws = new WebSocket(wsUrl(`/ws/events?ticket=${encodeURIComponent(ticketPayload.ticket)}`));
        wsRef.current = ws;
        ws.onopen = () => {
          if (!stopped && wsRef.current === ws) {
            wsAttempts = 0;
            setTransport("events");
          }
        };
        ws.onmessage = (event) => {
          if (stopped || wsRef.current !== ws) return;
          try {
            const message = JSON.parse(event.data);
            if (Array.isArray(message?.events) && message.events.length) scheduleRefresh();
          } catch (_error) {
            // The polling loop remains authoritative when a push payload is malformed.
          }
        };
        ws.onerror = () => { try { ws.close(); } catch (_error) {} };
        ws.onclose = () => {
          if (stopped || wsRef.current !== ws) return;
          wsRef.current = null;
          setTransport("polling");
          if (wsAttempts >= MAX_WS_RECONNECTS) return;
          const delay = 750 * (2 ** wsAttempts);
          wsAttempts += 1;
          clearReconnect();
          reconnectTimerRef.current = setTimeout(connect, delay);
        };
      } catch (_error) {
        setTransport("polling");
        if (!stopped && wsAttempts < MAX_WS_RECONNECTS) {
          const delay = 750 * (2 ** wsAttempts);
          wsAttempts += 1;
          clearReconnect();
          reconnectTimerRef.current = setTimeout(connect, delay);
        }
      }
    };

    schedulePoll();
    connect();
    return () => {
      stopped = true;
      clearReconnect();
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
      pollTimerRef.current = null;
      refreshTimerRef.current = null;
      const ws = wsRef.current;
      wsRef.current = null;
      try { ws?.close(); } catch (_error) {}
    };
  }, [enabled, live, refresh]);

  return useMemo(() => ({
    filters,
    setFilters,
    items,
    counts,
    total,
    nextCursor,
    hasMore,
    loading,
    loadingMore,
    error,
    transport,
    activeCount: Number(counts.active || 0),
    refresh,
    loadMore,
  }), [
    filters, setFilters, items, counts, total, nextCursor, hasMore, loading, loadingMore,
    error, transport, refresh, loadMore,
  ]);
}
