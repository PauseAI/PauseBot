"""Posting message edits and deletions to the moderators' log channel (MESSAGE_LOG_CHANNEL_ID)."""
import datetime
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


class FakeChannel:
    def __init__(self, guild_id=GUILD, history=()):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.sent = []
        self._history = list(history)
        self.history_args = None

    async def send(self, *, embeds, allowed_mentions):
        self.sent.append((embeds, allowed_mentions))

    def history(self, **kwargs):
        self.history_args = kwargs

        async def gen():
            for message in self._history:
                yield message
        return gen()


class FakeMessage:
    def __init__(self, author_id):
        self.author = types.SimpleNamespace(id=author_id)
        self.deleted = False

    async def delete(self):
        self.deleted = True


@pytest.fixture
def log_channel(monkeypatch, pool):
    channel = FakeChannel()
    monkeypatch.setattr(main, "db_pool", pool)
    monkeypatch.setattr(main, "DEVELOPER_MODE", False)
    monkeypatch.setattr(main, "MESSAGE_LOG_CHANNEL_ID", LOG_CHANNEL)
    monkeypatch.setattr(main.bot, "get_channel", lambda cid: channel if cid == LOG_CHANNEL else None)
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: None)
    users = {OTHER_BOT: types.SimpleNamespace(bot=True)}
    monkeypatch.setattr(main.bot, "get_user", users.get)
    monkeypatch.setattr(main.bot._connection, "user", types.SimpleNamespace(id=BOT_ID))
    return channel


def posted(channel):
    """Every embed posted, in order."""
    return [embed for embeds, _ in channel.sent for embed in embeds]


async def log(pool, message_id, content="hello", **overrides):
    fields = dict(message_id=message_id, guild_id=GUILD, channel_id=GENERAL, author_id=AUTHOR,
                  content=content, attachments=[], created_at=NOW - datetime.timedelta(hours=1))
    fields.update(overrides)
    await db.log_message(pool, **fields)


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
    [(_, allowed)] = log_channel.sent
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


async def test_an_embed_only_update_posts_nothing(log_channel, pool):
    await log(pool, 1, content="same")
    await main.on_raw_message_edit(edit_event(1, "same"))
    assert log_channel.sent == []


async def test_a_bots_edit_posts_nothing(log_channel, pool):
    await log(pool, 1, content="old", author_id=OTHER_BOT, author_bot=True)
    await main.on_raw_message_edit(edit_event(1, "new", author_id=OTHER_BOT, bot=True))
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
        await log(pool, i, content=f"message {i}", created_at=NOW - datetime.timedelta(minutes=20 - i))
    await log(pool, 13, author_id=OTHER_BOT, author_bot=True)
    await main.on_raw_bulk_message_delete(bulk_event([*range(1, 14), 404, 405]))
    embeds = posted(log_channel)
    assert [e.title for e in embeds] == ["Message deleted"] * 12 + ["2 more messages deleted"]
    # Oldest first, and split so no post carries more than Discord allows
    assert "message 1\n" in embeds[0].description + "\n" and "message 12" in embeds[11].description
    assert all(len(embeds) <= 10 and sum(len(e) for e in embeds) <= 6000 for embeds, _ in log_channel.sent)


# ── Discord's limits ──────────────────────────────────────────────────────────

def test_truncate_marks_cut_text_and_keeps_to_the_limit():
    assert modlog.truncate("short", 100) == "short"
    cut = modlog.truncate("x" * 5000, 1800)
    assert len(cut) == 1800 and cut.endswith("(truncated, 5000 characters in all)")


def test_the_longest_messages_fit_one_embed():
    text = "y" * 4000  # Discord's limit for a message with Nitro
    edit = modlog.edit_embed(guild_id=GUILD, channel_id=GENERAL, message_id=2**63 - 1, author_id=2**63 - 1,
                             before=text, after=text, edited_at=NOW, parent_id=2**63 - 1)
    row = {"message_id": 2**63 - 1, "channel_id": 2**63 - 1, "author_id": 2**63 - 1, "content": text,
           "created_at": NOW, "attachments": [{"filename": "f" * 200}] * 10}
    delete = modlog.delete_embed(row, NOW, parent_id=2**63 - 1)
    for embed in (edit, delete):
        assert len(embed.description) <= modlog.DESCRIPTION_LIMIT
        assert len(embed) <= modlog.MESSAGE_EMBED_CHARS
        assert "truncated" in embed.description


def test_an_empty_message_says_so():
    row = {"message_id": 1, "channel_id": GENERAL, "author_id": AUTHOR, "content": "",
           "created_at": NOW, "attachments": [{"filename": "a.png"}]}
    assert "*(no text)*" in modlog.delete_embed(row, NOW).description


def test_a_thread_names_its_parent_channel():
    assert modlog.where(7, 8) == "<#7> in <#8>"


# ── Retention ─────────────────────────────────────────────────────────────────

def snowflake(when):
    return discord.utils.time_snowflake(when)


