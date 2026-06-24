import importlib.util
import tarfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_release_artifact.py"
_SPEC = importlib.util.spec_from_file_location("check_release_artifact", _SCRIPT)
check_release_artifact = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(check_release_artifact)


def test_release_artifact_checker_allows_clean_source_tree(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "backend").mkdir()
    (root / "backend" / ".env.example").write_text("JWT_SECRET=\n", encoding="utf-8")
    (root / "backend" / ".env.local.example").write_text("NEXT_PUBLIC_API_BASE=\n", encoding="utf-8")
    (root / "README.md").write_text("ok\n", encoding="utf-8")

    assert check_release_artifact.main(["check_release_artifact.py", str(root)]) == 0


def test_release_artifact_checker_rejects_runtime_tree_entries(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    for rel in [
        "backend/.env",
        "frontend/.env.local",
        ".codex/config.json",
        ".agents/state.json",
        ".git/config",
        "__MACOSX/._README.md",
        "frontend/node_modules/pkg/index.js",
        "frontend/.next/server/app.js",
        "backend/.venv/bin/python",
        "backend/storage/preview/leak.png",
        "coverage/lcov.info",
        "htmlcov/index.html",
        "logs/api.log",
        ".coverage",
        "backend/celerybeat-schedule.db",
        "backend/certs/payment.key.example",
    ]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("blocked\n", encoding="utf-8")

    assert check_release_artifact.main(["check_release_artifact.py", str(root)]) == 1


def test_release_artifact_checker_rejects_tree_symlinks(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "README.md").write_text("ok\n", encoding="utf-8")
    (root / "external-secret").symlink_to("/etc/passwd")

    assert check_release_artifact.main(["check_release_artifact.py", str(root)]) == 1


def test_release_artifact_checker_rejects_zip_and_tar_members(tmp_path):
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("app/backend/.env.production", "secret")
        zf.writestr("../../README.md", "traversal")
        zf.writestr("app/frontend/package.json", "{}")

    tar_path = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload.pem"
    payload.write_text("secret", encoding="utf-8")
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(payload, arcname="app/certs/payment.pem")
        tf.add(payload, arcname="/tmp/absolute.pem")
        link = tarfile.TarInfo("app/safe-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../backend/.env"
        tf.addfile(link)

    assert check_release_artifact.main(["check_release_artifact.py", str(zip_path)]) == 1
    assert check_release_artifact.main(["check_release_artifact.py", str(tar_path)]) == 1


def test_release_artifact_checker_rejects_size_budget_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(check_release_artifact, "MAX_ARTIFACT_BYTES", 128)
    monkeypatch.setattr(check_release_artifact, "MAX_FRONTEND_PUBLIC_BYTES", 64)
    monkeypatch.setattr(check_release_artifact, "MAX_SINGLE_FILE_BYTES", 32)
    root = tmp_path / "src"
    public = root / "frontend" / "public"
    public.mkdir(parents=True)
    (public / "large.bin").write_bytes(b"x" * 40)
    (public / "other.bin").write_bytes(b"y" * 40)
    (root / "README.md").write_text("ok\n", encoding="utf-8")

    assert check_release_artifact.main(["check_release_artifact.py", str(root)]) == 1
