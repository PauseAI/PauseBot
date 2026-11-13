import datetime
import asyncpg
import pytest
import db
import members
from jobs import copy_airtable_members

NOW = datetime.datetime.now(datetime.UTC)
GUILD = 1100491867675709580


async def log(pool, message_id, content="hello", created_at=NOW, **overrides):
    fields = dict(
        message_id=message_id, guild_id=GUILD, channel_id=111, author_id=222,
        content=content, attachments=[], created_at=created_at,
    )
    fields.update(overrides)
    await db.log_message(pool, **fields)


# ── Schema ────────────────────────────────────────────────────────────────────

async def test_schema_is_idempotent(pool):
    await db.apply_schema(pool)
    await db.apply_schema(pool)
    tables = await pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    assert [t["tablename"] for t in tables] == ["discord_members", "message_edits", "message_log", "server_snapshots"]


async def test_discord_ids_fit_in_bigint(pool):
    # Snowflakes are up to 2^63 - 1; ints this large would overflow a plain integer column
    big = 2**63 - 1
    await log(pool, big, guild_id=big, channel_id=big, author_id=big)
    row = await db.get_message(pool, big)
    assert row["author_id"] == big


async def test_edits_need_a_logged_message(pool):
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            "INSERT INTO message_edits (message_id, content_before, edited_at) VALUES (1, 'x', now())"
        )


# ── Snapshots ─────────────────────────────────────────────────────────────────

async def test_snapshot_round_trips_jsonb(pool):
    data = {"guild": {"id": GUILD, "name": "PauseAI"}, "roles": [{"id": 1, "name": "Mod"}], "bans": None}
    snapshot_id = await db.save_snapshot(pool, GUILD, data)
    latest = await db.latest_snapshot(pool, GUILD)
    assert latest["id"] == snapshot_id
    assert latest["data"] == data
    assert await pool.fetchval("SELECT jsonb_typeof(data) FROM server_snapshots") == "object"


async def test_latest_snapshot_is_newest_for_that_guild(pool):
    await db.save_snapshot(pool, GUILD, {"n": 1})
    newest = await db.save_snapshot(pool, GUILD, {"n": 2})
    await db.save_snapshot(pool, 999, {"n": 3})
    await pool.execute("UPDATE server_snapshots SET taken_at = now() - interval '1 day' WHERE id <> $1", newest)
    assert (await db.latest_snapshot(pool, GUILD))["data"] == {"n": 2}
    assert await db.latest_snapshot(pool, 12345) is None


# ── Message log ───────────────────────────────────────────────────────────────

async def test_log_message_stores_fields_and_ignores_duplicates(pool):
    attachments = [{"id": 5, "filename": "a.png", "url": "https://cdn.example/a.png", "content_type": "image/png", "size": 10}]
    await log(pool, 1, content="first", attachments=attachments)
    await log(pool, 1, content="replayed by the gateway")
    row = await db.get_message(pool, 1)
    assert row["content"] == "first"
    assert row["attachments"] == attachments
    assert row["created_at"] == NOW
    assert row["edited_at"] is None and row["deleted_at"] is None


async def test_log_edit_keeps_history(pool):
    await log(pool, 1, content="v1")
    t1, t2 = NOW + datetime.timedelta(minutes=1), NOW + datetime.timedelta(minutes=2)
    assert await db.log_edit(pool, 1, "v2", t1) == "v1"
    assert await db.log_edit(pool, 1, "v3", t2) == "v2"
    row = await db.get_message(pool, 1)
    assert (row["content"], row["edited_at"]) == ("v3", t2)
    assert [(e["content_before"], e["edited_at"]) for e in await db.get_edits(pool, 1)] == [("v1", t1), ("v2", t2)]


async def test_log_edit_skips_unchanged_and_unknown_messages(pool):
    await log(pool, 1, content="same")
    assert await db.log_edit(pool, 1, "same", NOW) is None
    assert await db.log_edit(pool, 404, "anything", NOW) is None
    assert await db.get_edits(pool, 1) == []
    assert await pool.fetchval("SELECT count(*) FROM message_log") == 1


async def test_log_delete_marks_once_and_returns_logged_rows(pool):
    await log(pool, 1, content="one")
    await log(pool, 2, content="two")
    deleted = await db.log_delete(pool, {1, 2, 404}, NOW)
    assert sorted((r["message_id"], r["content"]) for r in deleted) == [(1, "one"), (2, "two")]
    assert (await db.get_message(pool, 1))["deleted_at"] == NOW
    # A second delete event for the same message changes nothing
    assert await db.log_delete(pool, [1], NOW + datetime.timedelta(hours=1)) == []
    assert (await db.get_message(pool, 1))["deleted_at"] == NOW


# ── Retention ─────────────────────────────────────────────────────────────────

async def test_prune_removes_only_rows_older_than_30_days(pool):
    old = NOW - datetime.timedelta(days=31)
    recent = NOW - datetime.timedelta(days=29)
    await log(pool, 1, created_at=old)
    await log(pool, 2, created_at=recent)
    await db.log_edit(pool, 1, "edited", NOW)

    old_snapshot = await db.save_snapshot(pool, GUILD, {"n": "old"})
    await db.save_snapshot(pool, GUILD, {"n": "recent"})
    await pool.execute("UPDATE server_snapshots SET taken_at = $2 WHERE id = $1", old_snapshot, old)

    assert await db.prune(pool) == (1, 1)
    assert await db.get_message(pool, 1) is None
    assert await db.get_message(pool, 2) is not None
    # Edits go with their message
    assert await pool.fetchval("SELECT count(*) FROM message_edits") == 0
    assert (await db.latest_snapshot(pool, GUILD))["data"] == {"n": "recent"}
    assert await db.prune(pool) == (0, 0)


