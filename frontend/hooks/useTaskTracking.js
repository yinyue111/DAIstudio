"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../lib/api";
import { RATIOS } from "../app/studio/constants";
import { assetDims, isTerminalTaskStatus, nearestRatio, videoRatioOptions } from "../app/studio/helpers";

export default function useTaskTracking({
  setCreationMode,
  setMsg,
  refreshMe,
  loadWorks,
}) {
  const [task, setTask] = useState(null);
  const [runningSnapshot, setRunningSnapshot] = useState(null);
  const [trackingLost, setTrackingLost] = useState(false);
  const [backgroundTasks, setBackgroundTasks] = useState([]);

  const pollRef = useRef(null);
  const wsRef = useRef(null);
  const backgroundTrackersRef = useRef(new Map());
  const activeIdRef = useRef(null);

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
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (wsRef.current) {
      try { wsRef.current.close(); } catch (e) {}
      wsRef.current = null;
    }
  }, []);

  const startPolling = useCallback((id) => {
    if (pollRef.current) clearInterval(pollRef.current);
    let fails = 0;
    const interval = setInterval(async () => {
      if (activeIdRef.current !== id) {
        clearInterval(interval);
        if (pollRef.current === interval) pollRef.current = null;
        return;
      }
      try {
        const nextTask = await api.task(id);
        fails = 0;
        if (activeIdRef.current !== id) return;
        if (isTerminalTaskStatus(nextTask.status)) {
          setTask({ ...nextTask, progress: 100 });
          setTrackingLost(false);
          clearInterval(interval);
          if (pollRef.current === interval) pollRef.current = null;
          refreshMe();
          loadWorks();
        } else {
          setTask(nextTask);
          setTrackingLost(false);
        }
      } catch (e) {
        if (++fails >= 5) {
          clearInterval(interval);
          if (pollRef.current === interval) pollRef.current = null;
          setTrackingLost(true);
          setMsg("无法获取任务进度,请稍后刷新页面查看结果。");
        }
      }
    }, 1200);
    pollRef.current = interval;
  }, [loadWorks, refreshMe, setMsg]);

  const startTracking = useCallback(async (id) => {
    stopActiveTracking();
    activeIdRef.current = id;
    setTrackingLost(false);
    let done = false;
    try {
      const { ticket } = await api.taskWsTicket(id);
      if (activeIdRef.current !== id) return;
      if (!ticket) {
        startPolling(id);
        return;
      }
      const ws = new WebSocket(wsUrl(`/ws/tasks/${id}?ticket=${encodeURIComponent(ticket)}`));
      wsRef.current = ws;
      ws.onmessage = (ev) => {
        let data = null;
        try {
          data = JSON.parse(ev.data);
        } catch (e) {
          if (!done && wsRef.current === ws) startPolling(id);
          try { ws.close(); } catch (_e) {}
          return;
        }
        const terminal = isTerminalTaskStatus(data.status);
        setTask((prev) => (
          prev && prev.id === id
            ? { ...prev, status: data.status, progress: terminal ? 100 : data.percent, error: data.error || prev.error }
            : prev
        ));
        if (terminal) {
          setTrackingLost(false);
          done = true;
          api.task(id).then((nextTask) => {
            if (activeIdRef.current !== id) return;
            setTask(nextTask);
          }).catch(() => {});
          refreshMe();
          loadWorks();
          try { ws.close(); } catch (e) {}
        }
      };
      ws.onerror = () => { if (!done && wsRef.current === ws) startPolling(id); };
      ws.onclose = () => { if (!done && wsRef.current === ws) startPolling(id); };
    } catch (e) {
      startPolling(id);
    }
  }, [loadWorks, refreshMe, startPolling, stopActiveTracking]);

  const startBackgroundTracking = useCallback((target) => {
    const id = typeof target === "object" ? target?.id : target;
    if (!id) return () => {};
    if (typeof target === "object") upsertBackgroundTask(target);
    let stopped = false;
    let timer = null;
    let failures = 0;
    const stop = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    const schedule = () => {
      if (!stopped) timer = setTimeout(tick, 3000);
    };
    const tick = async () => {
      try {
        const nextTask = await api.task(id);
        if (stopped) return;
        failures = 0;
        upsertBackgroundTask(nextTask);
        if (isTerminalTaskStatus(nextTask.status)) {
          stop();
          backgroundTrackersRef.current.delete(id);
          refreshMe();
          loadWorks();
          return;
        }
      } catch (e) {
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

  const trackBackgroundTask = useCallback((target) => {
    const id = typeof target === "object" ? target?.id : target;
    if (!id || backgroundTrackersRef.current.has(id)) return;
    backgroundTrackersRef.current.set(id, startBackgroundTracking(target));
  }, [startBackgroundTracking]);

  const cancelBackgroundTask = useCallback(async (id) => {
    if (!id) return;
    try {
      const nextTask = await api.cancelTask(id);
      upsertBackgroundTask(nextTask);
      refreshMe();
      loadWorks();
    } catch (e) {
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
    if (task && !isTerminalTaskStatus(task.status)) return;
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
    setTask(active);
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
  }, [setCreationMode, startTracking, task]);

  const refreshActiveTask = useCallback(async () => {
    if (!task?.id) return;
    setMsg("");
    try {
      const nextTask = await api.task(task.id);
      setTask(nextTask);
      setTrackingLost(false);
      if (!isTerminalTaskStatus(nextTask.status)) startTracking(nextTask.id);
      else {
        refreshMe();
        loadWorks();
      }
    } catch (e) {
      setMsg(e.message);
    }
  }, [loadWorks, refreshMe, setMsg, startTracking, task]);

  const cancelActiveTask = useCallback(async () => {
    if (!task) return;
    if (!["queued", "running"].includes(task.status)) return;
    const runningTask = task.status === "running";
    const text = runningTask
      ? "确认提交取消请求？运行中的任务会在安全阶段停止；如果外部网关已接收，可能无法中途取消。"
      : "确认取消这个排队任务？已冻结积分会退回。";
    if (!window.confirm(text)) return;
    setMsg("");
    try {
      const nextTask = await api.cancelTask(task.id);
      setTask(nextTask);
      setTrackingLost(false);
      refreshMe();
      loadWorks();
    } catch (e) {
      setMsg(e.message);
    }
  }, [loadWorks, refreshMe, setMsg, task]);

  const stopAllTracking = useCallback(() => {
    stopActiveTracking();
    backgroundTrackersRef.current.forEach((stop) => stop());
    backgroundTrackersRef.current.clear();
  }, [stopActiveTracking]);

  useEffect(() => stopAllTracking, [stopAllTracking]);

  const activeNonTerminalTask = Boolean(task && !isTerminalTaskStatus(task.status));
  const showRunningProgress = activeNonTerminalTask && !trackingLost;

  return {
    task,
    setTask,
    runningSnapshot,
    setRunningSnapshot,
    trackingLost,
    setTrackingLost,
    backgroundTasks,
    dismissBackgroundTask,
    cancelBackgroundTask,
    showRunningProgress,
    restoreActiveTaskFromList,
    startTracking,
    startBackgroundTracking,
    trackBackgroundTask,
    refreshActiveTask,
    cancelActiveTask,
    stopAllTracking,
  };
}
