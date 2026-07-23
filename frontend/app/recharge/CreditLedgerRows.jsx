"use client";

import Link from "next/link";
import {
  AlertCircle,
  ChevronDown,
  Coins,
  ExternalLink,
  RefreshCw,
  ScrollText,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import { formatLocalDateTime } from "../../lib/datetime";

const STATUS_LABELS = {
  queued: "排队中",
  running: "进行中",
  compensating: "补偿中",
  waiting_review: "待审核",
  pending: "待支付",
  succeeded: "已完成",
  paid: "已支付",
  failed: "失败",
  canceled: "已取消",
  closed: "已关闭",
  needs_review: "待确认",
  needs_confirmation: "待确认",
  refunded: "已退回",
  credited: "已入账",
  proposed: "待处理",
  accepted: "已接受",
  partially_accepted: "部分接受",
  rejected: "已拒绝",
  expired: "已过期",
};

const TYPE_LABELS = {
  grant: "入账",
  freeze: "冻结",
  settle: "结算",
  refund: "退回",
  unlock: "解锁扣减",
  consume: "直接扣减",
};

function statusClass(group) {
  if (group === "active" || group === "needs_attention") return "bg-warn/15 text-warn";
  if (group === "failed") return "bg-bad/15 text-bad";
  if (group === "canceled" || group === "refunded") return "bg-white/10 text-mist";
  if (group === "credited") return "bg-aqua/15 text-aqua";
  return "bg-ok/15 text-ok";
}

function Metric({ label, value, highlight = false }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-fog">{label}</div>
      <div className={`mt-0.5 truncate font-display text-sm font-semibold ${highlight ? "text-snow" : "text-mist"}`}>
        {value}
      </div>
    </div>
  );
}

const RELATED_LABELS = {
  generation: "生成任务",
  reverse: "反推任务",
  workflow: "工作流运行",
  prompt_optimization: "优化提案",
  payment: "充值订单",
  asset: "素材",
  admin: "管理操作",
};

const QUOTE_STATUS_LABELS = {
  active: "待确认",
  consumed: "已使用",
  expired: "已过期",
  canceled: "已取消",
};

const REVIEW_STATUS_LABELS = {
  pending: "等待人工核对",
  settled: "人工核对后结算",
  refunded: "人工核对后退款",
};

function relatedTaskHref(item) {
  const id = Number(item.related_id);
  if (!Number.isSafeInteger(id) || id <= 0) return "";
  const kind = {
    generation: "generation",
    reverse: "reverse",
    workflow: "workflow",
  }[item.related_kind];
  return kind ? `/history?task=${encodeURIComponent(`${kind}:${id}`)}` : "";
}

function BillingEntryDetails({ item }) {
  const taskHref = relatedTaskHref(item);
  const relatedLabel = RELATED_LABELS[item.related_kind] || item.biz_type || "未关联业务";
  const relatedValue = item.related_id != null ? `${relatedLabel} #${item.related_id}` : relatedLabel;
  const conserved = item.balance_conserved && item.reservation_conserved;
  const hasRefundBreakdown = (
    item.settlement_returned_credits > 0
    || item.reservation_refunded_credits > 0
    || item.consumed_refunded_credits > 0
  );
  return (
    <details className="group mt-3 border-t border-line/80 pt-3">
      <summary className="flex min-h-9 cursor-pointer list-none items-center justify-between gap-3 text-xs font-medium text-mist">
        <span>账务与业务明细</span>
        <ChevronDown size={15} className="transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="mt-2 grid gap-3 border-y border-line/70 py-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="业务实体" value={relatedValue} />
        <Metric
          label="计费快照"
          value={item.quote_id ? `#${item.quote_id} · ${item.quoted_credits ?? 0} 积分` : "历史业务无计费快照"}
        />
        <Metric
          label="计费状态"
          value={item.quote_status ? (QUOTE_STATUS_LABELS[item.quote_status] || item.quote_status) : "-"}
        />
        <Metric label="价格版本" value={item.price_version_id ? `#${item.price_version_id}` : "-"} />
      </div>
      {hasRefundBreakdown && (
        <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
          <Metric label="结算差额退回" value={item.settlement_returned_credits} />
          <Metric label="失败冻结退回" value={item.reservation_refunded_credits} />
          <Metric label="同步扣费退回" value={item.consumed_refunded_credits} />
        </div>
      )}
      <div className={`mt-3 flex items-start gap-2 text-xs ${conserved ? "text-ok" : "text-bad"}`}>
        {conserved
          ? <ShieldCheck size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
          : <ShieldAlert size={15} className="mt-0.5 shrink-0" aria-hidden="true" />}
        <span>{conserved ? "本业务可用余额、冻结、结算与退回金额守恒" : "本业务账务存在历史字段缺失或金额不守恒，需要人工核对"}</span>
      </div>
      {item.review_status && (
        <div className={`mt-3 border-l-2 pl-3 text-xs ${item.review_status === "pending" ? "border-warn text-warn" : "border-aqua text-mist"}`}>
          <p className="font-medium">{REVIEW_STATUS_LABELS[item.review_status] || item.review_status}</p>
          {(item.review_reason || item.review_note) && (
            <p className="mt-1 break-words text-fog">{item.review_reason || item.review_note}</p>
          )}
          {item.reviewed_at && <p className="mt-1 text-fog">处理时间 {formatLocalDateTime(item.reviewed_at, { includeSeconds: true })}</p>}
        </div>
      )}
      {taskHref && (
        <Link href={taskHref} className="btn-secondary btn-sm mt-3 inline-flex">
          <ExternalLink size={13} aria-hidden="true" />
          查看任务详情
        </Link>
      )}
    </details>
  );
}

