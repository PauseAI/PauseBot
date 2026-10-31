"""Bucket config, upload and retention, against a local moto S3 server. Each real provider still
needs one live run once its keys exist."""
import datetime
import os
import threading
import urllib.request
import pytest
from botocore.exceptions import ClientError
from moto.server import ThreadedMotoServer
from jobs import backup

NOW = datetime.datetime.now(datetime.UTC)
LATER = NOW + datetime.timedelta(days=31)  # Everything uploaded now counts as expired by then
R2_ACCOUNT = "0123456789abcdef0123456789abcdef"


# ── Config ────────────────────────────────────────────────────────────────────

def provider_env(name, location, bucket="bucket"):
    location_var = backup.PROVIDERS[name].location_var
    p = f"BACKUP_{name.upper()}_"
    return {f"{p}{location_var}": location, f"{p}BUCKET": bucket, f"{p}KEY_ID": "id", f"{p}SECRET": "secret"}


def test_no_targets_configured():
    assert backup.load_targets({}) == ([], [])


def test_each_provider_builds_its_endpoint():
    env = {
        **provider_env("b2", "eu-central-003", "b2-bucket"),
        **provider_env("r2", R2_ACCOUNT, "r2-bucket"),
        **provider_env("s3", "eu-west-2", "s3-bucket"),
    }
    targets, problems = backup.load_targets(env)
    assert problems == []
    targets = {t.name: t for t in targets}
    assert (targets["b2"].endpoint, targets["b2"].region, targets["b2"].bucket) == (
        "https://s3.eu-central-003.backblazeb2.com", "eu-central-003", "b2-bucket")
    assert (targets["r2"].endpoint, targets["r2"].region) == (f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com", "auto")
    # Explicit, so an AWS_ENDPOINT_URL set for another tool can't redirect it
    assert (targets["s3"].endpoint, targets["s3"].region) == ("https://s3.eu-west-2.amazonaws.com", "eu-west-2")
    assert [targets[n].versioned for n in ("b2", "r2", "s3")] == [True, False, True]
    assert (targets["s3"].key_id, targets["s3"].secret) == ("id", "secret")


@pytest.mark.parametrize("jurisdiction", ["eu", "fedramp"])
def test_r2_jurisdiction_endpoint(jurisdiction):
    targets, problems = backup.load_targets(provider_env("r2", f"{R2_ACCOUNT}.{jurisdiction}"))
    assert problems == []
    assert targets[0].endpoint == f"https://{R2_ACCOUNT}.{jurisdiction}.r2.cloudflarestorage.com"


def test_only_configured_providers_are_targets():
    targets, _ = backup.load_targets(provider_env("r2", R2_ACCOUNT))
    assert [t.name for t in targets] == ["r2"]


def test_partly_configured_provider_is_a_problem_but_others_still_load():
    env = {**provider_env("b2", "eu-central-003"), **provider_env("r2", R2_ACCOUNT), **provider_env("s3", "eu-west-2")}
    del env["BACKUP_R2_SECRET"]
    env["BACKUP_B2_BUCKET"] = "  "
    targets, problems = backup.load_targets(env)
    assert [t.name for t in targets] == ["s3"]
    assert problems == ["Missing BACKUP_B2_BUCKET", "Missing BACKUP_R2_SECRET"]


@pytest.mark.parametrize("name, location", [
    ("b2", "s3.eu-central-003.backblazeb2.com"),
    ("b2", "https://s3.eu-central-003.backblazeb2.com"),
    ("r2", f"https://{R2_ACCOUNT}.r2.cloudflarestorage.com"),
    ("r2", "abc123"),
    ("r2", f"{R2_ACCOUNT}.us"),
    ("s3", "s3.eu-west-2.amazonaws.com"),
    ("s3", "EU-WEST-2"),
])
def test_location_in_wrong_form_is_a_problem(name, location):
    targets, problems = backup.load_targets(provider_env(name, location))
    assert targets == []
    assert len(problems) == 1
    assert f"BACKUP_{name.upper()}_" in problems[0] and "should look like" in problems[0]


@pytest.mark.parametrize("location", ["us-east-1", "ap-southeast-2", "us-gov-west-1", "eu-central-2"])
def test_real_aws_regions_accepted(location):
    targets, problems = backup.load_targets(provider_env("s3", location))
    assert problems == [] and targets[0].region == location


def test_old_b2_variables_are_a_problem():
    targets, problems = backup.load_targets({"B2_KEY_ID": "k", "B2_APP_KEY": "a", **provider_env("s3", "eu-west-2")})
    assert [t.name for t in targets] == ["s3"]
    assert problems == ["B2_KEY_ID, B2_APP_KEY are no longer used; set BACKUP_B2_REGION, "
                        "BACKUP_B2_BUCKET, BACKUP_B2_KEY_ID and BACKUP_B2_SECRET instead"]


def test_target_repr_hides_secret():
    env = {**provider_env("s3", "eu-west-2"), "BACKUP_S3_SECRET": "sup3rsecret"}
    assert "sup3rsecret" not in repr(backup.load_targets(env)[0][0])


def test_expired_only_old_pausebot_dumps():
    old = NOW - datetime.timedelta(days=31)
    dumps = [
        {"Key": "backups/pausebot-old.dump", "VersionId": "v1", "LastModified": old},
        {"Key": "backups/pausebot-new.dump", "VersionId": "v2", "LastModified": NOW - datetime.timedelta(days=29)},
        {"Key": "backups/catalyse-old.dump", "VersionId": "v3", "LastModified": old},
    ]
    assert [d["Key"] for d in backup.expired(dumps, NOW)] == ["backups/pausebot-old.dump"]


def test_succeeded_needs_upload_and_working_cleanup():
    assert backup.succeeded({"uploaded": True, "cleaned": True})
    assert not backup.succeeded({"uploaded": True, "cleaned": False})
    assert not backup.succeeded({"uploaded": False, "cleaned": None})


# ── Against a local S3 server ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def s3_endpoint():
    # Port 0 lets the OS pick a free port as the server binds, so nothing can grab it first
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def make_target(s3_endpoint):
    # Fresh, empty server state for each test
    urllib.request.urlopen(urllib.request.Request(f"{s3_endpoint}/moto-api/reset", method="POST"))

    def make(name, bucket, create=True, versioned=False):
        # The endpoint has no {} for the location, so every target points at the local server
        provider = backup.Provider(location_var="LOCATION", location_format=".*", location_example="",
                                   endpoint=s3_endpoint, region="us-east-1", versioned=versioned)
        target = backup.Target(name=name, provider=provider, location="", bucket=bucket,
                               key_id="test", secret="test")
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


def all_versions(target):
    response = backup.make_client(target).list_object_versions(Bucket=target.bucket)
    return sorted(v["Key"] for v in response.get("Versions", []) + response.get("DeleteMarkers", []))


def dump_file(tmp_path, size=10):
    path = tmp_path / "pausebot-2026-11-01_030000.dump"
    path.write_bytes(os.urandom(size))
    return path


def client_error(code, operation="DeleteObjects"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def test_upload_lands_under_backups(make_target, tmp_path, capsys):
    target = make_target("b2", "pausebot")
    path = dump_file(tmp_path, size=4096)
    assert backup.backup_to_target(target, path) == {"uploaded": True, "cleaned": True}
    # Size in the log line makes an empty or truncated dump easy to spot
    assert "b2: uploaded backups/pausebot-2026-11-01_030000.dump to pausebot (4 KB)" in capsys.readouterr().out
    assert keys(target) == ["backups/pausebot-2026-11-01_030000.dump"]
    body = backup.make_client(target).get_object(Bucket="pausebot", Key=keys(target)[0])["Body"].read()
    assert body == path.read_bytes()


def test_large_dump_uploads_in_parts(make_target, tmp_path):
    # Over boto3's 8 MB multipart threshold, so this goes up as a multipart upload
    target = make_target("r2", "pausebot")
    path = dump_file(tmp_path, size=9 * 1024 * 1024)
    assert backup.backup_to_target(target, path)["uploaded"] is True
    head = backup.make_client(target).head_object(Bucket="pausebot", Key=keys(target)[0])
    assert head["ContentLength"] == path.stat().st_size
    assert "-" in head["ETag"]  # Multipart ETags end in -<part count>


def test_cleanup_deletes_only_expired_pausebot_dumps(make_target):
    target = make_target("s3", "shared")
    client = backup.make_client(target)
    for key in ("backups/pausebot-old.dump", "backups/catalyse-old.dump", "other/pausebot-old.dump"):
        client.put_object(Bucket="shared", Key=key, Body=b"x")
    backup.cleanup_target(client, target, now=LATER)
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

    backup.cleanup_target(client, target, now=LATER)
    assert all_versions(target) == []


def test_unversioned_listing_for_r2(make_target):
    # R2 has no versioning; its target uses the plain listing and deletes by key
    target = make_target("r2", "plain")
    client = backup.make_client(target)
    client.put_object(Bucket="plain", Key="backups/pausebot-old.dump", Body=b"x")
    assert [d["VersionId"] for d in backup.list_dumps(client, target)] == [None]
    backup.cleanup_target(client, target, now=LATER)
    assert keys(target) == []


def test_cleanup_falls_back_when_batch_delete_rejected(make_target, capsys):
    target = make_target("b2", "fallback", versioned=True)
    client = backup.make_client(target)
    client.put_object(Bucket="fallback", Key="backups/pausebot-old.dump", Body=b"x")

    def rejected(**kwargs):
        raise client_error("InvalidRequest")

    client.delete_objects = rejected
    backup.cleanup_target(client, target, now=LATER)
    assert all_versions(target) == []
    assert "batch delete rejected (InvalidRequest), deleting one at a time" in capsys.readouterr().out


@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "NoSuchBucket"])
def test_no_fallback_for_errors_a_retry_cant_fix(make_target, code):
    target = make_target("b2", "nofallback", versioned=True)
    client = backup.make_client(target)
    client.put_object(Bucket="nofallback", Key="backups/pausebot-old.dump", Body=b"x")
    single_deletes = []

    def rejected(**kwargs):
        raise client_error(code)

    client.delete_objects = rejected
    client.delete_object = lambda **kwargs: single_deletes.append(kwargs)
    with pytest.raises(ClientError, match=code):
        backup.cleanup_target(client, target, now=LATER)
    assert single_deletes == []


