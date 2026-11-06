import os
import urllib.parse
import urllib.request
import pytest
from moto.server import ThreadedMotoServer
import db
from jobs import backup

# Tests drop and recreate tables, so they only ever run against a database whose name ends in _test.
TEST_DSN = os.environ.setdefault(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/pausebot_test"
)

if not urllib.parse.urlparse(TEST_DSN).path.lstrip("/").endswith("_test"):
    raise RuntimeError(f"TEST_DATABASE_URL must name a *_test database, got {TEST_DSN!r}")
if TEST_DSN == os.environ.get("DATABASE_URL"):
    raise RuntimeError("TEST_DATABASE_URL must not be the same as DATABASE_URL")


@pytest.fixture
def dsn():
    return TEST_DSN


@pytest.fixture
async def pool(dsn):
    pool = await db.create_pool(dsn)
    await pool.execute("DROP TABLE IF EXISTS message_edits, message_log, server_snapshots CASCADE")
    await db.apply_schema(pool)
    yield pool
    await pool.close()


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
