"""Posting message edits and deletions to the moderators' log channel (MESSAGE_LOG_CHANNEL_ID)."""
import datetime
import itertools
import types
import discord
import pytest
import db
import main
import modlog

NOW = datetime.datetime.now(datetime.UTC)
GUILD = main.PAUSEAI_SERVER_ID
OTHER_GUILD = 999
LOG_CHANNEL = 5000
GENERAL = 111
AUTHOR = 222
BOT_ID = 333
OTHER_BOT = 444
POST_IDS = itertools.count(1_000_000)


def not_found():
    return discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone")


class FakePost:
    def __init__(self, channel):
        self.id, self.channel = next(POST_IDS), channel

    async def delete(self):
        self.channel.deleted.append(self.id)


class FakeChannel:
    def __init__(self, guild_id=GUILD):
        self.id = LOG_CHANNEL
        self.guild = types.SimpleNamespace(id=guild_id)
        self.sent = []      # (embeds, allowed_mentions, post)
        self.deleted = []   # post IDs
        self.gone = set()   # post IDs whose delete raises NotFound
        self.failing = set()  # post IDs whose delete raises another error

    async def send(self, *, embeds, allowed_mentions):
        post = FakePost(self)
        self.sent.append((embeds, allowed_mentions, post))
        return post

    def get_partial_message(self, post_id):
        channel = self

        class Partial:
            async def delete(self):
                if post_id in channel.gone:
                    raise not_found()
                if post_id in channel.failing:
                    raise discord.HTTPException(types.SimpleNamespace(status=500, reason="oops"), "oops")
                channel.deleted.append(post_id)
        return Partial()


@pytest.fixture
def log_channel(monkeypatch, pool):
    channel = FakeChannel()
    monkeypatch.setattr(main, "db_pool", pool)
    monkeypatch.setattr(main, "DEVELOPER_MODE", False)
    monkeypatch.setattr(main, "MESSAGE_LOG_CHANNEL_ID", LOG_CHANNEL)
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", set())
    monkeypatch.setattr(main.bot, "get_channel", lambda cid: channel if cid == LOG_CHANNEL else None)
    monkeypatch.setattr(main.bot, "get_partial_messageable", lambda cid: channel)
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: None)
    users = {OTHER_BOT: types.SimpleNamespace(bot=True)}
    monkeypatch.setattr(main.bot, "get_user", users.get)
    monkeypatch.setattr(main.bot._connection, "user", types.SimpleNamespace(id=BOT_ID))
    return channel


def posted(channel):
    """Every embed posted, in order."""
    return [embed for embeds, _, _ in channel.sent for embed in embeds]


async def log(pool, message_id, content="hello", **overrides):
    fields = dict(message_id=message_id, guild_id=GUILD, channel_id=GENERAL, author_id=AUTHOR,
                  content=content, attachments=[], created_at=NOW - datetime.timedelta(hours=1))
    fields.update(overrides)
    await db.log_message(pool, **fields)


def snowflake(when):
    return discord.utils.time_snowflake(when)


def edit_event(message_id, content, author_id=AUTHOR, bot=False, guild_id=GUILD, channel_id=GENERAL):
    return discord.RawMessageUpdateEvent({
        "id": str(message_id), "channel_id": str(channel_id), "guild_id": str(guild_id), "content": content,
        "author": {"id": str(author_id), "bot": bot}, "attachments": [],
        "timestamp": (NOW - datetime.timedelta(hours=1)).isoformat(), "edited_timestamp": NOW.isoformat(),
    })


def delete_event(message_id, guild_id=GUILD, channel_id=GENERAL):
    return discord.RawMessageDeleteEvent({"id": str(message_id), "channel_id": str(channel_id),
                                          "guild_id": str(guild_id)})


def bulk_event(message_ids, channel_id=GENERAL):
    return discord.RawBulkMessageDeleteEvent({"ids": [str(i) for i in message_ids],
                                              "channel_id": str(channel_id), "guild_id": str(GUILD)})


