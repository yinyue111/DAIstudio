"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  isActiveReverseOperation,
  isTerminalReverseOperation,
  normalizeReverseOperation,
  normalizeReverseOperationList,
  reverseOperationTrackingLost,
} from "../lib/reverseOperations";

const POLL_INTERVAL_MS = 1400;
const WS_WATCHDOG_INTERVAL_MS = 5000;
const MAX_WS_RECONNECTS = 2;

function operationMode(operation, fallback = "image") {
  const requestContext = operation?.request_context || {};
  const snapshot = operation?.workspace_snapshot_v2 || {};
  return String(
    requestContext.workspace_mode
      || requestContext.creation_mode
      || snapshot.creation_mode
      || fallback,
  );
}

function operationTrackingKey(operation, mode) {
  const target = operation?.target || operation?.workspace_snapshot_v2?.target || "";
  return ["product_profile", "portrait_profile"].includes(target) ? `${mode}::profile` : mode;
}

export default function useReverseOperationTracking({
  ownerKey = "",
  resumeOperations = [],
  onOperationUpdate,
  onOperationSettled,
}) {
  const [operationsByMode, setOperationsByMode] = useState({});
  const trackersRef = useRef(new Map());
  const runRef = useRef(0);
  const ownerRunRef = useRef(0);
  const resumeAttemptsRef = useRef(new Set());
  const listedOwnerRef = useRef("");
  const resumeOperationsRef = useRef([]);
  const callbacksRef = useRef({ onOperationUpdate, onOperationSettled });
  callbacksRef.current = { onOperationUpdate, onOperationSettled };
  resumeOperationsRef.current = Array.isArray(resumeOperations) ? resumeOperations : [];
  const resumeSignature = resumeOperationsRef.current
    .map((candidate) => `${candidate?.mode || ""}:${candidate?.trackingKey || ""}:${candidate?.operation?.id || ""}`)
    .filter((value) => !value.endsWith(":"))
    .sort()
    .join("|");

  const stopTracker = useCallback((trackingKey) => {
    const tracker = trackersRef.current.get(trackingKey);
    if (!tracker) return;
    tracker.stopped = true;
    if (tracker.timer) clearTimeout(tracker.timer);
    if (tracker.reconnectTimer) clearTimeout(tracker.reconnectTimer);
    if (tracker.ws) {
      try { tracker.ws.close(); } catch (error) { reportBackgroundError(error, "close reverse websocket"); }
    }
    trackersRef.current.delete(trackingKey);
  }, []);

  const publish = useCallback((operation, mode, context = null, trackingKey = mode) => {
    setOperationsByMode((current) => ({ ...current, [trackingKey]: operation }));
    callbacksRef.current.onOperationUpdate?.(operation, mode, context);
  }, []);

  const settle = useCallback((operation, mode, context = null, trackingKey = mode) => {
    stopTracker(trackingKey);
    publish(operation, mode, context, trackingKey);
    callbacksRef.current.onOperationSettled?.(operation, mode, context);
  }, [publish, stopTracker]);

  const startTracking = useCallback((payload, options = {}) => {
    let initial;
    try {
      initial = normalizeReverseOperation(payload);
    } catch (error) {
      return Promise.reject(error);
    }
    const mode = options.mode || operationMode(initial);
    const trackingKey = options.trackingKey || operationTrackingKey(initial, mode);
    const context = options.context || null;
    stopTracker(trackingKey);
    const tracker = {
      id: initial.id,
      mode,
      trackingKey,
      context,
      runId: ++runRef.current,
      stopped: false,
      timer: null,
      reconnectTimer: null,
      ws: null,
      wsAttempts: 0,
      failures: 0,
      polling: false,
      operation: initial,
    };
    trackersRef.current.set(trackingKey, tracker);
    publish(initial, mode, context, trackingKey);

    const isCurrent = () => (
      !tracker.stopped
      && trackersRef.current.get(trackingKey) === tracker
      && tracker.runId <= runRef.current
    );

    const handle = (nextPayload) => {
      if (!isCurrent()) return null;
      const operation = normalizeReverseOperation(nextPayload);
      if (String(operation.id) !== String(tracker.id)) return null;
      tracker.operation = operation;
      tracker.failures = 0;
      if (isTerminalReverseOperation(operation) || operation.status === "needs_confirmation") {
        settle(operation, mode, context, trackingKey);
      } else {
        publish(operation, mode, context, trackingKey);
      }
      return operation;
    };

    const poll = async () => {
      if (!isCurrent()) return;
      try {
        const operation = handle(await api.reverseOperation(tracker.id));
        if (!operation || !isActiveReverseOperation(operation) || operation.status === "needs_confirmation") return;
      } catch (error) {
        tracker.failures += 1;
        if (tracker.failures >= 5) {
          const operation = reverseOperationTrackingLost(tracker.operation || initial);
          stopTracker(trackingKey);
          publish(operation, mode, context, trackingKey);
          return;
        }
      }
      if (isCurrent()) {
        tracker.timer = setTimeout(
          poll,
          tracker.polling ? POLL_INTERVAL_MS : WS_WATCHDOG_INTERVAL_MS,
        );
      }
    };

    const fallbackToPolling = () => {
      if (!isCurrent() || tracker.polling) return;
      tracker.polling = true;
      if (tracker.reconnectTimer) clearTimeout(tracker.reconnectTimer);
      tracker.reconnectTimer = null;
      if (tracker.timer) clearTimeout(tracker.timer);
      tracker.timer = null;
      if (tracker.ws) {
        const ws = tracker.ws;
        tracker.ws = null;
        try { ws.close(); } catch (error) { reportBackgroundError(error, "close reverse websocket before polling"); }
      }
      tracker.timer = setTimeout(poll, 0);
    };

    const connect = async () => {
      if (!isCurrent() || tracker.polling) return;
      try {
        const ticketPayload = await api.reverseOperationWsTicket(tracker.id);
        if (!isCurrent() || !ticketPayload?.ticket) return fallbackToPolling();
        const ws = new WebSocket(wsUrl(
          `/ws/prompt/reverse-operations/${encodeURIComponent(tracker.id)}?ticket=${encodeURIComponent(ticketPayload.ticket)}`,
        ));
        tracker.ws = ws;
        ws.onmessage = async (event) => {
          if (!isCurrent()) return;
          let eventPayload;
          try { eventPayload = JSON.parse(event.data); } catch (_error) { return fallbackToPolling(); }
          const merged = { ...tracker.operation, ...(eventPayload.operation || eventPayload) };
          const status = String(merged.status || "");
          if (isTerminalReverseOperation(merged) || status === "needs_confirmation") {
            try { handle(await api.reverseOperation(tracker.id)); } catch (_error) { handle(merged); }
          } else {
            handle(merged);
          }
        };
        ws.onerror = () => {
          try { ws.close(); } catch (error) { reportBackgroundError(error, "close failed reverse websocket"); }
        };
        ws.onclose = () => {
          if (!isCurrent() || tracker.ws !== ws) return;
          tracker.ws = null;
          if (tracker.polling) return;
          if (tracker.wsAttempts >= MAX_WS_RECONNECTS) return fallbackToPolling();
          const delay = 600 * (2 ** tracker.wsAttempts);
          tracker.wsAttempts += 1;
          tracker.reconnectTimer = setTimeout(connect, delay);
        };
      } catch (_error) {
        fallbackToPolling();
      }
    };

    if (isTerminalReverseOperation(initial) || initial.status === "needs_confirmation") {
      settle(initial, mode, context, trackingKey);
    } else {
      connect();
      tracker.timer = setTimeout(poll, WS_WATCHDOG_INTERVAL_MS);
    }
    return Promise.resolve(initial);
  }, [publish, settle, stopTracker]);

  const cancelOperation = useCallback(async (trackingKey, id = null) => {
    const operationId = id || operationsByMode[trackingKey]?.id;
    if (!operationId) return null;
    const tracker = trackersRef.current.get(trackingKey);
    const currentOperation = tracker?.operation || operationsByMode[trackingKey] || null;
    if (currentOperation && !isActiveReverseOperation(currentOperation)) return currentOperation;
    const mode = tracker?.mode || trackingKey.split("::")[0];
    const context = tracker?.context || null;
    const ownerRun = ownerRunRef.current;
    const operation = normalizeReverseOperation(await api.cancelReverseOperation(operationId));
    if (ownerRun !== ownerRunRef.current) return operation;
    if (isTerminalReverseOperation(operation)) {
      settle(operation, mode, context, trackingKey);
      return operation;
    }
    if (isActiveReverseOperation(operation)) {
      const currentTracker = trackersRef.current.get(trackingKey);
      if (currentTracker) {
        currentTracker.operation = operation;
        publish(operation, mode, context, trackingKey);
      } else if (operation.status !== "needs_confirmation") {
        startTracking(operation, { mode, context, trackingKey });
      } else {
        publish(operation, mode, context, trackingKey);
      }
      return operation;
    }
    settle(operation, mode, context, trackingKey);
    return operation;
  }, [operationsByMode, publish, settle, startTracking]);

  const confirmCover = useCallback(async (mode, fallbackImage = null, id = null) => {
    const operationId = id || operationsByMode[mode]?.id;
    if (!operationId) throw new Error("没有等待确认的反推任务");
    const context = trackersRef.current.get(mode)?.context || null;
    const ownerRun = ownerRunRef.current;
    const operation = normalizeReverseOperation(
      await api.confirmReverseOperationCover(operationId, fallbackImage),
    );
    if (ownerRun !== ownerRunRef.current) return operation;
    return startTracking(operation, { mode, context });
  }, [operationsByMode, startTracking]);

  const stopAll = useCallback(() => {
    for (const mode of [...trackersRef.current.keys()]) stopTracker(mode);
  }, [stopTracker]);

  const resetOwner = useCallback(() => {
    ownerRunRef.current += 1;
    stopAll();
    resumeAttemptsRef.current.clear();
    listedOwnerRef.current = "";
    setOperationsByMode({});
  }, [stopAll]);

  useEffect(() => {
    if (!ownerKey) return undefined;
    let canceled = false;
    const restore = async () => {
      for (const candidate of resumeOperationsRef.current) {
        const operationId = candidate?.operation?.id;
        if (!operationId) continue;
        const attemptKey = `${ownerKey}:${operationId}`;
        if (resumeAttemptsRef.current.has(attemptKey)) continue;
        resumeAttemptsRef.current.add(attemptKey);
        try {
          const operation = normalizeReverseOperation(await api.reverseOperation(operationId));
          if (canceled) return;
          const mode = candidate.mode || operationMode(operation);
          const trackingKey = candidate.trackingKey || operationTrackingKey(operation, mode);
          startTracking(operation, {
            mode,
            trackingKey,
            context: { recovered: true, recoveredFromDraft: true },
          });
        } catch (error) {
          reportBackgroundError(error, "restore saved reverse operation");
          if (!canceled) {
            startTracking(candidate.operation, {
              mode: candidate.mode || operationMode(candidate.operation),
              trackingKey: candidate.trackingKey,
              context: { recovered: true, recoveredFromDraft: true },
            }).catch((fallbackError) => {
              reportBackgroundError(fallbackError, "track saved reverse operation fallback");
            });
          }
        }
      }
      if (listedOwnerRef.current === ownerKey || canceled) return;
      listedOwnerRef.current = ownerKey;
      try {
        const payload = await api.reverseOperations({ limit: 100 });
        if (canceled) return;
        for (const operation of normalizeReverseOperationList(payload)) {
          if (!isActiveReverseOperation(operation)) continue;
          const mode = operationMode(operation);
          const trackingKey = operationTrackingKey(operation, mode);
          if (!trackersRef.current.has(trackingKey)) {
            startTracking(operation, { mode, trackingKey, context: { recovered: true } });
          }
        }
      } catch (error) {
        listedOwnerRef.current = "";
        reportBackgroundError(error, "restore active reverse operations");
      }
    };
    restore();
    return () => { canceled = true; };
  }, [ownerKey, resumeSignature, startTracking]);

  useEffect(() => () => stopAll(), [stopAll]);

  return {
    operationsByMode,
    startTracking,
    cancelOperation,
    confirmCover,
    stopTracker,
    stopAll,
    resetOwner,
  };
}
