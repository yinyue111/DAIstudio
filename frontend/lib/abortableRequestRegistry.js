export function createAbortableRequestRegistry() {
  const controllers = new Set();

  return {
    capture() {
      const controller = new AbortController();
      controllers.add(controller);
      return {
        signal: controller.signal,
        release() {
          controllers.delete(controller);
        },
      };
    },
    abortAll() {
      for (const controller of controllers) controller.abort();
      controllers.clear();
    },
    size() {
      return controllers.size;
    },
  };
}