# ── Edit arriving before its message is logged ────────────────────────────────

def fields(**overrides):
    base = dict(guild_id=GUILD, channel_id=111, author_id=222, attachments=[], created_at=NOW)
    base.update(overrides)
    return base


async def test_edit_before_insert_stores_edited_content(pool):
    edited_at = NOW + datetime.timedelta(seconds=1)
    assert await db.log_edit(pool, 1, "edited", edited_at, message=fields()) is None
    # The on_message insert lands afterwards with the original text and must not overwrite the edit
    await log(pool, 1, content="original")
    row = await db.get_message(pool, 1)
    assert (row["content"], row["edited_at"], row["created_at"]) == ("edited", edited_at, NOW)
    assert await db.get_edits(pool, 1) == []


async def test_edit_with_message_fields_still_records_history_when_logged(pool):
    await log(pool, 1, content="v1")
    assert await db.log_edit(pool, 1, "v2", NOW, message=fields()) == "v1"
    assert [e["content_before"] for e in await db.get_edits(pool, 1)] == ["v1"]



async def test_out_of_order_edit_does_not_overwrite_newer(pool):
    await log(pool, 1, content="v1")
    t1, t2 = NOW + datetime.timedelta(seconds=1), NOW + datetime.timedelta(seconds=2)
    assert await db.log_edit(pool, 1, "v3", t2) == "v1"
    assert await db.log_edit(pool, 1, "v2", t1) is None
    row = await db.get_message(pool, 1)
    assert (row["content"], row["edited_at"]) == ("v3", t2)
    assert [e["content_before"] for e in await db.get_edits(pool, 1)] == ["v1"]


async def test_unfurl_before_insert_is_not_marked_edited(pool):
    # Discord's link-unfurl MESSAGE_UPDATE has the full message but no edited_timestamp
    assert await db.log_edit(pool, 1, "see https://pauseai.info", None, message=fields()) is None
    row = await db.get_message(pool, 1)
    assert row["content"] == "see https://pauseai.info" and row["edited_at"] is None


async def test_latest_snapshot_time(pool):
    assert await db.latest_snapshot_time(pool, GUILD) is None
    await db.save_snapshot(pool, GUILD, {"n": 1})
    latest = await db.latest_snapshot(pool, GUILD)
    assert await db.latest_snapshot_time(pool, GUILD) == latest["taken_at"]


# ── Discord members ───────────────────────────────────────────────────────────

async def save(pool, member_id=1, **overrides):
    fields = dict(member_id=member_id, username="ann", nick=None, global_name="Ann", joined_at=NOW,
                  role_ids=[10, 11], role_names=["UK", "Volunteer"])
    fields.update(overrides)
    await db.save_member(pool, **fields)


async def test_save_member_creates_then_updates(pool):
    assert await db.saved_roles(pool, 1) is None
    await save(pool)
    await save(pool, username="ann2", role_ids=[12], role_names=["Germany"])
    row = await pool.fetchrow("SELECT * FROM discord_members WHERE id = 1")
    assert row["username"] == "ann2"
    assert await db.saved_roles(pool, 1) == ([12], ["Germany"])


async def test_leaving_saves_roles_and_rejoining_clears_left_at(pool):
    await save(pool)
    await db.mark_left(pool, 1, NOW, [10, 11, 13], ["UK", "Volunteer", "Wants to volunteer"])
    row = await pool.fetchrow("SELECT left_at, role_ids FROM discord_members WHERE id = 1")
    assert row["left_at"] == NOW and row["role_ids"] == [10, 11, 13]
    await save(pool)
    assert await pool.fetchval("SELECT left_at FROM discord_members WHERE id = 1") is None


async def test_onboarding_notification_only_turns_on(pool):
    await save(pool, onboarding_notification=True)
    await save(pool)
    assert await pool.fetchval("SELECT onboarding_notification FROM discord_members WHERE id = 1") is True


async def test_set_flag_updates_only_existing_records(pool):
    await save(pool, 1)
    await save(pool, 2)
    assert await db.set_flag(pool, "discord_reaction", [1, 3]) == 1
    flags = await pool.fetch("SELECT id, discord_reaction FROM discord_members ORDER BY id")
    assert [(r["id"], r["discord_reaction"]) for r in flags] == [(1, True), (2, False)]


async def test_set_flag_refuses_other_columns(pool):
    with pytest.raises(ValueError):
        await db.set_flag(pool, "username", [1])


async def test_linking_survives_resaves(pool):
    await save(pool, 1)
    await save(pool, 2)
    await db.mark_linked(pool, 1, "ann")
    # A member the bot has no record of yet still gets one
    await db.mark_linked(pool, 3, "cat")
    await save(pool, 1)
    assert await db.linked_member_ids(pool) == {1, 3}


async def test_copy_from_airtable_keeps_the_link(pool):
    row = members.from_airtable({"id": "1", "username": "ann", "role_ids": ["10"], "role_names": ["UK"],
                                 "discord_reaction": True})
    await copy_airtable_members.copy_row(pool, row)
    await db.mark_linked(pool, 1, "ann")
    await copy_airtable_members.copy_row(pool, row)
    copied = await pool.fetchrow("SELECT * FROM discord_members WHERE id = 1")
    assert copied["role_ids"] == [10] and copied["discord_reaction"] is True
    assert copied["civicrm_linked_at"] is not None