def test_fallback_tries_every_object_and_reports_failures(make_target, capsys):
    target = make_target("r2", "partial")
    client = backup.make_client(target)
    for n in range(4):
        client.put_object(Bucket="partial", Key=f"backups/pausebot-{n}.dump", Body=b"x")
    real_delete = client.delete_object

    def delete_except_one(**kwargs):
        if kwargs["Key"] == "backups/pausebot-1.dump":
            raise client_error("AccessDenied", "DeleteObject")
        return real_delete(**kwargs)

    def rejected(**kwargs):
        raise client_error("NotImplemented")

    client.delete_objects = rejected
    client.delete_object = delete_except_one
    with pytest.raises(RuntimeError, match=r"1 delete\(s\) failed, first: AccessDenied on backups/pausebot-1\.dump"):
        backup.cleanup_target(client, target, now=LATER)
    # The objects after the failing one were still deleted, and the count says so
    assert keys(target) == ["backups/pausebot-1.dump"]
    assert "r2: removed 3 backup(s)" in capsys.readouterr().out


def test_cleanup_reports_per_object_delete_errors(make_target):
    target = make_target("b2", "errors", versioned=True)
    client = backup.make_client(target)
    client.put_object(Bucket="errors", Key="backups/pausebot-old.dump", Body=b"x")
    client.delete_objects = lambda **kwargs: {"Errors": [{"Key": "backups/pausebot-old.dump", "Code": "AccessDenied"}]}
    with pytest.raises(RuntimeError, match=r"1 delete\(s\) failed, first: AccessDenied on backups/pausebot-old\.dump"):
        backup.cleanup_target(client, target, now=LATER)


