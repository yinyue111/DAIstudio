"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import useAssetActions from "../useAssetActions";
import useReproductionActions from "../useReproductionActions";
import useShotGeneration from "../useShotGeneration";
import useTaskTracking from "../useTaskTracking";
import useVisibleItemWindow from "../useVisibleItemWindow";
import { api } from "../../lib/api";
import { errorMessage, reportBackgroundError } from "../../lib/errorHandling";

function taskAssets(tasks) {
  const assets = [];
  for (const task of tasks) {
    for (const asset of task.assets || []) {
      assets.push({
        ...asset,
        task_id: asset.task_id || task.id,
        _cat: task.category,
        _task_status: task.status,
        _task_stage: task.stage,
      });
    }
  }
  return assets;
}

export default function useStudioTaskDomain(foundation, model) {
  const {
    creationMode,
    workspaces,
    setWorkspacePatch,
    setCreationMode,
    setMsg,
    refreshMe,
    getOwnerSession,
    me,
    notify,
    refs,
  } = foundation;
  const { requestQuoteConfirmation } = model;

  const [lightbox, setLightbox] = useState(null);
  const [works, setWorks] = useState(null);
  const [worksError, setWorksError] = useState("");
  const worksWindow = useVisibleItemWindow(works, {
    initialCount: 48,
    step: 24,
    resetKey: "studio-works",
  });
  const visibleWorks = works === null ? null : worksWindow.items;

  const resultsRef = useRef(null);
  const loadWorksSeqRef = useRef(0);
  const taskRef = useRef(null);
  const loadWorksImplRef = useRef(null);
  const restoreActiveTaskFromListRef = useRef(null);

  const loadWorks = useCallback((...args) => {
    const implementation = loadWorksImplRef.current;
    return implementation ? implementation(...args) : Promise.resolve();
  }, []);

  const tracking = useTaskTracking({
    setCreationMode,
    setMsg,
    refreshMe,
    loadWorks,
    getOwnerSession,
  });
  restoreActiveTaskFromListRef.current = tracking.restoreActiveTaskFromList;

  const loadWorksImpl = useCallback(async ({ restoreActive = false } = {}) => {
    const seq = ++loadWorksSeqRef.current;
    try {
      const list = await api.tasks(20, 0);
      if (seq !== loadWorksSeqRef.current) return;
      setWorks(taskAssets(list));
      setWorksError("");
      if (restoreActive && !taskRef.current) {
        restoreActiveTaskFromListRef.current?.(list);
      }
    } catch (error) {
      if (seq !== loadWorksSeqRef.current) return;
      reportBackgroundError(error, "load studio works");
      const detail = errorMessage(error, "作品加载失败，请重试");
      setWorksError(`${detail}。已保留当前作品列表。`);
      setMsg("作品加载失败，请重试，当前作品列表已保留。");
      notify.error("作品加载失败，请重试，当前作品列表已保留。");
    }
  }, [notify, setMsg]);
  loadWorksImplRef.current = loadWorksImpl;

  const shotGeneration = useShotGeneration({
    creationMode,
    workspaces,
    workspacesRef: refs.workspacesRef,
    setWorkspacePatch,
    trackBackgroundTask: tracking.trackBackgroundTask,
    getOwnerSession,
    setMsg,
  });
  const reproduction = useReproductionActions({
    setWorkspacePatch,
    setMsg,
    trackBackgroundTask: tracking.trackBackgroundTask,
    getOwnerSession,
    refreshMe: (...args) => refreshMe(...args),
    loadWorks: (...args) => loadWorks(...args),
  });
  const assets = useAssetActions({
    getOwnerSession,
    ownerId: me?.id,
    requestQuoteConfirmation,
    studioActionPendingRequestRef: refs.studioActionPendingRequestRef,
    task: tracking.task,
    setTask: tracking.setTask,
    lightbox,
    setLightbox,
    refreshMe,
    loadWorks,
    setMsg,
  });

  useEffect(() => {
    taskRef.current = tracking.task;
  }, [tracking.task]);

  return {
    ...tracking,
    ...shotGeneration,
    ...reproduction,
    ...assets,
    lightbox,
    setLightbox,
    works,
    setWorks,
    worksError,
    setWorksError,
    worksWindow,
    visibleWorks,
    resultsRef,
    loadWorksSeqRef,
    taskRef,
    loadWorks,
  };
}
