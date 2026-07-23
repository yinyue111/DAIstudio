"use client";

import { useCallback, useEffect, useRef } from "react";
import { api } from "../lib/api";
import {
  confirmedRequestPayload,
  requestAuthoritativeStudioQuote,
  shouldRefreshGenerationQuote,
  studioQuoteEnvelope,
} from "../app/studio/generationQuote";
import {
  canInvalidateQuoteConfirmation,
  createQuoteConfirmationAction,
  executeQuoteActionAutomatically,
  settleQuoteConfirmationAction,
} from "../app/studio/quoteConfirmationAction";

function insufficientCreditsError(quote) {
  if (quote?.affordable !== false) return null;
  const required = Number(quote?.totalCredits);
  const available = Number(quote?.balanceBefore);
  const requiredText = Number.isFinite(required) ? `本次需要 ${required} 积分` : "本次操作积分不足";
  const availableText = Number.isFinite(available) ? `，当前可用 ${available} 积分` : "";
  return new Error(`${requiredText}${availableText}。`);
}

export default function useStudioQuoteConfirmation({ balanceCredits = null } = {}) {
  const actionRef = useRef(null);
  const balanceRef = useRef(balanceCredits);
  balanceRef.current = balanceCredits === null || balanceCredits === undefined || balanceCredits === ""
    ? null
    : Number.isFinite(Number(balanceCredits)) ? Number(balanceCredits) : null;

  const executeAction = useCallback(async (action) => {
    await executeQuoteActionAutomatically({
      action,
      loadQuote: () => requestAuthoritativeStudioQuote(
        api,
        action.envelope,
        balanceRef.current,
      ),
      buildRequest: confirmedRequestPayload,
      quoteError: insufficientCreditsError,
      shouldRefreshQuote: shouldRefreshGenerationQuote,
      isCurrent: () => actionRef.current === action,
    });
    if (actionRef.current === action && action.settled) actionRef.current = null;
  }, []);

  const requestConfirmation = useCallback(({
    kind,
    request,
    clientRequestId = "",
    execute,
  }) => {
    const envelope = studioQuoteEnvelope(kind, request, clientRequestId);
    if (typeof execute !== "function") {
      return Promise.reject(new Error("付费操作缺少可执行回调。"));
    }
    const previous = actionRef.current;
    if (previous?.executing) {
      return Promise.resolve({ status: "execution_in_progress" });
    }
    if (previous) settleQuoteConfirmationAction(previous, { status: "superseded" });

    return new Promise((resolve, reject) => {
      const action = createQuoteConfirmationAction({ envelope, execute, resolve, reject });
      actionRef.current = action;
      executeAction(action).catch((error) => {
        if (actionRef.current === action) actionRef.current = null;
        settleQuoteConfirmationAction(action, { status: "execution_failed", error });
      });
    });
  }, [executeAction]);

  const invalidate = useCallback((reason = "输入参数已变化，本次操作已取消，请重新执行。") => {
    const action = actionRef.current;
    if (!canInvalidateQuoteConfirmation(action)) return false;
    actionRef.current = null;
    settleQuoteConfirmationAction(action, { status: "invalidated", reason });
    return true;
  }, []);

  useEffect(() => () => {
    const action = actionRef.current;
    actionRef.current = null;
    settleQuoteConfirmationAction(action, { status: "unmounted" });
  }, []);

  return {
    requestQuoteConfirmation: requestConfirmation,
    invalidateQuote: invalidate,
  };
}
