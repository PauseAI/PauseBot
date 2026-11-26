import datetime
import itertools
from types import SimpleNamespace as NS
import discord
import pytest
import countries
import db
import main

REAL_SYNC = main.sync_member_record

# What the hard-coded COUNTRY_DATA said for each country before countries became automatic, keyed by
# the role's name in the server. Posts and DMs must call each one the same, with the same flag.
# Czechia is the exception: COUNTRY_DATA said "Czech", but the role and the country finder say Czechia.
BEFORE = {
    "United Kingdom": ("UK", "🇬🇧"), "United States": ("US", "🇺🇸"), "France": ("France", "🇫🇷"),
    "Germany": ("Germany", "🇩🇪"), "Netherlands": ("Netherlands", "🇳🇱"), "Italy": ("Italy", "🇮🇹"),
    "Spain": ("Spain", "🇪🇸"), "Canada": ("Canada", "🇨🇦"), "Nigeria": ("Nigeria", "🇳🇬"),
    "Romania": ("Romania", "🇷🇴"), "Australia": ("Australia", "🇦🇺"), "Poland": ("Poland", "🇵🇱"),
    "Serbia": ("Serbia", "🇷🇸"), "Sweden": ("Sweden", "🇸🇪"), "Czechia": ("Czechia", "🇨🇿"),
    "Kenya": ("Kenya", "🇰🇪"), "Israel": ("Israel", "🇮🇱"), "South Korea": ("South Korea", "🇰🇷"),
    "India": ("India", "🇮🇳"), "Japan": ("Japan", "🇯🇵"), "Philippines": ("Philippines", "🇵🇭"),
}


@pytest.mark.parametrize("role_name", BEFORE)
def test_previously_listed_countries_keep_their_name_and_flag(role_name):
    info = countries.info_for_name(role_name)
    assert (info["name"], info["flag"]) == BEFORE[role_name]
    # Only France has its own DM or skips the ping
    assert bool(info["msg"]) == info.get("noPing", False) == (role_name == "France")


def test_france_keeps_its_own_dm_and_no_ping():
    france = countries.info_for_name("France")
    assert france == {"name": "France", "flag": "🇫🇷", "msg": countries.FRANCE_DM, "noPing": True}


@pytest.mark.parametrize("name", ["California", "Chapter Greeter", "National Leader", "Wants to volunteer",
                                  "UK", "Czech", "france", ""])
def test_roles_not_named_after_a_country_in_the_table_are_ignored(name):
    assert countries.info_for_name(name) is None


def test_every_override_is_for_a_country_in_the_table():
    assert set(countries.OVERRIDES) <= set(countries.ISO_CODES)


def test_every_country_in_the_table_has_a_two_letter_flag_except_kosovo():
    for name, iso in countries.ISO_CODES.items():
        flag = countries.info_for_name(name)["flag"]
        if name == "Kosovo":
            assert flag == ""
            continue
        assert len(flag) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in flag), name


def test_a_duplicate_country_role_counts_once():
    first, second, other = NS(name="India"), NS(name="India"), NS(name="Kenya")
    assert [(r, i["name"]) for r, i in countries.infos([first, other, second])] == [(first, "India"), (other, "Kenya")]


# ── on_member_update, end to end ──────────────────────────────────────────────

_ids = itertools.count(1000)
NOW = datetime.datetime.now(datetime.UTC)


class Role:
    def __init__(self, name, members=()):
        self.id, self.name, self.members = next(_ids), name, list(members)
        self.permissions = discord.Permissions()

    def is_default(self):
        return False

    def is_bot_managed(self):
        return False


class Member:
    def __init__(self, guild, roles, joined_at):
        self.id, self.name, self.mention = 99, "pat", "<@99>"
        self.guild, self.roles, self.joined_at, self.pending = guild, list(roles), joined_at, False
        self.nick, self.global_name, self.dms = None, "pat", []

    async def add_roles(self, *roles, reason=None):
        self.roles += roles

    async def send(self, content):
        self.dms.append(content)


class Channel:
    def __init__(self):
        self.posts = []

    async def send(self, content):
        self.posts.append(content)


class Accepted:
    status = 202

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RecordingHTTP:
    """Records outgoing HTTP posts and accepts them, so a test can check none are made."""
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Accepted()


@pytest.fixture
def bot(monkeypatch):
    channel, synced, http = Channel(), [], RecordingHTTP()

    async def sync(member, onboarding_notification=False, **kwargs):
        synced.append(onboarding_notification)

    monkeypatch.setattr(main, "DEVELOPER_MODE", False)
    monkeypatch.setattr(main.bot, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(main, "http_session", http)
    monkeypatch.setattr(main, "sync_member_record", sync)
    return NS(channel=channel, synced=synced, http=http)


async def gain(role_name, greeters=(), joined_at=None):
    """A member gaining the role named role_name; greeters are member IDs holding it and Chapter Greeter."""
    country = Role(role_name, [NS(id=i) for i in (*greeters, 99)])
    greeter = Role("Chapter Greeter", [NS(id=i) for i in greeters])
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[country, greeter])
    before = Member(guild, [], joined_at)
    after = Member(guild, [country], joined_at)
    await main.on_member_update(before, after)
    return after