def test_failed_target_does_not_stop_others(make_target, pool, dsn, tmp_path, capsys):
    good = make_target("b2", "good")
    bad = make_target("r2", "missing", create=False)
    also_good = make_target("s3", "also-good")
    result = backup.run_backup_job(dsn, [good, bad, also_good], tmp_path)
    assert result == {"local_success": True, "targets": {
        "b2": {"uploaded": True, "cleaned": True},
        "r2": {"uploaded": False, "cleaned": None},
        "s3": {"uploaded": True, "cleaned": True},
    }}
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

    assert backup.backup_to_target(target, dump_file(tmp_path), client=FailingClient()) == {
        "uploaded": False, "cleaned": None}
    assert cleaned == []
    assert backup.backup_to_target(target, dump_file(tmp_path))["uploaded"] is True
    assert cleaned == [target]


def test_cleanup_failure_fails_the_target_but_upload_counts(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "pausebot")

    def broken_cleanup(client, t):
        raise RuntimeError("list denied")

    monkeypatch.setattr(backup, "cleanup_target", broken_cleanup)
    result = backup.backup_to_target(target, dump_file(tmp_path))
    assert result == {"uploaded": True, "cleaned": False}
    assert not backup.succeeded(result)
    assert keys(target) == ["backups/pausebot-2026-11-01_030000.dump"]
    assert "b2: cleanup failed: list denied" in capsys.readouterr().err


def test_targets_upload_at_the_same_time(monkeypatch, tmp_path):
    # Each upload waits until all three are running at once; run one after another, the
    # barrier times out and the test fails
    barrier = threading.Barrier(3, timeout=5)

    def upload(target, path):
        barrier.wait()
        return {"uploaded": target.name != "r2", "cleaned": None if target.name == "r2" else True}

    monkeypatch.setattr(backup, "backup_to_target", upload)
    provider = backup.PROVIDERS["r2"]
    targets = [backup.Target(name=n, provider=provider, location=R2_ACCOUNT, bucket="b", key_id="k", secret="s")
               for n in ("b2", "r2", "s3")]
    results = backup.backup_to_all(targets, tmp_path / "d.dump")
    assert {n: backup.succeeded(r) for n, r in results.items()} == {"b2": True, "r2": False, "s3": True}
