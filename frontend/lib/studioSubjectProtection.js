function shouldRetryPreview(error) {
  const status = Number(error?.status || 0);
  if (status === 429 || status >= 500) return true;
  if (error?.name === "TypeError") return true;
  return String(error?.message || "").includes("请求超时");
}

export function startSubjectProtectionPreview({
  load,
  onSuccess,
  onError,
  retryDelayMs = 250,
}) {
  let canceled = false;
  let activeRequest = null;
  let retryTimer = null;
  let releaseRetryWait = null;

  function waitBeforeRetry() {
    if (retryDelayMs <= 0) return Promise.resolve();
    return new Promise((resolve) => {
      releaseRetryWait = resolve;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        releaseRetryWait = null;
        resolve();
      }, retryDelayMs);
    });
  }

  async function run() {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (canceled) return;
      try {
        activeRequest = load();
        const result = await activeRequest;
        activeRequest = null;
        if (!canceled) onSuccess(result);
        return;
      } catch (error) {
        activeRequest = null;
        if (canceled) return;
        if (attempt === 0 && shouldRetryPreview(error)) {
          await waitBeforeRetry();
          continue;
        }
        onError(error);
        return;
      }
    }
  }

  const settled = Promise.resolve().then(run);

  return {
    cancel() {
      if (canceled) return;
      canceled = true;
      activeRequest?.cancel?.();
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
      releaseRetryWait?.();
      releaseRetryWait = null;
    },
    settled,
  };
}
