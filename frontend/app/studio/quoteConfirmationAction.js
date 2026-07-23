export function createQuoteConfirmationAction({
  envelope,
  execute,
  resolve,
  reject,
}) {
  return {
    envelope,
    execute,
    resolve,
    reject,
    executing: false,
    settled: false,
  };
}

export function quoteConfirmationBusyResult() {
  return {
    status: "execution_failed",
    error: new Error("已有操作正在提交，请稍候再试。"),
  };
}

export function beginQuoteConfirmationExecution(action) {
  if (!action || action.settled || action.executing) return false;
  action.executing = true;
  return true;
}

export function endQuoteConfirmationExecution(action) {
  if (action) action.executing = false;
}

export function canInvalidateQuoteConfirmation(action) {
  return Boolean(action && !action.settled && !action.executing);
}

export function settleQuoteConfirmationAction(action, value) {
  if (!action || action.settled) return false;
  action.settled = true;
  action.resolve(value);
  return true;
}

export async function executeQuoteActionAutomatically({
  action,
  loadQuote,
  buildRequest,
  quoteError = () => null,
  shouldRefreshQuote = () => false,
  maxRefreshes = 1,
  isCurrent = () => true,
}) {
  let refreshCount = 0;
  while (isCurrent() && !action.settled) {
    let quote;
    try {
      quote = await loadQuote({ refreshed: refreshCount > 0 });
    } catch (error) {
      if (isCurrent() && !action.settled) {
        settleQuoteConfirmationAction(action, { status: "quote_failed", error });
      }
      return;
    }
    if (!isCurrent() || action.settled) return;

    const validationError = quoteError(quote);
    if (validationError) {
      settleQuoteConfirmationAction(action, {
        status: "quote_failed",
        error: validationError,
        quote,
      });
      return;
    }
    if (!beginQuoteConfirmationExecution(action)) return;

    const request = buildRequest(action.envelope.request, quote);
    try {
      const result = await action.execute({
        request,
        quote,
        envelope: action.envelope,
      });
      endQuoteConfirmationExecution(action);
      if (!isCurrent() || action.settled) return;
      settleQuoteConfirmationAction(action, {
        status: "executed",
        result,
        quote,
        request,
      });
      return;
    } catch (error) {
      endQuoteConfirmationExecution(action);
      if (!isCurrent() || action.settled) return;
      if (refreshCount < maxRefreshes && shouldRefreshQuote(error)) {
        refreshCount += 1;
        continue;
      }
      settleQuoteConfirmationAction(action, { status: "execution_failed", error });
      return;
    }
  }
}