async def test_prune_deletes_the_bots_posts_quoting_messages_sent_over_30_days_ago(log_channel, pool):
    def post(author_id, posted_days_ago, *quoted_days_ago):
        rows = [{"message_id": snowflake(NOW - datetime.timedelta(days=d)), "channel_id": GENERAL,
                 "author_id": AUTHOR, "content": "x", "created_at": NOW, "attachments": []} for d in quoted_days_ago]
        message = FakeMessage(author_id)
        message.created_at = NOW - datetime.timedelta(days=posted_days_ago)
        message.embeds = [modlog.delete_embed(r, NOW) for r in rows]
        return message

    old_quote = post(BOT_ID, 2, 31)        # posted recently, about a message sent 31 days ago
    recent = post(BOT_ID, 2, 29)
    mixed_bulk = post(BOT_ID, 1, 5, 31)
    old_post = post(BOT_ID, 31)            # e.g. an "N more messages deleted" post
    quarks = post(OTHER_BOT, 40, 40)
    log_channel._history = [old_quote, recent, mixed_bulk, old_post, quarks]
    assert await main.prune_log_channel(now=NOW) == 3
    assert [m.deleted for m in log_channel._history] == [True, False, True, True, False]
    # Looks back far enough to find posts that quote a message from the retention window's start
    assert log_channel.history_args["after"] <= NOW - datetime.timedelta(days=2 * db.RETENTION_DAYS)


async def test_prune_does_nothing_while_the_channel_is_unset(log_channel, monkeypatch):
    monkeypatch.setattr(main, "MESSAGE_LOG_CHANNEL_ID", None)
    log_channel._history = [FakeMessage(BOT_ID)]
    assert await main.prune_log_channel(now=NOW) == 0
    assert log_channel.history_args is None


def test_channel_id_from_env(monkeypatch):
    monkeypatch.delenv("MESSAGE_LOG_CHANNEL_ID", raising=False)
    assert main.channel_id_from_env("MESSAGE_LOG_CHANNEL_ID") is None
    monkeypatch.setenv("MESSAGE_LOG_CHANNEL_ID", " ")
    assert main.channel_id_from_env("MESSAGE_LOG_CHANNEL_ID") is None
    monkeypatch.setenv("MESSAGE_LOG_CHANNEL_ID", "1234")
    assert main.channel_id_from_env("MESSAGE_LOG_CHANNEL_ID") == 1234


def cached_message(message_id, content, author_id=AUTHOR, bot=False):
    return types.SimpleNamespace(
        id=message_id, content=content, guild=types.SimpleNamespace(id=GUILD),
        channel=types.SimpleNamespace(id=GENERAL), author=types.SimpleNamespace(id=author_id, bot=bot),
        attachments=[types.SimpleNamespace(filename="spam.png")], created_at=NOW,
    )


async def test_a_deletion_that_beats_its_log_write_posts_from_the_message_cache(log_channel, pool):
    event = delete_event(7)
    event.cached_message = cached_message(7, "deleted at once")
    await main.on_raw_message_delete(event)
    [embed] = posted(log_channel)
    assert "deleted at once" in embed.description and "spam.png" in embed.description


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


async def test_one_failed_delete_does_not_stop_the_prune(log_channel):
    class Gone(FakeMessage):
        async def delete(self):
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone")

    old = NOW - datetime.timedelta(days=31)
    gone, ours = Gone(BOT_ID), FakeMessage(BOT_ID)
    for m in (gone, ours):
        m.created_at, m.embeds = old, []
    log_channel._history = [gone, ours]
    assert await main.prune_log_channel(now=NOW) == 1
    assert ours.deleted


def test_a_bad_channel_id_turns_the_log_channel_off_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("MESSAGE_LOG_CHANNEL_ID", "https://discord.com/channels/1/2")
    assert main.channel_id_from_env("MESSAGE_LOG_CHANNEL_ID") is None


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
        return None  # Tests that the parent's category is looked up, not this


class FakeGuild:
    def __init__(self):
        self.cached = {
            GENERAL: types.SimpleNamespace(id=GENERAL, category_id=MEMBER_CATEGORY),
            STAFF_CHANNEL: types.SimpleNamespace(id=STAFF_CHANNEL, category_id=STAFF_CATEGORY),
            9001: FakeThread(9001, STAFF_CHANNEL),
            9002: FakeThread(9002, GENERAL),
        }
        # Archived threads usually aren't cached; Discord still answers for them
        self.remote = {ARCHIVED_THREAD: FakeThread(ARCHIVED_THREAD, STAFF_CHANNEL)}

    def get_channel_or_thread(self, channel_id):
        return self.cached.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id not in self.remote:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "unknown")
        return self.remote[channel_id]


@pytest.fixture
def ignoring_staff(log_channel, monkeypatch):
    guild = FakeGuild()
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: guild if gid == GUILD else None)
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_CATEGORY_IDS", {STAFF_CATEGORY})
    return log_channel


@pytest.mark.parametrize("channel_id", [STAFF_CHANNEL, 9001, ARCHIVED_THREAD, 404])
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


async def test_a_listed_channel_id_is_left_out_too(ignoring_staff, pool, monkeypatch):
    monkeypatch.setattr(main, "MESSAGE_LOG_IGNORE_CATEGORY_IDS", {GENERAL})
    await log(pool, 1, channel_id=GENERAL)
    await main.on_raw_message_delete(delete_event(1, channel_id=GENERAL))
    assert ignoring_staff.sent == []


def test_id_set_from_env(monkeypatch):
    monkeypatch.delenv("MESSAGE_LOG_IGNORE_CATEGORY_IDS", raising=False)
    assert main.id_set_from_env("MESSAGE_LOG_IGNORE_CATEGORY_IDS") == set()
    monkeypatch.setenv("MESSAGE_LOG_IGNORE_CATEGORY_IDS", " 12, 34 ,,staff ")
    assert main.id_set_from_env("MESSAGE_LOG_IGNORE_CATEGORY_IDS") == {12, 34}
