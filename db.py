"""Postgres storage for server snapshots and the message log (replacing Xenon and Quark)."""
import json
import pathlib
import asyncpg

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")
RETENTION_DAYS = 30


async def _init_connection(conn):
    # Hand jsonb columns to and from Python as dicts/lists rather than strings
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn):
    # A slow database should fail a query rather than leave event handlers waiting on it
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5, init=_init_connection, command_timeout=10)


async def apply_schema(pool):
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_PATH.read_text())


# ── Server snapshots ──────────────────────────────────────────────────────────

async def save_snapshot(pool, guild_id, data):
    return await pool.fetchval(
        "INSERT INTO server_snapshots (guild_id, data) VALUES ($1, $2) RETURNING id",
        guild_id, data,
    )


async def latest_snapshot(pool, guild_id):
    return await pool.fetchrow(
        "SELECT id, guild_id, taken_at, data FROM server_snapshots WHERE guild_id = $1 ORDER BY taken_at DESC LIMIT 1",
        guild_id,
    )


# ── Message log ───────────────────────────────────────────────────────────────

async def log_message(pool, *, message_id, guild_id, channel_id, author_id, content, attachments, created_at):
    await pool.execute(
        """
        INSERT INTO message_log (message_id, guild_id, channel_id, author_id, content, attachments, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (message_id) DO NOTHING
        """,
        message_id, guild_id, channel_id, author_id, content, attachments, created_at,
    )


async def log_edit(pool, message_id, content, edited_at, message=None):
    """Records the old content and stores the new. Returns the content before the edit, or None if
    the message isn't logged or its text didn't change (e.g. Discord adding a link embed).

    `message` holds the log_message fields other than content (guild_id, channel_id, author_id,
    attachments, created_at). With it, an edit that arrives before its message was logged stores
    the message with the edited content; log_message then leaves that row alone."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            if message is not None:
                inserted = await conn.fetchval(
                    """
                    INSERT INTO message_log (message_id, guild_id, channel_id, author_id, content, attachments, created_at, edited_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (message_id) DO NOTHING
                    RETURNING message_id
                    """,
                    message_id, message["guild_id"], message["channel_id"], message["author_id"],
                    content, message["attachments"], message["created_at"], edited_at,
                )
                if inserted is not None:
                    return None
            before = await conn.fetchval(
                "SELECT content FROM message_log WHERE message_id = $1 FOR UPDATE", message_id
            )
            if before is None or before == content:
                return None
            await conn.execute(
                "INSERT INTO message_edits (message_id, content_before, edited_at) VALUES ($1, $2, $3)",
                message_id, before, edited_at,
            )
            await conn.execute(
                "UPDATE message_log SET content = $2, edited_at = $3 WHERE message_id = $1",
                message_id, content, edited_at,
            )
            return before


async def log_delete(pool, message_ids, deleted_at):
    """Marks messages deleted and returns the rows that were logged, for posting to a log channel."""
    return await pool.fetch(
        """
        UPDATE message_log SET deleted_at = $2
        WHERE message_id = ANY($1::bigint[]) AND deleted_at IS NULL
        RETURNING message_id, guild_id, channel_id, author_id, content, attachments, created_at
        """,
        list(message_ids), deleted_at,
    )


async def get_message(pool, message_id):
    return await pool.fetchrow("SELECT * FROM message_log WHERE message_id = $1", message_id)


async def get_edits(pool, message_id):
    return await pool.fetch(
        "SELECT content_before, edited_at FROM message_edits WHERE message_id = $1 ORDER BY edited_at, id",
        message_id,
    )


# ── Retention ─────────────────────────────────────────────────────────────────

async def prune(pool, days=RETENTION_DAYS):
    """Deletes snapshots and messages older than `days`. Returns (snapshots, messages) removed."""
    snapshots = await pool.execute(
        "DELETE FROM server_snapshots WHERE taken_at < now() - make_interval(days => $1)", days
    )
    messages = await pool.execute(
        "DELETE FROM message_log WHERE created_at < now() - make_interval(days => $1)", days
    )
    # execute() returns the command tag, e.g. "DELETE 3"
    return int(snapshots.split()[-1]), int(messages.split()[-1])
