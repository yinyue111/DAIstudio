from __future__ import annotations

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


def _configure(monkeypatch, repo: Path, *, enabled: bool = True, apply_command: str = ""):
    monkeypatch.setattr(settings, "online_update_enabled", enabled)
    monkeypatch.setattr(settings, "online_update_repo_dir", str(repo))
    monkeypatch.setattr(settings, "online_update_remote", "origin")
    monkeypatch.setattr(settings, "online_update_branch", "main")
    monkeypatch.setattr(settings, "online_update_apply_command", apply_command)
    monkeypatch.setattr(settings, "online_update_timeout_seconds", 20)
    monkeypatch.setattr(settings, "online_update_allow_dirty", False)


def test_admin_online_update_disabled_rejects_run(client, make_user, auth, monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"], enabled=False)
    admin_id = make_user("15700002001", admin=True)
    assert admin_id
    h = auth("15700002001")

    status = client.get("/api/admin/update/status", headers=h)
    assert status.status_code == 200, status.text
    assert status.json()["enabled"] is False

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)
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

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)
    assert run.status_code == 400, run.text
    assert "未提交改动" in run.text


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
    before = _git(git_repos["work"], "rev-parse", "HEAD")
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

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)
    assert run.status_code == 200, run.text
    data = run.json()
    assert data["changed"] is True
    assert data["applied"] is True
    assert data["before"] == before
    assert data["after"] != before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert apply_marker.read_text(encoding="utf-8") == "ok\n"


def test_admin_online_update_noops_when_already_current(client, make_user, auth, monkeypatch, git_repos):
    apply_script = git_repos["work"].parent / "apply-current.py"
    apply_marker = git_repos["work"] / "applied-current.txt"
    apply_script.write_text(
        "from pathlib import Path\nPath('applied-current.txt').write_text('ok\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _configure(
        monkeypatch,
        git_repos["work"],
        apply_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(apply_script))}",
    )
    make_user("15700002004", admin=True)
    h = auth("15700002004")

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)
    assert run.status_code == 200, run.text
    data = run.json()
    assert data["changed"] is False
    assert data["applied"] is True
    assert data["before"] == data["after"]
    assert apply_marker.read_text(encoding="utf-8") == "ok\n"


def test_admin_online_update_reports_partial_failure_when_apply_fails(
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
    before = _git(git_repos["work"], "rev-parse", "HEAD")
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

    run = client.post("/api/admin/update/run", json={"apply": True}, headers=h)

    assert run.status_code == 200, run.text
    data = run.json()
    assert data["ok"] is False
    assert data["changed"] is True
    assert data["applied"] is False
    assert data["partial_failure"] is True
    assert data["before"] == before
    assert data["after"] != before
    assert (git_repos["work"] / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert "sk-should-redact" not in data["output"]
    assert "sk-should-redact" not in data["error"]
    assert "<redacted>" in data["output"]
    assert "<redacted>" in data["error"]


def test_admin_online_update_status_returns_config_error(client, make_user, auth, monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path / "missing")
    make_user("15700002005", admin=True)
    h = auth("15700002005")

    status = client.get("/api/admin/update/status", headers=h)
    assert status.status_code == 200, status.text
    data = status.json()
    assert data["repo_dir"].endswith("missing")
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


def test_online_update_rejects_inline_apply_command(monkeypatch, git_repos):
    from app.services import online_update

    _configure(monkeypatch, git_repos["work"], apply_command="python -c print(1)")
    with pytest.raises(online_update.OnlineUpdateError, match="解释器内联参数"):
        online_update._apply_command()


def test_online_update_redacts_secrets_from_command_output():
    from app.services import online_update

    text = (
        "remote=https://ghp_abcdefghijklmnopqrstuvwxyz012345@example.com/repo.git\n"
        "api_key=sk-abcdef1234567890 token=ark-abcdef1234567890 password=hunter2"
    )
    redacted = online_update._clip(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert "sk-abcdef1234567890" not in redacted
    assert "ark-abcdef1234567890" not in redacted
    assert "hunter2" not in redacted
    assert "<redacted>" in redacted
