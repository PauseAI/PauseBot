import os
import urllib.parse
import pytest
import db

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
