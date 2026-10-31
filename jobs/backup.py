"""Daily off-site backup of the whole database: pg_dump, then upload to every configured bucket.

Runs as its own Railway cron service (Dockerfile.backup, railway.backup.json), ported from
catalyse's jobs/backup.ts. Needs DATABASE_URL. Each provider below is backed up to when its
BACKUP_<PROVIDER>_* variables are set; with none set it only keeps the local dump. Use different
providers rather than two buckets at one, so one provider's outage or account problem can't take
out every copy.
"""
import datetime
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOCAL_RETENTION_DAYS = 7
BUCKET_RETENTION_DAYS = 30
FILE_PREFIX = "pausebot-"
BUCKET_DIR = "backups/"
BUCKET_PREFIX = f"{BUCKET_DIR}{FILE_PREFIX}"


def timestamp():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d_%H%M%S")


# ── Targets ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Provider:
    location_var: str  # The one variable that says where the provider's buckets live
    location_format: str  # Regex the location must match, so a pasted URL is caught at startup
    location_example: str
    endpoint: str  # S3 endpoint, filled in with the location
    region: str | None  # Signing region; None means the location is the region
    versioned: bool  # Whether buckets can keep old versions, so deletes must name each version


# Everything that differs between providers. The rest (prefix, retention, cleanup) is shared.
PROVIDERS = {
    "b2": Provider(
        location_var="REGION", location_format=r"[a-z]+-[a-z]+-\d{3}", location_example="eu-central-003",
        endpoint="https://s3.{}.backblazeb2.com", region=None, versioned=True,
    ),
    # An R2 bucket created in a jurisdiction lives at <account_id>.<jurisdiction>.r2...
    "r2": Provider(
        location_var="ACCOUNT_ID", location_format=r"[0-9a-f]{32}(\.(eu|fedramp))?",
        location_example="the 32-character account ID, plus .eu for an EU-jurisdiction bucket",
        endpoint="https://{}.r2.cloudflarestorage.com", region="auto", versioned=False,
    ),
    "s3": Provider(
        location_var="REGION", location_format=r"[a-z]{2}(-[a-z]+)+-\d", location_example="eu-west-2",
        endpoint="https://s3.{}.amazonaws.com", region=None, versioned=True,
    ),
}

# Names the job used before BACKUP_B2_*; nothing reads them now
OLD_VARIABLES = ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET_NAME")


@dataclass
class Target:
    name: str
    provider: Provider
    location: str
    bucket: str
    key_id: str
    secret: str = field(repr=False)  # Keep it out of any log line that prints a Target

    @property
    def endpoint(self):
        return self.provider.endpoint.format(self.location)

    @property
    def region(self):
        return self.provider.region or self.location

    @property
    def versioned(self):
        return self.provider.versioned


def load_targets(env=None):
    """Returns (targets, problems): the providers whose BACKUP_<PROVIDER>_* variables are all set
    and valid, and what's wrong with any that are only partly set or malformed. A problem fails
    the run, but the dump and the valid targets still go ahead."""
    env = os.environ if env is None else env
    targets, problems = [], []
    old = [n for n in OLD_VARIABLES if env.get(n, "").strip()]
    if old:
        problems.append(f"{', '.join(old)} {'is' if len(old) == 1 else 'are'} no longer used; set BACKUP_B2_REGION, "
                        "BACKUP_B2_BUCKET, BACKUP_B2_KEY_ID and BACKUP_B2_SECRET instead")
    for name, provider in PROVIDERS.items():
        names = [f"BACKUP_{name.upper()}_{v}" for v in (provider.location_var, "BUCKET", "KEY_ID", "SECRET")]
        values = [env.get(n, "").strip() for n in names]
        if not any(values):
            continue
        missing = [n for n, v in zip(names, values) if not v]
        if missing:
            problems.append(f"Missing {', '.join(missing)}")
            continue
        location, bucket, key_id, secret = values
        if not re.fullmatch(provider.location_format, location):
            problems.append(f"{names[0]} should look like {provider.location_example}, got {location!r}")
            continue
        targets.append(Target(name=name, provider=provider, location=location, bucket=bucket,
                              key_id=key_id, secret=secret))
    return targets, problems


