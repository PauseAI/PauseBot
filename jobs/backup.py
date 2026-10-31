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
from dataclasses import dataclass, field
from pathlib import Path
import boto3
from botocore.config import Config

LOCAL_RETENTION_DAYS = 7
BUCKET_RETENTION_DAYS = 30
FILE_PREFIX = "pausebot-"
BUCKET_PREFIX = f"backups/{FILE_PREFIX}"


def timestamp():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d_%H%M%S")


# ── Targets ───────────────────────────────────────────────────────────────────

# Per provider: the one variable that says where its buckets live, and how that gives the
# S3 endpoint and signing region. Everything else is the same for every provider.
PROVIDERS = {
    "b2": ("REGION", lambda region: (f"https://s3.{region}.backblazeb2.com", region)),
    "r2": ("ACCOUNT_ID", lambda account_id: (f"https://{account_id}.r2.cloudflarestorage.com", "auto")),
    "s3": ("REGION", lambda region: (None, region)),
}


class ConfigError(Exception):
    pass


@dataclass
class Target:
    name: str
    bucket: str
    endpoint: str | None
    region: str
    key_id: str
    secret: str = field(repr=False)  # Keep it out of any log line that prints a Target


def load_targets(env=None):
    """The providers whose BACKUP_<PROVIDER>_* variables are set. A provider with only some of
    them set raises ConfigError, so a typo fails the run instead of quietly skipping a bucket."""
    env = os.environ if env is None else env
    targets, missing = [], []
    for name, (location_var, endpoint_for) in PROVIDERS.items():
        names = [f"BACKUP_{name.upper()}_{v}" for v in (location_var, "BUCKET", "KEY_ID", "SECRET")]
        values = [env.get(n, "").strip() for n in names]
        if not any(values):
            continue
        missing += [n for n, v in zip(names, values) if not v]
        if all(values):
            location, bucket, key_id, secret = values
            endpoint, region = endpoint_for(location)
            targets.append(Target(name, bucket, endpoint, region, key_id, secret))
    if missing:
        raise ConfigError(f"Missing {', '.join(missing)}")
    return targets


def make_client(target):
    return boto3.client(
        "s3",
        endpoint_url=target.endpoint,
        region_name=target.region,
        aws_access_key_id=target.key_id,
        aws_secret_access_key=target.secret,
        # boto3's default upload checksums aren't supported by every S3-compatible provider
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def expired_keys(objects, now=None):
    """Keys of our dumps older than BUCKET_RETENTION_DAYS, from list_objects_v2 entries."""
    now = now or datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(days=BUCKET_RETENTION_DAYS)
    return [o["Key"] for o in objects if o["Key"].startswith(BUCKET_PREFIX) and o["LastModified"] < cutoff]


def cleanup_target(client, target, now=None):
    # Only our own dumps, in case the bucket also holds another service's backups
    objects = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=target.bucket, Prefix=BUCKET_PREFIX):
        objects += page.get("Contents", [])
    keys = expired_keys(objects, now)
    for key in keys:
        client.delete_object(Bucket=target.bucket, Key=key)
    if keys:
        print(f"[BACKUP] {target.name}: removed {len(keys)} backup(s) older than {BUCKET_RETENTION_DAYS} days")


def backup_to_target(target, backup_path, client=None):
    """Uploads the dump, then prunes old ones. Returns whether the upload landed."""
    key = f"backups/{backup_path.name}"
    try:
        client = client or make_client(target)
        # Streams from disk, in parts for large files
        client.upload_file(str(backup_path), target.bucket, key)
        print(f"[BACKUP] {target.name}: uploaded {key} to {target.bucket}")
    except Exception as e:
        print(f"[BACKUP] {target.name}: upload failed: {e}", file=sys.stderr)
        # If uploads keep failing unnoticed, pruning would empty the bucket within 30 days
        print(f"[BACKUP] {target.name}: skipping cleanup because the upload failed", file=sys.stderr)
        return False
    try:
        cleanup_target(client, target)
    except Exception as e:
        print(f"[BACKUP] {target.name}: cleanup failed: {e}", file=sys.stderr)
    return True


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
    """Returns {"local_success": bool, "targets": {name: uploaded}}."""
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
        print("[BACKUP] No backup targets configured, keeping the local dump only")
    # One provider failing doesn't stop the others
    results = {t.name: backup_to_target(t, backup_path) for t in targets}
    cleanup_local_backups(backup_dir)
    if targets:
        print(f"[BACKUP] Done: {sum(results.values())} of {len(targets)} targets succeeded")
    return {"local_success": True, "targets": results}


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # The cron image only installs boto3; Railway sets the env directly
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("[BACKUP] DATABASE_URL is not set")
    try:
        targets = load_targets()
    except ConfigError as e:
        sys.exit(f"[BACKUP] {e}")
    result = run_backup_job(dsn, targets)
    # Fail the cron run if the dump failed or any bucket didn't get it
    if not result["local_success"] or not all(result["targets"].values()):
        sys.exit(1)
