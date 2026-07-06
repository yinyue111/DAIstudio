from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture()
def git_repos(tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    work = tmp_path / "work"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.email", "tester@example.com")
    _git(source, "config", "user.name", "Tester")
    (source / "README.md").write_text("v1\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "checkout", "main")
    return {"remote": remote, "source": source, "work": work}


def _configure(
    monkeypatch,
    repo: Path,
    *,
    enabled: bool = True,
    remote: str = "origin",
    apply_command: str = "",
    allow_local_remote: bool = True,
):
    monkeypatch.setattr(settings, "online_update_enabled", enabled)
    monkeypatch.setattr(settings, "online_update_repo_dir", str(repo))
    monkeypatch.setattr(settings, "online_update_remote", remote)
    monkeypatch.setattr(settings, "online_update_branch", "main")
    monkeypatch.setattr(settings, "online_update_apply_command", apply_command)
    monkeypatch.setattr(settings, "online_update_timeout_seconds", 20)
    monkeypatch.setattr(settings, "online_update_allow_dirty", False)
    monkeypatch.setattr(settings, "online_update_allow_local_remote", allow_local_remote)
    monkeypatch.setattr(settings, "online_update_require_signed_commits", False)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _run_body(remote_head: str | None = None, **overrides):
    body = {"apply": True, "confirm": "UPDATE"}
    if remote_head:
        body["expected_remote_head"] = remote_head
    body.update(overrides)
    return body


def test_admin_online_update_disabled_rejects_run(client, make_user, auth, monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"], enabled=False)
    admin_id = make_user("15700002001", admin=True)
    assert admin_id
    h = auth("15700002001")

    status = client.get("/api/admin/update/status", headers=h)
    assert status.status_code == 200, status.text
    assert status.json()["enabled"] is False

    run = client.post("/api/admin/update/run", json=_run_body(), headers=h)
    assert run.status_code == 400, run.text
    assert "在线更新未启用" in run.text

    direct = online_update.status()
    assert direct["repo_dir"] == str(git_repos["work"])


def test_admin_online_update_refuses_dirty_worktree(client, make_user, auth, monkeypatch, git_repos):
    _configure(monkeypatch, git_repos["work"])
    (git_repos["work"] / "local.txt").write_text("dirty\n", encoding="utf-8")
    make_user("15700002002", admin=True)
    h = auth("15700002002")

    status = client.get("/api/admin/update/status", headers=h)
    assert status.status_code == 200, status.text
    assert status.json()["dirty"] is True

    run = client.post("/api/admin/update/run", json=_run_body(), headers=h)
    assert run.status_code == 400, run.text
    assert "未提交改动" in run.text


def test_admin_online_update_requires_explicit_confirmation(client, make_user, auth, monkeypatch, git_repos):
    _configure(monkeypatch, git_repos["work"])
    make_user("15700002013", admin=True)
    h = auth("15700002013")

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)

    assert run.status_code == 400, run.text
    assert "确认码 UPDATE" in run.text


def test_admin_online_update_preview_does_not_merge_without_confirmation(client, make_user, auth, monkeypatch, git_repos):
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "update")
    _git(git_repos["source"], "push", "origin", "main")
    _configure(monkeypatch, git_repos["work"])
    make_user("15700002014", admin=True)
    h = auth("15700002014")

    run = client.post("/api/admin/update/run", json={"apply": False}, headers=h)

    assert run.status_code == 200, run.text
    data = run.json()
    assert data["changed"] is True
    assert data["applied"] is False
    assert data["before"] == before
    assert data["after"] == before
    assert _git(git_repos["work"], "rev-parse", "HEAD") == before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_admin_online_update_fast_forwards_and_runs_configured_apply_command(
    client, make_user, auth, monkeypatch, git_repos
):
    apply_script = git_repos["work"] / "apply.py"
    apply_marker = git_repos["work"] / "applied.txt"
    apply_script.write_text(
        "from pathlib import Path\nPath('applied.txt').write_text('ok\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(git_repos["work"], "add", "apply.py")
    _git(git_repos["work"], "config", "user.email", "tester@example.com")
    _git(git_repos["work"], "config", "user.name", "Tester")
    _git(git_repos["work"], "commit", "-m", "local apply script")
    _git(git_repos["work"], "push", "origin", "main")
    _git(git_repos["source"], "pull", "--ff-only")
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "update")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(apply_script))}",
    )
    make_user("15700002003", admin=True)
    h = auth("15700002003")

    remote_head = _head(git_repos["source"])
    run = client.post("/api/admin/update/run", json=_run_body(remote_head), headers=h)
    assert run.status_code == 200, run.text
    data = run.json()
    assert data["changed"] is True
    assert data["applied"] is True
    assert data["before"] == before
    assert data["after"] != before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert apply_marker.read_text(encoding="utf-8") == "ok\n"


