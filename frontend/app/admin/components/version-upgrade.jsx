"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "../../../lib/api";
import { Card } from "./admin-ui";

function shortSha(value) {
  return value ? value.slice(0, 12) : "-";
}

function StateBadge({ ok, children }) {
  return (
    <span className={`badge ${ok ? "bg-ok/15 text-ok" : "bg-warn/15 text-warn"}`}>
      {children}
    </span>
  );
}

function BadBadge({ children }) {
  return <span className="badge bg-bad/15 text-bad">{children}</span>;
}

function isGithubHttps(remote) {
  return /^https:\/\/github\.com\//i.test(remote || "");
}

function deploymentModeLabel(value) {
  if (value === "docker_compose") return "Docker Compose";
  if (value === "systemd_or_process") return "Systemd/进程";
  if (value === "local_source") return "本地源码";
  return "未知";
}

function strategyLabel(value) {
  if (value === "external_runner") return "外部执行器";
  if (value === "safe_apply_command") return "安全生效命令";
  if (value === "external_runner_required") return "需外部执行器";
  if (value === "manual_apply_required") return "需手动生效";
  if (value === "disabled") return "未启用";
  if (value === "blocked") return "已阻塞";
  return "手动";
}

export function VersionUpgrade() {
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [msg, setMsg] = useState("");
  const [msgKind, setMsgKind] = useState("ok");
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const runningRef = useRef(false);

  async function load(checkRemote = false, { preserveMessage = false } = {}) {
    setLoading(true);
    if (!preserveMessage) {
      setMsg("");
      setMsgKind("ok");
    }
    try {
      const data = await api.adminUpdateStatus(checkRemote);
      setStatus(data);
      if (data.error) {
        setMsg(data.error);
        setMsgKind("bad");
      }
      return data;
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
      return null;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(false); }, []);

  async function runUpgrade() {
    if (runningRef.current) return;
    runningRef.current = true;
    if (!status?.enabled) {
      setMsg("在线版本升级未启用，请先在服务端设置 ONLINE_UPDATE_ENABLED=true 并重启后端。");
      setMsgKind("bad");
      runningRef.current = false;
      return;
    }
    if (status?.dirty && !status?.allow_dirty) {
      setMsg("当前工作区有未提交改动，不能在线升级。请先提交或清理后再操作。");
      setMsgKind("bad");
      runningRef.current = false;
      return;
    }
    setRunning(true);
    setMsg("正在检查远端版本…");
    setMsgKind("ok");
    const freshStatus = await load(true);
    if (!freshStatus || freshStatus.error) {
      setRunning(false);
      runningRef.current = false;
      if (!freshStatus?.error) setMsg("远端版本检查失败，请稍后重试。");
      setMsgKind("bad");
      return;
    }
    if (freshStatus.dirty && !freshStatus.allow_dirty) {
      setRunning(false);
      runningRef.current = false;
      setMsg("当前工作区有未提交改动，不能在线升级。请先提交或清理后再操作。");
      setMsgKind("bad");
      return;
    }
    if (!freshStatus.remote_head) {
      setRunning(false);
      runningRef.current = false;
      setMsg("远端版本未检查成功，不能执行升级。请先确认 GitHub Token 和网络配置。");
      setMsgKind("bad");
      return;
    }
    const reapplyCurrent = Boolean(
      freshStatus.current_head
      && freshStatus.current_head === freshStatus.remote_head
      && freshStatus.apply_command_configured,
    );
    if (freshStatus.current_head && freshStatus.current_head === freshStatus.remote_head && !reapplyCurrent) {
      setRunning(false);
      runningRef.current = false;
      setMsg("当前已经是最新版本，且没有配置生效命令。");
      setMsgKind("ok");
      return;
    }
    if (freshStatus.current_head !== freshStatus.remote_head && !freshStatus.apply_command_configured) {
      setRunning(false);
      runningRef.current = false;
      setMsg("检测到新版本，但未配置安全的生效命令。请先在服务端配置 ONLINE_UPDATE_APPLY_COMMAND 后再升级，避免代码已更新但服务未重启/迁移未执行。");
      setMsgKind("bad");
      return;
    }
    if (freshStatus.apply_command_configured && freshStatus.apply_command_safe === false) {
      setRunning(false);
      runningRef.current = false;
      setMsg(freshStatus.apply_command_error || "当前 ONLINE_UPDATE_APPLY_COMMAND 不安全，已拒绝在线升级。请改为宿主机 systemd oneshot、外部运维队列或后台 supervisor 执行重启。");
      setMsgKind("bad");
      return;
    }
    if (!freshStatus.can_apply_online) {
      setRunning(false);
      runningRef.current = false;
      setMsg(freshStatus.next_action || "当前配置不能从页面直接升级生效，请先完成服务端升级配置。");
      setMsgKind("bad");
      return;
    }
    const applyText = freshStatus.apply_command_configured
      ? "升级完成后会执行服务端配置的生效命令。"
      : "当前没有配置生效命令，只能在已是最新版本时重新检查状态。";
    if (!window.confirm(
      (reapplyCurrent
        ? `当前代码已是最新版本，确认重新执行生效命令？\n`
        : `确认从 ${freshStatus.remote}/${freshStatus.branch} 拉取新代码并升级？\n`) +
      `当前版本：${shortSha(freshStatus.current_head)}\n` +
      `远端版本：${shortSha(freshStatus.remote_head)}\n${applyText}`,
    )) {
      setRunning(false);
      runningRef.current = false;
      return;
    }
    setMsg("");
    setMsgKind("ok");
    setResult(null);
    try {
      const data = await api.adminRunUpdate({
        apply: true,
        confirm: "UPDATE",
        force_apply: reapplyCurrent,
        expected_remote_head: freshStatus.remote_head,
      });
      setResult(data);
      if (!data.ok) {
        setMsg(data.partial_failure
          ? "生效命令执行失败，且自动回滚未完成。请查看输出并手动恢复。"
          : "生效命令执行失败，代码已回滚到升级前版本。请查看输出并修复后重试。");
        setMsgKind("bad");
      } else if (data.changed) {
        setMsg("版本升级已完成");
        setMsgKind("ok");
      } else if (data.applied) {
        setMsg("当前代码已是最新版本，生效命令已重新执行");
        setMsgKind("ok");
      } else {
        setMsg("当前已经是最新版本");
        setMsgKind("ok");
      }
      await load(false, { preserveMessage: true });
    } catch (e) {
      setMsg(e.message);
      setMsgKind("bad");
    } finally {
      setRunning(false);
      runningRef.current = false;
    }
  }

  if (!status) {
    return (
      <Card>
        <span className="text-sm text-mist">加载版本状态…</span>
        {msg && <span className="ml-3 text-sm text-bad">{msg}</span>}
      </Card>
    );
  }

  const currentIsRemote = status.current_head && status.remote_head && status.current_head === status.remote_head;
  const applyCommandUsable = Boolean(status.apply_command_configured && status.apply_command_safe !== false);
  const canReapplyCurrent = Boolean(currentIsRemote && applyCommandUsable);
  const updateRequiresApplyCommand = Boolean(status.current_head && status.remote_head && !currentIsRemote && !status.apply_command_configured);
  const unsafeApplyCommand = Boolean(status.apply_command_configured && status.apply_command_safe === false);
  const upgradeDisabled = running || !status.enabled || !!status.error || !status.remote_head
    || !status.can_apply_online
    || updateRequiresApplyCommand
    || unsafeApplyCommand
    || (currentIsRemote && !canReapplyCurrent);

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-display font-semibold text-snow">版本升级</div>
            <div className="mt-1 text-xs text-fog">
              从服务端配置的 GitHub remote/branch 快进更新代码，并执行固定的生效命令。
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => load(true)} disabled={loading || running} className="btn-secondary btn-sm">
              {loading ? "刷新中…" : "检查远端版本"}
            </button>
            <button onClick={runUpgrade} disabled={upgradeDisabled} className="btn-primary btn-sm">
              {running ? "升级中…" : unsafeApplyCommand ? "生效命令不安全" : updateRequiresApplyCommand ? "需先配置生效命令" : canReapplyCurrent ? "重新执行生效命令" : "从 GitHub 更新并生效"}
            </button>
          </div>
        </div>

        {msg && (
          <div className={`mb-4 rounded-xl border px-3 py-2 text-sm ${
            msgKind === "bad" ? "border-bad/30 bg-bad/10 text-bad" : "border-ok/30 bg-ok/10 text-ok"
          }`}>
            {msg}
          </div>
        )}

        <div className="grid gap-3 md:grid-cols-4">
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">功能状态</div>
            <StateBadge ok={status.enabled}>{status.enabled ? "已启用" : "未启用"}</StateBadge>
          </div>
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">工作区</div>
            <StateBadge ok={!status.dirty}>{status.dirty ? "有未提交改动" : "干净"}</StateBadge>
          </div>
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">生效命令</div>
            {status.apply_command_configured ? (
              status.apply_command_safe === false ? <BadBadge>不安全</BadBadge> : <StateBadge ok>已配置</StateBadge>
            ) : (
              <StateBadge ok={false}>未配置</StateBadge>
            )}
          </div>
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">GitHub Token</div>
            <StateBadge ok={!isGithubHttps(status.remote) || status.github_token_configured}>
              {status.github_token_configured ? "已配置" : isGithubHttps(status.remote) ? "未配置" : "不需要"}
            </StateBadge>
          </div>
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">部署模式</div>
            <StateBadge ok={status.deployment_mode !== "unknown"}>{deploymentModeLabel(status.deployment_mode)}</StateBadge>
          </div>
          <div className="rounded-xl border border-line bg-base2/60 p-3">
            <div className="mb-1 text-xs text-fog">升级策略</div>
            {status.can_apply_online ? (
              <StateBadge ok>{strategyLabel(status.update_strategy)}</StateBadge>
            ) : status.update_strategy === "blocked" || status.update_strategy === "external_runner_required" ? (
              <BadBadge>{strategyLabel(status.update_strategy)}</BadBadge>
            ) : (
              <StateBadge ok={false}>{strategyLabel(status.update_strategy)}</StateBadge>
            )}
          </div>
        </div>

        {status.next_action ? (
          <div className={`mt-4 rounded-xl border p-3 text-sm ${
            status.can_apply_online ? "border-ok/30 bg-ok/10 text-ok" : "border-warn/30 bg-warn/10 text-warn"
          }`}>
            <div className="font-semibold">下一步</div>
            <div className="mt-1 text-xs leading-relaxed text-mist">{status.next_action}</div>
          </div>
        ) : null}

        <div className="mt-4 grid gap-3 text-sm md:grid-cols-2">
          <div className="rounded-xl border border-line bg-white/5 p-3">
            <div className="text-xs text-fog">仓库目录</div>
            <div className="mt-1 break-all font-mono text-xs text-mist">{status.repo_dir}</div>
          </div>
          <div className="rounded-xl border border-line bg-white/5 p-3">
            <div className="text-xs text-fog">更新源</div>
            <div className="mt-1 font-mono text-xs text-mist">{status.remote}/{status.branch}</div>
          </div>
          <div className="rounded-xl border border-line bg-white/5 p-3">
            <div className="text-xs text-fog">当前版本</div>
            <div className="mt-1 font-mono text-xs text-snow">{shortSha(status.current_head)}</div>
          </div>
          <div className="rounded-xl border border-line bg-white/5 p-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-xs text-fog">远端版本</div>
                <div className="mt-1 font-mono text-xs text-snow">{shortSha(status.remote_head)}</div>
              </div>
              {status.remote_head ? (
                <StateBadge ok={currentIsRemote}>{currentIsRemote ? "最新" : "可升级"}</StateBadge>
              ) : null}
            </div>
          </div>
        </div>

        {status.dirty_status ? (
          <div className="mt-4">
            <div className="mb-1 text-xs text-fog">未提交改动</div>
            <pre className="max-h-40 overflow-auto rounded-xl border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
              {status.dirty_status}
            </pre>
          </div>
        ) : null}

        {unsafeApplyCommand ? (
          <div className="mt-4 rounded-xl border border-bad/30 bg-bad/10 p-3 text-sm text-bad">
            <div className="font-semibold">当前生效命令会导致升级中断，已禁止执行。</div>
            <div className="mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed">{status.apply_command_error}</div>
            <div className="mt-2 text-xs leading-relaxed text-mist">
              处理方式：把 <span className="font-mono">ONLINE_UPDATE_APPLY_COMMAND</span> 改成只通知宿主机的固定命令，例如 systemd oneshot、外部运维队列或 supervisor；真正的 <span className="font-mono">docker compose up/restart api</span> 必须由 API 容器外部执行。
            </div>
          </div>
        ) : null}
      </Card>

      {result ? (
        <Card>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-display font-semibold text-snow">升级结果</div>
            <div className="flex gap-2">
              <StateBadge ok={result.ok}>{result.partial_failure ? "生效失败" : result.ok ? "成功" : "失败"}</StateBadge>
              <StateBadge ok={result.changed}>{result.changed ? "已更新" : "无变化"}</StateBadge>
              <StateBadge ok={result.applied}>{result.applied ? "已生效" : "未执行生效命令"}</StateBadge>
            </div>
          </div>
          {result.error ? (
            <div className="mb-3 rounded-xl border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
              {result.error}
            </div>
          ) : null}
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <div className="rounded-xl border border-line bg-white/5 p-3">
              <div className="text-xs text-fog">升级前</div>
              <div className="mt-1 font-mono text-xs text-mist">{shortSha(result.before)}</div>
            </div>
            <div className="rounded-xl border border-line bg-white/5 p-3">
              <div className="text-xs text-fog">升级后</div>
              <div className="mt-1 font-mono text-xs text-mist">{shortSha(result.after)}</div>
            </div>
          </div>
          {result.output ? (
            <pre className="mt-4 max-h-72 overflow-auto rounded-xl border border-line bg-black/30 p-3 text-xs text-mist">
              {result.output}
            </pre>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}