def cached_message(message_id, content, author_id=AUTHOR, bot=False, created_at=None):
    attachment = types.SimpleNamespace(to_dict=lambda: {"id": "9", "filename": "spam.png"})
    return types.SimpleNamespace(
        id=message_id, content=content, guild=types.SimpleNamespace(id=GUILD),
        channel=types.SimpleNamespace(id=GENERAL), author=types.SimpleNamespace(id=author_id, bot=bot),
        attachments=[attachment], created_at=created_at or NOW,
    )


# ── Edits ─────────────────────────────────────────────────────────────────────

async def test_an_edit_posts_author_channel_old_and_new_text_and_a_link(log_channel, pool):
    await log(pool, 1, content="before text")
    await main.on_raw_message_edit(edit_event(1, "after text"))
    [embed] = posted(log_channel)
    assert embed.title == "Message edited"
    for part in (f"<@{AUTHOR}>", f"<#{GENERAL}>", "before text", "after text",
                 f"https://discord.com/channels/{GUILD}/{GENERAL}/1"):
        assert part in embed.description


async def test_log_posts_never_ping(log_channel, pool):
    await log(pool, 1, content="hi")
    await main.on_raw_message_edit(edit_event(1, "@everyone <@&123> <@456>"))
    [(_, allowed, _)] = log_channel.sent
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


async def test_an_embed_only_update_posts_nothing(log_channel, pool):
    await log(pool, 1, content="same")
    await main.on_raw_message_edit(edit_event(1, "same"))
    assert log_channel.sent == []


async def test_a_bots_edit_posts_nothing(log_channel, pool):
    await log(pool, 1, content="old", author_id=OTHER_BOT, author_bot=True)
    await main.on_raw_message_edit(edit_event(1, "new", author_id=OTHER_BOT, bot=True))
    # Payload without the bot flag: the user cache still knows
    await log(pool, 2, content="old", author_id=OTHER_BOT)
    await main.on_raw_message_edit(edit_event(2, "new", author_id=OTHER_BOT))
    assert log_channel.sent == []


async def test_nothing_is_posted_while_the_channel_is_unset(log_channel, pool, monkeypatch):
    monkeypatch.setattr(main, "MESSAGE_LOG_CHANNEL_ID", None)
    await log(pool, 1, content="old")
    await log(pool, 2)
    await main.on_raw_message_edit(edit_event(1, "new"))
    await main.on_raw_message_delete(delete_event(2))
    assert log_channel.sent == []
    # The message log itself still records both
    assert (await db.get_message(pool, 1))["content"] == "new"
    assert (await db.get_message(pool, 2))["deleted_at"] is not None


async def test_events_in_the_log_channel_itself_post_nothing(log_channel, pool):
    await log(pool, 1, content="old", channel_id=LOG_CHANNEL)
    await main.on_raw_message_edit(edit_event(1, "new", channel_id=LOG_CHANNEL))
    await main.on_raw_message_delete(delete_event(1, channel_id=LOG_CHANNEL))
    assert log_channel.sent == []


async def test_a_log_channel_in_another_server_gets_nothing(log_channel, pool):
    log_channel.guild.id = OTHER_GUILD
    await log(pool, 1, content="old")
    await main.on_raw_message_edit(edit_event(1, "new"))
    assert log_channel.sent == []


# ── Quoted text can't pass for the post's own lines ───────────────────────────

FORGERY = "nice\n**Author:** <@999>\n**After**\nsomething kind\n```\nunclosed"


async def test_quoted_text_is_fenced_so_it_cannot_forge_lines(log_channel, pool):
    await log(pool, 1, content="plain")
    await main.on_raw_message_edit(edit_event(1, FORGERY))
    [embed] = posted(log_channel)
    after = embed.description.split("**After**\n", 1)[1]
    # The whole quoted text sits inside one code block, and its own backticks can't close it
    assert after.startswith("```\n") and after.endswith("\n```")
    assert after[3:-3].count("```") == 0
    assert embed.description.count("**After**") == 2  # the post's own heading, and the quoted copy inside the block
    assert embed.description.index("**After**") < embed.description.index("```\nnice")