export function BillingEntryList({ items }) {
  return (
    <div role="list" className="divide-y divide-line border-y border-line">
      {items.map((item) => (
        <article key={item.key} role="listitem" className="py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold text-snow">{item.title}</h3>
                <span className={`badge ${statusClass(item.status_group)}`}>
                  {STATUS_LABELS[item.status] || item.status}
                </span>
                {item.outstanding_frozen_credits > 0 && (
                  <span className="badge bg-warn/15 text-warn">冻结中</span>
                )}
              </div>
              {item.subtitle && <p className="mt-1 truncate text-xs text-fog" title={item.subtitle}>{item.subtitle}</p>}
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-fog">
                <span>{formatLocalDateTime(item.updated_at)}</span>
                {item.model_name && <span title={item.model_id || item.model_name}>模型 {item.model_name}</span>}
                {item.transaction_count > 1 && <span>{item.transaction_count} 笔流水</span>}
              </div>
            </div>
            <div className="shrink-0 text-right">
              {item.credited_credits > 0 ? (
                <div className="font-display text-lg font-bold text-ok">+{item.credited_credits}</div>
              ) : item.net_consumed_credits > 0 ? (
                <div className="font-display text-lg font-bold text-snow">-{item.net_consumed_credits}</div>
              ) : item.outstanding_frozen_credits > 0 ? (
                <div className="font-display text-lg font-bold text-warn">{item.outstanding_frozen_credits}</div>
              ) : (
                <div className="font-display text-lg font-bold text-mist">0</div>
              )}
              <div className="text-[11px] text-fog">
                {item.credited_credits > 0 ? "已入账" : item.outstanding_frozen_credits > 0 ? "当前冻结" : "净消费"}
              </div>
            </div>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 rounded-xl border border-line bg-base2/40 px-3 py-2.5 sm:grid-cols-5">
            <Metric label="冻结" value={item.frozen_credits} />
            <Metric label="结算" value={item.settled_credits} />
            <Metric label="退回" value={item.refunded_credits} />
            <Metric label="净消费" value={item.net_consumed_credits} highlight />
            <Metric label="变动后可用" value={item.balance_after} />
          </div>
          <BillingEntryDetails item={item} />
        </article>
      ))}
    </div>
  );
}

export function CreditTransactionList({ items }) {
  return (
    <div role="list" className="divide-y divide-line border-y border-line">
      {items.map((item) => {
        const positive = item.balance_delta > 0;
        return (
          <article key={item.id} role="listitem" className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`badge ${positive ? "bg-ok/15 text-ok" : item.type === "freeze" ? "bg-warn/15 text-warn" : "bg-white/10 text-mist"}`}>
                  {TYPE_LABELS[item.type] || item.type}
                </span>
                <span className="text-xs text-fog">
                  {item.biz_type || "未关联业务"}{item.biz_ref != null ? ` #${item.biz_ref}` : ""}
                </span>
              </div>
              {item.note && <p className="mt-1 truncate text-xs text-mist" title={item.note}>{item.note}</p>}
              <p className="mt-1 text-xs text-fog">{formatLocalDateTime(item.created_at, { includeSeconds: true })}</p>
            </div>
            <div className="flex items-center justify-between gap-5 sm:justify-end sm:text-right">
              <div>
                <div className={`font-display text-base font-semibold ${positive ? "text-ok" : item.balance_delta < 0 ? "text-snow" : "text-mist"}`}>
                  {item.balance_delta > 0 ? "+" : ""}{item.balance_delta}
                </div>
                <div className="text-[11px] text-fog">可用变动</div>
              </div>
              <div>
                <div className="font-display text-sm font-semibold text-mist">{item.balance_after}</div>
                <div className="text-[11px] text-fog">变动后可用</div>
              </div>
              <div>
                <div className="font-display text-sm font-semibold text-mist">{item.frozen_after ?? "-"}</div>
                <div className="text-[11px] text-fog">变动后冻结</div>
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function LedgerLoading() {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="积分账单加载中">
      {Array.from({ length: 5 }).map((_, index) => <div key={index} className="skeleton h-28" />)}
    </div>
  );
}

export function LedgerEmpty({ raw }) {
  return (
    <div className="py-14 text-center" role="status">
      {raw ? <ScrollText size={28} className="mx-auto text-fog" aria-hidden="true" /> : <Coins size={28} className="mx-auto text-fog" aria-hidden="true" />}
      <h3 className="mt-3 text-sm font-medium text-mist">{raw ? "暂无匹配流水" : "暂无匹配账单"}</h3>
    </div>
  );
}

export function LedgerError({ message, onRetry }) {
  return (
    <div role="alert" className="rounded-xl2 border border-bad/30 bg-bad/10 px-4 py-8 text-center">
      <AlertCircle size={22} className="mx-auto text-bad" aria-hidden="true" />
      <p className="mt-2 text-sm text-bad">{message}</p>
      <button type="button" onClick={onRetry} className="btn-secondary btn-sm mt-4">
        <RefreshCw size={14} aria-hidden="true" />
        重新加载
      </button>
    </div>
  );
}
