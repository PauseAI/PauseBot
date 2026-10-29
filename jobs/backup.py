"""Daily off-site backup of the whole database: pg_dump, upload to Backblaze B2, prune old dumps.

Runs as its own Railway cron service (Dockerfile.backup, railway.backup.json), ported from
catalyse's jobs/backup.ts. Needs DATABASE_URL, plus B2_KEY_ID, B2_APP_KEY and B2_BUCKET_NAME
for the upload; without those it only keeps the local dump.
"""
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_RETENTION_DAYS = 7
B2_RETENTION_DAYS = 30
FILE_PREFIX = "pausebot-"


def timestamp():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d_%H%M%S")


# ── B2 API ────────────────────────────────────────────────────────────────────

def _b2_request(url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} failed: {e.code} {e.read().decode(errors='replace')}") from None


def _b2_api(auth, call, payload):
    return _b2_request(
        f"{auth['apiUrl']}/b2api/v2/{call}",
        {"Authorization": auth["authorizationToken"], "Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )


def b2_authorize():
    credentials = base64.b64encode(f"{os.environ['B2_KEY_ID']}:{os.environ['B2_APP_KEY']}".encode()).decode()
    return _b2_request(
        "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
        {"Authorization": f"Basic {credentials}"},
        None,
    )


def b2_get_bucket_id(auth, bucket_name):
    buckets = _b2_api(auth, "b2_list_buckets", {"accountId": auth["accountId"], "bucketName": bucket_name}).get("buckets")
    if not buckets:
        raise RuntimeError(f"Bucket '{bucket_name}' not found")
    return buckets[0]["bucketId"]


def b2_upload_file(auth, bucket_id, file_name, file_data):
    upload = _b2_api(auth, "b2_get_upload_url", {"bucketId": bucket_id})
    return _b2_request(
        upload["uploadUrl"],
        {
            "Authorization": upload["authorizationToken"],
            "X-Bz-File-Name": file_name,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(file_data)),
            "X-Bz-Content-Sha1": hashlib.sha1(file_data).hexdigest(),
        },
        file_data,
    )


def b2_list_files(auth, bucket_id, prefix):
    return _b2_api(auth, "b2_list_file_names", {"bucketId": bucket_id, "prefix": prefix, "maxFileCount": 1000})["files"]


def b2_delete_file(auth, file_id, file_name):
    _b2_api(auth, "b2_delete_file_version", {"fileId": file_id, "fileName": file_name})


# ── Backup logic ──────────────────────────────────────────────────────────────

def is_b2_configured():
    return all(os.environ.get(k) for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET_NAME"))


def local_backup_dir():
    # Dumps live on the volume when Railway provides one, so the last week survives a redeploy.
    return Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or tempfile.gettempdir()) / "backups"


def _major(version_text):
    return int(re.search(r"\d+", version_text).group())


def check_client_version(dsn):
    """Major versions of the pg_dump binary and the server; pg_dump refuses a newer server."""
    client = _major(subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, check=True).stdout)
    server = _major(subprocess.run(
        ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--command", "SHOW server_version", dsn],
        capture_output=True, text=True, check=True,
    ).stdout)
    if client < server:
        raise RuntimeError(
            f"pg_dump {client} cannot back up a PostgreSQL {server} server — bump postgresql-client-{server} in Dockerfile.backup"
        )
    return client, server


def create_local_backup(dsn, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{FILE_PREFIX}{timestamp()}.dump"
    # Custom format is compressed and restores selectively with pg_restore.
    subprocess.run(
        ["pg_dump", "--format=custom", "--schema=public", "--no-owner", "--no-privileges", "--file", str(dest), dsn],
        stdin=subprocess.DEVNULL, check=True,
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


def upload_to_b2(backup_path):
    if not is_b2_configured():
        print("[BACKUP] B2 not configured, skipping cloud upload")
        return False
    try:
        auth = b2_authorize()
        bucket_id = b2_get_bucket_id(auth, os.environ["B2_BUCKET_NAME"])
        file_name = f"backups/{backup_path.name}"
        file_data = backup_path.read_bytes()
        result = b2_upload_file(auth, bucket_id, file_name, file_data)
        print(f"[BACKUP] Uploaded to B2: {file_name} ({result.get('contentLength', len(file_data)) // 1024} KB)")
        return True
    except Exception as e:
        print(f"[BACKUP] B2 upload failed: {e}", file=sys.stderr)
        return False


def cleanup_b2_backups():
    if not is_b2_configured():
        return
    try:
        auth = b2_authorize()
        bucket_id = b2_get_bucket_id(auth, os.environ["B2_BUCKET_NAME"])
        cutoff_ms = (time.time() - B2_RETENTION_DAYS * 24 * 60 * 60) * 1000
        removed = 0
        for f in b2_list_files(auth, bucket_id, "backups/"):
            if f["uploadTimestamp"] < cutoff_ms:
                b2_delete_file(auth, f["fileId"], f["fileName"])
                removed += 1
        if removed:
            print(f"[BACKUP] Removed {removed} B2 backup(s) older than {B2_RETENTION_DAYS} days")
    except Exception as e:
        print(f"[BACKUP] B2 cleanup failed: {e}", file=sys.stderr)


def run_backup_job(dsn, backup_dir=None):
    backup_dir = backup_dir or local_backup_dir()
    print(f"[BACKUP] Starting backup at {datetime.datetime.now(datetime.UTC).isoformat()}")
    try:
        check_client_version(dsn)
        backup_path = create_local_backup(dsn, backup_dir)
    except Exception as e:
        print(f"[BACKUP] pg_dump failed: {e}", file=sys.stderr)
        return {"local_success": False, "b2_success": False}
    b2_success = upload_to_b2(backup_path)
    cleanup_local_backups(backup_dir)
    cleanup_b2_backups()
    print("[BACKUP] Backup cycle complete")
    return {"local_success": True, "b2_success": b2_success}


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # The cron image has no Python packages; Railway sets the env directly
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("[BACKUP] DATABASE_URL is not set")
    result = run_backup_job(dsn)
    # Fail the cron run if the dump failed, or if B2 is set up but the upload didn't land
    if not result["local_success"] or (is_b2_configured() and not result["b2_success"]):
        sys.exit(1)