def test_quoting_keeps_backtick_text_inside_the_block_when_truncated():
    text = "`" * 5000
    quoted = modlog.quoted(text, 1800)
    assert modlog.utf16_len(quoted) <= 1800
    block = quoted.split("\n```", 1)[0]
    assert "```" not in block[4:] and "(truncated, 5000 characters in all)" in quoted


def test_attachment_names_are_escaped():
    row = {"message_id": 1, "channel_id": GENERAL, "author_id": AUTHOR, "content": "",
           "created_at": NOW, "attachments": [{"filename": "**Author**_x_.png"}]}
    assert "\\*\\*Author\\*\\*" in modlog.delete_embed(row, NOW).description


# ── Deletions ─────────────────────────────────────────────────────────────────

async def test_a_deletion_posts_the_logged_text_attachments_and_when_it_was_sent(log_channel, pool):
    sent = NOW - datetime.timedelta(days=3)
    await log(pool, 1, content="photo caption", created_at=sent,
              attachments=[{"id": 9, "filename": "march.jpg"}, {"id": 10, "filename": "banner.png"}])
    await main.on_raw_message_delete(delete_event(1))
    [embed] = posted(log_channel)
    assert embed.title == "Message deleted"
    for part in (f"<@{AUTHOR}>", f"<#{GENERAL}>", "photo caption", "march.jpg, banner.png",
                 f"<t:{int(sent.timestamp())}:f>"):
        assert part in embed.description


async def test_a_deletion_shows_the_text_as_last_edited(log_channel, pool):
    await log(pool, 1, content="first")
    await db.log_edit(pool, 1, "second", NOW)
    await main.on_raw_message_delete(delete_event(1))
    [embed] = posted(log_channel)
    assert "second" in embed.description and "first" not in embed.description


async def test_a_bots_deletion_posts_nothing(log_channel, pool):
    await log(pool, 1, author_id=OTHER_BOT, author_bot=True)
    # Logged before author_bot existed: known as a bot from the user cache
    await log(pool, 2, author_id=OTHER_BOT)
    await main.on_raw_message_delete(delete_event(1))
    await main.on_raw_message_delete(delete_event(2))
    assert log_channel.sent == []


async def test_a_deletion_the_log_never_had_posts_nothing(log_channel, pool):
    await main.on_raw_message_delete(delete_event(404))
    assert log_channel.sent == []


async def test_a_bulk_deletion_posts_each_logged_message_and_counts_the_rest(log_channel, pool):
    for i in range(1, 13):
        await log(pool, i, content=f"message {i}", created_at=NOW.replace(hour=12) - datetime.timedelta(minutes=20 - i))
    await log(pool, 13, author_id=OTHER_BOT, author_bot=True)
    await main.on_raw_bulk_message_delete(bulk_event([*range(1, 14), 404, 405]))
    embeds = posted(log_channel)
    assert [e.title for e in embeds] == ["Message deleted"] * 12 + ["2 more messages deleted"]
    # Oldest first, and split so no post carries more than Discord allows
    assert "message 1\n" in embeds[0].description and "message 12" in embeds[11].description
    assert all(len(embeds) <= 10 and sum(modlog.embed_size(e) for e in embeds) <= 6000
               for embeds, _, _ in log_channel.sent)


async def test_a_deletion_that_beats_its_log_write_posts_from_the_cache_and_is_logged_deleted(log_channel, pool):
    event = delete_event(7)
    event.cached_message = cached_message(7, "deleted at once")
    await main.on_raw_message_delete(event)
    [embed] = posted(log_channel)
    assert "deleted at once" in embed.description and "spam.png" in embed.description
    # on_message's background write landing afterwards doesn't make it live again
    await log(pool, 7, content="deleted at once")
    row = await db.get_message(pool, 7)
    assert row["deleted_at"] is not None and row["attachments"][0]["filename"] == "spam.png"


