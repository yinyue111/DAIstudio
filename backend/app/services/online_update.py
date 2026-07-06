"""Admin-triggered source update from the configured Git remote.

This deliberately exposes only a narrow operation: fetch a fixed remote/branch,
fast-forward the checked-out repository, then optionally run a fixed operator
configured apply command. The UI never supplies shell commands.
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import settings
from . import locks
from .ssrf import SsrfError, assert_safe_url, resolve_safe

_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_OUTPUT_LIMIT = 12_000
_LOCK_KEY = "admin:online-update"
_URL_USERINFO_RE = re.compile(r"(?P<prefix>https?://)(?P<userinfo>[^/\s:@]+(?::[^/\s@]*)?@)", re.I)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?P<key>\b(?:[A-Za-z0-9_.-]*(?:api[_-]?key|token|secret|password|authorization|private[_-]?key)[A-Za-z0-9_.-]*)\b)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\r\n,;]+)",
    re.I,
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.S,
)
_OPENAI_LIKE_KEY_RE = re.compile(r"\b(?P<prefix>sk|ark)-[A-Za-z0-9_-]{8,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[opsru]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b")
_ALLOWED_REMOTE_SCHEMES = {"https", "ssh"}
_SCP_REMOTE_RE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>[A-Za-z0-9._~/-]+)(?:\.git)?$")
_GITHUB_SSH_REMOTE_RE = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\.git)?$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_COMPOSE_MUTATION_RE = re.compile(
    r"\b(?:docker\s+compose|docker-compose)\b[\s\S]{0,240}\b(?:up|restart|down|rm)\b",
    re.I,
)
_COMPOSE_API_SERVICE_RE = re.compile(r"(?:^|\s)(?:api|backend|server)(?:\s|$)", re.I)
_SCRIPT_SCAN_BYTES = 64_000


class OnlineUpdateError(RuntimeError):
    pass


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return _clip("\n".join(part for part in [self.stdout, self.stderr] if part))


def _clip(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    value = _redact_text(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...输出已截断..."


def _redact_text(text: str) -> str:
    value = _URL_USERINFO_RE.sub(r"\g<prefix><redacted>@", str(text or ""))
    value = _PEM_PRIVATE_KEY_RE.sub("<redacted-private-key>", value)
    value = _KEY_VALUE_SECRET_RE.sub(r"\g<key>\g<sep><redacted>", value)
    value = _OPENAI_LIKE_KEY_RE.sub(lambda m: f"{m.group('prefix')}-<redacted>", value)
    value = _GITHUB_TOKEN_RE.sub("<redacted>", value)
    configured_token = str(getattr(settings, "online_update_github_token", "") or "").strip()
    if len(configured_token) >= 8:
        value = value.replace(configured_token, "<redacted>")
        encoded = base64.b64encode(f"x-access-token:{configured_token}".encode()).decode("ascii")
        value = value.replace(encoded, "<redacted>")
    return value


def _safe_command(args: list[str]) -> str:
    return _redact_text(" ".join(str(part) for part in args))


def _read_candidate_apply_file(part: str) -> str:
    if not part or part.startswith("-"):
        return ""
    path = Path(part).expanduser()
    repo = Path(settings.online_update_repo_dir).expanduser()
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        # Scan repo-relative script names such as `python apply.py`; these are
        # common in deployment docs and otherwise bypass the self-restart guard.
        candidates.append(repo / path)
        if "/" in part:
            candidates.append(path)
        else:
            found = shutil.which(part)
            if found:
                candidates.append(Path(found))
    try:
        readable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if readable is None:
            return ""
        with readable.open("rb") as f:
            data = f.read(_SCRIPT_SCAN_BYTES)
    except OSError:
        return ""
    return data.decode("utf-8", errors="ignore")


def _looks_like_self_restarting_compose(text: str) -> bool:
    raw = str(text or "")
    # Deployment wrappers are often Python scripts:
    # subprocess.run(["docker", "compose", "up", ..., "api"]).
    # Normalize common punctuation so the same guard catches shell and argv-list
    # forms without trying to execute or parse the script.
    normalized = re.sub(r"['\"`,()[\]{}]", " ", raw)
    normalized = re.sub(r"\s+", " ", normalized)
    match = _COMPOSE_MUTATION_RE.search(normalized)
    if not match:
        return False
    # `docker compose up` without explicit services can recreate the whole app,
    # including the API process that is executing the update. If api-like service
    # names are present, it is definitely unsafe for in-process apply.
    lines = [
        re.sub(r"\s+", " ", re.sub(r"['\"`,()[\]{}]", " ", line))
        for line in raw.splitlines()
    ]
    lines = [line for line in lines if _COMPOSE_MUTATION_RE.search(line)]
    return any(_COMPOSE_API_SERVICE_RE.search(line) for line in lines) or bool(lines) or bool(match)


def _validate_apply_command_safety(parts: list[str]) -> None:
    candidates = [" ".join(parts)]
    candidates.extend(_read_candidate_apply_file(part) for part in parts)
    if any(_looks_like_self_restarting_compose(candidate) for candidate in candidates if candidate):
        raise OnlineUpdateError(
            "ONLINE_UPDATE_APPLY_COMMAND 不能在 API 进程内直接执行会重启 API 的 Docker Compose 命令。"
            "这会在升级过程中停掉当前后端,导致命令中断、8000 端口消失和登录 502。"
            "请改为宿主机 systemd oneshot、外部运维队列或后台 supervisor 执行重建/重启。"
        )


def _safe_ref(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise OnlineUpdateError(f"{label} 不能为空")
    if text.startswith("-") or ".." in text or not _REF_RE.fullmatch(text):
        raise OnlineUpdateError(f"{label} 配置非法")
    return text


def _safe_remote(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE 不能为空")
    if text.startswith("-") or ".." in text or any(ch.isspace() for ch in text):
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE 配置非法")
    if _REF_RE.fullmatch(text):
        return text
    if _SCP_REMOTE_RE.fullmatch(text):
        return text
    parsed = urlparse(text)
    if parsed.scheme not in _ALLOWED_REMOTE_SCHEMES:
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE 只允许 Git remote 名称或安全的 Git URL")
    if parsed.scheme in {"https", "ssh"} and not parsed.netloc:
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE URL 缺少主机")
    if parsed.scheme == "https" and (parsed.username or parsed.password):
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE 不能包含用户名或密码")
    if parsed.scheme == "ssh" and (parsed.password or (parsed.username and parsed.username != "git")):
        raise OnlineUpdateError("ONLINE_UPDATE_REMOTE SSH URL 只允许 git 用户且不能包含密码")
    return text


def _assert_safe_remote_host(host: str | None, *, label: str) -> None:
    if not host:
        raise OnlineUpdateError(f"{label} URL 缺少主机")
    try:
        resolve_safe(host)
    except SsrfError as e:
        raise OnlineUpdateError(f"{label} 主机不安全: {e}") from e


def _is_remote_name(remote: str) -> bool:
    return bool(_REF_RE.fullmatch((remote or "").strip()))


def _validate_remote_url(value: str, *, label: str = "Git remote") -> str:
    text = (value or "").strip()
    if not text:
        raise OnlineUpdateError(f"{label} 不能为空")
    if settings.online_update_allow_local_remote:
        return text
    if text.startswith("-") or any(ch.isspace() for ch in text):
        raise OnlineUpdateError(f"{label} 配置非法")
    scp = _SCP_REMOTE_RE.fullmatch(text)
    if scp:
        _assert_safe_remote_host(scp.group("host"), label=label)
        return text
    parsed = urlparse(text)
    if parsed.scheme not in _ALLOWED_REMOTE_SCHEMES:
        raise OnlineUpdateError(f"{label} 只允许安全的 Git URL，不能指向本地路径")
    if not parsed.netloc:
        raise OnlineUpdateError(f"{label} URL 缺少主机")
    if parsed.scheme == "https":
        if parsed.username or parsed.password:
            raise OnlineUpdateError(f"{label} 不能包含用户名或密码")
        try:
            assert_safe_url(text)
        except SsrfError as e:
            raise OnlineUpdateError(f"{label} 主机不安全: {e}") from e
    if parsed.scheme == "ssh":
        if parsed.password or (parsed.username and parsed.username != "git"):
            raise OnlineUpdateError(f"{label} SSH URL 只允许 git 用户且不能包含密码")
        _assert_safe_remote_host(parsed.hostname, label=label)
    return text


def _resolve_remote_for_validation(repo: Path, remote: str) -> str:
    if not _is_remote_name(remote):
        return _validate_remote_url(remote)
    try:
        resolved = _git(["remote", "get-url", remote], cwd=repo).stdout.strip()
    except OnlineUpdateError as e:
        raise OnlineUpdateError(f"Git remote 名称不存在或无法读取: {remote}") from e
    return _validate_remote_url(resolved, label=f"Git remote {remote}")


def _remote_uses_ssh(remote: str) -> bool:
    if _SCP_REMOTE_RE.fullmatch(remote):
        return True
    return urlparse(remote).scheme == "ssh"


def _github_https_equivalent(remote: str) -> str | None:
    match = _GITHUB_SSH_REMOTE_RE.fullmatch(remote.strip())
    if not match:
        return None
    return f"https://github.com/{match.group('repo')}.git"


def _ensure_transport_ready(remote: str) -> None:
    if not _remote_uses_ssh(remote):
        return
    if shutil.which("ssh"):
        return
    hint = _github_https_equivalent(remote)
    fallback = f"；也可以改用 HTTPS remote: {hint}" if hint else ""
    raise OnlineUpdateError(
        "当前运行后端的环境缺少 ssh 客户端，无法读取 SSH Git remote。"
        "请在容器/服务器安装 openssh-client，或把 ONLINE_UPDATE_REMOTE 改为可访问的 HTTPS Git URL"
        f"{fallback}"
    )


def _repo_dir() -> Path:
    repo = Path(settings.online_update_repo_dir).expanduser().resolve()
    configured_root = Path(settings.online_update_repo_dir).expanduser()
    if not configured_root.is_absolute():
        raise OnlineUpdateError("ONLINE_UPDATE_REPO_DIR 必须是绝对路径")
    if not repo.exists() or not repo.is_dir():
        raise OnlineUpdateError("在线更新仓库目录不存在")
    return repo


def _git_auth_env(remote: str | None = None) -> dict[str, str]:
    token = str(settings.online_update_github_token or "").strip()
    if not token or remote is None or _remote_uses_ssh(remote):
        return {}
    if remote and not _is_remote_name(remote):
        parsed = urlparse(remote)
        if parsed.scheme and (parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com"):
            return {}
    # Keep the token out of the remote URL and command argv. Git applies this
    # header only to github.com HTTPS remotes, including when the command uses
    # a remote name such as "origin".
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    base_count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
    return {
        "GIT_CONFIG_COUNT": str(base_count + 1),
        f"GIT_CONFIG_KEY_{base_count}": "http.https://github.com/.extraheader",
        f"GIT_CONFIG_VALUE_{base_count}": f"Authorization: Basic {auth}",
    }


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    proc_env = None
    if env:
        proc_env = os.environ.copy()
        proc_env.update(env)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout or max(1, int(settings.online_update_timeout_seconds)),
            check=False,
            env=proc_env,
        )
    except subprocess.TimeoutExpired as e:
        output = "\n".join(part for part in [e.stdout or "", e.stderr or ""] if part)
        raise OnlineUpdateError(f"命令超时: {_safe_command(args[:3])}\n{_clip(output)}") from e
    except OSError as e:
        raise OnlineUpdateError(f"命令执行失败: {e}") from e
    result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if proc.returncode != 0:
        raise OnlineUpdateError(f"命令失败: {_safe_command(args)}\n{result.output}")
    return result


def _git(args: list[str], *, cwd: Path, remote: str | None = None) -> CommandResult:
    return _run(["git", *args], cwd=cwd, env=_git_auth_env(remote))


def _is_git_repo(repo: Path) -> bool:
    try:
        result = _git(["rev-parse", "--is-inside-work-tree"], cwd=repo)
    except OnlineUpdateError:
        return False
    return result.stdout.strip() == "true"


def _head(repo: Path, rev: str = "HEAD") -> str:
    return _git(["rev-parse", rev], cwd=repo).stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()


def _dirty_status(repo: Path) -> str:
    return _git(["status", "--porcelain"], cwd=repo).stdout.strip()


def _remote_head(repo: Path, remote: str, branch: str) -> str:
    result = _git(["ls-remote", "--heads", remote, branch], cwd=repo, remote=remote)
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            return parts[0]
    raise OnlineUpdateError(f"远端分支不存在或无法读取: {remote}/{branch}")


def _safe_expected_head(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise OnlineUpdateError("执行在线升级前必须先检查远端版本，并提交 expected_remote_head")
    if not _COMMIT_SHA_RE.fullmatch(text):
        raise OnlineUpdateError("expected_remote_head 必须是 40 位 Git commit SHA")
    return text.lower()


def _verify_remote_head(expected_remote_head: str | None, actual_remote_head: str) -> None:
    expected = _safe_expected_head(expected_remote_head)
    actual = str(actual_remote_head or "").strip().lower()
    if actual != expected:
        raise OnlineUpdateError(
            "远端版本在确认后发生变化，已拒绝升级。请重新检查远端版本后再执行。"
        )


def _verify_commit_signature(repo: Path, rev: str) -> None:
    if not bool(getattr(settings, "online_update_require_signed_commits", False)):
        return
    try:
        _git(["verify-commit", rev], cwd=repo)
    except OnlineUpdateError as e:
        raise OnlineUpdateError("远端提交未通过 Git 签名校验，已拒绝在线升级") from e


def _ensure_ready(
    repo: Path,
    *,
    fetch: bool = False,
    check_remote: bool = False,
    enforce_clean: bool = True,
) -> dict[str, Any]:
    remote = _safe_remote(settings.online_update_remote)
    branch = _safe_ref(settings.online_update_branch, "ONLINE_UPDATE_BRANCH")
    if not _is_git_repo(repo):
        raise OnlineUpdateError("在线更新目录不是 Git 仓库")
    resolved_remote = _resolve_remote_for_validation(repo, remote)
    _ensure_transport_ready(resolved_remote)
    branch_now = _current_branch(repo)
    if branch_now != branch:
        raise OnlineUpdateError(f"当前分支是 {branch_now}, 配置分支是 {branch}, 为避免误更新已拒绝")
    dirty = _dirty_status(repo)
    if enforce_clean and dirty and not settings.online_update_allow_dirty:
        raise OnlineUpdateError("当前工作区存在未提交改动,请先提交/清理后再在线更新")
    if fetch:
        _git(["fetch", "--prune", remote, branch], cwd=repo, remote=remote)
    current = _head(repo)
    remote_head = ""
    if fetch:
        remote_head = _head(repo, "FETCH_HEAD")
    elif check_remote:
        remote_head = _remote_head(repo, remote, branch)
    else:
        remote_ref = f"refs/remotes/{remote}/{branch}"
        try:
            remote_head = _head(repo, remote_ref)
        except OnlineUpdateError:
            remote_head = ""
    return {
        "remote": remote,
        "remote_url": resolved_remote,
        "branch": branch,
        "current_branch": branch_now,
        "current_head": current,
        "remote_head": remote_head,
        "dirty": bool(dirty),
        "dirty_status": dirty,
    }


def status(*, check_remote: bool = False) -> dict[str, Any]:
    base = {
        "enabled": bool(settings.online_update_enabled),
        "repo_dir": str(Path(settings.online_update_repo_dir).expanduser()),
        "remote": settings.online_update_remote,
        "branch": settings.online_update_branch,
        "github_token_configured": bool(str(settings.online_update_github_token or "").strip()),
        "current_branch": "",
        "current_head": "",
        "remote_head": "",
        "dirty": False,
        "dirty_status": "",
        "apply_command_configured": bool(str(settings.online_update_apply_command or "").strip()),
        "allow_dirty": bool(settings.online_update_allow_dirty),
        "require_signed_commits": bool(getattr(settings, "online_update_require_signed_commits", False)),
    }
    try:
        repo = _repo_dir()
        base["repo_dir"] = str(repo)
        base.update(_ensure_ready(repo, check_remote=check_remote, enforce_clean=False))
    except OnlineUpdateError as e:
        base.update({"error": str(e)})
    return base


def _apply_command() -> list[str]:
    raw = str(settings.online_update_apply_command or "").strip()
    if not raw:
        return []
    try:
        parts = shlex.split(raw)
    except ValueError as e:
        raise OnlineUpdateError(f"ONLINE_UPDATE_APPLY_COMMAND 解析失败: {e}") from e
    if not parts:
        return []
    if parts[0] in {"sh", "bash", "zsh"} or parts[0].endswith(("/sh", "/bash", "/zsh")):
        raise OnlineUpdateError("生效命令不能直接调用 shell,请配置固定脚本路径或安全命令")
    if any(part in {"-c", "-m"} for part in parts[1:]):
        raise OnlineUpdateError("生效命令不能使用解释器内联参数,请配置固定脚本路径")
    _validate_apply_command_safety(parts)
    return parts


def _reset_to_head(repo: Path, rev: str, *, allow_dirty: bool = False) -> tuple[bool, str]:
    """Rollback a failed fast-forward before the new code is considered active."""
    if allow_dirty:
        return (
            False,
            "检测到配置允许脏工作区，apply 失败后未自动回滚，避免覆盖本地未提交改动。",
        )
    try:
        reset = _git(["reset", "--hard", rev], cwd=repo)
        return True, reset.output
    except OnlineUpdateError as e:
        return False, f"回滚失败:{_clip(str(e))}"


def run_update(
    *,
    apply: bool = True,
    force_apply: bool = False,
    expected_remote_head: str | None = None,
) -> dict[str, Any]:
    if not settings.online_update_enabled:
        raise OnlineUpdateError("在线更新未启用,请先设置 ONLINE_UPDATE_ENABLED=true 并重启后端")
    repo = _repo_dir()
    token = locks.acquire(_LOCK_KEY, ttl=max(60, int(settings.online_update_timeout_seconds) * 2 + 120))
    if not token:
        raise OnlineUpdateError("已有在线更新正在执行,请稍后再试")
    try:
        ready = _ensure_ready(repo, fetch=True)
        before = ready["current_head"]
        after_fetch = ready["remote_head"]
        changed = bool(after_fetch and after_fetch != before)
        force_apply = bool(force_apply and apply and not changed)
        if apply and (changed or force_apply):
            _verify_remote_head(expected_remote_head, after_fetch)
            _verify_commit_signature(repo, after_fetch)
        outputs: list[str] = []
        command: list[str] = []
        if apply and (changed or force_apply):
            command = _apply_command()
            if changed and not command:
                raise OnlineUpdateError(
                    "检测到新版本但未配置 ONLINE_UPDATE_APPLY_COMMAND,已拒绝直接合并代码。"
                    "请先配置安全的宿主机/外部生效命令，避免出现新代码已落盘但服务未重启或迁移未执行的半升级状态。"
                )
        if changed and apply:
            merge = _git(["merge", "--ff-only", "FETCH_HEAD"], cwd=repo)
            if merge.output:
                outputs.append(merge.output)
        after = _head(repo)
        applied = False
        apply_output = ""
        if apply and (changed or force_apply):
            if command:
                try:
                    # The configured command may point at a repo-local script.
                    # A just-merged update can replace that script, so re-scan
                    # the actual file immediately before execution.
                    _validate_apply_command_safety(command)
                    apply_result = _run(command, cwd=repo)
                    applied = True
                    apply_output = apply_result.output
                    if apply_output:
                        outputs.append(apply_output)
                except OnlineUpdateError as e:
                    safe_error = _clip(str(e))
                    rollback_output = ""
                    rolled_back = False
                    if changed:
                        rolled_back, rollback_output = _reset_to_head(
                            repo,
                            before,
                            allow_dirty=bool(ready.get("dirty")),
                        )
                        if rollback_output:
                            outputs.append(rollback_output)
                    after_rollback = _head(repo)
                    output = _clip("\n\n".join([*outputs, str(e)]))
                    return {
                        "ok": False,
                        "changed": changed,
                        "applied": False,
                        "partial_failure": True,
                        "before": before,
                        "after": after_rollback,
                        "remote_head": after_fetch,
                        "output": output,
                        "error": safe_error,
                    }
        return {
            "ok": True,
            "changed": changed,
            "applied": applied,
            "partial_failure": False,
            "before": before,
            "after": after,
            "remote_head": after_fetch,
            "output": _clip("\n\n".join(outputs)),
            "error": "",
        }
    finally:
        locks.release(_LOCK_KEY, token)