def make_client(target):
    # A session per client: clients are created from worker threads, and boto3's default
    # session isn't thread-safe
    return boto3.session.Session().client(
        "s3",
        endpoint_url=target.endpoint,
        region_name=target.region,
        aws_access_key_id=target.key_id,
        aws_secret_access_key=target.secret,
        config=Config(
            # boto3's default upload checksums aren't supported by every S3-compatible provider
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Never let AWS_ENDPOINT_URL* set for some other tool redirect a copy elsewhere
            ignore_configured_endpoint_urls=True,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def list_dumps(client, target):
    """Every stored copy of our dumps as {Key, VersionId, LastModified}. On a versioned bucket
    (B2 keeps all versions by default) that includes old versions and hide/delete markers, which
    a plain listing doesn't show and a plain delete doesn't remove."""
    # Only our own dumps, in case the bucket also holds another service's backups
    if target.versioned:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=target.bucket, Prefix=BUCKET_PREFIX):
            for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                yield {"Key": v["Key"], "VersionId": v["VersionId"], "LastModified": v["LastModified"]}
    else:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=target.bucket, Prefix=BUCKET_PREFIX):
            for o in page.get("Contents", []):
                yield {"Key": o["Key"], "VersionId": None, "LastModified": o["LastModified"]}


def expired(dumps, now=None):
    """The dumps older than BUCKET_RETENTION_DAYS."""
    now = now or datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(days=BUCKET_RETENTION_DAYS)
    # The listing already filters by prefix; checked again on purpose, because deleting another
    # service's files from a shared bucket is the one mistake cleanup must never make
    return [d for d in dumps if d["Key"].startswith(BUCKET_PREFIX) and d["LastModified"] < cutoff]


# DeleteObjects errors that a one-at-a-time retry can't fix and would only hide or make worse
NO_FALLBACK_ERRORS = {
    "AccessDenied", "SlowDown", "NoSuchBucket", "InvalidAccessKeyId", "SignatureDoesNotMatch",
    "ServiceUnavailable", "InternalError", "ExpiredToken",
}


def delete_dumps(client, target, dumps):
    """Deletes the dumps (each version on a versioned bucket). Returns (deleted, failures), where
    failures are "Code on Key" strings; one failure doesn't stop the rest."""
    ids = [{"Key": d["Key"], **({"VersionId": d["VersionId"]} if d["VersionId"] else {})} for d in dumps]
    deleted, failures = 0, []
    for i in range(0, len(ids), 1000):  # DeleteObjects takes at most 1000 keys
        chunk = ids[i:i + 1000]
        try:
            response = client.delete_objects(Bucket=target.bucket, Delete={"Objects": chunk, "Quiet": True})
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in NO_FALLBACK_ERRORS:
                raise
            # DeleteObjects needs a request checksum not every S3-compatible provider accepts
            print(f"[BACKUP] {target.name}: batch delete rejected ({code}), deleting one at a time")
            for obj in chunk:
                try:
                    client.delete_object(Bucket=target.bucket, **obj)
                    deleted += 1
                except ClientError as e:
                    failures.append(f"{e.response.get('Error', {}).get('Code', 'Error')} on {obj['Key']}")
            continue
        errors = response.get("Errors", [])
        deleted += len(chunk) - len(errors)
        failures += [f"{err.get('Code')} on {err.get('Key')}" for err in errors]
    return deleted, failures


def cleanup_target(client, target, now=None):
    dumps = expired(list_dumps(client, target), now)
    if not dumps:
        return
    deleted, failures = delete_dumps(client, target, dumps)
    if deleted:
        print(f"[BACKUP] {target.name}: removed {deleted} backup(s) older than {BUCKET_RETENTION_DAYS} days")
    if failures:
        raise RuntimeError(f"{len(failures)} delete(s) failed, first: {failures[0]}")


def backup_to_target(target, backup_path, client=None):
    """Uploads the dump, then prunes old ones. Returns {"uploaded": bool, "cleaned": bool | None};
    cleaned is None when cleanup was skipped because the upload failed."""
    key = f"{BUCKET_DIR}{backup_path.name}"
    try:
        client = client or make_client(target)
        # Streams from disk, in parts for large files
        client.upload_file(str(backup_path), target.bucket, key)
        print(f"[BACKUP] {target.name}: uploaded {key} to {target.bucket} ({backup_path.stat().st_size // 1024} KB)")
    except Exception as e:
        print(f"[BACKUP] {target.name}: upload failed: {e}", file=sys.stderr)
        # If uploads keep failing unnoticed, pruning would empty the bucket within 30 days
        print(f"[BACKUP] {target.name}: skipping cleanup because the upload failed", file=sys.stderr)
        return {"uploaded": False, "cleaned": None}
    try:
        cleanup_target(client, target)
    except Exception as e:
        # The copy landed, but retention is broken, so this still fails the run
        print(f"[BACKUP] {target.name}: cleanup failed: {e}", file=sys.stderr)
        return {"uploaded": True, "cleaned": False}
    return {"uploaded": True, "cleaned": True}


def succeeded(result):
    return result["uploaded"] and result["cleaned"] is not False


