"""Bucket config, upload and retention, against a local moto S3 server. Each real provider still
needs one live run once its keys exist."""
import datetime
import os
import socket
import urllib.request
import pytest
from moto.server import ThreadedMotoServer
from jobs import backup

NOW = datetime.datetime.now(datetime.UTC)


# ── Config ────────────────────────────────────────────────────────────────────

def provider_env(name, location, bucket="bucket"):
    location_var = backup.PROVIDERS[name][0]
    p = f"BACKUP_{name.upper()}_"
    return {f"{p}{location_var}": location, f"{p}BUCKET": bucket, f"{p}KEY_ID": "id", f"{p}SECRET": "secret"}


def test_no_targets_configured():
    assert backup.load_targets({}) == []


def test_each_provider_builds_its_endpoint():
    env = {
        **provider_env("b2", "eu-central-003", "b2-bucket"),
        **provider_env("r2", "abc123", "r2-bucket"),
        **provider_env("s3", "eu-west-2", "s3-bucket"),
    }
    targets = {t.name: t for t in backup.load_targets(env)}
    assert (targets["b2"].endpoint, targets["b2"].region, targets["b2"].bucket) == (
        "https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "b2-bucket")
    assert (targets["r2"].endpoint, targets["r2"].region) == ("https://abc123.r2.cloudflarestorage.com", "auto")
    assert (targets["s3"].endpoint, targets["s3"].region) == (None, "eu-west-2")
    assert (targets["s3"].key_id, targets["s3"].secret) == ("id", "secret")


def test_only_configured_providers_are_targets():
    assert [t.name for t in backup.load_targets(provider_env("r2", "abc123"))] == ["r2"]


def test_partly_configured_provider_names_missing_variables():
    env = {**provider_env("b2", "eu-central-003"), **provider_env("r2", "abc123")}
    del env["BACKUP_R2_SECRET"]
    env["BACKUP_B2_BUCKET"] = "  "
    with pytest.raises(backup.ConfigError) as e:
        backup.load_targets(env)
    assert str(e.value) == "Missing BACKUP_B2_BUCKET, BACKUP_R2_SECRET"


def test_target_repr_hides_secret():
    env = {**provider_env("s3", "eu-west-2"), "BACKUP_S3_SECRET": "sup3rsecret"}
    assert "sup3rsecret" not in repr(backup.load_targets(env)[0])


def test_expired_keys_only_old_pausebot_dumps():
    old = NOW - datetime.timedelta(days=31)
    objects = [
        {"Key": "backups/pausebot-old.dump", "LastModified": old},
        {"Key": "backups/pausebot-new.dump", "LastModified": NOW - datetime.timedelta(days=29)},
        {"Key": "backups/catalyse-old.dump", "LastModified": old},
    ]
    assert backup.expired_keys(objects, NOW) == ["backups/pausebot-old.dump"]


# ── Against a local S3 server ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def s3_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def make_target(s3_endpoint):
    # Fresh, empty server state for each test
    urllib.request.urlopen(urllib.request.Request(f"{s3_endpoint}/moto-api/reset", method="POST"))

    def make(name, bucket, create=True):
        target = backup.Target(name, bucket, s3_endpoint, "us-east-1", "test", "test")
        if create:
            backup.make_client(target).create_bucket(Bucket=bucket)
        return target

    return make


def keys(target):
    response = backup.make_client(target).list_objects_v2(Bucket=target.bucket)
    return sorted(o["Key"] for o in response.get("Contents", []))


def dump_file(tmp_path, size=10):
    path = tmp_path / "pausebot-2026-11-01_030000.dump"
    path.write_bytes(os.urandom(size))
    return path


def test_upload_lands_under_backups(make_target, tmp_path):
    target = make_target("b2", "pausebot")
    path = dump_file(tmp_path)
    assert backup.backup_to_target(target, path) is True
    assert keys(target) == ["backups/pausebot-2026-11-01_030000.dump"]
    body = backup.make_client(target).get_object(Bucket="pausebot", Key=keys(target)[0])["Body"].read()
    assert body == path.read_bytes()


def test_large_dump_uploads_in_parts(make_target, tmp_path):
    # Over boto3's 8 MB multipart threshold, so this goes up as a multipart upload
    target = make_target("r2", "pausebot")
    path = dump_file(tmp_path, size=9 * 1024 * 1024)
    assert backup.backup_to_target(target, path) is True
    head = backup.make_client(target).head_object(Bucket="pausebot", Key=keys(target)[0])
    assert head["ContentLength"] == path.stat().st_size
    assert "-" in head["ETag"]  # Multipart ETags end in -<part count>


def test_cleanup_deletes_only_expired_pausebot_dumps(make_target, tmp_path):
    target = make_target("s3", "shared")
    client = backup.make_client(target)
    for key in ("backups/pausebot-old.dump", "backups/catalyse-old.dump", "other/pausebot-old.dump"):
        client.put_object(Bucket="shared", Key=key, Body=b"x")
    # Everything just uploaded counts as expired 31 days from now
    backup.cleanup_target(client, target, now=NOW + datetime.timedelta(days=31))
    assert keys(target) == ["backups/catalyse-old.dump", "other/pausebot-old.dump"]


def test_failed_target_does_not_stop_others(make_target, pool, dsn, tmp_path, capsys):
    good = make_target("b2", "good")
    bad = make_target("r2", "missing", create=False)
    also_good = make_target("s3", "also-good")
    result = backup.run_backup_job(dsn, [good, bad, also_good], tmp_path)
    assert result == {"local_success": True, "targets": {"b2": True, "r2": False, "s3": True}}
    assert len(keys(good)) == 1 and len(keys(also_good)) == 1
    captured = capsys.readouterr()
    assert "r2: upload failed" in captured.err and "NoSuchBucket" in captured.err
    assert "r2: skipping cleanup because the upload failed" in captured.err
    assert "Done: 2 of 3 targets succeeded" in captured.out


def test_cleanup_skipped_when_upload_fails(make_target, tmp_path, monkeypatch):
    target = make_target("b2", "pausebot")
    cleaned = []
    monkeypatch.setattr(backup, "cleanup_target", lambda client, t: cleaned.append(t))

    class FailingClient:
        def upload_file(self, *args):
            raise RuntimeError("boom")

    assert backup.backup_to_target(target, dump_file(tmp_path), client=FailingClient()) is False
    assert cleaned == []
    assert backup.backup_to_target(target, dump_file(tmp_path)) is True
    assert cleaned == [target]


def test_cleanup_failure_still_counts_upload(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "pausebot")

    def broken_cleanup(client, t):
        raise RuntimeError("list denied")

    monkeypatch.setattr(backup, "cleanup_target", broken_cleanup)
    assert backup.backup_to_target(target, dump_file(tmp_path)) is True
    assert "b2: cleanup failed: list denied" in capsys.readouterr().err
