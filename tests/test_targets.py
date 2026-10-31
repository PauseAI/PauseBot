"""Bucket config, upload and retention, against a local moto S3 server. Each real provider still
needs one live run once its keys exist."""
import datetime
import os
import socket
import time
import urllib.request
import pytest
from botocore.exceptions import ClientError
from moto.server import ThreadedMotoServer
from jobs import backup

NOW = datetime.datetime.now(datetime.UTC)
R2_ACCOUNT = "0123456789abcdef0123456789abcdef"


# ── Config ────────────────────────────────────────────────────────────────────

def provider_env(name, location, bucket="bucket"):
    location_var = backup.PROVIDERS[name].location_var
    p = f"BACKUP_{name.upper()}_"
    return {f"{p}{location_var}": location, f"{p}BUCKET": bucket, f"{p}KEY_ID": "id", f"{p}SECRET": "secret"}


def test_no_targets_configured():
    assert backup.load_targets({}) == []


def test_each_provider_builds_its_endpoint():
    env = {
        **provider_env("b2", "eu-central-003", "b2-bucket"),
        **provider_env("r2", R2_ACCOUNT, "r2-bucket"),
        **provider_env("s3", "eu-west-2", "s3-bucket"),
    }
    targets = {t.name: t for t in backup.load_targets(env)}
    assert (targets["b2"].endpoint, targets["b2"].region, targets["b2"].bucket) == (
        "https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "b2-bucket")
    assert (targets["r2"].endpoint, targets["r2"].region) == (f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com", "auto")
    # Explicit, so an AWS_ENDPOINT_URL set for another tool can't redirect it
    assert (targets["s3"].endpoint, targets["s3"].region) == ("https://s3.eu-west-2.amazonaws.com", "eu-west-2")
    assert [targets[n].versioned for n in ("b2", "r2", "s3")] == [True, False, True]
    assert (targets["s3"].key_id, targets["s3"].secret) == ("id", "secret")


def test_only_configured_providers_are_targets():
    assert [t.name for t in backup.load_targets(provider_env("r2", R2_ACCOUNT))] == ["r2"]


def test_partly_configured_provider_names_missing_variables():
    env = {**provider_env("b2", "eu-central-003"), **provider_env("r2", R2_ACCOUNT)}
    del env["BACKUP_R2_SECRET"]
    env["BACKUP_B2_BUCKET"] = "  "
    with pytest.raises(backup.ConfigError) as e:
        backup.load_targets(env)
    assert str(e.value) == "Missing BACKUP_B2_BUCKET; Missing BACKUP_R2_SECRET"


@pytest.mark.parametrize("name, location", [
    ("b2", "s3.eu-central-003.backblazeb2.com"),
    ("b2", "https://s3.eu-central-003.backblazeb2.com"),
    ("r2", f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com"),
    ("r2", "abc123"),
    ("s3", "s3.eu-west-2.amazonaws.com"),
    ("s3", "EU-WEST-2"),
])
def test_location_in_wrong_form_fails_before_dump(name, location):
    with pytest.raises(backup.ConfigError) as e:
        backup.load_targets(provider_env(name, location))
    assert f"BACKUP_{name.upper()}_" in str(e.value) and "should look like" in str(e.value)


@pytest.mark.parametrize("location", ["us-east-1", "ap-southeast-2", "us-gov-west-1", "eu-central-2"])
def test_real_aws_regions_accepted(location):
    assert backup.load_targets(provider_env("s3", location))[0].region == location


def test_target_repr_hides_secret():
    env = {**provider_env("s3", "eu-west-2"), "BACKUP_S3_SECRET": "sup3rsecret"}
    assert "sup3rsecret" not in repr(backup.load_targets(env)[0])


def test_expired_only_old_pausebot_dumps():
    old = NOW - datetime.timedelta(days=31)
    dumps = [
        {"Key": "backups/pausebot-old.dump", "VersionId": "v1", "LastModified": old},
        {"Key": "backups/pausebot-new.dump", "VersionId": "v2", "LastModified": NOW - datetime.timedelta(days=29)},
        {"Key": "backups/catalyse-old.dump", "VersionId": "v3", "LastModified": old},
    ]
    assert [d["Key"] for d in backup.expired(dumps, NOW)] == ["backups/pausebot-old.dump"]


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

    def make(name, bucket, create=True, versioned=False):
        target = backup.Target(name, bucket, s3_endpoint, "us-east-1", versioned, "test", "test")
        if create:
            client = backup.make_client(target)
            client.create_bucket(Bucket=bucket)
            if versioned:
                # Like a B2 bucket's default "keep all versions"
                client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        return target

    return make


def keys(target):
    response = backup.make_client(target).list_objects_v2(Bucket=target.bucket)
    return sorted(o["Key"] for o in response.get("Contents", []))


def dump_file(tmp_path, size=10):
    path = tmp_path / "pausebot-2026-11-01_030000.dump"
    path.write_bytes(os.urandom(size))
    return path


def all_versions(target):
    response = backup.make_client(target).list_object_versions(Bucket=target.bucket)
    return sorted(v["Key"] for v in response.get("Versions", []) + response.get("DeleteMarkers", []))


def test_upload_lands_under_backups(make_target, tmp_path, capsys):
    target = make_target("b2", "pausebot")
    path = dump_file(tmp_path, size=4096)
    assert backup.backup_to_target(target, path) is True
    # Size in the log line makes an empty or truncated dump easy to spot
    assert "b2: uploaded backups/pausebot-2026-11-01_030000.dump to pausebot (4 KB)" in capsys.readouterr().out
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


def test_cleanup_on_versioned_bucket_removes_every_version(make_target):
    target = make_target("b2", "versioned", versioned=True)
    client = backup.make_client(target)
    for body in (b"v1", b"v2"):
        client.put_object(Bucket="versioned", Key="backups/pausebot-old.dump", Body=body)
    client.put_object(Bucket="versioned", Key="backups/pausebot-hidden.dump", Body=b"x")
    # A plain delete only hides it: still stored and billed, and missing from a plain listing
    client.delete_object(Bucket="versioned", Key="backups/pausebot-hidden.dump")
    assert keys(target) == ["backups/pausebot-old.dump"]
    assert len(all_versions(target)) == 4

    backup.cleanup_target(client, target, now=NOW + datetime.timedelta(days=31))
    assert all_versions(target) == []


def test_unversioned_listing_for_r2(make_target):
    # R2 has no versioning; its target uses the plain listing and deletes by key
    target = make_target("r2", "plain")
    client = backup.make_client(target)
    client.put_object(Bucket="plain", Key="backups/pausebot-old.dump", Body=b"x")
    assert [d["VersionId"] for d in backup.list_dumps(client, target)] == [None]
    backup.cleanup_target(client, target, now=NOW + datetime.timedelta(days=31))
    assert keys(target) == []


def test_cleanup_falls_back_when_batch_delete_rejected(make_target):
    target = make_target("b2", "fallback", versioned=True)
    client = backup.make_client(target)
    client.put_object(Bucket="fallback", Key="backups/pausebot-old.dump", Body=b"x")

    def rejected(**kwargs):
        raise ClientError({"Error": {"Code": "InvalidRequest", "Message": "checksum"}}, "DeleteObjects")

    client.delete_objects = rejected
    backup.cleanup_target(client, target, now=NOW + datetime.timedelta(days=31))
    assert all_versions(target) == []


def test_cleanup_reports_per_object_delete_errors(make_target):
    target = make_target("b2", "errors", versioned=True)
    client = backup.make_client(target)
    client.put_object(Bucket="errors", Key="backups/pausebot-old.dump", Body=b"x")
    client.delete_objects = lambda **kwargs: {"Errors": [{"Key": "backups/pausebot-old.dump", "Code": "AccessDenied"}]}
    with pytest.raises(RuntimeError, match="1 delete\\(s\\) failed, first: AccessDenied"):
        backup.cleanup_target(client, target, now=NOW + datetime.timedelta(days=31))


def test_targets_upload_at_the_same_time(monkeypatch, tmp_path):
    def slow(target, path):
        time.sleep(0.5)
        return target.name != "r2"

    monkeypatch.setattr(backup, "backup_to_target", slow)
    targets = [backup.Target(n, "b", "http://x", "auto", False, "k", "s") for n in ("b2", "r2", "s3")]
    started = time.monotonic()
    assert backup.backup_to_all(targets, tmp_path / "d.dump") == {"b2": True, "r2": False, "s3": True}
    assert time.monotonic() - started < 1.2


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
