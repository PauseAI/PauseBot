"""Downloads the newest database dump from a backup bucket to this machine.

Run by hand: python jobs/fetch_backup.py [--provider b2|r2|s3] [--out DIR]. Reads the same
BACKUP_<PROVIDER>_* variables as jobs/backup.py, from .env, and should be given a read-only key
(see the README). Only ever lists and reads under the backups/ prefix, and never prints the key:
errors are reported by their code, not their text, since a provider's message can quote the key ID.
"""
import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # So running this file directly can still import the jobs package

from jobs import backup  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "backups"
# One path segment after the prefix, so the file name is safe to use as a local path
DUMP_KEY = re.compile(rf"{re.escape(backup.BUCKET_PREFIX)}[^/]+\.dump")
AUTH_ERRORS = {"AccessDenied", "Forbidden", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch", "Unauthorized",
               "ExpiredToken", "InvalidToken"}


class FetchError(Exception):
    """A failure whose message is ours, so safe to print."""


def newest_dump(dumps):
    """The most recently uploaded current dump, or None. The name breaks ties, since some providers
    only report LastModified to the second."""
    current = [d for d in dumps if d["Current"] and DUMP_KEY.fullmatch(d["Key"])]
    return max(current, key=lambda d: (d["LastModified"], d["Key"]), default=None)


def download(client, target, dump, out_dir):
    """Returns (local path, whether it was downloaded). A file of the same name but another size is
    replaced, as it can't be the same dump."""
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = out_dir / dump["Key"].rsplit("/", 1)[-1]
    if dest.exists() and dest.stat().st_size == dump["Size"]:
        return dest, False
    # mkstemp makes the file readable only by this user, and dumps hold members' messages. Writing
    # to it first means an interrupted download never looks like a finished dump.
    fd, part = tempfile.mkstemp(dir=out_dir, prefix=f".{dest.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            client.download_fileobj(target.bucket, dump["Key"], f)
        size = os.path.getsize(part)
        if size != dump["Size"]:
            raise FetchError(f"download incomplete: got {size} of {dump['Size']} bytes")
        os.replace(part, dest)
    except BaseException:
        Path(part).unlink(missing_ok=True)
        raise
    return dest, True


def fetch(target, out_dir):
    """Returns (dump, local path, whether it was downloaded)."""
    client = backup.make_client(target)
    dump = newest_dump(backup.list_dumps(client, target))
    if dump is None:
        raise FetchError(f"no {backup.BUCKET_PREFIX}*.dump files in bucket {target.bucket}")
    return (dump, *download(client, target, dump, out_dir))


def describe(error, target):
    if isinstance(error, FetchError):
        return str(error)
    if isinstance(error, OSError):
        return f"could not write the file: {error.strerror or type(error).__name__}"
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "Unknown")
        if code in AUTH_ERRORS:
            p = f"BACKUP_{target.name.upper()}_"
            return f"auth failed ({code}): check {p}KEY_ID and {p}SECRET, and that the key can read this bucket"
        if code == "NoSuchBucket":
            return f"bucket {target.bucket} does not exist"
        return f"request failed ({code})"
    return f"could not talk to the bucket ({type(error).__name__})"


def format_size(size):
    return f"{size / 1024 / 1024:.1f} MB ({size} bytes)"


def required_variables(name):
    provider = backup.PROVIDERS[name]
    return [f"BACKUP_{name.upper()}_{v}" for v in (provider.location_var, "BUCKET", "KEY_ID", "SECRET")]


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description="Download the newest database dump from a backup bucket.")
    parser.add_argument("--provider", choices=list(backup.PROVIDERS), help="default: the first one that works")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory to save to (default: %(default)s)")
    args = parser.parse_args(argv)

    targets, problems = backup.load_targets(env)
    if args.provider:
        targets = [t for t in targets if t.name == args.provider]
        problems = [p for p in problems if f"BACKUP_{args.provider.upper()}_" in p]
    for problem in problems:
        # Not the ", got <value>" ending: a key pasted on the wrong line would end up there
        print(f"[FETCH] Config problem: {problem.split(', got ')[0]}", file=sys.stderr)
    if not targets:
        names = [args.provider] if args.provider else list(backup.PROVIDERS)[:1]
        print(f"[FETCH] No usable provider. Set {', '.join(required_variables(names[0]))} in .env"
              f"{'' if args.provider else ' (or the same for R2 or S3)'}; see the README", file=sys.stderr)
        return 1

    for target in targets:
        try:
            dump, path, downloaded = fetch(target, args.out)
        except Exception as e:
            print(f"[FETCH] {target.name}: {describe(e, target)}", file=sys.stderr)
            continue
        print(f"Provider:      {target.name}")
        print(f"Bucket:        {target.bucket}")
        print(f"File:          {path.name}")
        print(f"Size:          {format_size(dump['Size'])}")
        print(f"Last modified: {dump['LastModified'].isoformat()}")
        print(f"Local path:    {path}{'' if downloaded else ' (already there, not downloaded again)'}")
        return 0
    return 1


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    sys.exit(main())
