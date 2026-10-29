"""Exercises pg_dump and local retention against the test database. The B2 upload itself isn't
tested here; it needs real credentials and gets checked against a real bucket."""
import datetime
import os
import subprocess
import time
import pytest
import db
from jobs import backup


@pytest.fixture(autouse=True)
def no_b2(monkeypatch):
    for key in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET_NAME"):
        monkeypatch.delenv(key, raising=False)


async def seed(pool):
    await db.log_message(
        pool, message_id=1, guild_id=2, channel_id=3, author_id=4,
        content="backed up message", attachments=[], created_at=datetime.datetime.now(datetime.UTC),
    )
    await db.save_snapshot(pool, 2, {"guild": {"name": "snapshot in dump"}})


def test_client_version_is_new_enough(dsn):
    client, server = backup.check_client_version(dsn)
    assert client >= server


async def test_dump_contains_schema_and_data(pool, dsn, tmp_path):
    await seed(pool)
    dump = backup.create_local_backup(dsn, tmp_path)
    assert dump.name.startswith("pausebot-") and dump.suffix == ".dump"
    # Custom format starts with this magic
    assert dump.read_bytes()[:5] == b"PGDMP"

    listing = subprocess.run(["pg_restore", "--list", str(dump)], capture_output=True, text=True, check=True).stdout
    for table in ("message_log", "message_edits", "server_snapshots"):
        assert f"TABLE public {table}" in listing
        assert f"TABLE DATA public {table}" in listing

    sql = subprocess.run(["pg_restore", "--file=-", str(dump)], capture_output=True, text=True, check=True).stdout
    assert "backed up message" in sql
    assert "snapshot in dump" in sql


async def test_run_backup_job_without_b2(pool, dsn, tmp_path):
    await seed(pool)
    assert backup.run_backup_job(dsn, tmp_path) == {"local_success": True, "b2_success": False}
    assert len(list(tmp_path.glob("pausebot-*.dump"))) == 1


def test_run_backup_job_reports_dump_failure(tmp_path):
    bad_dsn = "postgresql://postgres:postgres@localhost:5433/does_not_exist_test"
    assert backup.run_backup_job(bad_dsn, tmp_path) == {"local_success": False, "b2_success": False}


def test_cleanup_removes_only_old_dumps(tmp_path):
    old = tmp_path / "pausebot-2026-01-01_030000.dump"
    recent = tmp_path / "pausebot-2026-10-28_030000.dump"
    unrelated = tmp_path / "something-else.dump"
    for f in (old, recent, unrelated):
        f.write_bytes(b"x")
    eight_days_ago = time.time() - 8 * 24 * 60 * 60
    for f in (old, unrelated):
        os.utime(f, (eight_days_ago, eight_days_ago))

    backup.cleanup_local_backups(tmp_path)
    assert not old.exists()
    assert recent.exists() and unrelated.exists()


def test_backup_dir_uses_railway_volume(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    assert backup.local_backup_dir() == tmp_path / "backups"


def test_upload_skipped_when_b2_not_configured(tmp_path):
    assert backup.upload_to_b2(tmp_path / "pausebot-x.dump") is False


async def test_dump_restores_into_empty_database(pool, dsn, tmp_path):
    # The same pg_restore command the README gives for restoring a backup
    await seed(pool)
    dump = backup.create_local_backup(dsn, tmp_path)
    restore_db = "pausebot_restore_test"
    await pool.execute(f"DROP DATABASE IF EXISTS {restore_db}")
    await pool.execute(f"CREATE DATABASE {restore_db}")
    restore_dsn = dsn.rsplit("/", 1)[0] + f"/{restore_db}"
    try:
        subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", restore_dsn, str(dump)],
            capture_output=True, text=True, check=True,
        )
        restored = await db.create_pool(restore_dsn)
        try:
            assert (await db.get_message(restored, 1))["content"] == "backed up message"
            assert (await db.latest_snapshot(restored, 2))["data"] == {"guild": {"name": "snapshot in dump"}}
        finally:
            await restored.close()
    finally:
        await pool.execute(f"DROP DATABASE IF EXISTS {restore_db}")


# ── Credentials stay out of command lines and logs ────────────────────────────

def test_libpq_args_moves_password_to_env():
    dsn, env = backup.libpq_args("postgresql://user:p%40ss@db.example:5432/app?sslmode=require")
    assert dsn == "postgresql://user@db.example:5432/app?sslmode=require"
    assert env["PGPASSWORD"] == "p@ss"
    assert backup.libpq_args("postgresql://[::1]:5433/app")[0] == "postgresql://[::1]:5433/app"
    assert backup.libpq_args("postgresql://u:pw@[::1]:5433/app")[0] == "postgresql://u@[::1]:5433/app"


def test_failed_backup_does_not_log_password(tmp_path, capfd):
    bad_dsn = "postgresql://postgres:sup3rsecret@localhost:5433/does_not_exist_test"
    assert backup.run_backup_job(bad_dsn, tmp_path)["local_success"] is False
    out, err = capfd.readouterr()
    assert "does_not_exist_test" in out + err
    assert "sup3rsecret" not in out + err


# ── B2 retention (B2 calls stubbed; the real API is checked against a bucket) ─

@pytest.fixture
def fake_b2(monkeypatch):
    for key in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET_NAME"):
        monkeypatch.setenv(key, "x")
    calls = {"prefixes": [], "deleted": []}
    old = (time.time() - 31 * 24 * 60 * 60) * 1000
    monkeypatch.setattr(backup, "b2_authorize", lambda: {})
    monkeypatch.setattr(backup, "b2_get_bucket_id", lambda auth, name: "bucket")

    def list_files(auth, bucket_id, prefix):
        calls["prefixes"].append(prefix)
        return [
            {"fileId": "1", "fileName": "backups/pausebot-old.dump", "uploadTimestamp": old},
            {"fileId": "2", "fileName": "backups/pausebot-new.dump", "uploadTimestamp": time.time() * 1000},
        ]

    monkeypatch.setattr(backup, "b2_list_files", list_files)
    monkeypatch.setattr(backup, "b2_delete_file", lambda auth, file_id, name: calls["deleted"].append(name))
    return calls


def test_b2_cleanup_only_touches_pausebot_dumps(fake_b2):
    backup.cleanup_b2_backups()
    assert fake_b2["prefixes"] == ["backups/pausebot-"]
    assert fake_b2["deleted"] == ["backups/pausebot-old.dump"]


async def test_b2_cleanup_skipped_when_upload_fails(pool, dsn, tmp_path, fake_b2, monkeypatch):
    monkeypatch.setattr(backup, "upload_to_b2", lambda path: False)
    assert backup.run_backup_job(dsn, tmp_path) == {"local_success": True, "b2_success": False}
    assert fake_b2["deleted"] == [] and fake_b2["prefixes"] == []


async def test_b2_cleanup_runs_after_successful_upload(pool, dsn, tmp_path, fake_b2, monkeypatch):
    monkeypatch.setattr(backup, "upload_to_b2", lambda path: True)
    assert backup.run_backup_job(dsn, tmp_path) == {"local_success": True, "b2_success": True}
    assert fake_b2["deleted"] == ["backups/pausebot-old.dump"]
