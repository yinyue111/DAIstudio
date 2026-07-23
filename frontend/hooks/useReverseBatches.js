"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { normalizeReverseBatch, normalizeReverseBatchList } from "../lib/reverseBatches";

const BATCH_POLL_INTERVAL_MS = 1800;

export default function useReverseBatches({ ownerKey = "", enabled = true, onError } = {}) {
  const [batch, setBatch] = useState(null);
  const [recentBatches, setRecentBatches] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const ownerRunRef = useRef(0);
  const batchRef = useRef(null);
  const errorHandlerRef = useRef(onError);
  errorHandlerRef.current = onError;
  batchRef.current = batch;

  const publish = useCallback((payload) => {
    const normalized = normalizeReverseBatch(payload);
    batchRef.current = normalized;
    setBatch(normalized);
    setRecentBatches((current) => [
      normalized,
      ...current.filter((item) => item.id !== normalized.id),
    ].slice(0, 10));
    return normalized;
  }, []);

  const refreshBatch = useCallback(async (id = batchRef.current?.id) => {
    if (!id) return null;
    const ownerRun = ownerRunRef.current;
    const payload = await api.reverseBatch(id);
    if (ownerRun !== ownerRunRef.current) return null;
    return publish(payload);
  }, [publish]);

  const refreshRecent = useCallback(async () => {
    if (!enabled || !ownerKey) return [];
    const ownerRun = ownerRunRef.current;
    setLoading(true);
    try {
      const rows = normalizeReverseBatchList(await api.reverseBatches({ limit: 10 }));
      if (ownerRun !== ownerRunRef.current) return [];
      setRecentBatches(rows);
      if (!batchRef.current && rows[0]) {
        try {
          await refreshBatch(rows[0].id);
        } catch (_error) {
          // List summaries may remain usable when one detail refresh fails.
        }
      }
      return rows;
    } catch (error) {
      if (ownerRun === ownerRunRef.current) errorHandlerRef.current?.(error);
      return [];
    } finally {
      if (ownerRun === ownerRunRef.current) setLoading(false);
    }
  }, [enabled, ownerKey, refreshBatch]);

  const createBatch = useCallback(async (body) => {
    const ownerRun = ownerRunRef.current;
    setBusyAction("create");
    try {
      const created = publish(await api.createReverseBatch(body));
      if (ownerRun !== ownerRunRef.current) return null;
      return created;
    } finally {
      if (ownerRun === ownerRunRef.current) setBusyAction("");
    }
  }, [publish]);

  const cancelBatch = useCallback(async () => {
    const current = batchRef.current;
    if (!current?.id) return null;
    const ownerRun = ownerRunRef.current;
    setBusyAction("cancel");
    try {
      const canceled = publish(await api.cancelReverseBatch(current.id));
      if (ownerRun !== ownerRunRef.current) return null;
      return canceled;
    } finally {
      if (ownerRun === ownerRunRef.current) setBusyAction("");
    }
  }, [publish]);

  const openBatch = useCallback(async (id) => {
    setBusyAction(`open-${id}`);
    try {
      return await refreshBatch(id);
    } finally {
      setBusyAction("");
    }
  }, [refreshBatch]);

  useEffect(() => {
    ownerRunRef.current += 1;
    batchRef.current = null;
    setBatch(null);
    setRecentBatches([]);
    if (enabled && ownerKey) refreshRecent();
  }, [enabled, ownerKey, refreshRecent]);

  useEffect(() => {
    if (!enabled || !batch?.id || !batch.active) return undefined;
    let canceled = false;
    const timer = setTimeout(async () => {
      if (canceled) return;
      try {
        await refreshBatch(batch.id);
      } catch (error) {
        if (!canceled) errorHandlerRef.current?.(error);
      }
    }, BATCH_POLL_INTERVAL_MS);
    return () => {
      canceled = true;
      clearTimeout(timer);
    };
  }, [enabled, batch?.id, batch?.active, batch?.updated_at, refreshBatch]);

  return {
    batch,
    recentBatches,
    loading,
    busyAction,
    createBatch,
    cancelBatch,
    openBatch,
    refreshBatch,
    refreshRecent,
    setBatch,
  };
}
