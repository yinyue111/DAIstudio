"use client";

import { isTerminalTaskStatus } from "./helpers.js";

export function shouldBlockNewGeneration(currentTask, nextCategory) {
  if (!currentTask || isTerminalTaskStatus(currentTask.status)) return false;
  return currentTask.category !== "image" || nextCategory !== "image";
}

export function generationSubmitDisabled({
  submitting,
  currentTask,
  nextCategory,
  currentModelEnabled,
}) {
  return Boolean(
    submitting
      || !currentModelEnabled
      || shouldBlockNewGeneration(currentTask, nextCategory)
  );
}
