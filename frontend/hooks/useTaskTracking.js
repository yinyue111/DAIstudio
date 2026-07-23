"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import {
  createStudioOwnerRequestContext,
  createStudioTaskActionContext,
} from "../lib/studioSession";
import { RATIOS } from "../app/studio/constants";
import { assetDims, isTerminalTaskStatus, nearestRatio, videoRatioOptions } from "../app/studio/helpers";

const ACTIVE_IMAGE_POLL_INTERVAL_MS = 1200;
const ACTIVE_VIDEO_POLL_INTERVAL_MS = 4000;
const MAX_TASK_WS_RECONNECT_ATTEMPTS = 3;

function estimateVideoRemainingText(task) {
  if (!task || task.category !== "video" || isTerminalTaskStatus(task.status)) return "";
  const etaRemaining = Number(task.eta_remaining_seconds || 0);
  const etaTotal = Number(task.eta_total_seconds || 0);
  if (etaRemaining > 0 && etaTotal > 0) {
    const progress = Math.max(1, Math.min(95, Number(task.progress || task.percent || 8)));
    const minutes = Math.ceil(etaRemaining / 60);
    const source = task.eta_source === "history" ? "历史同类任务" : "系统估算";
    const sampleText = task.eta_sample_count ? `，参考 ${task.eta_sample_count} 个样本` : "";
    return `视频生成通常需要数分钟，当前约 ${progress}%；按${source}${sampleText}预计还需 ${minutes} 分钟左右。`;
  }
  const progress = Math.max(1, Math.min(95, Number(task.progress || task.percent || 8)));
  const duration = Number(task.params?.duration || task.params?.target_duration || 5);
  const resolution = String(task.params?.resolution || task.params?.target_resolution || "720p");
  const baseSeconds = Math.max(120, duration * (resolution === "1080p" ? 45 : 30));
  const elapsedShare = progress / 100;
  const remaining = Math.max(30, Math.round(baseSeconds * (1 - elapsedShare)));
  const minutes = Math.ceil(remaining / 60);
  return `视频生成通常需要数分钟，当前约 ${progress}%；预计还需 ${minutes} 分钟左右，页面可保持打开或稍后到历史记录查看。`;
}

