import { isTerminalTaskStatus } from "./helpers";

export function shouldBlockNewGeneration(currentTask, nextCategory) {
  if (!currentTask) return false;
  if (currentTask.category === "video" && currentTask.status === "needs_review") return true;
  if (isTerminalTaskStatus(currentTask.status)) return false;
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