def test_admin_online_update_noops_when_already_current(client, make_user, auth, monkeypatch, git_repos):
    apply_marker = git_repos["work"] / "applied-current.txt"
    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} -V",
    )
    make_user("15700002004", admin=True)
    h = auth("15700002004")

    run = client.post("/api/admin/update/run", json=_run_body(_head(git_repos["work"])), headers=h)
    assert run.status_code == 200, run.text
    data = run.json()
    assert data["changed"] is False
    assert data["applied"] is False
    assert data["before"] == data["after"]
    assert not apply_marker.exists()


def test_admin_online_update_can_reapply_current_head(client, make_user, auth, monkeypatch, git_repos):
    apply_script = git_repos["work"] / "apply_current.py"
    apply_marker = git_repos["work"] / "applied-current.txt"
    apply_script.write_text(
        "from pathlib import Path\nPath('applied-current.txt').write_text('reapplied\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(git_repos["work"], "add", "apply_current.py")
    _git(git_repos["work"], "config", "user.email", "tester@example.com")
    _git(git_repos["work"], "config", "user.name", "Tester")
    _git(git_repos["work"], "commit", "-m", "local apply current script")
    _git(git_repos["work"], "push", "origin", "main")
    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(apply_script))}",
    )
    make_user("15700002024", admin=True)
    h = auth("15700002024")

    run = client.post(
        "/api/admin/update/run",
        json=_run_body(_head(git_repos["work"]), force_apply=True),
        headers=h,
    )

    assert run.status_code == 200, run.text
    data = run.json()
    assert data["ok"] is True
    assert data["changed"] is False
    assert data["applied"] is True
    assert data["before"] == data["after"]
    assert apply_marker.read_text(encoding="utf-8") == "reapplied\n"


def test_admin_online_update_rolls_back_when_apply_fails(
    client, make_user, auth, monkeypatch, git_repos
):
    apply_script = git_repos["work"] / "apply_fail.py"
    apply_script.write_text(
        "import sys\nprint('apply failed with token=sk-should-redact')\nsys.exit(7)\n",
        encoding="utf-8",
    )
    _git(git_repos["work"], "add", "apply_fail.py")
    _git(git_repos["work"], "config", "user.email", "tester@example.com")
    _git(git_repos["work"], "config", "user.name", "Tester")
    _git(git_repos["work"], "commit", "-m", "local failing apply script")
    _git(git_repos["work"], "push", "origin", "main")
    _git(git_repos["source"], "pull", "--ff-only")
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "remote code update")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(apply_script))}",
    )
    make_user("15700002006", admin=True)
    h = auth("15700002006")

    remote_head = _head(git_repos["source"])
    run = client.post("/api/admin/update/run", json=_run_body(remote_head), headers=h)

    assert run.status_code == 200, run.text
    data = run.json()
    assert data["ok"] is False
    assert data["changed"] is True
    assert data["applied"] is False
    assert data["partial_failure"] is True
    assert data["before"] == before
    assert data["after"] == before
    assert _git(git_repos["work"], "rev-parse", "HEAD") == before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v1\n"
    assert "sk-should-redact" not in data["output"]
    assert "sk-should-redact" not in data["error"]
    assert "<redacted>" in data["output"]
    assert "<redacted>" in data["error"]


