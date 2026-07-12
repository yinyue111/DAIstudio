const TERMINAL_PAYMENT_STATUSES = new Set(["paid", "closed", "failed"]);

export function shouldStopPaymentOrderPoll(current, next) {
  return Boolean(
    current?.order_no
    && current.order_no === next?.order_no
    && TERMINAL_PAYMENT_STATUSES.has(next.status),
  );
}

export function selectMonotonicPaymentOrder(current, next) {
  if (!current || current.order_no !== next?.order_no) return next;
  if (!next) return current;
  if (current.status === "paid" && next.status !== "paid") return current;
  if (TERMINAL_PAYMENT_STATUSES.has(current.status) && next.status === "pending") return current;
  if (
    TERMINAL_PAYMENT_STATUSES.has(current.status)
    && TERMINAL_PAYMENT_STATUSES.has(next.status)
    && current.status !== next.status
    && next.status !== "paid"
  ) {
    return current;
  }
  return next;
}

export function selectMonotonicPaymentOrders(currentRows, nextRows) {
  const currentByOrderNo = new Map(
    (currentRows || []).map((order) => [order.order_no, order]),
  );
  return (nextRows || []).map((order) => (
    selectMonotonicPaymentOrder(currentByOrderNo.get(order.order_no), order)
  ));
}

export function createPaymentOrderPoller({
  request,
  onResult,
  onError = () => true,
  intervalMs = 2500,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let stopped = false;
  let timer = null;
  let inFlight = false;

  const schedule = (delay = intervalMs) => {
    if (stopped) return;
    timer = setTimer(tick, delay);
  };

  const continueAfter = (decision) => {
    if (stopped || decision === false) return;
    schedule(typeof decision === "number" ? decision : intervalMs);
  };

  async function tick() {
    timer = null;
    if (stopped || inFlight) return;
    inFlight = true;
    try {
      const result = await request();
      if (stopped) return;
      continueAfter(await onResult(result));
    } catch (error) {
      if (stopped) return;
      continueAfter(await onError(error));
    } finally {
      inFlight = false;
    }
  }

  schedule();
  return {
    stop() {
      stopped = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}