async def test_the_cache_never_posts_or_logs_a_message_past_retention(log_channel, pool):
    event = delete_event(7)
    event.cached_message = cached_message(7, "40 days old", created_at=NOW - datetime.timedelta(days=40))
    await main.on_raw_message_delete(event)
    assert log_channel.sent == []
    assert await db.get_message(pool, 7) is None


async def test_a_cached_bots_deletion_posts_nothing(log_channel, pool):
    event = delete_event(7)
    event.cached_message = cached_message(7, "bot text", author_id=OTHER_BOT, bot=True)
    await main.on_raw_message_delete(event)
    assert log_channel.sent == []


async def test_a_bulk_deletion_uses_the_cache_for_messages_not_yet_logged(log_channel, pool):
    await log(pool, 1, content="logged")
    event = bulk_event([1, 2, 3])
    event.cached_messages = [cached_message(2, "cached"), cached_message(1, "stale cache copy")]
    await main.on_raw_bulk_message_delete(event)
    texts = [e.description for e in posted(log_channel)]
    assert any("logged" in t for t in texts) and any("cached" in t for t in texts)
    assert not any("stale cache copy" in t for t in texts)
    assert posted(log_channel)[-1].title == "1 more message deleted"


# ── Discord's limits ──────────────────────────────────────────────────────────

def test_truncate_marks_cut_text_and_keeps_to_the_limit():
    assert modlog.truncate("short", 100) == "short"
    cut = modlog.truncate("x" * 5000, 1800)
    assert len(cut) == 1800 and cut.endswith("(truncated, 5000 characters in all)")


@pytest.mark.parametrize("char", ["y", "😀"])  # an emoji is two UTF-16 units
def test_the_longest_messages_fit_one_embed(char):
    text = char * 4000  # Discord's limit for a message with Nitro
    edit = modlog.edit_embed(guild_id=GUILD, channel_id=GENERAL, message_id=2**63 - 1, author_id=2**63 - 1,
                             before=text, after=text, edited_at=NOW, parent_id=2**63 - 1)
    row = {"message_id": 2**63 - 1, "channel_id": 2**63 - 1, "author_id": 2**63 - 1, "content": text,
           "created_at": NOW, "attachments": [{"filename": "😀" * 200}] * 10}
    delete = modlog.delete_embed(row, NOW, parent_id=2**63 - 1)
    for embed in (edit, delete):
        assert modlog.utf16_len(embed.description) <= modlog.DESCRIPTION_LIMIT
        assert modlog.embed_size(embed) <= modlog.MESSAGE_EMBED_CHARS
        assert "truncated" in embed.description


def test_cutting_never_splits_an_emoji():
    assert modlog.cut_utf16("a😀b", 2) == "a"
    assert modlog.cut_utf16("a😀b", 3) == "a😀"


def test_an_empty_message_says_so():
    row = {"message_id": 1, "channel_id": GENERAL, "author_id": AUTHOR, "content": "",
           "created_at": NOW, "attachments": [{"filename": "a.png"}]}
    assert "*(no text)*" in modlog.delete_embed(row, NOW).description


def test_a_thread_names_its_parent_channel():
    assert modlog.where(7, 8) == "<#7> in <#8>"


def test_posts_only_bundle_messages_sent_on_the_same_day():
    day1, day2 = datetime.datetime(2026, 11, 1, 23, tzinfo=datetime.UTC), datetime.datetime(2026, 11, 2, 1, tzinfo=datetime.UTC)
    embed = discord.Embed(description="x")
    groups = modlog.chunk([(embed, day1), (embed, day1), (embed, day2)])
    assert [len(g) for g in groups] == [2, 1]


# ── Retention ─────────────────────────────────────────────────────────────────

