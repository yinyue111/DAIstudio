"use client";

import { AlertCircle, ChevronDown, Eye, QrCode, ReceiptText, RefreshCw } from "lucide-react";
import { formatLocalDateTime } from "../../lib/datetime";
import { paymentStatusStyle, paymentStatusText } from "./status";

function OrderSkeleton() {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="充值订单加载中">
      {Array.from({ length: 4 }).map((_, index) => (
        <div key={index} className="skeleton h-[76px]" />
      ))}
    </div>
  );
}

export default function PaymentOrdersPanel({
  orders,
  loading,
  actionLoading,
  error,
  hasMore,
  onRefresh,
  onLoadMore,
  onOpen,
}) {
  return (
    <section
      id="account-panel-orders"
      role="tabpanel"
      aria-labelledby="account-tab-orders"
      className="panel p-4 sm:p-5"
    >
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold">充值订单</h2>
          <p className="mt-1 text-xs text-fog">共 {orders.length} 条已加载</p>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading} className="btn-secondary btn-sm">
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          刷新
        </button>
      </div>

      {loading && orders.length === 0 ? <OrderSkeleton /> : error && orders.length === 0 ? (
        <div role="alert" className="rounded-xl2 border border-bad/30 bg-bad/10 px-4 py-8 text-center">
          <AlertCircle size={22} className="mx-auto text-bad" aria-hidden="true" />
          <p className="mt-2 text-sm text-bad">{error}</p>
          <button type="button" onClick={onRefresh} className="btn-secondary btn-sm mt-4">重新加载</button>
        </div>
      ) : orders.length === 0 ? (
        <div className="py-12 text-center" role="status">
          <ReceiptText size={28} className="mx-auto text-fog" aria-hidden="true" />
          <h3 className="mt-3 text-sm font-medium text-mist">还没有充值订单</h3>
        </div>
      ) : (
        <div role="list" className="divide-y divide-line border-y border-line">
          {orders.map((order) => {
            const pending = order.status === "pending";
            return (
              <article
                key={order.order_no}
                role="listitem"
                className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`badge ${paymentStatusStyle(order.status)}`}>
                      {paymentStatusText(order.status)}
                    </span>
                    <span className="text-sm font-medium text-snow">
                      {order.provider === "wechat" ? "微信" : "支付宝"}充值
                    </span>
                  </div>
                  <p className="mt-1 truncate font-mono text-xs text-fog" title={order.order_no}>{order.order_no}</p>
                  <p className="mt-1 text-xs text-fog">{formatLocalDateTime(order.created_at)}</p>
                </div>
                <div className="flex items-baseline gap-3 sm:block sm:text-right">
                  <div className="font-display text-base font-semibold text-snow">{order.credits} 积分</div>
                  <div className="text-xs text-fog">￥{(order.amount_cents / 100).toFixed(2)}</div>
                </div>
                <button
                  type="button"
                  onClick={() => onOpen(order.order_no)}
                  disabled={actionLoading}
                  className={pending ? "btn-secondary btn-sm" : "btn-ghost btn-sm"}
                >
                  {pending ? <QrCode size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
                  {pending ? "继续支付" : "查看"}
                </button>
              </article>
            );
          })}
        </div>
      )}

      {error && orders.length > 0 && <p role="alert" className="mt-3 text-sm text-bad">{error}</p>}
      {hasMore && (
        <div className="mt-4 text-center">
          <button type="button" onClick={onLoadMore} disabled={loading} className="btn-secondary btn-sm">
            <ChevronDown size={14} aria-hidden="true" />
            {loading ? "加载中" : "加载更多"}
          </button>
        </div>
      )}
    </section>
  );
}
