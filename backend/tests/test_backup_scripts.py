import hashlib
import os
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup_db.sh"
VERIFY = ROOT / "scripts" / "verify_restore.sh"


def _run_script(path: Path, *args, **kwargs):
    """Use posix_spawn so the threaded app test harness cannot deadlock after fork."""
    return subprocess.run(
        [path, *args],
        close_fds=False,
        timeout=15,
        **kwargs,
    )


def _popen_script(path: Path, *args, **kwargs):
    return subprocess.Popen([path, *args], close_fds=False, **kwargs)


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _executable(
        bin_dir / "pg_dump",
        'printf "pg_dump" >> "$COMMAND_LOG"\n'
        'for arg in "$@"; do printf " <%s>" "$arg" >> "$COMMAND_LOG"; done\n'
        'mode="$(stat -c %a "$PGPASSFILE" 2>/dev/null || stat -f %Lp "$PGPASSFILE")"\n'
        'printf " PGPASSFILE_MODE=%s\\n" "$mode" >> "$COMMAND_LOG"\n'
        '[[ -z "${EXPECTED_PGPASS:-}" ]] || grep -Fq "$EXPECTED_PGPASS" "$PGPASSFILE"\n'
        '[[ "${ASSERT_PGPASSWORD_UNSET:-}" != 1 || -z "${PGPASSWORD+x}" ]]\n'
        'if [[ -n "${DUMP_ENTERED_FILE:-}" ]]; then touch "$DUMP_ENTERED_FILE"; while [[ ! -f "$DUMP_RELEASE_FILE" ]]; do sleep 0.01; done; fi\n'
        '[[ "${FAIL_PG_DUMP:-}" != 1 ]] || exit 42\n'
        "printf '%s\\n' 'CREATE TABLE backup_probe(id integer);'\n",
    )
    _executable(
        bin_dir / "psql",
        'printf "psql %s\\n" "$*" >> "$COMMAND_LOG"\n'
        '[[ "${ASSERT_PGPASSWORD_UNSET:-}" != 1 || -z "${PGPASSWORD+x}" ]]\n'
        'if [[ "$*" == *"--command="* ]]; then [[ "${FAIL_SCHEMA_PROBE:-}" != 1 ]] && printf "true\\n" || printf "false\\n"; else cat >/dev/null; fi\n'
        '[[ "${FAIL_PSQL:-}" != 1 ]] || exit 43\n',
    )
    _executable(
        bin_dir / "createdb",
        'printf "createdb %s\\n" "$*" >> "$COMMAND_LOG"\n[[ "${FAIL_CREATEDB:-}" != 1 ]] || exit 44\n',
    )
    _executable(bin_dir / "dropdb", 'printf "dropdb %s\\n" "$*" >> "$COMMAND_LOG"\n')
    return bin_dir, log


def _backup_env(tmp_path: Path, bin_dir: Path, log: Path) -> dict[str, str]:
    media = tmp_path / "media"
    media.mkdir()
    (media / "asset.txt").write_text("media payload", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "DATABASE_URL": "postgresql+psycopg2://backup_user:p%40ss%3Aword@db.example:5433/studio",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "MEDIA_DIR": str(media),
        "BACKUP_STAMP": "20260712_010203",
        "EXPECTED_PGPASS": "p@ss\\:word",
    }


def _restore_env(bin_dir: Path, log: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "TARGET_DATABASE_URL": "postgresql://restore_user@localhost:5434/postgres",
        "PGPASSWORD": "r@store",
        "RESTORE_VERIFY_NON_PRODUCTION": "1",
        "ASSERT_PGPASSWORD_UNSET": "1",
        **extra,
    }