async def test_a_country_that_was_never_hard_coded_gets_onboarding(bot):
    await gain("Portugal", greeters=[7])
    assert bot.channel.posts == ["<@7> : <@99> has joined from Portugal!"]
    assert bot.synced == [True]


async def test_a_country_with_nobody_to_ping_says_so(bot):
    await gain("Portugal")
    assert bot.channel.posts == ["<@99> has joined from Portugal, but there's no Chapter Greeter "
                                 "or National Leader for Portugal to ping."]
    assert bot.synced == [False]


async def test_a_role_not_named_after_a_country_gets_nothing(bot):
    member = await gain("California", greeters=[7])
    assert bot.channel.posts == [] and bot.synced == [] and member.dms == []


async def test_the_uk_is_still_called_uk(bot):
    await gain("United Kingdom", greeters=[7])
    assert bot.channel.posts == ["<@7> : <@99> has joined from UK!"]


async def test_france_posts_nothing_and_an_existing_member_gets_its_dm(bot):
    member = await gain("France", greeters=[7], joined_at=NOW - datetime.timedelta(days=30))
    assert bot.channel.posts == []
    dm, = member.dms
    assert dm.startswith("Welcome to PauseAI in France 🇫🇷:\n\nBienvenue pat")


def test_no_country_has_a_chapter_email():
    # The MailerSend chapter emails are gone: the #onboarding-pipeline post covers them
    assert [name for name in countries.ISO_CODES if "email" in countries.info_for_name(name)] == []


@pytest.mark.parametrize("name", ("Germany", "Canada", "India"))
async def test_germany_canada_and_india_ping_but_send_no_email(bot, name):
    await gain(name, greeters=[7])
    assert bot.http.posts == []
    assert bot.channel.posts == [f"<@7> : <@99> has joined from {name}!"]
    assert bot.synced == [True]


@pytest.mark.parametrize("name", ("Germany", "Canada", "India"))
async def test_germany_canada_and_india_with_nobody_to_ping_say_so_and_send_no_email(bot, name):
    await gain(name)
    assert bot.http.posts == []
    assert bot.channel.posts == [f"<@99> has joined from {name}, but there's no Chapter Greeter "
                                 f"or National Leader for {name} to ping."]


async def test_other_countries_send_no_email(bot):
    for name in ("Portugal", "United Kingdom", "France", "Spain"):
        await gain(name, greeters=[7])
    assert bot.http.posts == []


async def test_the_delayed_welcome_includes_a_never_hard_coded_countrys_dm(monkeypatch):
    # The delayed welcome finds country roles the same way: any country, and France's own DM
    monkeypatch.setattr(main.asyncio, "sleep", lambda seconds: _noop())
    france, portugal, california = Role("France"), Role("Portugal"), Role("California")
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[france, portugal, california])
    member = Member(guild, [portugal, california, france], None)
    guild.get_member = lambda member_id: member
    await main._send_delayed_welcome_dm(member)
    general, france_dm = member.dms
    assert general.startswith("Welcome to the PauseAI Global Discord, pat!")
    assert france_dm.startswith("Welcome to PauseAI in France 🇫🇷:")


async def _noop():
    return None


async def test_an_existing_member_adding_a_country_with_nobody_to_ping_posts_nothing(bot):
    await gain("Portugal", joined_at=NOW - datetime.timedelta(days=30))
    assert bot.channel.posts == []


async def test_restored_countries_never_post_nobody_to_ping(bot, pool, monkeypatch):
    # A member leaves and rejoins: the join-time sync gives back their saved countries, which
    # fires on_member_update like a pick. Countries with onboarders still ping; the rest post nothing.
    monkeypatch.setattr(main, "db_pool", pool)
    portugal, kenya = Role("Portugal", [NS(id=99)]), Role("Kenya", [NS(id=7), NS(id=99)])
    greeter = Role("Chapter Greeter", [NS(id=7)])
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[portugal, kenya, greeter])
    guild.get_role = lambda role_id: next((r for r in guild.roles if r.id == role_id), None)
    member = Member(guild, [portugal, kenya], NOW - datetime.timedelta(days=400))
    await REAL_SYNC(member)  # their saved roles
    rejoined = Member(guild, [], NOW)
    await REAL_SYNC(rejoined, restore_roles=True)
    assert rejoined.roles == [portugal, kenya]
    await main.on_member_update(Member(guild, [], NOW), rejoined)
    assert bot.channel.posts == ["<@7> : <@99> has joined from Kenya!"]
    # Picking Portugal again later is a real pick, so it says nobody's there to ping
    await main.on_member_update(Member(guild, [], NOW), Member(guild, [portugal], NOW))
    assert bot.channel.posts[1:] == ["<@99> has joined from Portugal, but there's no Chapter Greeter "
                                     "or National Leader for Portugal to ping."]


def test_restore_marks_expire():
    then = NOW - datetime.timedelta(seconds=main.RESTORE_MARK_SECONDS + 1)
    main.mark_restoring(1, [NS(id=10)], now=then)
    main.mark_restoring(2, [NS(id=20)], now=NOW)
    assert not main.take_restored(1, 10)
    assert main.take_restored(2, 20) and not main.take_restored(2, 20)
