"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import { api } from "../../lib/api";
import {
  BillingEntryList,
  CreditTransactionList,
  LedgerEmpty,
  LedgerError,
  LedgerLoading,
} from "./CreditLedgerRows";

const BILLING_KINDS = [
  ["all", "全部业务"],
  ["generation", "图片/视频生成"],
  ["reverse", "图片/视频反推"],
  ["workflow", "工具工作流"],
  ["prompt", "提示词优化"],
  ["unlock", "素材解锁"],
  ["recharge", "充值入账"],
  ["grant", "系统发放"],
  ["other", "其他"],
];

const TRANSACTION_TYPES = [
  ["all", "全部类型"],
  ["grant", "入账"],
  ["freeze", "冻结"],
  ["settle", "结算"],
  ["refund", "退回"],
  ["consume", "直接扣减"],
  ["unlock", "解锁扣减"],
];

export default function CreditLedger({ refreshKey = 0 }) {
  const [mode, setMode] = useState("entries");
  const [kind, setKind] = useState("all");
  const [transactionType, setTransactionType] = useState("all");
  const [items, setItems] = useState([]);
  const [nextCursor, setNextCursor] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestSeq = useRef(0);

  const load = useCallback(async ({ append = false, cursor = "" } = {}) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError("");
    try {
      const page = mode === "entries"
        ? await api.billingEntries({ kind, limit: 20, cursor })
        : await api.creditTransactions({ type: transactionType, limit: 20, cursor });
      if (seq !== requestSeq.current) return;
      setItems((current) => {
        if (!append) return page.items || [];
        const keyOf = (item) => mode === "entries" ? item.key : item.id;
        const known = new Set(current.map(keyOf));
        return [...current, ...(page.items || []).filter((item) => !known.has(keyOf(item)))];
      });
      setNextCursor(page.next_cursor || "");
      setHasMore(Boolean(page.has_more));
      setTotal(Number(page.total || 0));
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(caught.message || "积分账单加载失败");
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [kind, mode, transactionType]);

  useEffect(() => {
    setItems([]);
    setNextCursor("");
    setHasMore(false);
    load();
    return () => { requestSeq.current += 1; };
  }, [load, refreshKey]);

  function resetView() {
    requestSeq.current += 1;
    setItems([]);
    setNextCursor("");
    setHasMore(false);
    setTotal(0);
    setError("");
    setLoading(true);
  }

  function changeMode(nextMode) {
    if (nextMode === mode) return;
    resetView();
    setMode(nextMode);
  }

  function changeFilter(nextValue) {
    resetView();
    if (mode === "entries") setKind(nextValue);
    else setTransactionType(nextValue);
  }

  return (
    <section
      id="account-panel-billing"
      role="tabpanel"
      aria-labelledby="account-tab-billing"
      className="panel p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold">积分账单</h2>
          <p className="mt-1 text-xs text-fog">共 {total} 条{mode === "entries" ? "业务账单" : "原始流水"}</p>
        </div>
        <button type="button" onClick={() => load()} disabled={loading} className="btn-secondary btn-sm">
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          刷新
        </button>
      </div>

      <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div role="tablist" aria-label="账单视图" className="inline-flex w-fit gap-1 rounded-full border border-line bg-white/5 p-1">
          {[["entries", "业务账单"], ["transactions", "原始流水"]].map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={mode === value}
              onClick={() => changeMode(value)}
              className={mode === value ? "chip chip-active" : "chip"}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="w-full sm:w-52">
          <span className="sr-only">账单筛选</span>
          <select
            className="select py-2"
            value={mode === "entries" ? kind : transactionType}
            onChange={(event) => changeFilter(event.target.value)}
          >
            {(mode === "entries" ? BILLING_KINDS : TRANSACTION_TYPES).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-4">
        {loading && items.length === 0 ? <LedgerLoading /> : error && items.length === 0 ? (
          <LedgerError message={error} onRetry={() => load()} />
        ) : items.length === 0 ? <LedgerEmpty raw={mode === "transactions"} /> : mode === "entries" ? (
          <BillingEntryList items={items} />
        ) : (
          <CreditTransactionList items={items} />
        )}
      </div>

      {error && items.length > 0 && <p role="alert" className="mt-3 text-sm text-bad">{error}</p>}
      {hasMore && (
        <div className="mt-4 text-center">
          <button
            type="button"
            onClick={() => load({ append: true, cursor: nextCursor })}
            disabled={loading || !nextCursor}
            className="btn-secondary btn-sm"
          >
            <ChevronDown size={14} aria-hidden="true" />
            {loading ? "加载中" : "加载更多"}
          </button>
        </div>
      )}
    </section>
  );
}