async def test_each_post_is_recorded_with_its_oldest_quoted_message(log_channel, pool):
    sent = NOW - datetime.timedelta(days=3)
    await log(pool, 1, created_at=sent)
    await main.on_raw_message_delete(delete_event(1))
    [(_, _, post)] = log_channel.sent
    assert [r["post_id"] for r in await db.expired_log_posts(pool, sent + datetime.timedelta(seconds=1))] == [post.id]
    assert await db.expired_log_posts(pool, sent) == []


async def test_an_edit_post_expires_with_the_edited_message(log_channel, pool):
    sent = NOW - datetime.timedelta(days=3)
    message_id = snowflake(sent)
    await log(pool, message_id, content="old", created_at=sent)
    await main.on_raw_message_edit(edit_event(message_id, "new"))
    [(_, _, post)] = log_channel.sent
    assert [r["post_id"] for r in await db.expired_log_posts(pool, sent + datetime.timedelta(seconds=1))] == [post.id]


async def test_a_post_that_cannot_be_recorded_is_taken_down(log_channel, pool, monkeypatch):
    async def broken(*args):
        raise RuntimeError("database down")
    monkeypatch.setattr(main.db, "record_log_post", broken)
    await log(pool, 1)
    await main.on_raw_message_delete(delete_event(1))
    [(_, _, post)] = log_channel.sent
    assert log_channel.deleted == [post.id]


async def test_prune_deletes_posts_whose_oldest_quoted_message_passed_retention(log_channel, pool):
    days = lambda n: NOW - datetime.timedelta(days=n)
    for post_id, oldest in ((1, days(31)), (2, days(29)), (3, days(400)), (4, days(31)), (5, days(31))):
        await db.record_log_post(pool, post_id, LOG_CHANNEL, oldest)
    log_channel.gone = {4}       # a moderator deleted it already
    log_channel.failing = {5}    # Discord errored: kept to try again
    assert await main.prune_log_channel(now=NOW) == 2
    assert sorted(log_channel.deleted) == [1, 3]
    cutoff = days(30)
    assert [r["post_id"] for r in await db.expired_log_posts(pool, cutoff)] == [5]


# ── Settings ──────────────────────────────────────────────────────────────────

def test_channel_id_from_env(monkeypatch):
    for value, expected in ((None, None), (" ", None), ("1234", 1234), (" 1234 ", 1234), ("-5", None),
                            ("+5", None), ("1_000", None), ("https://discord.com/channels/1/2", None),
                            ("12 34", None)):
        if value is None:
            monkeypatch.delenv("MESSAGE_LOG_CHANNEL_ID", raising=False)
        else:
            monkeypatch.setenv("MESSAGE_LOG_CHANNEL_ID", value)
        assert main.channel_id_from_env("MESSAGE_LOG_CHANNEL_ID") == expected, value


def test_ids_from_env(monkeypatch):
    monkeypatch.delenv("MESSAGE_LOG_IGNORE_IDS", raising=False)
    assert main.ids_from_env("MESSAGE_LOG_IGNORE_IDS") == set()
    monkeypatch.setenv("MESSAGE_LOG_IGNORE_IDS", " 12, 34 ,,56 78\n90 ")
    assert main.ids_from_env("MESSAGE_LOG_IGNORE_IDS") == {12, 34, 56, 78, 90}
    # Any entry that isn't an ID makes the whole list unusable, rather than shrinking it
    for bad in ("12, staff", "<#12>", "12, -5", "12, +6", "١٢"):
        monkeypatch.setenv("MESSAGE_LOG_IGNORE_IDS", bad)
        assert main.ids_from_env("MESSAGE_LOG_IGNORE_IDS") is None, bad


async def test_a_malformed_ignore_list_turns_posting_off(log_channel, pool, monkeypatch):
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", None)
    await log(pool, 1, content="old")
    await log(pool, 2)
    await main.on_raw_message_edit(edit_event(1, "new"))
    await main.on_raw_message_delete(delete_event(2))
    assert log_channel.sent == []
    assert (await db.get_message(pool, 2))["deleted_at"] is not None