def test_admin_online_update_rescans_apply_script_after_merge(
    client, make_user, auth, monkeypatch, git_repos
):
    apply_script = git_repos["work"] / "apply_safe.py"
    apply_script.write_text(
        "from pathlib import Path\nPath('applied.txt').write_text('ok\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(git_repos["work"], "add", "apply_safe.py")
    _git(git_repos["work"], "config", "user.email", "tester@example.com")
    _git(git_repos["work"], "config", "user.name", "Tester")
    _git(git_repos["work"], "commit", "-m", "safe apply script")
    _git(git_repos["work"], "push", "origin", "main")
    _git(git_repos["source"], "pull", "--ff-only")
    before = _head(git_repos["work"])

    apply_script_source = git_repos["source"] / "apply_safe.py"
    apply_script_source.write_text(
        "import subprocess\nsubprocess.run(['docker', 'compose', 'up', '-d', 'api'], check=True)\n",
        encoding="utf-8",
    )
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md", "apply_safe.py")
    _git(git_repos["source"], "commit", "-m", "unsafe apply replacement")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(apply_script))}",
    )
    make_user("15700002026", admin=True)
    h = auth("15700002026")

    remote_head = _head(git_repos["source"])
    run = client.post("/api/admin/update/run", json=_run_body(remote_head), headers=h)

    assert run.status_code == 200, run.text
    data = run.json()
    assert data["ok"] is False
    assert data["changed"] is True
    assert data["applied"] is False
    assert data["partial_failure"] is True
    assert data["before"] == before
    assert data["after"] == before
    assert _git(git_repos["work"], "rev-parse", "HEAD") == before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v1\n"
    assert not (git_repos["work"] / "applied.txt").exists()
    assert "不能在 API 进程内直接执行" in data["error"]


def test_admin_online_update_refuses_changed_apply_without_command(
    client, make_user, auth, monkeypatch, git_repos
):
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "remote code update")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(monkeypatch, git_repos["work"], apply_command="")
    make_user("15700002016", admin=True)
    h = auth("15700002016")

    remote_head = _head(git_repos["source"])
    run = client.post("/api/admin/update/run", json=_run_body(remote_head), headers=h)

    assert run.status_code == 400, run.text
    assert "未配置 ONLINE_UPDATE_APPLY_COMMAND" in run.text
    assert _git(git_repos["work"], "rev-parse", "HEAD") == before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v1\n"


def test_admin_online_update_requires_expected_remote_head_for_changed_apply(
    client, make_user, auth, monkeypatch, git_repos
):
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "remote code update")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} -V",
    )
    make_user("15700002027", admin=True)
    h = auth("15700002027")

    run = client.post("/api/admin/update/run", json=_run_body(), headers=h)

    assert run.status_code == 400, run.text
    assert "expected_remote_head" in run.text
    assert _head(git_repos["work"]) == before


def test_admin_online_update_rejects_remote_head_changed_after_confirmation(
    client, make_user, auth, monkeypatch, git_repos
):
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "first remote code update")
    _git(git_repos["source"], "push", "origin", "main")
    expected = _head(git_repos["source"])
    (git_repos["source"] / "README.md").write_text("v3\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "second remote code update")
    _git(git_repos["source"], "push", "origin", "main")

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} -V",
    )
    make_user("15700002028", admin=True)
    h = auth("15700002028")

    run = client.post("/api/admin/update/run", json=_run_body(expected), headers=h)

    assert run.status_code == 400, run.text
    assert "远端版本在确认后发生变化" in run.text
    assert _head(git_repos["work"]) == before


