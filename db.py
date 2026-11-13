"""Postgres storage for server snapshots and the message log (replacing Xenon and Quark), and the
Discord member records (replacing Airtable's Discord table)."""
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


async def latest_snapshot_time(pool, guild_id):
    """When the newest snapshot was taken, without pulling its (large) data."""
    return await pool.fetchval("SELECT max(taken_at) FROM server_snapshots WHERE guild_id = $1", guild_id)


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

    `edited_at` is None when Discord updated the message without an edit (e.g. a link unfurl).
    Edit events can be handled out of order, so one older than the stored edit is dropped.

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
            row = await conn.fetchrow(
                "SELECT content, edited_at FROM message_log WHERE message_id = $1 FOR UPDATE", message_id
            )
            if row is None or row["content"] == content:
                return None
            if edited_at is not None and row["edited_at"] is not None and edited_at <= row["edited_at"]:
                return None
            # A text change always carries an edit time; now() only guards the NOT NULL column
            await conn.execute(
                "INSERT INTO message_edits (message_id, content_before, edited_at) VALUES ($1, $2, coalesce($3, now()))",
                message_id, row["content"], edited_at,
            )
            await conn.execute(
                "UPDATE message_log SET content = $2, edited_at = coalesce($3, now()) WHERE message_id = $1",
                message_id, content, edited_at,
            )
            return row["content"]


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


# ── Discord members ───────────────────────────────────────────────────────────

# The flags set_flag may set; the column name is put into the SQL, so it must come from this list
MEMBER_FLAGS = ("onboarding_notification", "discord_reaction")


async def saved_roles(pool, member_id):
    """The (role_ids, role_names) last saved for a member, or None if there's no record of them."""
    row = await pool.fetchrow("SELECT role_ids, role_names FROM discord_members WHERE id = $1", member_id)
    return (row["role_ids"], row["role_names"]) if row else None


async def save_member(pool, *, member_id, username, nick, global_name, joined_at, role_ids, role_names,
                      onboarding_notification=False):
    """Creates or updates a member's record, clearing left_at. onboarding_notification only ever
    turns the flag on."""
    await pool.execute(
        """
        INSERT INTO discord_members (id, username, nick, global_name, joined_at, role_ids, role_names, onboarding_notification)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (id) DO UPDATE SET
            username = EXCLUDED.username, nick = EXCLUDED.nick, global_name = EXCLUDED.global_name,
            joined_at = EXCLUDED.joined_at, role_ids = EXCLUDED.role_ids, role_names = EXCLUDED.role_names,
            left_at = NULL,
            onboarding_notification = discord_members.onboarding_notification OR EXCLUDED.onboarding_notification
        """,
        member_id, username, nick, global_name, joined_at, list(role_ids), list(role_names), onboarding_notification,
    )


async def mark_left(pool, member_id, username, left_at, role_ids, role_names):
    """Records a leave and the roles held at that moment, so they can be given back on rejoin."""
    await pool.execute(
        """
        INSERT INTO discord_members (id, username, left_at, role_ids, role_names) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO UPDATE SET left_at = EXCLUDED.left_at, role_ids = EXCLUDED.role_ids,
            role_names = EXCLUDED.role_names
        """,
        member_id, username, left_at, list(role_ids), list(role_names),
    )


async def set_flag(pool, flag, member_ids):
    """Turns one of MEMBER_FLAGS on for the given members. Returns how many records were updated."""
    if flag not in MEMBER_FLAGS:
        raise ValueError(f"Not a member flag: {flag!r}")
    result = await pool.execute(
        f"UPDATE discord_members SET {flag} = true WHERE id = ANY($1::bigint[])", list(member_ids)
    )
    return int(result.split()[-1])


async def linked_member_ids(pool):
    """IDs of the members CiviCRM has linked."""
    rows = await pool.fetch("SELECT id FROM discord_members WHERE civicrm_linked_at IS NOT NULL")
    return {row["id"] for row in rows}


async def mark_linked(pool, member_id, username):
    await pool.execute(
        """
        INSERT INTO discord_members (id, username, civicrm_linked_at) VALUES ($1, $2, now())
        ON CONFLICT (id) DO UPDATE SET civicrm_linked_at = now()
        """,
        member_id, username,
    )


# ── Retention ─────────────────────────────────────────────────────────────────

# A day's expired messages and their cascaded edits can take far longer than the pool's 10s default
PRUNE_TIMEOUT = 600


async def prune(pool, days=RETENTION_DAYS):
    """Deletes snapshots and messages older than `days`. Returns (snapshots, messages) removed."""
    snapshots = await pool.execute(
        "DELETE FROM server_snapshots WHERE taken_at < now() - make_interval(days => $1)", days,
        timeout=PRUNE_TIMEOUT,
    )
    messages = await pool.execute(
        "DELETE FROM message_log WHERE created_at < now() - make_interval(days => $1)", days,
        timeout=PRUNE_TIMEOUT,
    )
    # execute() returns the command tag, e.g. "DELETE 3"
    return int(snapshots.split()[-1]), int(messages.split()[-1])