# ── Internal channels left out ────────────────────────────────────────────────

STAFF_CATEGORY = 7000
STAFF_CHANNEL = 7001
MEMBER_CATEGORY = 8000
ARCHIVED_THREAD = 7002


class FakeThread(discord.Thread):
    def __init__(self, thread_id, parent_id):
        self.id, self.parent_id = thread_id, parent_id

    @property
    def category_id(self):
        # The real property raises when the parent isn't cached
        raise discord.ClientException("Parent channel not found")


class FakeGuild:
    def __init__(self):
        self.cached = {
            GENERAL: types.SimpleNamespace(id=GENERAL, category_id=MEMBER_CATEGORY),
            STAFF_CHANNEL: types.SimpleNamespace(id=STAFF_CHANNEL, category_id=STAFF_CATEGORY),
            9001: FakeThread(9001, STAFF_CHANNEL),
            9002: FakeThread(9002, GENERAL),
            9003: FakeThread(9003, 404),  # parent not cached
        }
        # Archived threads usually aren't cached; Discord still answers for them
        self.remote = {ARCHIVED_THREAD: FakeThread(ARCHIVED_THREAD, STAFF_CHANNEL)}

    def get_channel_or_thread(self, channel_id):
        return self.cached.get(channel_id)

    def get_channel(self, channel_id):
        channel = self.cached.get(channel_id)
        return None if isinstance(channel, discord.Thread) else channel

    async def fetch_channel(self, channel_id):
        if channel_id not in self.remote:
            raise not_found()
        return self.remote[channel_id]


@pytest.fixture
def ignoring_staff(log_channel, monkeypatch):
    guild = FakeGuild()
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: guild if gid == GUILD else None)
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", {STAFF_CATEGORY})
    return log_channel


@pytest.mark.parametrize("channel_id", [STAFF_CHANNEL, 9001, ARCHIVED_THREAD, 404, 9003])
async def test_internal_channels_their_threads_and_unknown_channels_post_nothing(ignoring_staff, pool, channel_id):
    await log(pool, 1, content="internal", channel_id=channel_id)
    await log(pool, 2, content="old", channel_id=channel_id)
    await main.on_raw_message_delete(delete_event(1, channel_id=channel_id))
    await main.on_raw_message_edit(edit_event(2, "new", channel_id=channel_id))
    assert ignoring_staff.sent == []
    # The message log itself still records them
    assert (await db.get_message(pool, 1))["deleted_at"] is not None


@pytest.mark.parametrize("channel_id", [GENERAL, 9002])
async def test_member_channels_and_their_threads_still_post(ignoring_staff, pool, channel_id):
    await log(pool, 1, content="member text", channel_id=channel_id)
    await main.on_raw_message_delete(delete_event(1, channel_id=channel_id))
    [embed] = posted(ignoring_staff)
    assert "member text" in embed.description


@pytest.mark.parametrize("channel_id", [GENERAL, 9002])
async def test_a_listed_channel_leaves_out_it_and_its_threads_but_not_its_category(
        ignoring_staff, pool, monkeypatch, channel_id):
    # A mixed category (like TEAMS): one channel listed, the rest still logged
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", {GENERAL})
    await log(pool, 1, channel_id=channel_id)
    await main.on_raw_message_delete(delete_event(1, channel_id=channel_id))
    assert ignoring_staff.sent == []
    await log(pool, 2, content="same category", channel_id=STAFF_CHANNEL)
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", {1})
    await main.on_raw_message_delete(delete_event(2, channel_id=STAFF_CHANNEL))
    assert len(posted(ignoring_staff)) == 1


async def test_a_listed_thread_is_left_out(ignoring_staff, pool, monkeypatch):
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_IDS", {9002})
    await log(pool, 1, channel_id=9002)
    await main.on_raw_message_delete(delete_event(1, channel_id=9002))
    assert ignoring_staff.sent == []
