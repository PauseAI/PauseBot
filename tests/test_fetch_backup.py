"""Finding and downloading the latest dump, against a local moto S3 server."""
import datetime
import os
import stat
import subprocess
import sys
from pathlib import Path
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from jobs import backup, fetch_backup

NOW = datetime.datetime.now(datetime.UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent


def dump(key, age_days=0, current=True, size=1):
    return {"Key": key, "VersionId": None, "LastModified": NOW - datetime.timedelta(days=age_days),
            "Size": size, "Current": current}


def put(target, *keys, body=b"dump"):
    client = backup.make_client(target)
    for key in keys:
        client.put_object(Bucket=target.bucket, Key=key, Body=body)
    return client


def use_targets(monkeypatch, *targets):
    monkeypatch.setattr(backup, "load_targets", lambda env=None: (list(targets), []))


def rejecting_client(code, message="rejected"):
    class Client:
        def get_paginator(self, name):
            raise ClientError({"Error": {"Code": code, "Message": message}}, "ListObjectVersions")
    return Client()


# ── Choosing the dump ─────────────────────────────────────────────────────────

def test_newest_dump_goes_by_last_modified():
    dumps = [dump("backups/pausebot-b.dump", 2), dump("backups/pausebot-c.dump", 1), dump("backups/pausebot-a.dump", 3)]
    assert fetch_backup.newest_dump(dumps)["Key"] == "backups/pausebot-c.dump"


def test_newest_dump_breaks_ties_by_name():
    dumps = [dump("backups/pausebot-2026-11-02_030000.dump"), dump("backups/pausebot-2026-11-01_030000.dump")]
    dumps[1]["LastModified"] = dumps[0]["LastModified"]
    assert fetch_backup.newest_dump(dumps)["Key"] == "backups/pausebot-2026-11-02_030000.dump"


def test_newest_dump_ignores_old_versions_and_other_files():
    dumps = [
        dump("backups/pausebot-wanted.dump", 5),
        dump("backups/pausebot-hidden.dump", 1, current=False),
        dump("backups/pausebot-notes.txt", 1),
        dump("backups/pausebot-sub/dir.dump", 1),
        dump("backups/pausebot-.dump.tmp", 1),
        dump("backups/catalyse-1.dump", 1),
    ]
    assert fetch_backup.newest_dump(dumps)["Key"] == "backups/pausebot-wanted.dump"


def test_newest_dump_of_nothing():
    assert fetch_backup.newest_dump([]) is None


def test_versioned_bucket_ignores_hidden_dumps_and_old_versions(make_target, tmp_path):
    target = make_target("b2", "versioned", versioned=True)
    client = put(target, "backups/pausebot-1.dump", body=b"old version, longer")
    put(target, "backups/pausebot-1.dump", body=b"current")
    # Newer than the wanted file, but hidden, so only old versions and a delete marker remain
    put(target, "backups/pausebot-2.dump")
    client.delete_object(Bucket="versioned", Key="backups/pausebot-2.dump")

    found, path, downloaded = fetch_backup.fetch(target, tmp_path)
    assert found["Key"] == "backups/pausebot-1.dump" and found["Size"] == len(b"current")
    assert path.read_bytes() == b"current" and downloaded


def test_only_hidden_dumps_counts_as_no_dumps(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "versioned", versioned=True)
    put(target, "backups/pausebot-1.dump").delete_object(Bucket="versioned", Key="backups/pausebot-1.dump")
    use_targets(monkeypatch, target)
    assert fetch_backup.main(["--out", str(tmp_path)]) == 1
    assert "no backups/pausebot-*.dump files in bucket versioned" in capsys.readouterr().err


def test_ignores_files_that_are_not_dumps_on_an_unversioned_bucket(make_target, tmp_path):
    target = make_target("r2", "plain")
    put(target, "backups/pausebot-2026-11-01_030000.dump")
    put(target, "backups/pausebot-zzz.txt", "backups/catalyse-zzz.dump", "other/pausebot-zzz.dump")
    _, path, _ = fetch_backup.fetch(target, tmp_path)
    assert path == tmp_path / "pausebot-2026-11-01_030000.dump"


# ── Downloading ───────────────────────────────────────────────────────────────

def test_download_is_private_and_leaves_nothing_else(make_target, tmp_path):
    target = make_target("b2", "pausebot")
    put(target, "backups/pausebot-1.dump", body=os.urandom(4096))
    out = tmp_path / "new" / "dir"
    _, path, downloaded = fetch_backup.fetch(target, out)
    assert downloaded and path.stat().st_size == 4096
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert [p.name for p in out.iterdir()] == ["pausebot-1.dump"]


def test_large_dump_downloads_whole(make_target, tmp_path):
    # Over boto3's 8 MB threshold, so this comes down in parallel ranged parts
    target = make_target("b2", "pausebot")
    body = os.urandom(9 * 1024 * 1024)
    put(target, "backups/pausebot-1.dump", body=body)
    _, path, _ = fetch_backup.fetch(target, tmp_path)
    assert path.read_bytes() == body


def test_skips_a_file_already_there(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "pausebot")
    put(target, "backups/pausebot-1.dump", body=b"dump")
    existing = tmp_path / "pausebot-1.dump"
    existing.write_bytes(b"same")  # Same size, and different bytes prove it wasn't replaced
    use_targets(monkeypatch, target)
    assert fetch_backup.main(["--out", str(tmp_path)]) == 0
    assert existing.read_bytes() == b"same"
    assert "already there" in capsys.readouterr().out


def test_replaces_a_file_of_another_size(make_target, tmp_path):
    target = make_target("b2", "pausebot")
    put(target, "backups/pausebot-1.dump", body=b"whole dump")
    existing = tmp_path / "pausebot-1.dump"
    existing.write_bytes(b"whole")
    _, _, downloaded = fetch_backup.fetch(target, tmp_path)
    assert downloaded and existing.read_bytes() == b"whole dump"


def test_failed_download_leaves_nothing_behind(make_target, tmp_path):
    target = make_target("b2", "pausebot")
    client = put(target, "backups/pausebot-1.dump", body=b"dump")

    def short_read(bucket, key, f):
        f.write(b"du")

    client.download_fileobj = short_read
    found = fetch_backup.newest_dump(backup.list_dumps(client, target))
    with pytest.raises(fetch_backup.FetchError, match="got 2 of 4 bytes"):
        fetch_backup.download(client, target, found, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_only_lists_and_reads_under_the_prefix(make_target, tmp_path, monkeypatch):
    target = make_target("b2", "versioned", versioned=True)
    put(target, "backups/pausebot-1.dump")
    calls = []
    client = backup.make_client(target)
    client.meta.events.register("provide-client-params.s3", lambda params, model, **_: calls.append((model.name, params)))
    monkeypatch.setattr(backup, "make_client", lambda t: client)

    fetch_backup.fetch(target, tmp_path)
    assert {name for name, _ in calls} <= {"ListObjectVersions", "HeadObject", "GetObject"}
    assert all((p.get("Prefix") or p.get("Key")).startswith(backup.BUCKET_PREFIX) for _, p in calls)


# ── Choosing the provider ─────────────────────────────────────────────────────

def test_prints_the_details_and_nothing_secret(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "pausebot")
    put(target, "backups/pausebot-1.dump", body=b"dump")
    target.key_id, target.secret = "keyid123", "sup3rsecret"
    use_targets(monkeypatch, target)

    assert fetch_backup.main(["--out", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert [line.split(":")[0] for line in lines] == ["Provider", "Bucket", "File", "Size", "Last modified", "Local path"]
    assert lines[0].split() == ["Provider:", "b2"] and lines[1].split() == ["Bucket:", "pausebot"]
    assert lines[2].split() == ["File:", "pausebot-1.dump"]
    assert lines[5].split() == ["Local", "path:", str(tmp_path / "pausebot-1.dump")]
    assert "keyid123" not in captured.out + captured.err and "sup3rsecret" not in captured.out + captured.err


def test_uses_the_first_provider_that_works(make_target, tmp_path, monkeypatch, capsys):
    broken = make_target("b2", "missing", create=False)
    empty = make_target("r2", "empty")
    good = make_target("s3", "good")
    put(good, "backups/pausebot-1.dump")
    use_targets(monkeypatch, broken, empty, good)

    assert fetch_backup.main(["--out", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].split() == ["Provider:", "s3"]
    assert "b2: bucket missing does not exist" in captured.err
    assert "r2: no backups/pausebot-*.dump files" in captured.err


def test_provider_flag_chooses_one(make_target, tmp_path, monkeypatch):
    first, second = make_target("b2", "first"), make_target("r2", "second")
    put(first, "backups/pausebot-1.dump")
    put(second, "backups/pausebot-2.dump")
    use_targets(monkeypatch, first, second)

    assert fetch_backup.main(["--provider", "r2", "--out", str(tmp_path)]) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["pausebot-2.dump"]


def test_provider_flag_does_not_fall_back(make_target, tmp_path, monkeypatch):
    good = make_target("r2", "good")
    put(good, "backups/pausebot-1.dump")
    use_targets(monkeypatch, make_target("b2", "missing", create=False), good)
    assert fetch_backup.main(["--provider", "b2", "--out", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []


# ── Errors ────────────────────────────────────────────────────────────────────

B2_ENV = {"BACKUP_B2_REGION": "eu-central-003", "BACKUP_B2_BUCKET": "bucket",
          "BACKUP_B2_KEY_ID": "keyid123", "BACKUP_B2_SECRET": "sup3rsecret"}


def test_nothing_configured(capsys):
    assert fetch_backup.main([], env={}) == 1
    err = capsys.readouterr().err
    assert "No usable provider" in err
    assert "BACKUP_B2_REGION, BACKUP_B2_BUCKET, BACKUP_B2_KEY_ID, BACKUP_B2_SECRET in .env" in err


def test_chosen_provider_not_configured(capsys):
    assert fetch_backup.main(["--provider", "r2"], env=B2_ENV) == 1
    assert "BACKUP_R2_ACCOUNT_ID, BACKUP_R2_BUCKET, BACKUP_R2_KEY_ID, BACKUP_R2_SECRET" in capsys.readouterr().err


def test_partly_configured_provider_names_the_missing_variable(capsys):
    env = {k: v for k, v in B2_ENV.items() if k != "BACKUP_B2_SECRET"}
    assert fetch_backup.main([], env=env) == 1
    assert "Missing BACKUP_B2_SECRET" in capsys.readouterr().err


def test_key_pasted_into_the_location_is_not_printed(capsys):
    assert fetch_backup.main([], env={**B2_ENV, "BACKUP_B2_REGION": "sup3rsecret"}) == 1
    err = capsys.readouterr().err
    assert "BACKUP_B2_REGION should look like eu-central-003" in err and "sup3rsecret" not in err


@pytest.mark.parametrize("code", ["AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"])
def test_auth_failure_is_clear_and_does_not_repeat_the_providers_message(tmp_path, monkeypatch, capsys, code):
    monkeypatch.setattr(backup, "make_client", lambda t: rejecting_client(code, "The key 'keyid123' is not valid"))
    assert fetch_backup.main(["--out", str(tmp_path)], env=B2_ENV) == 1
    err = capsys.readouterr().err
    assert f"b2: auth failed ({code}): check BACKUP_B2_KEY_ID and BACKUP_B2_SECRET" in err
    assert "keyid123" not in err


def test_unreachable_provider(tmp_path, monkeypatch, capsys):
    def unreachable(t):
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    monkeypatch.setattr(backup, "make_client", unreachable)
    assert fetch_backup.main(["--out", str(tmp_path)], env=B2_ENV) == 1
    assert "b2: could not talk to the bucket (EndpointConnectionError)" in capsys.readouterr().err


def test_unwritable_output_is_reported(make_target, tmp_path, monkeypatch, capsys):
    target = make_target("b2", "pausebot")
    put(target, "backups/pausebot-1.dump")
    use_targets(monkeypatch, target)
    blocked = tmp_path / "file"
    blocked.write_text("in the way")
    assert fetch_backup.main(["--out", str(blocked)]) == 1
    assert "b2: could not write the file" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["jobs/fetch_backup.py"], ["-m", "jobs.fetch_backup"]])
def test_runs_both_ways(command):
    result = subprocess.run([sys.executable, *command, "--help"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and "--provider" in result.stdout
