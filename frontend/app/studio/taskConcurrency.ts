import { isTerminalTaskStatus } from "./helpers";

export function shouldBlockNewGeneration(currentTask, nextCategory) {
  if (!currentTask || isTerminalTaskStatus(currentTask.status)) return false;
  return currentTask.category !== "image" || nextCategory !== "image";
}

export function generationSubmitDisabled({
  submitting,
  busy,
  currentTask,
  nextCategory,
  currentModelEnabled,
}) {
  return Boolean(
    submitting
      || busy
      || !currentModelEnabled
      || shouldBlockNewGeneration(currentTask, nextCategory)
  );
}
