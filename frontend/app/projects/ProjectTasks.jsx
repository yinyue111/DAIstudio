"use client";

import Link from "next/link";
import { ExternalLink, Link2, ListTodo, X } from "lucide-react";
import { parseUnifiedTaskKey, taskDetailHref } from "../../lib/unifiedTasks";

const STATUS_LABELS = {
  queued: "排队中",
  running: "进行中",
  succeeded: "已完成",
  done: "已完成",
  failed: "失败",
  canceled: "已取消",
  needs_review: "待确认",
  needs_confirmation: "待确认",
  waiting_review: "待审核",
};

function taskHref(task) {
  const parsed = parseUnifiedTaskKey(`${task?.task_kind || ""}:${task?.task_id || ""}`);
  return parsed ? taskDetailHref(parsed) : "";
}

export default function ProjectTasks({ tasks = [], busy, onRemove }) {
  if (!tasks.length) {
    return <div className="border-y border-line py-12 text-center text-sm text-mist" role="status">暂无项目任务</div>;
  }
  return (
    <ul className="divide-y divide-line border-y border-line" aria-label="项目任务">
      {tasks.map((task) => {
        const href = taskHref(task);
        const title = task.title || `${task.task_kind || "任务"} #${task.task_id || "-"}`;
        return (
          <li key={`${task.task_kind}:${task.task_id}`} className="flex min-w-0 items-center gap-3 py-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line bg-white/[0.03] text-aqua">
              {task.task_kind === "parse" ? <Link2 size={15} aria-hidden="true" /> : <ListTodo size={15} aria-hidden="true" />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                {href ? (
                  <Link href={href} className="truncate text-sm font-medium text-snow hover:text-aqua" title={`查看${title}`}>
                    {title}
                  </Link>
                ) : (
                  <span className="truncate text-sm font-medium text-snow">{title}</span>
                )}
                <span className="shrink-0 text-[11px] text-fog">{STATUS_LABELS[task.status] || task.status || "已失效"}</span>
              </div>
              <p className="mt-1 truncate text-xs text-fog">{task.summary || `任务 #${task.task_id || "-"}`}</p>
            </div>
            {Number.isFinite(Number(task.progress)) && (
              <span className="hidden shrink-0 text-[11px] tabular-nums text-fog sm:block">{Math.min(100, Math.max(0, Number(task.progress)))}%</span>
            )}
            {href && (
              <Link href={href} className="icon-btn h-8 w-8" title="查看任务详情" aria-label={`查看${title}详情`}>
                <ExternalLink size={14} aria-hidden="true" />
              </Link>
            )}
            <button type="button" className="icon-btn h-8 w-8" onClick={() => onRemove(task)} disabled={busy} title="移出项目" aria-label="移出项目">
              <X size={14} aria-hidden="true" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}