def test_admin_online_update_rejects_unsigned_commit_when_required(
    client, make_user, auth, monkeypatch, git_repos
):
    before = _head(git_repos["work"])
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "unsigned remote code update")
    _git(git_repos["source"], "push", "origin", "main")
    remote_head = _head(git_repos["source"])

    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} -V",
    )
    monkeypatch.setattr(settings, "online_update_require_signed_commits", True)
    make_user("15700002029", admin=True)
    h = auth("15700002029")

    run = client.post("/api/admin/update/run", json=_run_body(remote_head), headers=h)

    assert run.status_code == 400, run.text
    assert "签名校验" in run.text
    assert _head(git_repos["work"]) == before


def test_admin_online_update_status_returns_config_error(client, make_user, auth, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path / "missing")
    monkeypatch.setattr(settings, "online_update_github_token", "github_pat_should_not_leak_123456")
    make_user("15700002005", admin=True)
    h = auth("15700002005")

    status = client.get("/api/admin/update/status", headers=h)
    assert status.status_code == 200, status.text
    data = status.json()
    assert data["repo_dir"].endswith("missing")
    assert data["github_token_configured"] is True
    assert "github_pat_should_not_leak" not in status.text
    assert "不存在" in data["error"]


def test_admin_online_update_remote_check_uses_ls_remote_without_fetch(monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"])
    before_fetch_head = git_repos["work"] / ".git" / "FETCH_HEAD"
    if before_fetch_head.exists():
        before_fetch_head.unlink()
    (git_repos["source"] / "README.md").write_text("v2\n", encoding="utf-8")
    _git(git_repos["source"], "add", "README.md")
    _git(git_repos["source"], "commit", "-m", "remote update")
    _git(git_repos["source"], "push", "origin", "main")

    status = online_update.status(check_remote=True)

    assert status["remote_head"]
    assert status["remote_head"] != status["current_head"]
    assert not before_fetch_head.exists()


def test_admin_online_update_uses_configured_repo_url_not_origin(monkeypatch, git_repos):
    from app.services import online_update

    _git(git_repos["work"], "remote", "set-url", "origin", "https://github.com/private/requires-auth.git")
    _git(git_repos["work"], "remote", "add", "release", str(git_repos["remote"]))
    _configure(monkeypatch, git_repos["work"], remote="release")

    status = online_update.status(check_remote=True)

    assert "error" not in status
    assert status["remote"] == "release"
    assert status["remote_head"]
    assert status["current_head"] == status["remote_head"]


def test_admin_online_update_rejects_named_remote_backed_by_local_path(monkeypatch, git_repos):
    from app.services import online_update

    _git(git_repos["work"], "remote", "add", "local-release", str(git_repos["remote"]))
    _configure(
        monkeypatch,
        git_repos["work"],
        remote="local-release",
        allow_local_remote=False,
    )

    status = online_update.status(check_remote=True)

    assert "本地路径" in status["error"]


def test_online_update_accepts_github_ssh_scp_remote():
    from app.services import online_update

    assert (
        online_update._safe_remote("git@github.com:yinyue111/DAIstudio.git")
        == "git@github.com:yinyue111/DAIstudio.git"
    )


def test_online_update_rejects_https_remote_with_embedded_credentials():
    from app.services import online_update

    with pytest.raises(online_update.OnlineUpdateError, match="不能包含用户名或密码"):
        online_update._safe_remote("https://github_pat_secret@github.com/yinyue111/DAIstudio.git")


def test_online_update_rejects_file_remote():
    from app.services import online_update

    with pytest.raises(online_update.OnlineUpdateError, match="只允许 Git remote 名称或安全的 Git URL"):
        online_update._safe_remote("file:///tmp/private-release.git")


def test_online_update_rejects_plain_git_remote():
    from app.services import online_update

    with pytest.raises(online_update.OnlineUpdateError, match="只允许 Git remote 名称或安全的 Git URL"):
        online_update._safe_remote("git://github.com/yinyue111/DAIstudio.git")
    with pytest.raises(online_update.OnlineUpdateError, match="只允许安全的 Git URL"):
        online_update._validate_remote_url(
            "git://github.com/yinyue111/DAIstudio.git",
            label="Git remote origin",
        )


def test_online_update_rejects_private_https_remote():
    from app.services import online_update

    with pytest.raises(online_update.OnlineUpdateError, match="主机不安全"):
        online_update._validate_remote_url(
            "https://127.0.0.1:8443/repo.git",
            label="Git remote origin",
        )
    with pytest.raises(online_update.OnlineUpdateError, match="主机不安全"):
        online_update._validate_remote_url(
            "https://169.254.169.254/latest/meta-data/repo.git",
            label="Git remote origin",
        )


def test_online_update_rejects_private_ssh_and_scp_remote():
    from app.services import online_update

    with pytest.raises(online_update.OnlineUpdateError, match="主机不安全"):
        online_update._validate_remote_url(
            "ssh://git@10.0.0.1/yinyue111/DAIstudio.git",
            label="Git remote origin",
        )
    with pytest.raises(online_update.OnlineUpdateError, match="主机不安全"):
        online_update._validate_remote_url(
            "git@169.254.169.254:yinyue111/DAIstudio.git",
            label="Git remote origin",
        )


def test_online_update_injects_github_https_token_via_env(monkeypatch, git_repos):
    from app.services import online_update

    captured: dict[str, object] = {}
    token = "github_pat_1234567890abcdef"
    remote = "https://github.com/yinyue111/DAIstudio.git"
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")

    def fake_run(args, *, cwd, timeout=None, env=None):
        captured["args"] = args
        captured["env"] = env
        return online_update.CommandResult(
            args=list(args),
            returncode=0,
            stdout=f"{'a' * 40}\trefs/heads/main\n",
            stderr="",
        )

    monkeypatch.setattr(settings, "online_update_github_token", token)
    monkeypatch.setattr(online_update, "_run", fake_run)

    assert online_update._remote_head(git_repos["work"], remote, "main") == "a" * 40

    assert captured["args"] == ["git", "ls-remote", "--heads", remote, "main"]
    assert token not in " ".join(captured["args"])
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {encoded}"
    assert token not in online_update._safe_command(captured["args"])


def test_online_update_does_not_inject_github_token_for_local_git_commands(monkeypatch):
    from app.services import online_update

    monkeypatch.setattr(settings, "online_update_github_token", "github_pat_1234567890abcdef")

    assert online_update._git_auth_env(None) == {}
    assert online_update._git_auth_env("git@github.com:yinyue111/DAIstudio.git") == {}
    assert online_update._git_auth_env("https://gitlab.example.com/group/repo.git") == {}


def test_online_update_reports_missing_ssh_for_ssh_remote(monkeypatch):
    from app.services import online_update

    monkeypatch.setattr(online_update.shutil, "which", lambda name: None if name == "ssh" else "/bin/tool")

    with pytest.raises(online_update.OnlineUpdateError) as exc:
        online_update._ensure_transport_ready("git@github.com:yinyue111/DAIstudio.git")

    message = str(exc.value)
    assert "缺少 ssh 客户端" in message
    assert "openssh-client" in message
    assert "https://github.com/yinyue111/DAIstudio.git" in message


def test_online_update_rejects_inline_apply_command(monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"], apply_command="python -c print(1)")
    with pytest.raises(online_update.OnlineUpdateError, match="解释器内联参数"):
        online_update._apply_command()


def test_online_update_rejects_direct_docker_compose_apply_command(monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"], apply_command="docker compose up -d --build migrate api worker")

    with pytest.raises(online_update.OnlineUpdateError, match="不能在 API 进程内直接执行"):
        online_update._apply_command()


def test_online_update_rejects_script_that_restarts_api_container(monkeypatch, git_repos, tmp_path):
    from app.services import online_update

    script = tmp_path / "apply-update.sh"
    script.write_text(
        "#!/bin/sh\n"
        "cd /home/drumxian/DAIstudio\n"
        "docker compose up -d --build migrate api worker beat frontend\n",
        encoding="utf-8",
    )
    _configure(monkeypatch, git_repos["work"], apply_command=str(script))

    with pytest.raises(online_update.OnlineUpdateError, match="登录 502"):
        online_update._apply_command()


def test_online_update_rejects_repo_relative_apply_script_that_restarts_api(monkeypatch, git_repos):
    from app.services import online_update

    script = git_repos["work"] / "apply-update.py"
    script.write_text(
        "import subprocess\n"
        "subprocess.run(['docker', 'compose', 'up', '-d', '--build', 'migrate', 'api', 'worker'])\n",
        encoding="utf-8",
    )
    _configure(monkeypatch, git_repos["work"], apply_command=f"{shlex.quote(sys.executable)} apply-update.py")

    with pytest.raises(online_update.OnlineUpdateError, match="登录 502"):
        online_update._apply_command()


def test_online_update_rejects_path_apply_script_that_restarts_api(monkeypatch, git_repos, tmp_path):
    from app.services import online_update

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ai-studio-apply-update"
    script.write_text(
        "#!/bin/sh\n"
        "docker compose restart api worker\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    _configure(monkeypatch, git_repos["work"], apply_command="ai-studio-apply-update")

    with pytest.raises(online_update.OnlineUpdateError, match="登录 502"):
        online_update._apply_command()


def test_online_update_redacts_secrets_from_command_output():
    from app.services import online_update

    text = (
        "remote=https://ghp_abcdefghijklmnopqrstuvwxyz012345@example.com/repo.git\n"
        "api_key=sk-abcdef1234567890 token=ark-abcdef1234567890 password=hunter2 "
        "github_pat_1234567890abcdef\n"
        "ALIPAY_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----abc123-----END PRIVATE KEY-----\n"
        "WECHAT_PAY_PRIVATE_KEY=wx-secret\n"
        "ALIYUN_ACCESS_KEY_SECRET=aliyun-secret"
    )
    redacted = online_update._clip(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "github_pat_1234567890abcdef" not in redacted
    assert "sk-abcdef1234567890" not in redacted
    assert "ark-abcdef1234567890" not in redacted
    assert "hunter2" not in redacted
    assert "abc123" not in redacted
    assert "wx-secret" not in redacted
    assert "aliyun-secret" not in redacted
    assert "<redacted>" in redacted


def test_compose_preserves_backend_env_file_application_values(tmp_path):
    root = Path(__file__).resolve().parents[2]
    compose = root / "docker-compose.yml"
    data = compose.read_text(encoding="utf-8")

    # api/migrate/worker/beat load backend/.env through env_file. If application
    # settings are repeated in explicit environment blocks with empty/default
    # values, Compose overwrites the real .env values after deployment.
    for key in (
        "ONLINE_UPDATE_GITHUB_TOKEN",
        "ONLINE_UPDATE_REMOTE",
        "ONLINE_UPDATE_REPO_DIR",
        "PUBLIC_BASE_URL",
        "CORS_ORIGINS",
        "PAYMENT_FRONTEND_BASE_URL",
        "TRUSTED_PROXY_IPS",
        "PAYMENT_MOCK_ENABLED",
        "SMS_PROVIDER",
        "SMS_HTTP_URL",
        "SMS_HTTP_API_KEY",
        "SMS_HTTP_TIMEOUT_SECONDS",
        "SMS_SIGN_NAME",
        "SMS_TEMPLATE_CODE",
        "DEBUG",
    ):
        assert f"{key}:" not in data