export default function useTaskTracking({
  setCreationMode,
  setMsg,
  refreshMe,
  loadWorks,
  getOwnerSession,
}) {
  const [task, setTask] = useState(null);
  const [runningSnapshot, setRunningSnapshot] = useState(null);
  const [trackingLost, setTrackingLost] = useState(false);
  const [backgroundTasks, setBackgroundTasks] = useState([]);

  const pollRef = useRef(null);
  const wsRef = useRef(null);
  const wsReconnectTimerRef = useRef(null);
  const backgroundTrackersRef = useRef(new Map());
  const activeIdRef = useRef(null);
  const trackingRunRef = useRef(0);
  const taskStateRef = useRef(null);
  const getOwnerSessionRef = useRef(getOwnerSession);
  const ownerRequestContextRef = useRef(null);

  getOwnerSessionRef.current = getOwnerSession;
  if (!ownerRequestContextRef.current) {
    ownerRequestContextRef.current = createStudioOwnerRequestContext(
      () => getOwnerSessionRef.current?.(),
    );
  }

  const setTrackedTask = useCallback((nextTask) => {
    if (typeof nextTask === "function") {
      setTask((previousTask) => {
        const resolvedTask = nextTask(previousTask);
        taskStateRef.current = resolvedTask;
        return resolvedTask;
      });
      return;
    }
    taskStateRef.current = nextTask;
    setTask(nextTask);
  }, []);

  const upsertBackgroundTask = useCallback((nextTask) => {
    if (!nextTask?.id) return;
    setBackgroundTasks((prev) => {
      const idx = prev.findIndex((item) => item.id === nextTask.id);
      const normalized = {
        ...nextTask,
        progress: isTerminalTaskStatus(nextTask.status) ? 100 : (nextTask.progress || nextTask.percent || 8),
        background_updated_at: Date.now(),
      };
      if (idx < 0) return [normalized, ...prev].slice(0, 6);
      const next = [...prev];
      next[idx] = { ...next[idx], ...normalized };
      return next;
    });
  }, []);

  const dismissBackgroundTask = useCallback((id) => {
    setBackgroundTasks((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const stopActiveTracking = useCallback(() => {
    trackingRunRef.current += 1;
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      try { wsRef.current.close(); } catch (e) { reportBackgroundError(e, "close active task websocket"); }
      wsRef.current = null;
    }
  }, []);

  const startPolling = useCallback((
    id,
    runId = trackingRunRef.current,
    ownerRequest = ownerRequestContextRef.current.capture(),
  ) => {
    if (pollRef.current) clearTimeout(pollRef.current);
    let fails = 0;
    let stopped = false;
    const isCurrent = () => (
      !stopped
      && trackingRunRef.current === runId
      && activeIdRef.current === id
      && ownerRequest.isCurrent()
    );
    const schedule = (nextTask = null) => {
      const intervalMs = nextTask?.category === "video"
        ? ACTIVE_VIDEO_POLL_INTERVAL_MS
        : ACTIVE_IMAGE_POLL_INTERVAL_MS;
      if (isCurrent()) pollRef.current = setTimeout(tick, intervalMs);
    };
    const stop = () => {
      stopped = true;
      if (pollRef.current) clearTimeout(pollRef.current);
      pollRef.current = null;
    };
    const tick = async () => {
      if (!isCurrent()) {
        stop();
        return;
      }
      try {
        const nextTask = await api.task(id);
        fails = 0;
        if (!isCurrent()) return;
        if (isTerminalTaskStatus(nextTask.status)) {
          setTrackedTask({ ...nextTask, progress: 100 });
          setTrackingLost(false);
          stop();
          refreshMe();
          loadWorks();
        } else {
          setTrackedTask(nextTask);
          setTrackingLost(false);
          schedule(nextTask);
        }
      } catch (e) {
        if (!isCurrent()) return;
        if (++fails >= 5) {
          stop();
          setTrackingLost(true);
          setMsg("无法获取任务进度,请稍后刷新页面查看结果。");
        } else {
          schedule();
        }
      }
    };
    tick();
  }, [loadWorks, refreshMe, setMsg, setTrackedTask]);

  const startTracking = useCallback(async (id) => {
    const ownerRequest = ownerRequestContextRef.current.capture();
    stopActiveTracking();
    const runId = ++trackingRunRef.current;
    activeIdRef.current = id;
    setTrackingLost(false);
    let done = false;
    const isCurrent = () => (
      trackingRunRef.current === runId
      && activeIdRef.current === id
      && ownerRequest.isCurrent()
    );

    const fallbackToPolling = () => {
      if (!done && isCurrent()) startPolling(id, runId, ownerRequest);
    };

    const scheduleReconnect = (attempt) => {
      if (done || !isCurrent()) return;
      if (attempt >= MAX_TASK_WS_RECONNECT_ATTEMPTS) {
        fallbackToPolling();
        return;
      }
      const delay = Math.min(8000, 1000 * 2 ** attempt);
      setMsg(`任务连接中断，正在第 ${attempt + 1}/${MAX_TASK_WS_RECONNECT_ATTEMPTS} 次重连...`);
      wsReconnectTimerRef.current = setTimeout(() => connect(attempt + 1), delay);
    };

    const connect = async (attempt = 0) => {
      try {
        const { ticket } = await api.taskWsTicket(id);
        if (!isCurrent()) return;
        if (!ticket) {
          fallbackToPolling();
          return;
        }
        const ws = new WebSocket(wsUrl(`/ws/tasks/${id}?ticket=${encodeURIComponent(ticket)}`));
        let reconnecting = false;
        const reconnectOnce = () => {
          if (reconnecting || done || !isCurrent() || wsRef.current !== ws) return;
          reconnecting = true;
          scheduleReconnect(attempt);
        };
        wsRef.current = ws;
        ws.onopen = () => {
          if (isCurrent()) {
            setTrackingLost(false);
            setMsg("");
          }
        };
        ws.onmessage = (ev) => {
          if (!isCurrent()) return;
          let data = null;
          try {
            data = JSON.parse(ev.data);
          } catch (e) {
            try { ws.close(); } catch (_e) { reportBackgroundError(_e, "close malformed task websocket"); }
            reconnectOnce();
            return;
          }
          const terminal = isTerminalTaskStatus(data.status);
          setTrackedTask((prev) => (
            prev && prev.id === id
              ? { ...prev, status: data.status, progress: terminal ? 100 : data.percent, error: data.error || prev.error }
              : prev
          ));
          if (terminal) {
            setTrackingLost(false);
            done = true;
            api.task(id).then((nextTask) => {
              if (!isCurrent()) return;
              setTrackedTask(nextTask);
            }).catch((e) => reportBackgroundError(e, "refresh terminal task after websocket"));
            refreshMe();
            loadWorks();
            try { ws.close(); } catch (e) { reportBackgroundError(e, "close malformed task websocket"); }
          }
        };
        ws.onerror = reconnectOnce;
        ws.onclose = reconnectOnce;
      } catch (e) {
        scheduleReconnect(attempt);
      }
    };

    connect(0);
  }, [loadWorks, refreshMe, setTrackedTask, startPolling, stopActiveTracking]);

  const startBackgroundTracking = useCallback((target, callbacks = {}) => {
    const ownerRequest = ownerRequestContextRef.current.capture();
    const id = typeof target === "object" ? target?.id : target;
    if (!id) return () => {};
    let stopped = false;
    const isCurrent = () => !stopped && ownerRequest.isCurrent();
    if (typeof target === "object") upsertBackgroundTask(target);
    let timer = null;
    let failures = 0;
    const stop = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    const schedule = () => {
      if (isCurrent()) timer = setTimeout(tick, 3000);
    };
    const tick = async () => {
      try {
        const nextTask = await api.task(id);
        if (!isCurrent()) return;
        failures = 0;
        upsertBackgroundTask(nextTask);
        if (isTerminalTaskStatus(nextTask.status)) {
          stop();
          backgroundTrackersRef.current.delete(id);
          Promise.resolve(callbacks.onTerminal?.(nextTask)).catch((error) => {
            reportBackgroundError(error, "handle terminal background task");
          });
          refreshMe();
          loadWorks();
          return;
        }
      } catch (e) {
        if (!isCurrent()) return;
        failures += 1;
        if (failures >= 5) {
          stop();
          backgroundTrackersRef.current.delete(id);
          upsertBackgroundTask({
            id,
            category: "image",
            status: "unknown",
            error: "后台任务进度获取失败，请到历史记录确认结果。",
            progress: 100,
          });
          return;
        }
      }
      schedule();
    };
    schedule();
    return stop;
  }, [loadWorks, refreshMe, upsertBackgroundTask]);

  const trackBackgroundTask = useCallback((target, callbacks = {}) => {
    const id = typeof target === "object" ? target?.id : target;
    if (!id || backgroundTrackersRef.current.has(id)) return;
    backgroundTrackersRef.current.set(id, startBackgroundTracking(target, callbacks));
  }, [startBackgroundTracking]);

  const cancelBackgroundTask = useCallback(async (id) => {
    if (!id) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    try {
      const nextTask = await api.cancelTask(id);
      if (!ownerRequest.isCurrent()) return;
      upsertBackgroundTask(nextTask);
      refreshMe();
      loadWorks();
    } catch (e) {
      if (!ownerRequest.isCurrent()) return;
      upsertBackgroundTask({
        id,
        category: "image",
        status: "unknown",
        error: e.message || "后台任务取消失败，请到历史记录确认状态。",
        progress: 100,
      });
    }
  }, [loadWorks, refreshMe, upsertBackgroundTask]);

  const restoreActiveTaskFromList = useCallback((list) => {
    if (taskStateRef.current && !isTerminalTaskStatus(taskStateRef.current.status)) return;
    const active = (list || []).find((item) => (
      item
      && !isTerminalTaskStatus(item.status)
      && (item.category === "video" || item.category === "image")
    ));
    if (!active) return;
    const traceMode = active.params?._source_trace?.mode;
    const restoredMode = (
      ["image", "video", "image_edit", "video_edit"].includes(traceMode)
        ? traceMode
        : (active.category === "video" ? "video" : "image")
    );
    setCreationMode(restoredMode);
    setTrackedTask(active);
    setTrackingLost(false);
    const dims = assetDims(active.assets?.[0]);
    const activeRatioOptions = active.category === "video" ? videoRatioOptions() : RATIOS;
    const activeRatio = dims
      ? activeRatioOptions.find((r) => r.key === nearestRatio(dims.width, dims.height, activeRatioOptions))
      : null;
    setRunningSnapshot({
      category: active.category,
      n: active.category === "image" ? Number(active.requested_count || active.params?.n || 1) : 1,
      ratio: activeRatio || activeRatioOptions[0],
    });
    startTracking(active.id);
  }, [setCreationMode, setTrackedTask, startTracking]);

  const refreshActiveTask = useCallback(async () => {
    const currentTask = taskStateRef.current;
    const ownerRequest = ownerRequestContextRef.current.capture();
    if (!currentTask?.id) return;
    const taskAction = createStudioTaskActionContext(
      currentTask.id,
      () => taskStateRef.current?.id,
    );
    setMsg("");
    try {
      const nextTask = await api.task(currentTask.id);
      ownerRequest.commit(() => taskAction.commit(() => {
        setTrackedTask(nextTask);
        setTrackingLost(false);
        if (!isTerminalTaskStatus(nextTask.status)) startTracking(nextTask.id);
        else {
          refreshMe();
          loadWorks();
        }
      }));
    } catch (e) {
      ownerRequest.commit(() => taskAction.commit(() => setMsg(e.message)));
    }
  }, [loadWorks, refreshMe, setMsg, setTrackedTask, startTracking]);

  const cancelActiveTask = useCallback(async () => {
    const currentTask = taskStateRef.current;
    const ownerRequest = ownerRequestContextRef.current.capture();
    if (!currentTask) return;
    const taskAction = createStudioTaskActionContext(
      currentTask.id,
      () => taskStateRef.current?.id,
    );
    if (!["queued", "running"].includes(currentTask.status)) return;
    const runningTask = currentTask.status === "running";
    const text = runningTask
      ? "确认提交取消请求？运行中的任务会在安全阶段停止；如果外部网关已接收，可能无法中途取消。"
      : "确认取消这个排队任务？已冻结积分会退回。";
    if (!window.confirm(text)) return;
    setMsg("");
    try {
      const nextTask = await api.cancelTask(currentTask.id);
      ownerRequest.commit(() => taskAction.commit(() => {
        setTrackedTask(nextTask);
        setTrackingLost(false);
        refreshMe();
        loadWorks();
      }));
    } catch (e) {
      ownerRequest.commit(() => taskAction.commit(() => setMsg(e.message)));
    }
  }, [loadWorks, refreshMe, setMsg, setTrackedTask]);

  const stopAllTracking = useCallback(() => {
    stopActiveTracking();
    backgroundTrackersRef.current.forEach((stop) => stop());
    backgroundTrackersRef.current.clear();
  }, [stopActiveTracking]);

  const resetOwnerTracking = useCallback(() => {
    ownerRequestContextRef.current.invalidate();
    stopAllTracking();
    activeIdRef.current = null;
    taskStateRef.current = null;
    setTask(null);
    setRunningSnapshot(null);
    setTrackingLost(false);
    setBackgroundTasks([]);
  }, [stopAllTracking]);

  useEffect(() => stopAllTracking, [stopAllTracking]);

  const activeNonTerminalTask = Boolean(task && !isTerminalTaskStatus(task.status));
  const showRunningProgress = activeNonTerminalTask && !trackingLost;
  const taskEtaText = estimateVideoRemainingText(task);

  return {
    task,
    setTask: setTrackedTask,
    runningSnapshot,
    setRunningSnapshot,
    trackingLost,
    setTrackingLost,
    backgroundTasks,
    dismissBackgroundTask,
    cancelBackgroundTask,
    showRunningProgress,
    taskEtaText,
    restoreActiveTaskFromList,
    startTracking,
    startBackgroundTracking,
    trackBackgroundTask,
    refreshActiveTask,
    cancelActiveTask,
    stopAllTracking,
    resetOwnerTracking,
  };
}