def backup_to_all(targets, backup_path):
    """Uploads to every target at once, so one slow provider doesn't hold up the rest.
    Returns {name: backup_to_target result}; one failing doesn't stop the others."""
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = pool.map(lambda t: backup_to_target(t, backup_path), targets)
        return dict(zip((t.name for t in targets), results))


# ── Dump ──────────────────────────────────────────────────────────────────────

def local_backup_dir():
    # Dumps live on the volume when Railway provides one, so the last week survives a redeploy.
    return Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or tempfile.gettempdir()) / "backups"


def _major(version_text):
    return int(re.search(r"\d+", version_text).group())


def libpq_args(dsn):
    """Splits the password out of the DSN into PGPASSWORD, so it never appears in a command line,
    and so never in the CalledProcessError text that ends up in the logs."""
    url = urllib.parse.urlsplit(dsn)
    env = dict(os.environ)
    if url.password is None:
        return dsn, env
    env["PGPASSWORD"] = urllib.parse.unquote(url.password)
    netloc = url.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"  # IPv6 literal
    if url.username:
        netloc = f"{url.username}@{netloc}"
    if url.port:
        netloc = f"{netloc}:{url.port}"
    return urllib.parse.urlunsplit(url._replace(netloc=netloc)), env


def check_client_version(dsn):
    """Major versions of the pg_dump binary and the server; pg_dump refuses a newer server."""
    client = _major(subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, check=True).stdout)
    dsn, env = libpq_args(dsn)
    server = _major(subprocess.run(
        ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--command", "SHOW server_version", dsn],
        capture_output=True, text=True, check=True, env=env,
    ).stdout)
    if client < server:
        raise RuntimeError(
            f"pg_dump {client} cannot back up a PostgreSQL {server} server — bump postgresql-client-{server} in Dockerfile.backup"
        )
    return client, server


def create_local_backup(dsn, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{FILE_PREFIX}{timestamp()}.dump"
    dsn, env = libpq_args(dsn)
    # Custom format is compressed and restores selectively with pg_restore.
    subprocess.run(
        ["pg_dump", "--format=custom", "--schema=public", "--no-owner", "--no-privileges", "--file", str(dest), dsn],
        stdin=subprocess.DEVNULL, check=True, env=env,
    )
    print(f"[BACKUP] Local backup created: {dest.name} ({dest.stat().st_size // 1024} KB)")
    return dest


def cleanup_local_backups(backup_dir):
    if not backup_dir.exists():
        return
    cutoff = time.time() - LOCAL_RETENTION_DAYS * 24 * 60 * 60
    removed = 0
    for f in backup_dir.glob(f"{FILE_PREFIX}*.dump"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        print(f"[BACKUP] Removed {removed} local backup(s) older than {LOCAL_RETENTION_DAYS} days")


def run_backup_job(dsn, targets, backup_dir=None):
    """Returns {"local_success": bool, "targets": {name: backup_to_target result}}."""
    backup_dir = backup_dir or local_backup_dir()
    print(f"[BACKUP] Starting backup at {datetime.datetime.now(datetime.UTC).isoformat()}")
    try:
        check_client_version(dsn)
        backup_path = create_local_backup(dsn, backup_dir)
    except subprocess.CalledProcessError as e:
        # Not str(e): that includes the command line, and with it any DSN form libpq_args can't split
        detail = f": {e.stderr.strip()}" if e.stderr else ""
        print(f"[BACKUP] {e.cmd[0]} exited with status {e.returncode}{detail}", file=sys.stderr)
        return {"local_success": False, "targets": {}}
    except Exception as e:
        print(f"[BACKUP] pg_dump failed: {e}", file=sys.stderr)
        return {"local_success": False, "targets": {}}
    if not targets:
        print("[BACKUP] No valid backup targets configured, keeping the local dump only")
    results = backup_to_all(targets, backup_path)
    cleanup_local_backups(backup_dir)
    if targets:
        print(f"[BACKUP] Done: {sum(map(succeeded, results.values()))} of {len(targets)} targets succeeded")
    return {"local_success": True, "targets": results}


if __name__ == "__main__":
    # Keep stdout and stderr lines in order in the Railway log
    sys.stdout.reconfigure(line_buffering=True)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # The cron image only installs boto3; Railway sets the env directly
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("[BACKUP] DATABASE_URL is not set")
    targets, problems = load_targets()
    # A config mistake still fails the run, but doesn't cost the night's backup to the providers
    # that are set up correctly
    for problem in problems:
        print(f"[BACKUP] Config problem: {problem}", file=sys.stderr)
    result = run_backup_job(dsn, targets)
    if problems:
        print(f"[BACKUP] Failing the run because of {len(problems)} config problem(s) above", file=sys.stderr)
    if problems or not result["local_success"] or not all(map(succeeded, result["targets"].values())):
        sys.exit(1)