def test_backup_publishes_verified_private_bundle_without_password_in_argv(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _backup_env(tmp_path, bin_dir, log)

    result = _run_script(BACKUP, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    out_dir = tmp_path / "backups"
    bundle = out_dir / "ai_studio_20260712_010203"
    assert bundle.is_dir()
    assert (out_dir.stat().st_mode & 0o777) == 0o700
    assert (bundle / "database.sql.gz").is_file()
    assert (bundle / "media.tar.gz").is_file()
    assert (bundle / "SHA256SUMS").is_file()
    assert "maintenance window" in (bundle / "BACKUP_INFO.txt").read_text(encoding="utf-8")
    command_log = log.read_text(encoding="utf-8")
    assert "p%40ss" not in command_log
    assert "p@ss:word" not in command_log
    assert "PGPASSFILE_MODE=600" in command_log
    assert "--host=db.example" in command_log
    assert "--port=5433" in command_log
    assert "--username=backup_user" in command_log
    assert "--dbname=studio" in command_log
    assert not list(out_dir.glob(".*.tmp.*"))
    assert "not a cryptographic signature" in (bundle / "BACKUP_INFO.txt").read_text(encoding="utf-8")


def test_backup_accepts_passwordless_url_with_pgpassword(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _backup_env(tmp_path, bin_dir, log)
    env["DATABASE_URL"] = "postgresql://backup_user@db.example:5433/studio"
    env["PGPASSWORD"] = "env-secret"
    env["EXPECTED_PGPASS"] = "env-secret"
    env["ASSERT_PGPASSWORD_UNSET"] = "1"

    result = _run_script(BACKUP, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "env-secret" not in log.read_text(encoding="utf-8")


def test_backup_rejects_disagreeing_password_sources_and_decoded_newline(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _backup_env(tmp_path, bin_dir, log)
    env["PGPASSWORD"] = "different"
    assert _run_script(BACKUP, env=env, capture_output=True).returncode != 0
    env["BACKUP_STAMP"] = "20260712_010204"
    env["PGPASSWORD"] = ""
    env["DATABASE_URL"] = "postgresql://backup_user:bad%0Asecret@db.example/studio"
    assert _run_script(BACKUP, env=env, capture_output=True).returncode != 0
    assert not log.exists()


def test_backup_failure_leaves_no_final_or_temporary_bundle(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _backup_env(tmp_path, bin_dir, log)
    env["FAIL_PG_DUMP"] = "1"

    result = _run_script(BACKUP, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    out_dir = tmp_path / "backups"
    assert not (out_dir / "ai_studio_20260712_010203").exists()
    assert not list(out_dir.glob(".*.tmp.*"))


def test_verify_restore_rejects_checksum_corruption_before_database_commands(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    db = bundle / "database.sql.gz"
    subprocess.run(["gzip", "-c"], input=b"SELECT 1;\n", stdout=db.open("wb"), check=True)
    media = bundle / "media.tar.gz"
    subprocess.run(["tar", "-czf", media, "--files-from", "/dev/null"], check=True)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    media_digest = hashlib.sha256(media.read_bytes()).hexdigest()
    (bundle / "SHA256SUMS").write_text(
        f"{digest}  database.sql.gz\n{media_digest}  media.tar.gz\n", encoding="ascii"
    )
    db.write_bytes(db.read_bytes() + b"tampered")
    env = _restore_env(bin_dir, log)

    result = _run_script(
        VERIFY,
        "--bundle",
        bundle,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "checksum" in (result.stdout + result.stderr).lower()
    assert not log.exists()


def test_verify_restore_rejects_production_like_target(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _restore_env(bin_dir, log, TARGET_DATABASE_URL="postgresql://admin@prod.example/postgres")

    result = _run_script(
        VERIFY,
        "--bundle",
        tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "non-production" in (result.stdout + result.stderr).lower()
    assert not log.exists()


def test_verify_restore_uses_sanitized_argv_and_drops_temporary_target(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    backup_env = _backup_env(tmp_path, bin_dir, log)
    assert _run_script(BACKUP, env=backup_env, capture_output=True).returncode == 0
    log.unlink()
    bundle = tmp_path / "backups" / "ai_studio_20260712_010203"
    env = _restore_env(bin_dir, log)

    result = _run_script(
        VERIFY,
        "--bundle",
        bundle,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    command_log = log.read_text(encoding="utf-8")
    assert "r%40store" not in command_log
    assert "r@store" not in command_log
    assert "createdb" in command_log
    assert "psql" in command_log
    assert "version_num" in command_log
    assert "information_schema.columns" in command_log
    assert command_log.count("dropdb") == 1
    created_name = next(line.split()[-1] for line in command_log.splitlines() if line.startswith("createdb"))
    assert created_name.startswith("ai_studio_restore_")
    drop_command = next(line for line in command_log.splitlines() if line.startswith("dropdb"))
    assert created_name in drop_command
    assert "--maintenance-db=postgres" in drop_command


def test_verify_restore_cleans_up_target_when_import_fails(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    backup_env = _backup_env(tmp_path, bin_dir, log)
    assert _run_script(BACKUP, env=backup_env, capture_output=True).returncode == 0
    log.unlink()
    bundle = tmp_path / "backups" / "ai_studio_20260712_010203"
    env = _restore_env(bin_dir, log, FAIL_PSQL="1")

    result = _run_script(
        VERIFY,
        "--bundle",
        bundle,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    command_log = log.read_text(encoding="utf-8")
    assert "createdb" in command_log
    assert command_log.count("dropdb") == 1


def test_verify_restore_createdb_failure_never_drops_an_existing_target(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    backup_env = _backup_env(tmp_path, bin_dir, log)
    assert _run_script(BACKUP, env=backup_env, capture_output=True).returncode == 0
    log.unlink()
    bundle = tmp_path / "backups" / "ai_studio_20260712_010203"
    env = _restore_env(bin_dir, log, FAIL_CREATEDB="1")
    result = _run_script(VERIFY, "--bundle", bundle, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    command_log = log.read_text(encoding="utf-8")
    assert "createdb" in command_log
    assert "dropdb" not in command_log


def test_verify_restore_schema_probe_failure_cleans_up_created_database(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    backup_env = _backup_env(tmp_path, bin_dir, log)
    assert _run_script(BACKUP, env=backup_env, capture_output=True).returncode == 0
    log.unlink()
    bundle = tmp_path / "backups" / "ai_studio_20260712_010203"
    env = _restore_env(bin_dir, log, FAIL_SCHEMA_PROBE="1")

    result = _run_script(VERIFY, "--bundle", bundle, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "schema probe" in (result.stdout + result.stderr).lower()
    assert log.read_text(encoding="utf-8").count("dropdb") == 1


def test_same_stamp_concurrent_backup_cannot_publish_into_existing_bundle(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    env = _backup_env(tmp_path, bin_dir, log)
    entered, release = tmp_path / "entered", tmp_path / "release"
    env.update(DUMP_ENTERED_FILE=str(entered), DUMP_RELEASE_FILE=str(release))
    first = _popen_script(BACKUP, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists()

    second = _run_script(BACKUP, env=env, text=True, capture_output=True)
    release.touch()
    _, first_stderr = first.communicate(timeout=5)

    assert first.returncode == 0, first_stderr.decode()
    assert second.returncode != 0
    assert "owns stamp" in second.stderr
    assert len(list((tmp_path / "backups").glob("ai_studio_*"))) == 1


def test_verify_restore_rejects_link_member_even_with_matching_checksum(tmp_path):
    bin_dir, log = _fake_tools(tmp_path)
    backup_env = _backup_env(tmp_path, bin_dir, log)
    assert _run_script(BACKUP, env=backup_env, capture_output=True).returncode == 0
    log.unlink()
    bundle = tmp_path / "backups" / "ai_studio_20260712_010203"
    media = bundle / "media.tar.gz"
    with tarfile.open(media, "w:gz") as archive:
        member = tarfile.TarInfo("unsafe-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        archive.addfile(member)
    database = bundle / "database.sql.gz"
    (bundle / "SHA256SUMS").write_text(
        f"{hashlib.sha256(database.read_bytes()).hexdigest()}  database.sql.gz\n"
        f"{hashlib.sha256(media.read_bytes()).hexdigest()}  media.tar.gz\n",
        encoding="ascii",
    )

    result = _run_script(
        VERIFY,
        "--bundle",
        bundle,
        env=_restore_env(bin_dir, log),
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "unsafe media archive member" in (result.stdout + result.stderr)
    assert not log.exists()
